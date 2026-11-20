"""gpu-gate — only runs a low-priority session when the inference server is free.

The gate hooks the ``llm_execution`` middleware: it is the only seam that wraps
**every** provider call (one turn iteration) and is not bounded by
``plugins.hook_callback_timeout`` — so it can wait. It cannot cleanly refuse
instead (the chain is fail-open), and that is exactly the semantics we want
here: wait, and proceed anyway once the budget runs out.

A session's priority, in order:
  1. an explicit tag (``/gpu-prio low`` in the session, ``hermes gpu-prio set``)
  2. a working-directory rule (``mode_by_cwd``, e.g. a background launcher's own
     directory → ``ultralow``; the gateway cannot see the launcher's env markers)
  3. a platform rule (``mode_by_platform``, e.g. every cron → ``ultralow``)
  4. unattended session (cron, one-shot, detached worker) → ``unattended_mode``
  5. the profile's ``default_mode``

Settings: ``plugins.entries.gpu-gate.settings.*`` (see README.md).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from . import gate, pause, perf, warmth

logger = logging.getLogger("plugins.gpu_gate")

#: settings are re-read at most once per this delay (the middleware runs per iteration)
_SETTINGS_TTL_S = 5.0
#: Hermes markers set by the unattended paths (cron, `chat -q`, one-shot)
_UNATTENDED_ENVS = ("HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION")

_HELP = (
    "GPU gate: low = passes when the engine accepts one more request; "
    "ultralow = also requires the engine quiet for idle_min_s at turn entry."
)


# -------------------------------------------------------------------- settings

def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_tuple(value: Any, default: tuple) -> tuple:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value.strip():
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return default


def _as_platform_map(value: Any, default: tuple = (), lower: bool = True) -> tuple:
    """`{cron: ultralow}` (YAML) or `cron=ultralow,telegram=low` → tuple of pairs.

    Entries whose mode is unknown are dropped silently: a bad setting must not
    make the plugin fail to load. `lower=False` keeps the key verbatim (paths).
    """
    if value is None:
        return tuple(default)
    items: list = []
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, str):
        for part in value.replace(";", ",").split(","):
            if "=" in part:
                key, mode = part.split("=", 1)
                items.append((key, mode))
    else:
        for row in value or ():
            if isinstance(row, (list, tuple)) and len(row) == 2:
                items.append((row[0], row[1]))
            elif isinstance(row, str) and "=" in row:
                key, mode = row.split("=", 1)
                items.append((key, mode))
    out = []
    for key, mode in items:
        plat, mode = (str(key).strip().lower() if lower else str(key).strip()), str(mode).strip().lower()
        if plat and mode in gate.MODES:
            out.append((plat, mode))
    return tuple(out)


class Settings:
    """`plugins.entries.gpu-gate.settings` → `gate.Policy`, reloaded at most every 5 s."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._at = 0.0
        self._policy = gate.Policy()

    def get(self) -> gate.Policy:
        now = time.monotonic()
        if now - self._at >= _SETTINGS_TTL_S:
            self._at = now
            self._policy = self._load()
        return self._policy

    def _load(self) -> gate.Policy:
        g = self._ctx.get_config
        mode = str(g("default_mode", gate.NORMAL) or gate.NORMAL).strip().lower()
        unattended = str(g("unattended_mode", gate.LOW) or gate.LOW).strip().lower()
        prefixes = _as_tuple(g("engine_prefixes", None), gate.Policy.engine_prefixes)
        return gate.Policy(
            enabled=_as_bool(g("enabled", True), True),
            url=str(g("verdict_url", gate.Policy.url) or gate.Policy.url),
            engine_prefixes=prefixes,
            idle_min_s=_as_float(g("idle_min_s", 30.0), 30.0),
            max_wait_s=_as_float(g("max_wait_s", 1200.0), 1200.0),
            max_wait_unattended_s=_as_float(g("max_wait_unattended_s", 420.0), 420.0),
            slice_s=_as_float(g("poll_slice_s", 5.0), 5.0),
            timeout_s=_as_float(g("verdict_timeout_s", 2.0), 2.0),
            cache_s=_as_float(g("verdict_cache_s", 2.0), 2.0),
            default_mode=mode if mode in gate.MODES else gate.NORMAL,
            unattended_mode=unattended if unattended in gate.MODES else gate.LOW,
            mode_by_platform=_as_platform_map(g("mode_by_platform", None)),
            mode_by_cwd=_as_platform_map(g("mode_by_cwd", None), lower=False),
            auto_low_platforms=_as_tuple(g("auto_low_platforms", ()), ()),
            tag_ttl_days=_as_float(g("tag_ttl_days", 7.0), 7.0),
            journal_max_bytes=_as_float(g("journal_max_bytes", 1_000_000.0), 1_000_000.0),
            pause_max_wait_s=_as_float(g("pause_max_wait_s", 300.0), 300.0),
            indefinite_max_s=_as_float(g("indefinite_max_s", 21600.0), 21600.0),
            perf_enabled=_as_bool(g("perf_enabled", True), True),
            perf_max_bytes=_as_float(g("perf_max_bytes", 2_000_000.0), 2_000_000.0),
            perf_min_output_tokens=int(_as_float(g("perf_min_output_tokens", 64), 64)),
            perf_min_prompt_tokens=int(_as_float(g("perf_min_prompt_tokens", 2000), 2000)),
            perf_decode_tps_min=_as_float(g("perf_decode_tps_min", 12.0), 12.0),
            perf_cache_ratio_min=_as_float(g("perf_cache_ratio_min", 0.25), 0.25),
            perf_consecutive=int(_as_float(g("perf_consecutive", 3), 3)),
            auto_pause_enabled=_as_bool(g("auto_pause_enabled", False), False),
            auto_pause_min_s=_as_float(g("auto_pause_min_s", 20.0), 20.0),
            auto_pause_max_s=_as_float(g("auto_pause_max_s", 90.0), 90.0),
            auto_pause_per_hour=int(_as_float(g("auto_pause_per_hour", 6), 6)),
        )


# --------------------------------------------------------------------- priority

def finite_turn() -> bool:
    """Is the turn being served right now a finite one (`chat -q --oneshot`, `-z`)?

    A session spawned by a launcher is served by the **running gateway**, so the
    launcher's own environment (`HERMES_SINGLE_QUERY_SESSION`) never reaches this
    process. Hermes carries the one-shot fact on the admission instead, in a
    turn-scoped context var: that is the only marker a gateway-side gate can see.
    `None` is the standalone-CLI case (no admission) — the env markers cover it.
    """
    try:
        from gateway.session_finite import finite_turn_required
        return finite_turn_required() is True
    except Exception:  # noqa: BLE001 - no gateway package here, or outside a turn
        return False


def unattended_reason(ctx: Any) -> str:
    """Why this turn is unattended — `""` when somebody is watching it.

    Kept as a string because it lands in the decision journal: « why is this
    session gated / why is it not » is the question the journal answers.
    """
    if any(os.environ.get(name) for name in _UNATTENDED_ENVS):
        return "env"
    if finite_turn():
        return "finite"
    try:
        if ctx.current_cron_execution() is not None:
            return "cron"
    except Exception:  # noqa: BLE001 - outside a turn the cron context may be absent
        pass
    return ""


def unattended(ctx: Any) -> bool:
    """Unattended session? (cron, `chat -q`, detached worker)"""
    return bool(unattended_reason(ctx))


def session_cwd() -> str:
    """Working directory of the session whose turn is running, `""` if unknown.

    In a gateway process the process cwd is the install root and shared by every
    session, so the per-session value must come from the turn context.
    """
    try:
        from hermes_cli.plugin_session import session_cwd as _cwd
        return str(_cwd() or "")
    except Exception:  # noqa: BLE001 - outside a turn, or older Hermes
        return ""


def resolve_mode(ctx: Any, state: Any, session_id: str, platform: Any,
                 policy: gate.Policy, cwd: str = "") -> tuple[str, str]:
    """→ (mode, where the choice came from) — an explicit tag always wins.

    Order: hand-set tag → working-directory rule (a background launcher's own
    directory) → platform rule (e.g. every cron in `ultralow`) → "unattended"
    rule (cron, gateway one-shot) → default mode.
    """
    tagged = gate.read_tag(state, session_id, policy.tag_ttl_days)
    if tagged:
        return tagged, "tag"
    by_cwd = gate.cwd_mode(policy, cwd)
    if by_cwd:
        return by_cwd, "auto:cwd:%s" % cwd
    by_platform = gate.platform_mode(policy, platform)
    if by_platform:
        return by_platform, "auto:platform:%s" % str(platform or "")
    reason = unattended_reason(ctx)
    if reason:
        return policy.unattended_mode, "auto:unattended:%s" % reason
    return policy.default_mode, "default"


# ------------------------------------------------------------------- middleware

def make_middleware(ctx: Any, settings: Settings, cache: gate.VerdictCache,
                    state: Any) -> Callable[..., Any]:
    """The gate called before every provider call."""

    def gpu_gate(request: Any = None, next_call: Optional[Callable[[Any], Any]] = None,
                 session_id: str = "", platform: Any = "", base_url: Any = "",
                 api_call_count: Any = 1, model: Any = "", **_extra: Any) -> Any:
        if next_call is None:
            return None
        policy = settings.get()
        if not policy.enabled:
            return next_call(request)
        entry = int(api_call_count or 1) <= 1
        mode, origin = resolve_mode(ctx, state, session_id, platform, policy,
                                    cwd=session_cwd() if policy.mode_by_cwd else "")
        # A pause is not a verdict about the engine: it holds the session whatever
        # model it is talking to, so it is checked before the engine filter and
        # before the `normal` shortcut. Re-read every slice, so `/gpu-prio resume`
        # releases a waiting turn at once instead of at its next iteration.
        held, record = pause.check(state, session_id, mode)
        if held:
            waited, why = _hold(state, session_id, mode, record, policy)
            logger.info("gpu-gate PAUSE session=%s scope=%s entry=%s waited=%.0fs %s",
                        session_id or "?", record.get("scope", "?"), entry, waited, why)
            gate.journal_write(getattr(state, "data_dir", None), {
                "ts": round(time.time(), 3), "session": session_id or "?",
                "platform": str(platform or ""), "mode": mode, "origin": origin,
                "entry": entry, "iter": int(api_call_count or 1),
                "waited_s": round(waited, 1), "budget_s": round(_hold_budget(record, policy), 0),
                "outcome": "pause:" + why, "model": str(model or ""),
                "engine": str(base_url or ""), "pause": pause.describe(record),
                "pid": os.getpid(),
            }, policy.journal_max_bytes)
        if not gate.targets_engine(base_url, policy.engine_prefixes):
            return next_call(request)
        if mode == gate.NORMAL:
            return next_call(request)
        budget = policy.max_wait_unattended_s if unattended(ctx) else policy.max_wait_s
        # First probe goes through the shared cache (parallel sessions don't hammer
        # the service); later ones probe live — that is the wait loop.
        probes = {"n": 0, "last": {}}

        def probe() -> dict:
            probes["n"] += 1
            verdict = cache.get(force=probes["n"] > 1)
            probes["last"] = verdict
            perf.note_engine(session_id, verdict)
            return verdict

        waited, why = gate.wait_for_slot(probe, mode, entry, policy, budget)
        if waited >= policy.slice_s:
            logger.info("gpu-gate session=%s mode=%s entry=%s waited=%.0fs budget=%.0fs %s",
                        session_id or "?", mode, entry, waited, budget, why)
        else:
            logger.debug("gpu-gate session=%s mode=%s entry=%s %s",
                         session_id or "?", mode, entry, why)
        # Journal: every turn entry (this is what answers "which session was
        # running in which mode") plus any real wait or forced pass mid-turn.
        if entry or waited >= policy.slice_s or "budget" in why:
            gate.journal_write(getattr(state, "data_dir", None), {
                "ts": round(time.time(), 3),
                "session": session_id or "?",
                "platform": str(platform or ""),
                "mode": mode,
                "origin": origin,
                "entry": entry,
                "iter": int(api_call_count or 1),
                "waited_s": round(waited, 1),
                "budget_s": round(budget, 0),
                "outcome": why,
                "model": str(model or ""),
                "engine": str(base_url or ""),
                "verdict": _brief_verdict(probes["last"]),
                "pid": os.getpid(),
            }, policy.journal_max_bytes)
        return next_call(request)

    return gpu_gate


def _hold_budget(record: dict, policy: gate.Policy) -> float:
    """How long this turn may sit in a pause: its own remaining time if it is timed
    (plus one slice so the loop notices the deadline itself), never over the cap."""
    left = pause.remaining(record)
    if left is None:
        return policy.pause_max_wait_s
    return min(left + policy.slice_s, policy.pause_max_wait_s)


def _hold(state: Any, session_id: str, mode: str, record: dict,
          policy: gate.Policy) -> tuple[float, str]:
    """Wait out a pause, re-reading the state each slice so a manual resume lands
    immediately. Bounded: past `_hold_budget` we proceed and say `pause_overrun`."""
    def hold() -> Optional[str]:
        still, fresh = pause.check(state, session_id, mode)
        if not still:
            return None
        left = pause.remaining(fresh)
        return "indefinite" if left is None else "%.0fs left" % left

    return gate.wait_while(hold, _hold_budget(record, policy), policy.slice_s)


def _brief_verdict(v: dict) -> dict:
    """Just enough to re-read a decision afterwards."""
    if not v:
        return {}
    if not v.get("ok"):
        return {"ok": False, "error": v.get("error", "?")}
    return {"ok": True, "free": v.get("free"), "idle_for_s": v.get("idle_for_s"),
            "idle_ok": v.get("idle_ok"), "reason": v.get("reason"),
            "inflight": (v.get("signals") or {}).get("inflight")}


# --------------------------------------------------------------- perf + autopause

def make_perf_hook(settings: Settings, state: Any,
                   tracker: perf.SlowTracker) -> Callable[..., Any]:
    """`post_api_request`: one sample per provider call, and the auto-pause that may follow.

    Observational by default. The auto-pause is armed by `auto_pause_enabled`, and it
    is deliberately off until the journal has shown what a normal decode rate and cache
    ratio look like on this engine: a threshold picked before the data pauses either
    nothing or the wrong session.
    """

    def gpu_perf(session_id: str = "", platform: str = "", model: str = "",
                 provider: str = "", base_url: str = "", api_call_count: Any = 0,
                 api_duration: Any = None, started_at: Any = None,
                 first_chunk_at: Any = None, usage: Any = None,
                 finish_reason: str = "", **_kw: Any) -> None:
        policy = settings.get()
        if not policy.perf_enabled:
            return None
        tracker.configure(policy.perf_decode_tps_min, policy.perf_cache_ratio_min,
                          policy.perf_min_output_tokens, policy.perf_min_prompt_tokens,
                          policy.perf_consecutive, policy.auto_pause_per_hour)
        row = perf.sample({
            "session_id": session_id, "platform": platform, "model": model,
            "provider": provider, "base_url": base_url, "api_call_count": api_call_count,
            "api_duration": api_duration, "started_at": started_at,
            "first_chunk_at": first_chunk_at, "usage": usage,
            "finish_reason": finish_reason,
        })
        if row is None:
            return None
        row["engine"] = perf.engine_note(session_id)
        data_dir = getattr(state, "data_dir", None)
        gate.journal_write(data_dir, row, policy.perf_max_bytes, name=perf.JOURNAL_NAME)
        # Ship what only this call knows — how much of *our* prompt the engine found
        # already resident — to the arbiter, which is the only place that sees every
        # Hermes instance. Fire-and-forget: the reply is for the next time we ask.
        body = warmth.payload(row, (row["engine"] or {}).get("cache_evicted_total"))
        if body:
            warmth.spawn(warmth.endpoint(policy.url), body, policy.timeout_s)
        decision = tracker.observe(row)
        if not decision:
            return None
        detail = "%s: %.1f t/s, cache %s, %s tokens out in %.1fs" % (
            decision["reason"], float(row.get("decode_tps") or 0),
            row.get("cache_ratio"), row.get("output_tokens"), row.get("duration_s"))
        if decision["kind"] == "throttled":
            logger.warning("gpu-gate perf session=%s %s — already paused %s× this hour, "
                           "not escalating", session_id or "?", detail,
                           policy.auto_pause_per_hour)
            gate.journal_write(data_dir, {"ts": row["ts"], "kind": "auto_pause_throttled",
                                          "session": session_id, "detail": detail},
                               policy.journal_max_bytes, name=perf.JOURNAL_NAME)
            return None
        if not policy.auto_pause_enabled:
            logger.info("gpu-gate perf session=%s %s — %s slow calls in a row "
                        "(auto-pause is disarmed)", session_id or "?", detail,
                        decision["streak"])
            gate.journal_write(data_dir, {"ts": row["ts"], "kind": "slow_streak",
                                          "session": session_id, "detail": detail,
                                          "streak": decision["streak"],
                                          "auto_pause": "off"}, policy.journal_max_bytes,
                               name=perf.JOURNAL_NAME)
            return None
        seconds = pause.jitter_seconds(policy.auto_pause_min_s, policy.auto_pause_max_s)
        record = pause.write(state, pause.scope_of_session(session_id),
                             time.time() + seconds,
                             reason="auto %s (%s)" % (decision["reason"], detail),
                             source="auto:%s" % decision["reason"],
                             indefinite_max_s=policy.indefinite_max_s)
        logger.warning("gpu-gate AUTO-PAUSE session=%s %.0fs — %s (streak %s)",
                       session_id or "?", seconds, detail, decision["streak"])
        gate.journal_write(data_dir, {"ts": row["ts"], "kind": "auto_pause",
                                      "session": session_id, "detail": detail,
                                      "streak": decision["streak"],
                                      "pause_s": round(seconds, 1)}, policy.journal_max_bytes,
                           name=perf.JOURNAL_NAME)

    return gpu_perf


def make_pause_gate(ctx: Any, settings: Settings, state: Any) -> Callable[..., Any]:
    """`pre_llm_call`: an **indefinite** pause refuses the turn before any inference.

    Only indefinite pauses are handled here. A timed pause is waited out inside the
    turn by the middleware, which is what "pause for 30 s" should mean; refusing the
    turn instead would throw away the user's message for a wait we could simply sit
    through. A refusal is a drop, not a hold: Hermes withholds the vetoed turn and
    never replays it, so the message has to say exactly that.
    """

    def gpu_pause_entry(session_id: str = "", platform: Any = "", **_kw: Any) -> Optional[dict]:
        policy = settings.get()
        if not policy.enabled:
            return None
        mode, _origin = resolve_mode(ctx, state, session_id, platform, policy)
        held, record = pause.check(state, session_id, mode)
        if not held or pause.remaining(record) is not None:
            return None
        logger.info("gpu-gate REFUSED turn session=%s %s", session_id or "?",
                    pause.describe(record))
        gate.journal_write(getattr(state, "data_dir", None), {
            "ts": round(time.time(), 3), "kind": "paused_turn", "session": session_id or "?",
            "platform": str(platform or ""), "mode": mode, "outcome": "refused",
            "pause": pause.describe(record)}, policy.journal_max_bytes)
        return {"action": "block", "message": (
            "**gpu-gate: this session is on pause** (%s).\n\n"
            "Your message was **not** run and will not be replayed — send it again after "
            "`/gpu-prio resume`, or wait for the pause to expire."
            % pause.describe(record))}

    return gpu_pause_entry


# -------------------------------------------------------------------- commands

#: a scope word in `pause`/`resume` means something other than the current session
_SCOPES = ("all",) + gate.MODES


def parse_pause_args(rest: str, session_id: str) -> tuple[str, str, str]:
    """`[all|low|ultralow] [30m|until 15:00|indef] [reason…]` → (scope, duration, reason).

    A bare mode word is a scope here (`pause ultralow 30m` holds every ultralow
    session); anything else opens the duration, then a free-text reason.
    """
    tokens = (rest or "").split()
    scope = session_id or pause.ALL
    if tokens and tokens[0] in _SCOPES:
        scope = pause.mode_scope(tokens[0]) if tokens[0] in gate.MODES else tokens[0]
        tokens = tokens[1:]
    duration = tokens[0] if tokens else ""
    return scope, duration, " ".join(tokens[1:])


def do_pause(state: Any, policy: gate.Policy, scope: str, duration: str,
             reason: str) -> str:
    until, label = pause.parse_duration(duration or "indef")
    if not label:
        return ("gpu-gate: I did not understand `%s` — try `30m`, `2h`, `45s`, "
                "`until 15:30` or `indef`" % duration)
    record = pause.write(state, scope, until, reason, "manual", policy.indefinite_max_s)
    return "gpu-gate: paused — %s" % pause.describe(record)


def do_resume(state: Any, scope: str) -> str:
    return ("gpu-gate: resumed — %s" % scope if pause.clear(state, scope)
            else "gpu-gate: no pause on %s" % scope)


def do_pauses(state: Any) -> str:
    rows = pause.listing(state)
    if not rows:
        return "gpu-gate: nothing is paused"
    return "gpu-gate, live pauses:\n" + "\n".join(
        "- %s · %s · %s" % (pause.describe(row), time.strftime("%H:%M:%S",
                                                              time.localtime(row["ts"])),
                            row["source"]) for row in rows)


def do_perf(state: Any, policy: gate.Policy, limit: int = 12, needle: str = "",
            session_id: str = "") -> str:
    rows = gate.journal_read(getattr(state, "data_dir", None), 400, needle,
                             name=perf.JOURNAL_NAME)
    calls = [row for row in rows if "decode_tps" in row or "output_tokens" in row]
    block = perf.fmt_summary(perf.summarise(list(reversed(calls))))
    lines = ["gpu-gate, last %d sampled calls (thresholds: decode < %.1f t/s, "
             "cache < %.0f%%, %d in a row; auto-pause %s)"
             % (len(calls), policy.perf_decode_tps_min, policy.perf_cache_ratio_min * 100,
                policy.perf_consecutive,
                "ARMED %.0f-%.0fs" % (policy.auto_pause_min_s, policy.auto_pause_max_s)
                if policy.auto_pause_enabled else "off"),
             block]
    view = warmth.last(session_id) or (perf.engine_note(session_id) or {}).get("warmth") or {}
    lines.append(warmth.fmt_view(view))
    if rows:
        lines.append("recent:")
        for row in rows[:limit]:
            stamp = time.strftime("%H:%M:%S", time.localtime(float(row.get("ts", 0) or 0)))
            if row.get("kind"):
                lines.append("- %s · %s · %s · %s" % (stamp, row["kind"],
                                                      str(row.get("session", "?"))[-12:],
                                                      row.get("detail", "")))
            else:
                lines.append("- %s · %s · %s t/s · cache %s · %s tok out / %s s · %s" % (
                    stamp, str(row.get("session", "?"))[-12:], row.get("decode_tps"),
                    row.get("cache_ratio"), row.get("output_tokens"),
                    row.get("duration_s"), row.get("model", "")))
    return "\n".join(lines)


def make_slash(ctx: Any, settings: Settings, cache: gate.VerdictCache, state: Any):
    """/gpu-prio [low|ultralow|normal|auto|status|log|pause|resume|pauses|perf]."""

    def handler(raw_args: str, session_id: str = "", **_kw: Any) -> str:
        policy = settings.get()
        parts = (raw_args or "").strip().split(None, 1)
        verb, rest = (parts[0].lower() if parts else ""), (parts[1] if len(parts) > 1 else "")
        if verb in gate.MODES:
            gate.write_tag(state, session_id, verb)
            return "gpu-gate: session %s → **%s**" % (session_id or "?", verb)
        if verb in ("auto", "rm", "clear", ""):
            gate.drop_tag(state, session_id)
            mode, origin = resolve_mode(ctx, state, session_id, "", policy)
            return ("gpu-gate: tag removed, current mode **%s** (%s)" % (mode, origin) if verb
                    else _usage(session_id, ctx, state, policy))
        if verb == "pause":
            scope, duration, reason = parse_pause_args(rest, session_id)
            return do_pause(state, policy, scope, duration, reason)
        if verb == "resume":
            target = (rest or "").strip() or session_id
            scope = pause.mode_scope(target) if target in gate.MODES else target
            return do_resume(state, scope)
        if verb == "pauses":
            return do_pauses(state)
        if verb == "perf":
            limit = 12
            if (rest or "").isdigit():
                limit = max(1, int(rest))
            return do_perf(state, policy, limit, "", session_id)
        if verb == "status":
            v = cache.get(force=True)
            mode, origin = resolve_mode(ctx, state, session_id, "", policy)
            held, record = pause.check(state, session_id, mode)
            return ("gpu-gate: session **%s** (%s)%s\n- verdict: %s\n- budget: %.0fs "
                    "(unattended %.0fs), no slot → we proceed anyway\n- pause hold: %.0fs "
                    "max inside a turn"
                    % (mode, origin,
                       "\n- **PAUSED**: %s" % pause.describe(record) if held else "",
                       _fmt_verdict(v), policy.max_wait_s,
                       policy.max_wait_unattended_s, policy.pause_max_wait_s))
        if verb == "log":
            rows = gate.journal_read(getattr(state, "data_dir", None), 5, session_id)
            if not rows:
                return "gpu-gate: no journalled decision for this session"
            lines = []
            for rec in rows:
                lines.append("- %s · iter %s · waited %.0fs · %s · %s" % (
                    time.strftime("%H:%M:%S", time.localtime(float(rec.get("ts", 0) or 0))),
                    rec.get("iter", "?"), float(rec.get("waited_s", 0) or 0),
                    rec.get("mode", "?"), rec.get("outcome", "?")))
            return "gpu-gate, latest decisions for this session:\n" + "\n".join(lines)
        return _usage(session_id, ctx, state, policy)

    return handler


def _fmt_verdict(v: dict) -> str:
    if not v.get("ok"):
        return "unreachable (%s) → gate is neutral" % v.get("error", "?")
    return "`free=%s` `idle_for_s=%s` `idle_ok=%s` reason `%s`" % (
        v.get("free"), v.get("idle_for_s"), v.get("idle_ok"), v.get("reason"))


def _usage(session_id: str, ctx: Any, state: Any, policy: gate.Policy) -> str:
    mode, origin = resolve_mode(ctx, state, session_id, "", policy)
    held, record = pause.check(state, session_id, mode)
    return ("`/gpu-prio low|ultralow|normal` — tag this session\n"
            "`/gpu-prio log` — this session's latest decisions\n"
            "`/gpu-prio auto` — remove the tag (back to the auto rule)\n"
            "`/gpu-prio status` — the gate's verdict\n"
            "`/gpu-prio pause [30m|until 15:30|indef] [reason]` — hold this session; "
            "add `all` or a mode first to hold a whole class\n"
            "`/gpu-prio resume [all|mode|session]` · `/gpu-prio pauses` — lift / list\n"
            "`/gpu-prio perf [n]` — measured decode rate and cache ratio\n"
            "Current mode: **%s** (%s), quiet required %.0fs, engine %s%s"
            % (mode, origin, policy.idle_min_s, policy.url,
               "\n**On pause**: %s" % pause.describe(record) if held else ""))


def make_cli(ctx: Any, state: Any, settings: Settings):
    """`hermes gpu-prio` — view and set tags, pauses and perf from outside a session."""

    def setup(sub: Any) -> None:
        sub.add_argument("verb", choices=["ls", "get", "set", "rm", "status", "log",
                                          "pause", "resume", "pauses", "perf"],
                         help="ls: active tags · get/set/rm <session> [mode] · "
                              "status: verdict · log [n|session]: recent decisions · "
                              "pause <session|all|mode> [30m|until 15:30|indef] [reason] · "
                              "resume <scope> · pauses · perf [n|session]")
        sub.add_argument("session", nargs="?", default="", help="session identifier or scope")
        sub.add_argument("arg", nargs="?", default="",
                         help="mode for `set`, duration for `pause`, count for `log`/`perf`")
        sub.add_argument("rest", nargs="*", default=[], help="pause reason, free text")

    def run(args: Any) -> None:
        policy = settings.get()
        verb = getattr(args, "verb", "ls")
        session = getattr(args, "session", "") or ""
        arg = getattr(args, "arg", "") or ""
        if verb == "pause":
            if not session:
                print("a scope is required: a session id, `all`, or a mode")
                return
            duration = arg if arg not in gate.MODES else ""
            reason = " ".join(getattr(args, "rest", []))
            print(do_pause(state, policy, session, duration, reason))
            return
        if verb == "resume":
            print(do_resume(state, session or pause.ALL))
            return
        if verb == "pauses":
            print(do_pauses(state))
            return
        if verb == "perf":
            limit, needle = 12, ""
            if session.isdigit():
                limit = max(1, int(session))
            else:
                needle = session
            print(do_perf(state, policy, limit, needle))
            return
        if verb == "log":
            limit, needle = 20, ""
            if session.isdigit():
                limit = max(1, int(session))
            else:
                needle = session
            rows = gate.journal_read(getattr(state, "data_dir", None), limit, needle)
            if not rows:
                print("no journalled decision"
                      + (" for %r" % needle if needle else ""))
                return
            print("%-19s %-9s %-10s %5s %8s  %s" % ("date", "mode", "platform",
                                                    "iter", "waited", "outcome"))
            for rec in rows:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(float(rec.get("ts", 0) or 0)))
                print("%-19s %-9s %-10s %5s %7.0fs  %s [%s]" % (
                    stamp, rec.get("mode", "?"), rec.get("platform", "") or "-",
                    rec.get("iter", "?"), float(rec.get("waited_s", 0) or 0),
                    rec.get("outcome", "?"), str(rec.get("session", "?"))[-12:]))
            return
        if verb == "ls":
            rows = gate.list_tags(state, policy.tag_ttl_days)
            if not rows:
                print("no active gpu-gate tag")
                return
            for row in rows:
                print("%-42s %-9s %s" % (row["session_id"], row["mode"],
                                          time.strftime("%Y-%m-%d %H:%M",
                                                        time.localtime(row["ts"]))))
            return
        if verb == "status":
            print("verdict %s\n%s" % (policy.url, _fmt_verdict(
                gate.fetch_verdict(policy.url, policy.timeout_s))))
            print("budgets: %.0fs (attended) / %.0fs (unattended), "
                  "quiet required %.0fs" % (policy.max_wait_s, policy.max_wait_unattended_s,
                                            policy.idle_min_s))
            if policy.mode_by_platform:
                print("platform rules: %s" % ", ".join(
                    "%s=%s" % pair for pair in policy.mode_by_platform))
            if policy.mode_by_cwd:
                print("cwd rules: %s" % ", ".join(
                    "%s=%s" % pair for pair in policy.mode_by_cwd))
            # what an untagged session in each situation would actually get
            print("untagged session: %s (%s), unattended: %s"
                  % (resolve_mode(ctx, state, "", "", policy)[0], policy.default_mode,
                     policy.unattended_mode))
            return
        if not session:
            print("a session identifier is required (`hermes gpu-prio ls`)")
            return
        if verb == "get":
            print(gate.read_tag(state, session, policy.tag_ttl_days) or "no tag")
        elif verb == "set":
            mode = arg
            if mode not in gate.MODES:
                print("expected mode: %s" % " | ".join(gate.MODES))
                return
            gate.write_tag(state, session, mode)
            print("gpu-gate: session %s → %s" % (session, mode))
        else:
            print("removed" if gate.drop_tag(state, session) else "no tag")

    return setup, run


def register(ctx: Any) -> None:
    settings = Settings(ctx)
    cache = gate.VerdictCache(lambda: settings.get().url,
                              lambda: settings.get().timeout_s,
                              lambda: settings.get().cache_s)
    state = ctx.state
    tracker = perf.SlowTracker()

    ctx.register_middleware("llm_execution", make_middleware(ctx, settings, cache, state))
    ctx.register_hook("post_api_request", make_perf_hook(settings, state, tracker))
    ctx.register_hook("pre_llm_call", make_pause_gate(ctx, settings, state))
    ctx.register_command("gpu-prio", handler=make_slash(ctx, settings, cache, state),
                         description="GPU gate: priority, pause and speed of this session.",
                         args_hint="[low|ultralow|normal|auto|status|log|pause|resume"
                                   "|pauses|perf]")
    setup_fn, handler_fn = make_cli(ctx, state, settings)
    ctx.register_cli_command("gpu-prio", help=_HELP, description=_HELP,
                             setup_fn=setup_fn, handler_fn=handler_fn)

    def prune(session_id: str = "", **_kw: Any) -> None:
        """The tag and any pause follow the session's life: deleted, they go too."""
        if gate.drop_tag(state, session_id):
            logger.debug("gpu-gate: tag removed (session deleted) %s", session_id)
        pause.clear(state, pause.scope_of_session(session_id))

    ctx.register_hook("on_session_delete", prune)
    policy = settings.get()
    logger.info("gpu-gate: active — verdict %s, quiet %.0fs, budgets %.0f/%.0fs, "
                "pause hold %.0fs, perf %s, auto-pause %s",
                policy.url, policy.idle_min_s, policy.max_wait_s,
                policy.max_wait_unattended_s, policy.pause_max_wait_s,
                "on" if policy.perf_enabled else "off",
                "ARMED %.0f-%.0fs" % (policy.auto_pause_min_s, policy.auto_pause_max_s)
                if policy.auto_pause_enabled else "off")
