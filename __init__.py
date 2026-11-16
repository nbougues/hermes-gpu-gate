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

from . import gate

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
        if not policy.enabled or not gate.targets_engine(base_url, policy.engine_prefixes):
            return next_call(request)
        mode, origin = resolve_mode(ctx, state, session_id, platform, policy,
                                    cwd=session_cwd() if policy.mode_by_cwd else "")
        if mode == gate.NORMAL:
            return next_call(request)
        entry = int(api_call_count or 1) <= 1
        budget = policy.max_wait_unattended_s if unattended(ctx) else policy.max_wait_s
        # First probe goes through the shared cache (parallel sessions don't hammer
        # the service); later ones probe live — that is the wait loop.
        probes = {"n": 0, "last": {}}

        def probe() -> dict:
            probes["n"] += 1
            probes["last"] = cache.get(force=probes["n"] > 1)
            return probes["last"]

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


def _brief_verdict(v: dict) -> dict:
    """Just enough to re-read a decision afterwards."""
    if not v:
        return {}
    if not v.get("ok"):
        return {"ok": False, "error": v.get("error", "?")}
    return {"ok": True, "free": v.get("free"), "idle_for_s": v.get("idle_for_s"),
            "idle_ok": v.get("idle_ok"), "reason": v.get("reason"),
            "inflight": (v.get("signals") or {}).get("inflight")}


# -------------------------------------------------------------------- commands

def make_slash(ctx: Any, settings: Settings, cache: gate.VerdictCache, state: Any):
    """/gpu-prio [low|ultralow|normal|auto|status|log] — tags the current session."""

    def handler(raw_args: str, session_id: str = "", **_kw: Any) -> str:
        policy = settings.get()
        verb = (raw_args or "").strip().lower()
        if verb in gate.MODES:
            gate.write_tag(state, session_id, verb)
            return "gpu-gate: session %s → **%s**" % (session_id or "?", verb)
        if verb in ("auto", "rm", "clear", ""):
            gate.drop_tag(state, session_id)
            mode, origin = resolve_mode(ctx, state, session_id, "", policy)
            return ("gpu-gate: tag removed, current mode **%s** (%s)" % (mode, origin) if verb
                    else _usage(session_id, ctx, state, policy))
        if verb == "status":
            v = cache.get(force=True)
            mode, origin = resolve_mode(ctx, state, session_id, "", policy)
            return ("gpu-gate: session **%s** (%s)\n- verdict: %s\n- budget: %.0fs "
                    "(unattended %.0fs), no slot → we proceed anyway"
                    % (mode, origin, _fmt_verdict(v), policy.max_wait_s,
                       policy.max_wait_unattended_s))
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
    return ("`/gpu-prio low|ultralow|normal` — tag this session\n"
            "`/gpu-prio log` — this session's latest decisions\n"
            "`/gpu-prio auto` — remove the tag (back to the auto rule)\n"
            "`/gpu-prio status` — the gate's verdict\n"
            "Current mode: **%s** (%s), quiet required %.0fs, engine %s"
            % (mode, origin, policy.idle_min_s, policy.url))


def make_cli(ctx: Any, state: Any, settings: Settings):
    """`hermes gpu-prio` — view and set tags from outside a session."""

    def setup(sub: Any) -> None:
        sub.add_argument("verb", choices=["ls", "get", "set", "rm", "status", "log"],
                         help="ls: active tags · get/set/rm <session> [mode] · "
                              "status: verdict · log [n|session]: recent decisions")
        sub.add_argument("session", nargs="?", default="", help="session identifier")
        sub.add_argument("mode", nargs="?", default="", choices=list(gate.MODES) + [""])

    def run(args: Any) -> None:
        policy = settings.get()
        verb = getattr(args, "verb", "ls")
        session = getattr(args, "session", "") or ""
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
            mode = getattr(args, "mode", "") or ""
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

    ctx.register_middleware("llm_execution", make_middleware(ctx, settings, cache, state))
    ctx.register_command("gpu-prio", handler=make_slash(ctx, settings, cache, state),
                         description="GPU gate: low / ultralow priority for this session.",
                         args_hint="[low|ultralow|normal|auto|status|log]")
    setup_fn, handler_fn = make_cli(ctx, state, settings)
    ctx.register_cli_command("gpu-prio", help=_HELP, description=_HELP,
                             setup_fn=setup_fn, handler_fn=handler_fn)

    def prune(session_id: str = "", **_kw: Any) -> None:
        """The tag follows the session's life: deleted, its tag goes too."""
        if gate.drop_tag(state, session_id):
            logger.debug("gpu-gate: tag removed (session deleted) %s", session_id)

    ctx.register_hook("on_session_delete", prune)
    logger.info("gpu-gate: active — verdict %s, quiet %.0fs, budgets %.0f/%.0fs",
                settings.get().url, settings.get().idle_min_s, settings.get().max_wait_s,
                settings.get().max_wait_unattended_s)
