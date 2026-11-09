"""GPU gate core — pure logic, no Hermes dependency (testable on its own).

Three priority modes:

  normal    no waiting, nothing changes
  low       passes as soon as the engine still accepts a request (`free`:
            at most `max_inflight` requests in flight)
  ultralow  additionally requires the engine to have been quiet for
            `idle_min_s` (`idle_ok`) at turn entry, then settles for `free`
            on the following iterations

Waiting is **bounded**: past the budget we proceed anyway (fail-open). A task
that waits forever gets killed by a watchdog (gateway inactivity, a launcher's
`max_task_minutes`) before it ever gets its slot — a late turn beats a turn
killed with nothing to show for it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

NORMAL, LOW, ULTRALOW = "normal", "low", "ultralow"
MODES = (NORMAL, LOW, ULTRALOW)

#: Verdict reasons that make waiting pointless: the engine is reachable but its
#: backend is not answering, waiting will not change that.
_UNREACHABLE = "engine_unreachable"


@dataclass
class Policy:
    """Effective settings (read from `plugins.entries.gpu-gate.settings`)."""

    enabled: bool = True
    url: str = "http://127.0.0.1:8765/free"
    #: only gates requests whose `base_url` starts with one of these prefixes:
    #: a session running against a remote model has nothing to wait for here.
    #: The default covers loopback and LAN engines; set it explicitly to be sure.
    engine_prefixes: tuple = ("http://127.0.0.1", "http://localhost",
                              "http://192.168.", "http://10.")
    idle_min_s: float = 30.0
    max_wait_s: float = 1200.0
    #: unattended (cron, one-shot, detached worker): tighter watchdogs
    max_wait_unattended_s: float = 420.0
    slice_s: float = 5.0
    timeout_s: float = 2.0
    cache_s: float = 2.0
    default_mode: str = NORMAL
    unattended_mode: str = LOW
    #: platform → mode, applied before the "unattended" rule. This is the rule
    #: that makes every cron `ultralow` without tagging them one by one
    #: (`{cron: ultralow}`); a cron session reports platform `cron`.
    mode_by_platform: tuple = ()
    #: legacy alias of `mode_by_platform`; platforms listed here become `low`.
    auto_low_platforms: tuple = ()
    tag_ttl_days: float = 7.0
    #: decision journal ceiling before rotation (the tail is kept); 0 disables it
    journal_max_bytes: float = 1_000_000.0


def fetch_verdict(url: str, timeout: float = 2.0) -> dict:
    """Normalised `ia-free` verdict, or `{"ok": False, ...}` — never raises.

    `ok=False` (service unreachable, invalid JSON, timeout) is treated
    fail-open by `admit`: a broken gate must never freeze the agent.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - the gate must never surface an error
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(raw, dict):
        return {"ok": False, "error": "verdict is not an object"}
    signals = raw.get("signals") if isinstance(raw.get("signals"), dict) else {}
    idle_for = raw.get("idle_for_s")
    if idle_for is None:  # older service: no idle field, ultralow falls back to `free`
        idle_for = 0.0 if not raw.get("free") else 1e9
    return {
        "ok": True,
        "free": bool(raw.get("free")),
        "idle_for_s": float(idle_for or 0.0),
        "idle_ok": bool(raw.get("idle_ok", float(idle_for or 0.0) >= 30.0)),
        "reason": str(raw.get("reason") or ""),
        "inflight": signals.get("inflight"),
    }


class VerdictCache:
    """Verdict shared across sessions and iterations.

    The middleware runs once per iteration and several sessions run in
    parallel: without a cache, a 90-iteration turn in 5 s would fire 90 requests
    at the gate. `force=True` (the wait loop) bypasses the cache.
    """

    def __init__(self, url_get: Callable[[], str], timeout_get: Callable[[], float],
                 ttl_get: Callable[[], float]) -> None:
        self._url_get, self._timeout_get, self._ttl_get = url_get, timeout_get, ttl_get
        self._lock = threading.Lock()
        self._at = 0.0
        self._value: dict = {"ok": False, "error": "never probed"}

    def get(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and (now - self._at) < self._ttl_get():
            return self._value
        value = fetch_verdict(self._url_get(), self._timeout_get())
        with self._lock:
            self._at, self._value = now, value
        return value


def targets_engine(base_url: Any, prefixes: tuple) -> bool:
    """Is this request heading to the gated engine? No prefix declared means yes."""
    if not prefixes:
        return True
    text = str(base_url or "")
    return any(text.startswith(str(prefix)) for prefix in prefixes)


def admit(verdict: dict, mode: str, entry: bool, policy: Policy) -> tuple[bool, str]:
    """Can this turn/iteration go now? → (yes, human-readable reason)."""
    if mode == NORMAL:
        return True, "normal"
    if not verdict.get("ok"):
        return True, "gate_unreachable(%s)" % verdict.get("error", "?")
    if verdict.get("reason") == _UNREACHABLE:
        return True, "engine_unreachable"
    if mode == ULTRALOW and entry:
        if verdict.get("idle_ok") or float(verdict.get("idle_for_s") or 0) >= policy.idle_min_s:
            return True, "quiet_%ss" % verdict.get("idle_for_s")
        return False, "quiet_%ss<%ss" % (verdict.get("idle_for_s"), policy.idle_min_s)
    if verdict.get("free"):
        return True, "free_inflight_%s" % verdict.get("inflight")
    return False, "busy_%s" % (verdict.get("reason") or "unknown")


def wait_for_slot(probe: Callable[[], dict], mode: str, entry: bool, policy: Policy,
                  budget_s: float, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> tuple[float, str]:
    """Wait for the slot. Returns `(seconds_waited, reason)` — never raises.

    Reason: `admitted:<detail>` when a slot opened, `budget_exhausted:<detail>`
    when we proceed anyway, `error:<detail>` if the inner logic blew up.
    """
    start = clock()
    try:
        while True:
            allowed, why = admit(probe(), mode, entry, policy)
            if allowed:
                return clock() - start, "admitted:" + why
            waited = clock() - start
            if waited + policy.slice_s > budget_s:
                return waited, "budget_exhausted:" + why
            sleep(policy.slice_s)
    except Exception as exc:  # noqa: BLE001 - a broken gate must not kill the turn
        return clock() - start, "error:%s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------- session tags

def tag_key(session_id: str) -> str:
    return "prio:%s" % str(session_id or "").replace("/", "-")[:120]


def read_tag(state: Any, session_id: str, ttl_days: float) -> Optional[str]:
    """Mode explicitly set on this session, or None (absent / expired)."""
    if state is None or not session_id:
        return None
    entry = state.get(tag_key(session_id))
    if not isinstance(entry, dict):
        return None
    mode = entry.get("mode")
    if mode not in MODES:
        return None
    try:
        age_days = (time.time() - float(entry.get("ts", 0))) / 86400.0
    except (TypeError, ValueError):
        return None
    return mode if age_days <= ttl_days else None


def write_tag(state: Any, session_id: str, mode: str) -> dict:
    state.set(tag_key(session_id), {"mode": mode, "ts": time.time()})
    return {"session_id": session_id, "mode": mode}


def drop_tag(state: Any, session_id: str) -> bool:
    if state is None or not session_id:
        return False
    if state.get(tag_key(session_id)) is None:
        return False
    state.set(tag_key(session_id), {"mode": NORMAL, "ts": 0.0})
    return True


def platform_mode(policy: Policy, platform: Any) -> Optional[str]:
    """Mode forced by the platform, `None` if the platform has no rule.

    `mode_by_platform` takes precedence over `auto_low_platforms` (legacy → `low`).
    """
    key = str(platform or "").strip().lower()
    if not key:
        return None
    for plat, mode in policy.mode_by_platform:
        if plat == key:
            return mode
    if key in policy.auto_low_platforms:
        return LOW
    return None


# ---------------------------------------------------------------- journal

#: one line per gate decision, in `plugin-data/gpu-gate/`
JOURNAL_NAME = "decisions.jsonl"
_journal_lock = threading.Lock()


def journal_path(data_dir: Any) -> Any:
    from pathlib import Path
    return Path(str(data_dir)) / JOURNAL_NAME


def journal_write(data_dir: Any, record: dict, max_bytes: float = 1_000_000.0) -> bool:
    """Append one decision to the journal. Never raises: a journal that fails to
    write must never keep a turn from running (same fail-open as the gate)."""
    if max_bytes <= 0 or data_dir is None:
        return False
    try:
        path = journal_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode()
        with _journal_lock:
            try:  # rotation: keep the tail, the truncated line is skipped on read
                if path.stat().st_size > max_bytes:
                    tail = path.read_bytes()[-int(max_bytes / 2):]
                    with open(path, "wb") as fh:
                        fh.write(tail)
            except OSError:
                pass
            with open(path, "ab") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - the journal is accessory
        return False
    return True


def journal_read(data_dir: Any, limit: int = 20,
                 session: str = "", since: float = 0.0) -> list[dict]:
    """Latest decisions first, filtered by `session` (substring) and `since`
    (epoch timestamp)."""
    if data_dir is None:
        return []
    try:
        path = journal_path(data_dir)
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 400_000))
            if size > 400_000:  # we landed in the middle of a line
                fh.readline()
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in reversed(blob.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if session and session not in str(rec.get("session", "")):
            continue
        if since and float(rec.get("ts", 0) or 0) < since:
            continue
        rows.append(rec)
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def list_tags(state: Any, ttl_days: float) -> list[dict]:
    """Live tags, newest first (for `hermes gpu-prio ls`).

    `PluginState` only exposes `get`/`set`: listing reads the JSON behind
    `state.path`, read-only and tolerant (a file mid-rewrite or unreadable
    yields an empty list, never an exception).
    """
    if state is None:
        return []
    try:
        with open(state.path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    rows = []
    for key, entry in (data if isinstance(data, dict) else {}).items():
        if not str(key).startswith("prio:") or not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if mode not in MODES or mode == NORMAL:
            continue
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if (time.time() - ts) / 86400.0 > ttl_days:
            continue
        rows.append({"session_id": str(key)[len("prio:"):], "mode": mode, "ts": ts})
    rows.sort(key=lambda row: row["ts"], reverse=True)
    return rows
