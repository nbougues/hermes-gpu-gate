"""Offline: the Hermes glue (Settings, resolve_mode, middleware) without Hermes.

`make_middleware` only touches `ctx.get_config`, `ctx.current_cron_execution` and
`ctx.state`: a fake ctx is enough to prove the gate waits, proceeds, and lets
through what it must not gate.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gpu_gate", os.path.join(_HERE, "__init__.py"),
                                              submodule_search_locations=[_HERE])
_module = importlib.util.module_from_spec(_spec)
sys.modules["gpu_gate"] = _module
_module_spec = _spec.loader.exec_module(_module)
plugin, gate = _module, _module.gate

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + "%-56s got=%r want=%r" % (label, got, want))
    if not ok:
        FAILS.append(label)


class FakeState:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="gpu-gate-mw-")
        self.data_dir = self.dir
        self.path = os.path.join(self.dir, "state.json")

    def _read(self):
        try:
            with open(self.path, encoding="utf-8-sig") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def get(self, key, default=None):
        return self._read().get(key, default)

    def set(self, key, value):
        data = self._read()
        data[key] = value
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)


class FakeCtx:
    """`PluginContext` reduced to the bare minimum the plugin uses."""

    def __init__(self, settings=None, cron=False):
        self._settings = settings or {}
        self._cron = cron
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def current_cron_execution(self):
        return object() if self._cron else None


class VerdictServer:
    """Fake `ia-free`: we drive the verdict and count the probes."""

    def __init__(self, payload):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                body = json.dumps(outer.payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.payload = payload
        self.hits = 0
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/free" % self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


FREE = {"free": True, "reason": "free", "idle_for_s": 300.0, "idle_ok": True,
        "signals": {"inflight": 0}}
BUSY = {"free": False, "reason": "inflight_above_limit", "idle_for_s": 0.0,
        "idle_ok": False, "signals": {"inflight": 3}}

FAST = {"verdict_cache_s": 0.0, "poll_slice_s": 0.05, "verdict_timeout_s": 1.0,
        "max_wait_s": 1.0, "max_wait_unattended_s": 0.2}

LOCAL = "http://10.0.0.5:4000/v1"


def run_middleware(ctx, url, settings, session_id="s1", platform="cli",
                   base_url=LOCAL, api_call_count=1):
    cfg = dict(FAST)
    cfg.update(settings)
    cfg.setdefault("verdict_url", url)
    ctx._settings = cfg
    settings_obj = plugin.Settings(ctx)
    settings_obj._at = 0.0
    cache = gate.VerdictCache(lambda: settings_obj.get().url,
                              lambda: settings_obj.get().timeout_s,
                              lambda: settings_obj.get().cache_s)
    mw = plugin.make_middleware(ctx, settings_obj, cache, ctx.state)
    calls = []
    mw(request={"model": "x"}, next_call=lambda r: calls.append(r) or "answer",
       session_id=session_id, platform=platform, base_url=base_url,
       api_call_count=api_call_count)
    return len(calls)


print("middleware — what must go through without waiting")
srv = VerdictServer(FREE)
check("normal mode: next_call invoked", run_middleware(FakeCtx(), srv.url, {}), 1)
check("remote model: not gated", run_middleware(FakeCtx(), srv.url, {},
                                                base_url="https://api.remote/v1"), 1)
srv.stop()

print("\nmiddleware — wait then proceed")
srv = VerdictServer(BUSY)
check("low + busy engine + short budget: proceeds anyway",
      run_middleware(FakeCtx(), srv.url, {"default_mode": "low"}), 1)
check("the gate did probe", srv.hits >= 2, True)
check("ultralow same", run_middleware(FakeCtx(), srv.url, {"default_mode": "ultralow"}), 1)
check("unattended session: reduced budget used",
      run_middleware(FakeCtx(cron=True), srv.url, {"default_mode": "low"}), 1)
srv.stop()

print("\npriority resolution")
srv = VerdictServer(FREE)
ctx = FakeCtx()
policy = gate.Policy()
check("interactive default = normal", plugin.resolve_mode(ctx, ctx.state, "s1", "cli", policy),
      ("normal", "default"))
gate.write_tag(ctx.state, "s1", gate.ULTRALOW)
check("explicit tag wins", plugin.resolve_mode(ctx, ctx.state, "s1", "cli", policy),
      (gate.ULTRALOW, "tag"))
gate.drop_tag(ctx.state, "s1")
check("cron auto = low", plugin.resolve_mode(FakeCtx(cron=True), ctx.state, "s2", "cli", policy),
      (gate.LOW, "auto:unattended:cron"))
check("no rule, attended cron platform = normal",
      plugin.resolve_mode(FakeCtx(), ctx.state, "s3", "cron", policy),
      ("normal", "default"))
# platform rule: this is what puts every cron in ultralow
by_plat = gate.Policy(mode_by_platform=(("cron", gate.ULTRALOW),))
check("mode_by_platform cron = ultralow",
      plugin.resolve_mode(FakeCtx(), ctx.state, "s3", "cron", by_plat),
      (gate.ULTRALOW, "auto:platform:cron"))
check("mode_by_platform leaves other platforms alone",
      plugin.resolve_mode(FakeCtx(), ctx.state, "s4", "telegram", by_plat),
      ("normal", "default"))
gate.write_tag(ctx.state, "s3", gate.NORMAL)
check("explicit tag wins over the platform rule",
      plugin.resolve_mode(FakeCtx(), ctx.state, "s3", "cron", by_plat),
      (gate.NORMAL, "tag"))
gate.drop_tag(ctx.state, "s3")
check("auto_low_platforms (alias) = low",
      plugin.resolve_mode(FakeCtx(), ctx.state, "s5", "cron",
                          gate.Policy(auto_low_platforms=("cron",))),
      (gate.LOW, "auto:platform:cron"))
check("auto_low_platforms: alias absent from the default", gate.Policy().auto_low_platforms, ())
check("mode_by_platform: YAML dict",
      plugin._as_platform_map({"cron": "ultralow", "Telegram": " Low "}),
      (("cron", gate.ULTRALOW), ("telegram", gate.LOW)))
check("mode_by_platform: cron=ultralow form",
      plugin._as_platform_map("cron=ultralow, whatsapp = low"),
      (("cron", gate.ULTRALOW), ("whatsapp", gate.LOW)))
check("mode_by_platform: unknown mode dropped",
      plugin._as_platform_map({"cron": "hyperlow", "sms": "low"}), (("sms", gate.LOW),))
check("mode_by_platform: absent = empty", plugin._as_platform_map(None), ())

print("\nunattended: gateway one-shot admission (the auto-created MAIL threads)")
# A launcher's `chat -q --oneshot` turn is served by the RUNNING gateway: the
# launcher's env never reaches it, only the admission's finite flag does, in a
# turn-scoped context var. Fake that module to prove the gate reads it.
import types  # noqa: E402
_saved = {n: sys.modules.get(n) for n in ("gateway", "gateway.session_finite")}
_fake_pkg, _fake_mod = types.ModuleType("gateway"), types.ModuleType("gateway.session_finite")
_finite = {"v": None}
_fake_mod.finite_turn_required = lambda: _finite["v"]
_fake_pkg.session_finite = _fake_mod
sys.modules["gateway"], sys.modules["gateway.session_finite"] = _fake_pkg, _fake_mod
_finite["v"] = True
check("finite admission = unattended", plugin.unattended_reason(FakeCtx()), "finite")
check("finite admission gets unattended_mode",
      plugin.resolve_mode(FakeCtx(), ctx.state, "s6", "cli",
                          gate.Policy(unattended_mode=gate.ULTRALOW)),
      (gate.ULTRALOW, "auto:unattended:finite"))
_finite["v"] = False
check("attended turn on that same session stays free", plugin.unattended_reason(FakeCtx()), "")
_finite["v"] = None
check("None = standalone CLI, no gateway admission", plugin.unattended_reason(FakeCtx()), "")
for _name, _mod in _saved.items():
    if _mod is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _mod

print("\nsettings: defaults and overrides")
loaded = plugin.Settings(FakeCtx()).get()
check("default max_wait_s", loaded.max_wait_s, 1200.0)
check("default idle_min_s", loaded.idle_min_s, 30.0)
check("default default_mode", loaded.default_mode, gate.NORMAL)
over = plugin.Settings(FakeCtx({"max_wait_s": "60", "engine_prefixes": "http://a, http://b",
                                "default_mode": "ULTRALOW", "enabled": "false"})).get()
check("number accepted", over.max_wait_s, 60.0)
check("list given as a string is split", over.engine_prefixes, ("http://a", "http://b"))
check("mode normalised", over.default_mode, gate.ULTRALOW)
check("boolean given as a string", over.enabled, False)
check("invalid mode falls back to normal",
      plugin.Settings(FakeCtx({"default_mode": "nonsense"})).get().default_mode, gate.NORMAL)

print("\ncommands")
ctx = FakeCtx()
slash = plugin.make_slash(ctx, plugin.Settings(ctx), None, ctx.state)
check("/gpu-prio low answers", "low" in slash("low", session_id="s9"), True)
check("tag set by the slash command", gate.read_tag(ctx.state, "s9", 7.0), gate.LOW)
slash("auto", session_id="s9")
check("/gpu-prio auto removes", gate.read_tag(ctx.state, "s9", 7.0), None)
check("/gpu-prio with no argument = help", "gpu-prio" in slash("", session_id="s9"), True)

setup, run = plugin.make_cli(ctx.state, plugin.Settings(ctx))
import argparse  # noqa: E402
parser = argparse.ArgumentParser()
sub = parser.add_subparsers()
cli_parser = sub.add_parser("gpu-prio")
setup(cli_parser)
run(cli_parser.parse_args(["set", "sess-7", "ultralow"]))
check("CLI set", gate.read_tag(ctx.state, "sess-7", 7.0), gate.ULTRALOW)
run(cli_parser.parse_args(["rm", "sess-7"]))
check("CLI rm", gate.read_tag(ctx.state, "sess-7", 7.0), None)

print("\ndecision journal")
jdir = tempfile.mkdtemp(prefix="gpu-gate-journal-")
now = time.time()
check("journal: write",
      gate.journal_write(jdir, {"ts": now, "session": "aaa", "mode": "low",
                                "iter": 1, "waited_s": 0.0, "outcome": "admitted:free"}), True)
gate.journal_write(jdir, {"ts": now + 1, "session": "bbb", "mode": "ultralow",
                          "iter": 1, "waited_s": 12.0, "outcome": "admitted:quiet"})
rows = gate.journal_read(jdir, 10)
check("journal: two lines", len(rows), 2)
check("journal: newest first", rows[0]["session"], "bbb")
check("journal: session filter",
      [r["session"] for r in gate.journal_read(jdir, 10, "aaa")], ["aaa"])
check("journal: file name", os.path.basename(gate.journal_path(jdir)), "decisions.jsonl")
check("journal: disabled by max_bytes=0", gate.journal_write(jdir, {"ts": now}, max_bytes=0), False)
check("journal: no directory", gate.journal_write(None, {"ts": now}), False)
check("journal: read with no file", gate.journal_read(os.path.join(jdir, "empty")), [])
for i in range(2000):
    gate.journal_write(jdir, {"ts": now + i, "session": "r%d" % i, "mode": "low",
                              "outcome": "x" * 40}, max_bytes=4000)
check("journal: rotation is bounded",
      os.path.getsize(gate.journal_path(jdir)) < 9000, True)
check("journal: still readable after rotation", len(gate.journal_read(jdir, 5)), 5)

srv = VerdictServer(FREE)
ctx = FakeCtx()
run_middleware(ctx, srv.url, {"default_mode": "ultralow"}, session_id="journal-me")
srv.stop()
mine = gate.journal_read(ctx.state.data_dir, 5, "journal-me")
check("middleware: the turn entry is journalled", len(mine), 1)
check("middleware: mode recorded", mine[0]["mode"], gate.ULTRALOW)
check("middleware: origin recorded", mine[0]["origin"], "default")
check("middleware: verdict recorded", mine[0]["verdict"].get("free"), True)
check("middleware: platform recorded", mine[0]["platform"], "cli")
run_middleware(ctx, srv.url, {}, session_id="journal-silent")
check("middleware: normal mode = no journal",
      gate.journal_read(ctx.state.data_dir, 5, "journal-silent"), [])
run_middleware(ctx, srv.url, {"default_mode": "low"}, session_id="journal-iter",
               api_call_count=4)
check("middleware: a fast mid-turn iteration = no journal",
      gate.journal_read(ctx.state.data_dir, 5, "journal-iter"), [])
srv = VerdictServer(BUSY)
run_middleware(ctx, srv.url, {"default_mode": "low"}, session_id="journal-forced",
               api_call_count=7)
srv.stop()
forced = gate.journal_read(ctx.state.data_dir, 5, "journal-forced")
check("middleware: a mid-turn wait is recorded", len(forced), 1)
check("middleware: iteration number recorded", forced[0]["iter"], 7)
check("middleware: wait measured", float(forced[0]["waited_s"]) > 0, True)
check("middleware: budget recorded", float(forced[0]["budget_s"]), 1.0)

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
