"""Offline: the interrupted-turn recovery's logic, no network, no Hermes
(`python3 test_recover.py`).

Builds a throwaway `state.db` with the three tables the script reads and drives
the selection, the ledger and the verdict throttle.
"""

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import recover_interrupted as rec  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + "%-58s got=%r want=%r" % (label, got, want))
    if not ok:
        FAILS.append(label)


SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT,
                       archived INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                       role TEXT, content TEXT, active INTEGER DEFAULT 1,
                       display_kind TEXT, timestamp REAL);
CREATE TABLE session_turn_leases (conversation_id TEXT, holder TEXT,
                                  acquired_at REAL, expires_at REAL);
"""

SENTINEL = rec.SENTINEL_PREFIX + "%.1fs elapsed)."
#: what agent/turn_liveness.py leaves behind when it aborts a stalled turn
DEAD_COPY = "Your request was not processed. Send it again if you still want me to carry it out."


def make_home(with_db=True):
    home = pathlib.Path(tempfile.mkdtemp(prefix="gpu-gate-recover-test-"))
    if not with_db:
        return str(home), None
    conn = sqlite3.connect(str(home / "state.db"))
    conn.executescript(SCHEMA)
    conn.commit()
    return str(home), conn


def session(conn, sid, *, source="local", title="t", cwd="", archived=0,
            turns=(("user", "do the thing"), ("assistant", SENTINEL % 726.6)), ts=600.0,
            now=0.0):
    conn.execute("INSERT INTO sessions (id, source, title, cwd, archived) VALUES (?,?,?,?,?)",
                 (sid, source, title, cwd, archived))
    for turn in turns:
        role, content = turn[0], turn[1]
        kind = turn[2] if len(turn) > 2 else None
        conn.execute("INSERT INTO messages (session_id, role, content, display_kind, timestamp)"
                     " VALUES (?,?,?,?,?)", (sid, role, content, kind, now - ts))


def opts(home, **kw):
    kwargs: dict = dict(home=pathlib.Path(home), max_age_s=21600.0, min_age_s=180.0,
                        max_restarts=2, max_attempts=2, require_free=False, sources=())
    kwargs.update(kw)
    return rec.Options(**kwargs)


def ids(cands):
    return [c.session_id for c in cands]


NOW = time.time()

# ------------------------------------------------------------------ detection

print("detection")
home, db = make_home()
session(db, "s_sentinel", now=NOW, ts=600)
session(db, "s_answered", now=NOW, ts=600,
        turns=(("user", "hi"), ("assistant", SENTINEL % 10),
               ("user", "again"), ("assistant", "here is the answer")))
session(db, "s_healthy", now=NOW, ts=600,
        turns=(("user", "hi"), ("assistant", "fine")))
session(db, "s_archived", now=NOW, ts=600, archived=1)
session(db, "s_young", now=NOW, ts=10)
session(db, "s_old", now=NOW, ts=30 * 86400)
session(db, "s_leased", now=NOW, ts=600)
db.execute("INSERT INTO session_turn_leases VALUES ('s_leased','h',?,?)", (NOW, NOW + 600))
db.commit()

picks, skipped, census = rec.scan(db, opts(home), NOW)
why = dict((c.session_id, w) for c, w in skipped)
check("only the unanswered, in-window sentinel is picked", ids(picks), ["s_sentinel"])
check("answered / healthy / archived never appear", sorted(why), ["s_leased", "s_old", "s_young"])
check("reason for a live turn lease", why["s_leased"], "turn lease is live")
check("reason for a young sentinel", why["s_young"].startswith("too young"), True)
check("reason for a stale sentinel", why["s_old"].startswith("too old"), True)

tight = rec.scan(db, opts(home, max_age_s=120.0), NOW)[1]
check("max_age_s moves a candidate to skipped",
      [c.session_id for c, w in tight if w.startswith("too old") and c.session_id == "s_sentinel"],
      ["s_sentinel"])

check("source whitelist filters out", ids(rec.scan(db, opts(home, sources=("desktop",)), NOW)[0]), [])
check("source whitelist keeps the match", ids(rec.scan(db, opts(home, sources=("local",)), NOW)[0]),
      ["s_sentinel"])

session(db, "s_older", now=NOW, ts=1200)
db.commit()
picks2, _ = rec.scan(db, opts(home), NOW)[:2]
check("oldest interruption first", ids(picks2), ["s_older", "s_sentinel"])
check("last user prompt carried for the audit",
      [c.last_prompt for c in picks2 if c.session_id == "s_sentinel"], ["do the thing"])

home_nolase, db2 = make_home()
db2.executescript("DROP TABLE session_turn_leases;")
session(db2, "x", now=NOW)
db2.commit()
check("missing lease table is tolerated", ids(rec.scan(db2, opts(home_nolase), NOW)[0]), ["x"])
check("missing state.db is reported, not raised", rec.run(opts(make_home(with_db=False)[0])), 1)

# ------------------------------------------------------- what a death looks like

print("classify: the shapes a session can end on")
check("the interrupt sentinel", rec.classify("assistant", "", SENTINEL % 1), "sentinel")
check("the liveness watchdog's copy", rec.classify("assistant", "failed_turn", DEAD_COPY),
      "failed_turn")
check("a real answer", rec.classify("assistant", "", "here is the answer"), "answered")
check("a prompt nobody answered", rec.classify("user", "", "and now?"), "unanswered_user")
check("a tool row means the turn is mid-flight", rec.classify("tool", "", "{}"), "mid_flight")
check("empty content is not a marker", rec.classify("assistant", "", ""), "answered")

print("the deaths the old query could not see")
home3, db3 = make_home()
session(db3, "watchdog", now=NOW, ts=600,
        turns=(("user", "run the thing"), ("assistant", DEAD_COPY, "failed_turn")))
session(db3, "buried", now=NOW, ts=600,
        turns=(("user", "u"), ("assistant", SENTINEL % 1),
               ("assistant", "[PRIOR CONTEXT — for reference only]")))
session(db3, "notified", now=NOW, ts=600,
        turns=(("user", "u"), ("assistant", SENTINEL % 1),
               ("assistant", "background job done", "process_complete")))
session(db3, "user_last", now=NOW, ts=600, turns=(("user", "and now?"),))
session(db3, "tool_last", now=NOW, ts=600, turns=(("tool", '{"output": "…"}'),))
db3.commit()
picks3, skipped3, census3 = rec.scan(db3, opts(home3), NOW)
check("a watchdog abort is restartable", "watchdog" in ids(picks3), True)
check("…and is labelled as one", [c.kind for c in picks3 if c.session_id == "watchdog"],
      ["failed_turn"])
check("a sentinel under a compaction handoff is still found", "buried" in ids(picks3), True)
check("a sentinel under a notification is still found", "notified" in ids(picks3), True)
check("a session ending on a bare prompt is left alone", "user_last" in ids(picks3), False)
check("a session ending on a tool row is left alone", "tool_last" in ids(picks3), False)
check("census counts the live sessions it saw", census3["sessions"], 5)
check("census breaks them down", census3["by_last_message"],
      {"sentinel": 2, "failed_turn": 1, "unanswered_user": 1, "mid_flight": 1})
check("census counts the markers of the last day", census3["markers_recent"], 3)
check("census is printable", rec.fmt_census(census3).startswith("recover-interrupted: census 5"),
      True)

# --------------------------------------------------------------------- ledger

print("ledger")
led = {}
check("no entry → 0 attempts", rec.attempts_in_ledger(led, "s", NOW, 1000), 0)
rec.record_attempt(led, "s", NOW, "ok")
rec.record_attempt(led, "s", NOW + 10, "ok")
check("attempts accumulate", rec.attempts_in_ledger(led, "s", NOW + 20, 1000), 2)
check("attempts expire with the window", rec.attempts_in_ledger(led, "s", NOW + 5000, 1000), 0)
check("prune drops stale entries", sorted(rec.prune_ledger(dict(led), NOW + 5000, 1000)), [])
check("prune keeps fresh entries", sorted(rec.prune_ledger(dict(led), NOW + 20, 1000)), ["s"])
check("unreadable ledger reads as empty", rec.read_ledger(pathlib.Path(home) / "nope.json"), {})

# ------------------------------------------------------------------ verdict

print("verdict throttle")
check("require_free=False always allows",
      rec.verdict_gate(opts(home, require_free=False)), (True, "not required"))
down = rec.verdict_gate(opts(home, require_free=True,
                             verdict_url="http://127.0.0.1:1/free", verdict_timeout_s=0.2))
check("unreachable verdict blocks the run", down[0], False)
check("unreachable verdict explains itself", down[1].startswith("verdict unreachable"), True)

# ------------------------------------------------- hermes launcher resolution

print("hermes launcher resolution")
check("existing absolute path is kept", rec.resolve_hermes(sys.executable), sys.executable)
check("missing absolute path is rejected", rec.resolve_hermes("/no/such/hermes"), "")
check("unknown bare name is rejected", rec.resolve_hermes("definitely-not-a-binary"), "")
_resolved = rec.resolve_hermes("hermes")
check("bare name gives an existing file or nothing",
      _resolved == "" or os.path.isfile(_resolved), True)

# --------------------------------------------------------------- run() wiring

print("run() wiring")
home4, db4 = make_home()
for i in range(5):
    session(db4, "many%d" % i, now=NOW, ts=600 + i,
            turns=(("user", "u%d" % i), ("assistant", SENTINEL % 1)))
db4.commit()

captured = []
real_restart = rec.restart_session
rec.restart_session = lambda o, c: (captured.append(c.session_id), (True, "stub"))[1]
try:
    check("dry run exits 0", rec.run(opts(home4, dry_run=True)), 0)
    check("dry run restarts nothing", captured, [])
    check("run exits 0", rec.run(opts(home4)), 0)
    check("per-run cap honoured, oldest first", captured, ["many4", "many3"])
    check("ledger records both", sorted(json.loads(
        open(opts(home4).ledger_path, encoding="utf-8").read())), ["many3", "many4"])
    captured.clear()
    check("second run exits 0", rec.run(opts(home4)), 0)
    check("the oldest under-budget sessions win again", captured, ["many4", "many3"])
    check("journal has one line per restart",
          len(open(opts(home4).journal_path, encoding="utf-8").read().splitlines()), 4)
    check("and a census line per run, even the runs that did nothing",
          len(open(pathlib.Path(home4) / "plugin-data" / "gpu-gate" / "recover.jsonl",
                   encoding="utf-8").read().splitlines()), 3)
    _census_rec = json.loads(open(pathlib.Path(home4) / "plugin-data" / "gpu-gate" /
                                  "recover.jsonl", encoding="utf-8").read().splitlines()[-1])
    check("the census record says what was seen",
          (_census_rec["kind"], _census_rec["sessions"], _census_rec["picks"]),
          ("census", 5, 5))
    captured.clear()
    check("verdict block stops the run",
          rec.run(opts(home4, require_free=True,
                      verdict_url="http://127.0.0.1:1/free", verdict_timeout_s=0.2)), 0)
    check("nothing restarted while the verdict blocks", captured, [])

    # the per-session breaker, on its own: one session, no per-run cap in the way
    home5, db5 = make_home()
    session(db5, "solo", now=NOW, ts=600)
    db5.commit()
    breaker = opts(home5, max_restarts=5, max_attempts=2)
    rec.run(breaker)
    rec.run(breaker)
    captured.clear()
    check("max_attempts stops restarting the same session", rec.run(breaker), 0)
    check("the broken session is left alone", captured, [])
    check("exactly max_attempts restarts were journalled",
          len(open(breaker.journal_path, encoding="utf-8").read().splitlines()), 2)
finally:
    rec.restart_session = real_restart

print()
print("FAILURES: %d" % len(FAILS))
sys.exit(1 if FAILS else 0)
