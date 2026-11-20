"""Cache warmth: what this Hermes instance can say about its own KV context.

The engine publishes no way to ask "is *this* session's prefix still resident?".
It only publishes, after the fact, how many of the prompt tokens a call found in
the cache (``usage.prompt_tokens_details.cached_tokens``) — and a global eviction
counter that every instance reads. So warmth is *inferred*, and the inference needs
one number per call, which only the caller has.

This module is the caller half: it ships that number to the arbiter (``ia-free``),
which is the one place that sees all 2-3 Hermes instances, and reads back the
verdict the arbiter deduces. Everything here is advisory and fail-open: an unreachable
arbiter costs a dropped report, never a stalled turn.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

#: last views the arbiter handed back, for `/gpu-prio perf`
_KEEP = 64
_views: "deque[tuple[str, dict]]" = deque(maxlen=_KEEP)
_lock = threading.Lock()

#: identifies which Hermes instance is talking — the arbiter has no other way to
#: tell three `acemagic`-looking sessions apart
AGENT = "%s:%s" % (socket.gethostname(), os.getpid())


def endpoint(verdict_url: Any) -> str:
    """`…/free` → `…/warmth`, same origin. Empty when the URL is unusable.

    An unexpected tail (someone pointed `verdict_url` somewhere else) falls back to
    the origin, which is where the arbiter's other endpoints live — guessing inside
    an unknown path would only produce a 404.
    """
    parts = urlsplit(str(verdict_url or "").strip())
    if not parts.scheme.startswith("http") or not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    path = path[: -len("free")] if path.endswith("free") else ""
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", "")) + "/warmth"


def payload(row: dict, evicted_total: Any = None, tag: str = "") -> Optional[dict]:
    """Body for one report, from a `perf.sample()` row — or None if nothing to say.

    A call that reported no prompt tokens carries no information (no usage block at
    all is not the same thing as a cold cache), so it is dropped rather than counted
    as cold. `cached_tokens` *is* meaningful at 0: the engine omits the key entirely
    on a miss, and `perf` normalises that to 0, which is exactly the answer.
    """
    if not isinstance(row, dict):
        return None
    try:
        prompt = int(row.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if prompt <= 0:
        return None
    try:
        cached = int(row.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        cached = 0
    body = {"session": str(row.get("session") or "?"), "prompt_tokens": prompt,
            "cached_tokens": max(0, min(cached, prompt)), "agent": AGENT,
            "tag": str(tag or row.get("model") or "")[:64]}
    if isinstance(evicted_total, (int, float)):
        body["evicted_total"] = evicted_total
    return body


def post(url: str, body: dict, timeout: float = 1.0) -> Optional[dict]:
    """One report. Returns the arbiter's view, or None — never raises."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - a dead arbiter must cost nothing
        return None


def spawn(url: str, body: dict, timeout: float = 1.0) -> None:
    """Fire the report and do not wait for it.

    `post_api_request` runs on the turn's own thread; blocking it on a network call
    to add a number nobody is consulting yet is a bad trade. The reply lands in
    `_views` for the next time someone asks.
    """
    if not url:
        return

    def run() -> None:
        view = post(url, body, timeout)
        if isinstance(view, dict):
            note(str(body.get("session") or "?"), view)

    try:
        threading.Thread(target=run, name="gpu-gate-warmth", daemon=True).start()
    except Exception:  # noqa: BLE001 - thread-starved host: drop the report
        pass


def note(session: str, view: dict) -> None:
    if not session or not isinstance(view, dict):
        return
    with _lock:
        _views.append((session, dict(view, _at=time.time())))


def last(session: str) -> dict:
    with _lock:
        for name, row in reversed(_views):
            if name == session:
                return dict(row)
    return {}


def fmt_view(view: dict) -> str:
    """One line: what the arbiter thinks of this session's context right now."""
    if not view:
        return "warmth: never reported (no call sampled yet)"
    age = view.get("age_s")
    ratio = view.get("ratio")
    bits = [
        "warmth: %s" % str(view.get("reason") or "?"),
        "age %s" % ("%.0fs" % age if isinstance(age, (int, float)) else "?"),
        "cache %s" % ("%.0f%%" % (ratio * 100) if isinstance(ratio, (int, float)) else "?"),
        "%s tok" % (view.get("prompt_tokens") if view.get("prompt_tokens") is not None else "?"),
    ]
    if view.get("evicted_since"):
        bits.append("+%s evicted since" % view["evicted_since"])
    reclaim = view.get("reclaimable_tokens")
    if reclaim is not None:
        bits.append("evictable %s tok" % reclaim)
    if view.get("needs_tokens") is not None:
        bits.append("needs %s" % view["needs_tokens"])
        if view.get("room_for_cold") is not None:
            bits.append("room: %s" % ("yes" if view["room_for_cold"] else "no"))
    if view.get("advice"):
        bits.append("→ %s" % view["advice"])
    stamp = view.get("_at")
    if isinstance(stamp, (int, float)):
        bits.append("seen %s" % time.strftime("%H:%M:%S", time.localtime(stamp)))
    return " · ".join(bits)
