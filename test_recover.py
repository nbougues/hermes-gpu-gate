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
                       timestamp REAL);
CREATE TABLE session_turn_leases (conversation_id TEXT, holder TEXT,
                                  acquired_at REAL, expires_at REAL);
"""

SENTINEL = rec.SENTINEL_PREFIX + "%.1fs elapsed)."


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
    for role, content in turns:
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                     (sid, role, content, now - ts))


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

picks, skipped = rec.find_candidates(db, opts(home), NOW)
why = dict((c.session_id, w) for c, w in skipped)
check("only the unanswered, in-window sentinel is picked", ids(picks), ["s_sentinel"])
check("answered / healthy / archived never appear", sorted(why), ["s_leased", "s_old", "s_young"])
check("reason for a live turn lease", why["s_leased"], "turn lease is live")
check("reason for a young sentinel", why["s_young"].startswith("too young"), True)
check("reason for a stale sentinel", why["s_old"].startswith("too old"), True)

tight = rec.find_candidates(db, opts(home, max_age_s=120.0), NOW)[1]
check("max_age_s moves a candidate to skipped",
      [c.session_id for c, w in tight if w.startswith("too old") and c.session_id == "s_sentinel"],
      ["s_sentinel"])

check("source whitelist filters out", ids(rec.find_candidates(db, opts(home, sources=("desktop",)), NOW)[0]), [])
check("source whitelist keeps the match", ids(rec.find_candidates(db, opts(home, sources=("local",)), NOW)[0]),
      ["s_sentinel"])

session(db, "s_older", now=NOW, ts=1200)
db.commit()
picks2, _ = rec.find_candidates(db, opts(home), NOW)
check("oldest interruption first", ids(picks2), ["s_older", "s_sentinel"])
check("last user prompt carried for the audit",
      [c.last_prompt for c in picks2 if c.session_id == "s_sentinel"], ["do the thing"])

home_nolase, db2 = make_home()
db2.executescript("DROP TABLE session_turn_leases;")
session(db2, "x", now=NOW)
db2.commit()
check("missing lease table is tolerated", ids(rec.find_candidates(db2, opts(home_nolase), NOW)[0]), ["x"])
check("missing state.db is reported, not raised", rec.run(opts(make_home(with_db=False)[0])), 1)

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
