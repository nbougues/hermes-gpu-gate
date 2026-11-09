"""Offline: the gate's logic, no network, no Hermes (`python3 test_gate.py`)."""

import importlib.util
import json
import os
import sys
import tempfile
import time

# The plugin dir is named `gpu-gate` (hyphen: Hermes display name), so it is only
# importable through a spec, not with `import gpu_gate`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gpu_gate", os.path.join(_HERE, "__init__.py"),
                                              submodule_search_locations=[_HERE])
_module = importlib.util.module_from_spec(_spec)
sys.modules["gpu_gate"] = _module
_spec.loader.exec_module(_module)
gate = _module.gate

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + "%-56s got=%r want=%r" % (label, got, want))
    if not ok:
        FAILS.append(label)


class FakeState:
    """Stand-in for `PluginState`: same real contract (`get`/`set` + `path` file)."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="gpu-gate-test-")
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


P = gate.Policy(idle_min_s=30.0, slice_s=5.0)
FREE = {"ok": True, "free": True, "idle_for_s": 120.0, "idle_ok": True,
        "reason": "free", "inflight": 0}
BUSY1 = {"ok": True, "free": True, "idle_for_s": 2.0, "idle_ok": False,
         "reason": "free", "inflight": 1}
BUSY2 = {"ok": True, "free": False, "idle_for_s": 0.0, "idle_ok": False,
         "reason": "inflight_above_limit", "inflight": 2}
DOWN = {"ok": True, "free": False, "idle_for_s": 0.0, "idle_ok": False,
        "reason": "engine_unreachable", "inflight": None}
DEAD = {"ok": False, "error": "URLError"}

print("admit() — turn entry")
check("normal is never gated", gate.admit(BUSY2, gate.NORMAL, True, P)[0], True)
check("low + free", gate.admit(FREE, gate.LOW, True, P)[0], True)
check("low + 1 in flight passes (max 1)", gate.admit(BUSY1, gate.LOW, True, P)[0], True)
check("low + 2 in flight blocks", gate.admit(BUSY2, gate.LOW, True, P)[0], False)
check("ultralow + quiet 120s", gate.admit(FREE, gate.ULTRALOW, True, P)[0], True)
check("ultralow + quiet 2s blocks", gate.admit(BUSY1, gate.ULTRALOW, True, P)[0], False)
check("gate unreachable → proceed", gate.admit(DEAD, gate.ULTRALOW, True, P)[0], True)
check("engine unreachable → proceed", gate.admit(DOWN, gate.LOW, True, P)[0], True)

print("\nadmit() — later iteration: quiet no longer required")
check("ultralow off-entry with 1 in flight", gate.admit(BUSY1, gate.ULTRALOW, False, P)[0], True)
check("ultralow off-entry with 2 in flight", gate.admit(BUSY2, gate.ULTRALOW, False, P)[0], False)

print("\nwait_for_slot()")


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def slot(probes, mode, entry=True, budget=600.0, policy=P):
    clock = Clock()
    seq = list(probes)
    it = iter(seq)

    def probe():
        try:
            return next(it)
        except StopIteration:
            return seq[-1]

    waited, why = gate.wait_for_slot(probe, mode, entry, policy, budget,
                                     sleep=clock.sleep, clock=clock.now)
    return round(waited, 1), why.split(":")[0]


check("free slot right away: 0 s", slot([FREE], gate.LOW), (0.0, "admitted"))
check("free after 3 slices", slot([BUSY2, BUSY2, BUSY2, FREE], gate.LOW), (15.0, "admitted"))
check("budget exhausted → proceed anyway",
      slot([BUSY2] * 500, gate.LOW, budget=60.0), (60.0, "budget_exhausted"))
check("ultralow waits for quiet", slot([BUSY1, BUSY1, FREE], gate.ULTRALOW), (10.0, "admitted"))
check("zero budget → proceeds at once", slot([BUSY2], gate.LOW, budget=0.0),
      (0.0, "budget_exhausted"))


class Boom(Exception):
    pass


def exploding_probe():
    raise Boom("probe down")


clock = Clock()
waited, why = gate.wait_for_slot(exploding_probe, gate.LOW, True, P, 600.0,
                                 sleep=clock.sleep, clock=clock.now)
check("exploding probe → error reason, no exception", why.split(":")[0], "error")

print("\ngated target (base_url)")
check("local engine is gated", gate.targets_engine("http://10.0.0.5:4000/v1",
                                                  ("http://10.0.0.",)), True)
check("remote model is spared", gate.targets_engine("https://api.example.com/v1",
                                                   ("http://10.0.0.",)), False)
check("no prefix = everything gated", gate.targets_engine("https://api.example.com/v1", ()), True)
check("no base_url, prefixes set → spared", gate.targets_engine(None, ("http://x",)), False)

print("\nsession tags")
st = FakeState()
check("no tag to begin with", gate.read_tag(st, "sess-1", 7.0), None)
gate.write_tag(st, "sess-1", gate.ULTRALOW)
check("tag read back", gate.read_tag(st, "sess-1", 7.0), gate.ULTRALOW)
check("tag isolated per session", gate.read_tag(st, "sess-2", 7.0), None)
check("ls shows only live tags", [r["mode"] for r in gate.list_tags(st, 7.0)],
      [gate.ULTRALOW])
check("re-tag replaces", gate.write_tag(st, "sess-1", gate.LOW)["mode"], gate.LOW)
check("tag removed", gate.drop_tag(st, "sess-1"), True)
check("removed tag = no mode", gate.read_tag(st, "sess-1", 7.0), None)
check("removing an absent tag", gate.drop_tag(st, "unknown"), False)
st.set(gate.tag_key("old"), {"mode": gate.LOW, "ts": time.time() - 30 * 86400})
check("tag older than the TTL is ignored", gate.read_tag(st, "old", 7.0), None)
check("ls excludes expired tags", gate.list_tags(st, 7.0), [])
check("no state → normal", gate.read_tag(None, "sess-1", 7.0), None)

print("\ntolerant verdict")
check("unreachable service", gate.fetch_verdict("http://127.0.0.1:1/free", 0.2)["ok"], False)

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
