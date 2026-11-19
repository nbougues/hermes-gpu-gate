"""Pause state — hold a session, a mode, or everything off, for a while or indefinitely.

Pure logic (no Hermes import), so it is testable on its own, like `gate.py`.

A pause is stored in the plugin's own state under ``pause:<scope>`` where a scope is

  ``<session id>``   one session
  ``mode:<mode>``    every session that resolves to that mode (e.g. all `ultralow`)
  ``all``            every gated session

Two shapes:

  timed       ``until`` is a timestamp — the gate waits it out, then proceeds
  indefinite  ``until`` is None — the gate refuses new turns until someone resumes

An indefinite pause always carries an ``expires_at`` safety net (`indefinite_max_s`):
a forgotten "pause everything" must not freeze the fleet forever.

Nobody needs a timer to honour a pause: every consumer re-reads the entry at its own
next checkpoint (turn entry, or the next iteration of a running turn), so a pause
lifts lazily the moment its deadline passes.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

ALL = "all"
_PREFIX = "pause:"
_INDEFINITE = ("", "indef", "indefinite", "forever", "inf", "manual")
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])?$")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def scope_key(scope: str) -> str:
    return _PREFIX + str(scope or "").strip().replace("/", "-")[:120]


def scope_of_session(session_id: str) -> str:
    return str(session_id or "").strip()


def mode_scope(mode: str) -> str:
    return "mode:%s" % str(mode or "").strip().lower()


def parse_duration(text: Any, now: Optional[float] = None) -> tuple[Optional[float], str]:
    """`30m` / `2h` / `45` / `until 14:30` / `indef` → (`until` or None, label).

    Seconds when the unit is bare. `until HH:MM` is the next such clock time (today
    if still ahead, tomorrow otherwise). Unparsable input returns (None, "") so the
    caller can print usage instead of pausing on a typo.
    """
    raw = str(text or "").strip().lower()
    now = time.time() if now is None else now
    if raw in _INDEFINITE:
        return None, "indefinite"
    if raw.startswith("until "):
        raw = raw[6:].strip()
        match = _CLOCK_RE.match(raw)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour > 23 or minute > 59:
                return None, ""
            stamp = datetime.fromtimestamp(now).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            if stamp.timestamp() <= now:
                stamp += timedelta(days=1)
            return stamp.timestamp(), "until %s" % stamp.strftime("%H:%M")
        try:
            return float(raw), "until %.0fs" % (float(raw) - now)
        except ValueError:
            return None, ""
    match = _DURATION_RE.match(raw)
    if not match:
        return None, ""
    seconds = float(match.group(1)) * _UNITS[match.group(2) or "s"]
    return now + seconds, "%ss" % int(seconds)


def jitter_seconds(lo: float, hi: float, rng: Any = None) -> float:
    """A random hold between `lo` and `hi` — jitter so N sessions caught by the same
    slow spell do not all resume on the same second and re-create the congestion."""
    low, high = max(0.0, float(lo)), max(0.0, float(hi))
    if high < low:
        low, high = high, low
    return (rng or random).uniform(low, high)


def write(state: Any, scope: str, until: Optional[float], reason: str = "",
          source: str = "manual", indefinite_max_s: float = 21600.0) -> dict:
    """Record a pause. `until=None` means indefinite, bounded by `indefinite_max_s`."""
    now = time.time()
    record = {
        "scope": scope,
        "until": None if until is None else round(float(until), 3),
        "reason": str(reason or "")[:200],
        "source": source,
        "ts": round(now, 3),
        "expires_at": None if until is not None else round(now + indefinite_max_s, 3),
    }
    state.set(scope_key(scope), record)
    return record


def read(state: Any, scope: str, now: Optional[float] = None) -> Optional[dict]:
    """The live pause on `scope`, or None (absent, cleared, or past either deadline)."""
    if state is None or not scope:
        return None
    entry = state.get(scope_key(scope))
    if not isinstance(entry, dict):
        return None
    now = time.time() if now is None else now
    until, expires_at = entry.get("until"), entry.get("expires_at")
    try:
        if until is not None and float(until) <= now:
            return None
        if expires_at is not None and float(expires_at) <= now:
            return None
    except (TypeError, ValueError):
        return None
    return entry


def clear(state: Any, scope: str) -> bool:
    """Lift one pause. `PluginState` has no delete, so a cleared pause is a tombstone."""
    if state is None or not scope:
        return False
    if read(state, scope) is None:
        return False
    state.set(scope_key(scope), {"scope": scope, "until": None, "expires_at": 0.0,
                                 "reason": "resumed", "source": "clear",
                                 "ts": round(time.time(), 3)})
    return True


def _rows(state: Any, now: float) -> list[dict]:
    """Every live pause, newest first — `PluginState` only exposes get/set, so listing
    reads the JSON behind `state.path` (tolerant: unreadable or mid-rewrite → empty)."""
    if state is None:
        return []
    try:
        with open(state.path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    out = []
    for key, entry in (data if isinstance(data, dict) else {}).items():
        if not str(key).startswith(_PREFIX) or not isinstance(entry, dict):
            continue
        if read(state, str(key)[len(_PREFIX):], now) is None:
            continue
        out.append(entry)
    out.sort(key=lambda row: float(row.get("ts", 0) or 0), reverse=True)
    return out


def listing(state: Any, now: Optional[float] = None) -> list[dict]:
    now = time.time() if now is None else now
    rows = []
    for entry in _rows(state, now):
        until = entry.get("until")
        rows.append({
            "scope": entry.get("scope", "?"),
            "until": until,
            "remaining_s": None if until is None else max(0.0, float(until) - now),
            "indefinite": until is None,
            "reason": entry.get("reason", ""),
            "source": entry.get("source", "?"),
            "ts": float(entry.get("ts", 0) or 0),
        })
    return rows


def check(state: Any, session_id: str, mode: str = "",
          now: Optional[float] = None) -> tuple[bool, dict]:
    """Is this session held off? → (paused, the record that said so).

    Most specific scope wins: the session itself, then its mode, then `all`.
    """
    now = time.time() if now is None else now
    for scope in (scope_of_session(session_id), mode_scope(mode), ALL):
        if not scope:
            continue
        entry = read(state, scope, now)
        if entry is not None:
            return True, entry
    return False, {}


def remaining(entry: dict, now: Optional[float] = None) -> Optional[float]:
    """Seconds left on a timed pause, `None` when it is indefinite."""
    until = entry.get("until")
    if until is None:
        return None
    now = time.time() if now is None else now
    try:
        return max(0.0, float(until) - now)
    except (TypeError, ValueError):
        return None


def describe(entry: dict, now: Optional[float] = None) -> str:
    """One human line: `all · indefinite · 30m of manual hold`."""
    left = remaining(entry, now)
    when = "indefinite" if left is None else "%.0fs left" % left
    bits = [entry.get("scope", "?"), when]
    if entry.get("reason"):
        bits.append(str(entry["reason"]))
    if entry.get("source") and entry.get("source") != "manual":
        bits.append(str(entry["source"]))
    return " · ".join(bits)
