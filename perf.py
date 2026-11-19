"""Per-call performance — how fast did this session's request actually run?

Pure logic (no Hermes import), fed by the ``post_api_request`` payload:

  decode_tps   output tokens / (duration − time-to-first-chunk) — the decode rate
  cache_ratio  cached prompt tokens / prompt tokens — how much of the prefix was reused
  ttft_s       time to first chunk: queueing + prefill, the part decode_tps excludes

Two thresholds turn a sample into a verdict: a decode rate under `decode_tps_min`, or a
cache ratio under `cache_ratio_min` on a prompt big enough to have had something to reuse.
One slow call is noise (a long tool result, a cold prefill, a fallback model), so a pause
only follows `consecutive` slow samples in a row — see `SlowTracker`.

The tracker is deliberately in-process: a pause it writes lives in the shared plugin state
(see `pause.py`), so the detector can die with the process and the pause still expires on
its own.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional

#: usage key aliases — the payload is normalised by Hermes, but providers differ and a
#: missing bucket must read as 0, never as a crash or a bogus rate.
_OUT = ("output_tokens", "completion_tokens")
_IN = ("input_tokens", "prompt_tokens")
_CACHED = ("cached_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens")

SLOW_DECODE, SLOW_CACHE = "slow_decode", "slow_cache"

#: one line per sampled provider call, next to `decisions.jsonl`
JOURNAL_NAME = "perf.jsonl"

#: last engine verdict seen per session, so a perf row can be read against the
#: contention that produced it (the middleware sees the verdict, the response hook
#: does not). Bounded: one entry per session, oldest evicted past _ENGINE_KEEP.
_ENGINE_KEEP = 64
_engine: "deque[tuple[str, dict]]" = deque(maxlen=_ENGINE_KEEP)
_engine_lock = threading.Lock()


def note_engine(session: str, verdict: dict) -> None:
    """Remember what the gate's verdict said when this session's request went out."""
    if not session or not isinstance(verdict, dict) or not verdict.get("ok"):
        return
    with _engine_lock:
        _engine.append((session, {
            "free": verdict.get("free"),
            "inflight": verdict.get("inflight"),
            "decode_tps": verdict.get("decode_tps"),
            "engine_cache_ratio": verdict.get("engine_cache_ratio"),
            "queue_wait_s": verdict.get("queue_wait_s"),
        }))


def engine_note(session: str) -> dict:
    with _engine_lock:
        for name, row in reversed(_engine):
            if name == session:
                return dict(row)
    return {}


def _pick(usage: dict, names: tuple) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)) and value:
            return int(value)
    return 0


def sample(payload: dict) -> Optional[dict]:
    """One call's metrics from a ``post_api_request`` payload, or None if unusable."""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    out_tokens = _pick(usage, _OUT)
    in_tokens = _pick(usage, _IN)
    cached = _pick(usage, _CACHED)
    duration = payload.get("api_duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None
    first_chunk = payload.get("first_chunk_at")
    started = payload.get("started_at")
    ttft = None
    if isinstance(first_chunk, (int, float)) and isinstance(started, (int, float)):
        ttft = max(0.0, float(first_chunk) - float(started))
    # Without a first-chunk stamp the whole call is the only clock we have; the rate is
    # then an underestimate (queue + prefill are inside it), and `approx` says so.
    decode_window = float(duration) - (ttft or 0.0)
    if decode_window <= 0.5:
        decode_window = float(duration)
        ttft = None
    return {
        "ts": round(time.time(), 3),
        "session": str(payload.get("session_id") or "?"),
        "platform": str(payload.get("platform") or ""),
        "model": str(payload.get("model") or ""),
        "provider": str(payload.get("provider") or ""),
        "engine": str(payload.get("base_url") or ""),
        "iter": int(payload.get("api_call_count") or 0),
        "duration_s": round(float(duration), 3),
        "ttft_s": None if ttft is None else round(ttft, 3),
        "output_tokens": out_tokens,
        "prompt_tokens": in_tokens,
        "cached_tokens": cached,
        "decode_tps": round(out_tokens / decode_window, 2) if out_tokens else None,
        "cache_ratio": round(cached / in_tokens, 4) if in_tokens else None,
        "approx": ttft is None,
        "finish_reason": str(payload.get("finish_reason") or ""),
    }


def slow_reason(row: dict, decode_tps_min: float, cache_ratio_min: float,
                min_output_tokens: int, min_prompt_tokens: int) -> str:
    """Why this sample counts as slow — `""` when it counts as normal.

    A call that produced almost nothing carries no signal, and a small prompt had
    nothing worth caching, so a low cache ratio there is not a finding.
    """
    if int(row.get("output_tokens") or 0) < min_output_tokens:
        return ""
    tps = row.get("decode_tps")
    if tps is not None and float(tps) < decode_tps_min:
        return SLOW_DECODE
    ratio, prompt = row.get("cache_ratio"), int(row.get("prompt_tokens") or 0)
    if (ratio is not None and prompt >= min_prompt_tokens and float(ratio) < cache_ratio_min):
        return SLOW_CACHE
    return ""


class SlowTracker:
    """Consecutive-slow streaks per session, with a per-hour cap on auto-pauses.

    `observe` returns a decision dict when a pause should be taken, else None. Nothing
    here sleeps or touches state: the caller writes the pause and journals it.
    """

    def __init__(self, decode_tps_min: float = 12.0, cache_ratio_min: float = 0.25,
                 min_output_tokens: int = 64, min_prompt_tokens: int = 2000,
                 consecutive: int = 3, max_per_hour: int = 6,
                 clock: Any = time.time) -> None:
        self.thresholds = (decode_tps_min, cache_ratio_min, min_output_tokens,
                           min_prompt_tokens)
        self.consecutive = max(1, int(consecutive))
        self.max_per_hour = max(0, int(max_per_hour))
        self._clock = clock
        self._lock = threading.Lock()
        self._streak: dict[str, int] = {}
        self._reasons: dict[str, str] = {}
        self._pauses: dict[str, deque] = {}
        self._last: dict[str, dict] = {}

    def configure(self, decode_tps_min: float, cache_ratio_min: float,
                  min_output_tokens: int, min_prompt_tokens: int,
                  consecutive: int, max_per_hour: int) -> None:
        """Re-read the thresholds from the live policy (settings are hot-reloaded)."""
        self.thresholds = (decode_tps_min, cache_ratio_min, min_output_tokens,
                           min_prompt_tokens)
        self.consecutive = max(1, int(consecutive))
        self.max_per_hour = max(0, int(max_per_hour))

    def observe(self, row: dict) -> Optional[dict]:
        decode_min, ratio_min, min_out, min_prompt = self.thresholds
        session = row.get("session") or "?"
        reason = slow_reason(row, decode_min, ratio_min, min_out, min_prompt)
        with self._lock:
            self._last[session] = row
            if not reason:
                self._streak[session] = 0
                self._reasons.pop(session, None)
                return None
            streak = self._streak[session] = self._streak.get(session, 0) + 1
            self._reasons[session] = reason
            if streak < self.consecutive:
                return None
            now = self._clock()
            hits = self._pauses.setdefault(session, deque(maxlen=24))
            recent = [stamp for stamp in hits if now - stamp < 3600.0]
            if self.max_per_hour and len(recent) >= self.max_per_hour:
                # Already held off as often as we allow this hour: stop escalating,
                # keep the streak so a persistent problem still shows in the journal.
                return {"kind": "throttled", "streak": streak, "reason": reason,
                        "row": row}
            hits.append(now)
            self._streak[session] = 0
            return {"kind": "pause", "streak": streak, "reason": reason, "row": row}

    def snapshot(self, session: str = "") -> dict:
        with self._lock:
            if session:
                return {"streak": self._streak.get(session, 0),
                        "reason": self._reasons.get(session, ""),
                        "last": self._last.get(session, {})}
            return {"streaks": dict(self._streak),
                    "reasons": dict(self._reasons),
                    "last": {key: dict(row) for key, row in self._last.items()}}


def _percentile(values: list[float], part: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(part * (len(ordered) - 1)))))
    return ordered[index]


def summarise(rows: list[dict]) -> dict:
    """Aggregate a journal tail: what would the thresholds have caught?"""
    rows = [row for row in rows if isinstance(row, dict)]
    tps = [float(row["decode_tps"]) for row in rows if row.get("decode_tps")]
    ratios = [float(row["cache_ratio"]) for row in rows if row.get("cache_ratio") is not None]
    ttfts = [float(row["ttft_s"]) for row in rows if row.get("ttft_s")]
    return {
        "calls": len(rows),
        "with_tps": len(tps),
        "tps_p10": _percentile(tps, 0.10),
        "tps_p50": _percentile(tps, 0.50),
        "tps_min": min(tps) if tps else None,
        "tps_max": max(tps) if tps else None,
        "cache_mean": (sum(ratios) / len(ratios)) if ratios else None,
        "cache_p10": _percentile(ratios, 0.10),
        "ttft_p50": _percentile(ttfts, 0.50),
        "sessions": len({row.get("session") for row in rows}),
    }


def fmt_summary(summary: dict) -> str:
    """One readable block for `hermes gpu-prio perf`."""
    def num(value, unit=""):
        return "—" if value is None else ("%.1f%s" % (float(value), unit))
    return (
        "%s calls · %s sessions · %s with a decode rate\n"
        "  decode t/s   p10 %s  p50 %s  min %s  max %s\n"
        "  cache ratio  p10 %s  mean %s\n"
        "  ttft         p50 %s s"
        % (summary.get("calls", 0), summary.get("sessions", 0), summary.get("with_tps", 0),
           num(summary.get("tps_p10")), num(summary.get("tps_p50")),
           num(summary.get("tps_min")), num(summary.get("tps_max")),
           num(summary.get("cache_p10")), num(summary.get("cache_mean")),
           num(summary.get("ttft_p50")))
    )
