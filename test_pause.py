"""Offline: pause.py — durations, scopes, expiry. No Hermes, no network."""

import importlib.util
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gpu_pause", os.path.join(_HERE, "pause.py"))
pause = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pause)

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + "%-58s got=%r want=%r" % (label, got, want))
    if not ok:
        FAILS.append(label)


class FakeState:
    """`PluginState` reduced to get/set over one JSON file, like the real one."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="gpu-gate-pause-")
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


print("durations")
now = 1_800_000_000.0
check("30m", pause.parse_duration("30m", now)[0], now + 1800)
check("bare number is seconds", pause.parse_duration("45", now)[0], now + 45)
check("90s", pause.parse_duration("90s", now)[0], now + 90)
check("2h", pause.parse_duration("2h", now)[0], now + 7200)
check("1d", pause.parse_duration("1d", now)[0], now + 86400)
check("indefinite", pause.parse_duration("indef", now), (None, "indefinite"))
check("empty is indefinite", pause.parse_duration("", now), (None, "indefinite"))
check("garbage is refused", pause.parse_duration("un moment", now), (None, ""))
check("negative is refused", pause.parse_duration("-5m", now), (None, ""))
check("until clock time", pause.parse_duration("until 23:59", now)[1].startswith("until "), True)
check("until epoch seconds", pause.parse_duration("until %.0f" % (now + 60), now)[0], now + 60.0)
check("jitter stays inside the window",
      all(20.0 <= pause.jitter_seconds(20, 90) <= 90.0 for _ in range(200)), True)
check("inverted window is tolerated", 5.0 <= pause.jitter_seconds(90, 5) <= 90.0, True)

print("\nwrite / read / expiry")
st = FakeState()
check("nothing paused at first", pause.read(st, "s1"), None)
rec = pause.write(st, "s1", time.time() + 60, "test hold")
check("live pause reads back", pause.read(st, "s1")["reason"], "test hold")
check("remaining is positive", pause.remaining(rec) > 59, True)
past = pause.write(st, "s2", time.time() - 1)
check("an expired pause is gone", pause.read(st, "s2"), None)
indef = pause.write(st, "s3", None)
check("indefinite has no deadline", pause.remaining(indef), None)
check("indefinite carries a safety net", pause.read(st, "s3")["expires_at"] > time.time(), True)
check("safety net can be shortened",
      pause.read(st, "s4", time.time()) is None
      and pause.write(st, "s4", None, indefinite_max_s=-1)
      and pause.read(st, "s4") is None, True)
check("clear returns True once", pause.clear(st, "s1"), True)
check("cleared is not live", pause.read(st, "s1"), None)
check("clear again returns False", pause.clear(st, "s1"), False)
check("clear on nothing", pause.clear(st, "never"), False)

print("\nscopes: session beats mode beats all")
st = FakeState()
pause.write(st, "s1", time.time() + 60, "session hold")
pause.write(st, "mode:ultralow", time.time() + 60, "mode hold")
pause.write(st, "all", time.time() + 60, "everything")
check("session scope wins", pause.check(st, "s1", "ultralow")[1]["reason"], "session hold")
check("mode scope for an ultralow session",
      pause.check(st, "other", "ultralow")[1]["reason"], "mode hold")
check("all catches the rest", pause.check(st, "other", "normal")[1]["reason"], "everything")
check("a pause is a pause", pause.check(st, "other", "normal")[0], True)
pause.clear(st, "all")
pause.clear(st, "mode:ultralow")
check("no pause left for that session", pause.check(st, "free-one", "normal"), (False, {}))

print("\nlisting")
st = FakeState()
check("empty listing", pause.listing(st), [])
pause.write(st, "all", time.time() + 300, "deploy")
pause.write(st, "s9", None, "hand")
rows = pause.listing(st)
check("both listed", sorted(row["scope"] for row in rows), ["all", "s9"])
check("timed row shows remaining", [r for r in rows if r["scope"] == "all"][0]["remaining_s"] > 299, True)
check("indefinite row is flagged", [r for r in rows if r["scope"] == "s9"][0]["indefinite"], True)
check("expired rows are not listed",
      (pause.clear(st, "all"), pause.listing(st) and "s9" in [r["scope"] for r in pause.listing(st)])[1],
      True)
check("describe names the scope", pause.describe({"scope": "all", "until": time.time() + 10}).startswith("all · "), True)
check("describe says indefinite", "indefinite" in pause.describe({"scope": "s", "until": None}), True)

print("\nstate robustness")
broken = FakeState()
with open(broken.path, "w", encoding="utf-8") as handle:
    handle.write("{not json")
check("unreadable state = no pause", pause.read(broken, "s1"), None)
check("unreadable state = empty listing", pause.listing(broken), [])
junk = FakeState()
junk.set("pause:s1", "not a dict")
check("wrong type = no pause", pause.read(junk, "s1"), None)
junk.set("pause:s2", {"until": "soon"})
check("unparsable deadline = no pause", pause.read(junk, "s2"), None)
check("no state object", pause.read(None, "s1"), None)
check("empty scope", pause.read(FakeState(), ""), None)

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
