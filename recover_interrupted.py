#!/usr/bin/env python3
"""Recover sessions left on an interrupted turn — the gpu-gate companion cron.

A gated session spends most of its life *inside* the ``llm_execution``
middleware, waiting for a slot. From the turn loop's point of view an API call
has been in flight the whole time, so anything that tears the session down while
it waits — a desktop/backend shutdown, a Stop, a busy-submit interrupt — ends the
turn with the sentinel::

    Operation interrupted: waiting for model response (726.6s elapsed).

The user message is then sitting there unanswered, and nothing retries it. This
script finds those sessions and re-submits a short continuation nudge so the
turn resumes on its own, on a schedule (cron, every 10 minutes).

Detection looks at the **last meaningful message** of each live session and
restarts it only when that message is a proven death, never an answer:

* ``Operation interrupted: waiting for model response (…)`` — an interrupt (Stop,
  shutdown, busy-submit) landed mid-call;
* ``display_kind = 'failed_turn'`` — the turn liveness watchdog aborted the turn
  ("Turn made no progress for 600s"). A gpu-gate wait longer than
  ``agent.turn_liveness.timeout_s`` dies exactly this way, so this is the shape
  the gate itself produces.

Rows the turn loop appends *after* the fact — a compaction handoff, a
notification — are not answers and are skipped, so a marker buried under one is
still found. A session whose last message is a real answer, a bare user prompt
or a tool result is never touched.

Every run prints a census (sessions seen, how each one's last message was
classified, live leases, markers seen in 24 h) and appends it to
``plugin-data/gpu-gate/recover.jsonl``, so a run that restarts nothing says what
it looked at instead of looking broken.

Two throttles keep the recovery from becoming the load spike it is recovering
from (see ``--help``): at most ``--max`` restarts per run, and only while the
verdict service still reports the engine ``free``. Each restarted session then
queues behind the gate like anything else.

Usage (cron, every 10 minutes)::

    */10 * * * * /usr/bin/python3 ~/.hermes/plugins/gpu-gate/recover_interrupted.py \
        --verdict-url http://192.168.30.32:8765/free \
        >> ~/.hermes/logs/gpu-gate-recover.log 2>&1

    # another profile: prefix the line with HERMES_HOME=~/.hermes/profiles/<name>

Stdlib only. Read-only on the state database; the only writes are the ledger,
the journal, and the resumed session's own turn.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402  (installed next to this script by install.sh)

#: what the turn loop writes when an interrupt lands during the API call — the
#: gpu-gate wait is inside that call, so every torn-down gate wait looks like this
SENTINEL_PREFIX = "Operation interrupted: waiting for model response ("
_SENTINEL_LIKE = SENTINEL_PREFIX + "%"

#: re-submitted instead of the original prompt: the interrupted turn's user
#: message, tool calls and tool results are already persisted, so re-sending the
#: prompt would ask for the same work twice
NUDGE = (
    "[recovery] Your previous turn was interrupted while waiting for the model "
    "response (the inference server was busy). Nothing was lost: everything up to "
    "the interruption is in this conversation. Pick up where you stopped, and do "
    "not redo steps that already completed."
)

#: the liveness watchdog's own words, stored as ``display_kind='failed_turn'``:
#: "Turn made no progress for 600s; aborting to release the session." lands here
#: (agent/turn_liveness.py -> agent/turn_failure_copy.py). A gpu-gate wait longer
#: than ``agent.turn_liveness.timeout_s`` is killed with exactly this copy, so a
#: recovery that only knows about SENTINEL_PREFIX never sees the deaths the gate
#: itself causes.
FAILED_TURN_KIND = "failed_turn"

#: trailing rows that are not an answer and must not hide the marker underneath
#: them. This is Hermes' own definition of machinery rows — see
#: ``agent/turn_empty_response.py::_MACHINERY_DISPLAY_KINDS`` — plus ``hidden``.
IGNORED_DISPLAY_KINDS = ("hidden", "internal_notification", "process_complete",
                         "async_delegation_complete")

#: compaction handoff rows are assistant messages too, and they land *after* the
#: sentinel of the turn they summarise — they must not count as "the session was
#: answered".
IGNORED_CONTENT_PREFIXES = ("[PRIOR CONTEXT", "[CONTEXT COMPACTION")


# ------------------------------------------------------------------- settings

@dataclass
class Options:
    home: Path
    hermes_bin: str = "hermes"
    verdict_url: str = gate.Policy.url
    verdict_timeout_s: float = 3.0
    #: never touch a sentinel younger than this: the turn may still be finishing
    min_age_s: float = 180.0
    #: stop resurrecting old dead sessions
    max_age_s: float = 6 * 3600.0
    #: restarts per run — the GPU throttle
    max_restarts: int = 2
    #: per-session crash-loop breaker (mirrors desktop.auto_continue.max_attempts)
    max_attempts: int = 2
    #: refuse to add load when the verdict service cannot be read
    require_free: bool = True
    #: empty = every source; otherwise a whitelist like {"local", "oneshot"}
    sources: tuple = ()
    #: wall-clock ceiling for one resumed turn (it queues behind the gate first)
    turn_timeout_s: float = 1500.0
    dry_run: bool = False

    @property
    def data_dir(self) -> Path:
        return self.home / "plugin-data" / "gpu-gate"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "restarts.json"

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "restarts.jsonl"


def parse_sources(value: str) -> tuple:
    return tuple(p.strip() for p in (value or "").replace(";", ",").split(",") if p.strip())


# ------------------------------------------------------------------ detection

@dataclass
class Candidate:
    session_id: str
    source: str
    title: str
    cwd: str
    interrupted_at: float
    age_s: float
    kind: str = "sentinel"
    last_prompt: str = ""
    reason: str = field(default="", repr=False)

    def brief(self) -> str:
        return "%-22s %-8s %6.0fmin  %s" % (
            "…" + self.session_id[-18:], self.source or "-", self.age_s / 60.0,
            (self.title or "(untitled)")[:48])


#: The **last meaningful message** of each live session, whatever it is, so the
#: run can report what it found instead of silently matching nothing. Rows the
#: turn loop appends after the fact (compaction handoff, notifications) are not
#: answers and are skipped, otherwise a sentinel buried under one of them is
#: invisible forever — which is exactly how every gated death so far was missed.
_SQL_LAST_MESSAGE = (
    "WITH last AS (SELECT session_id, MAX(id) AS id FROM messages "
    "   WHERE active = 1 AND IFNULL(display_kind,'') NOT IN (%s) AND %s "
    "   GROUP BY session_id) "
    "SELECT s.id, IFNULL(s.source,''), IFNULL(s.title,''), IFNULL(s.cwd,''), m.timestamp, "
    "       m.role, IFNULL(m.display_kind,''), substr(IFNULL(m.content,''), 1, 400), "
    "       (SELECT u.content FROM messages u WHERE u.session_id = s.id AND u.role = 'user' "
    "        AND u.active = 1 AND u.id < m.id ORDER BY u.id DESC LIMIT 1) "
    "  FROM sessions s JOIN last l ON l.session_id = s.id JOIN messages m ON m.id = l.id "
    " WHERE IFNULL(s.archived, 0) = 0 ORDER BY m.timestamp ASC"
) % (
    ",".join("'%s'" % k for k in IGNORED_DISPLAY_KINDS),
    " AND ".join("IFNULL(content,'') NOT LIKE '%s%%'" % p
                 for p in IGNORED_CONTENT_PREFIXES),
)

#: how many of these restart the session; the rest are census material
RESTARTABLE = ("sentinel", "failed_turn")


def classify(role: str, display_kind: str, content: str) -> str:
    """One word for what a session's last message means. ``answered`` and
    ``mid_flight`` are the two shapes that must never be restarted."""
    if (content or "").startswith(SENTINEL_PREFIX):
        return "sentinel"
    if display_kind == FAILED_TURN_KIND:
        return "failed_turn"
    if role == "user":
        return "unanswered_user"
    if role == "tool":
        return "mid_flight"
    return "answered"


def open_db(home: Path) -> sqlite3.Connection:
    """Read-only: recovery must never be able to corrupt the transcript."""
    return sqlite3.connect("file:%s?mode=ro" % (home / "state.db"), uri=True, timeout=10.0)


def live_session_ids(conn: sqlite3.Connection, now: float) -> set:
    """Sessions with an unexpired turn lease — somebody is working on them now."""
    try:
        rows = conn.execute(
            "SELECT conversation_id FROM session_turn_leases WHERE expires_at > ?", (now,))
    except sqlite3.Error:
        return set()  # older Hermes without the lease table: age is the only guard
    return {str(r[0]) for r in rows}


def scan(conn: sqlite3.Connection, opts: Options, now: float):
    """→ (restartable candidates oldest-first, skipped [(Candidate, why)], census).

    The census is the point of the whole function: a run that finds nothing has
    to say *what it looked at*, or "it never fires" stays undiagnosable.
    """
    live = live_session_ids(conn, now)
    picks: list[Candidate] = []
    skipped: list[tuple[Candidate, str]] = []
    census = {"sessions": 0, "by_last_message": {}, "leases_live": len(live),
              "markers_recent": 0}
    for row in conn.execute(_SQL_LAST_MESSAGE):
        # positional: the script must not depend on a row factory
        sid, source, title, cwd, ts, role, display_kind, content, last_prompt = row
        age = now - float(ts or 0)
        kind = classify(str(role), str(display_kind), str(content or ""))
        cand = Candidate(session_id=str(sid), source=str(source), title=str(title),
                         cwd=str(cwd), interrupted_at=float(ts or 0), age_s=age,
                         kind=kind, last_prompt=str(last_prompt or ""))
        census["sessions"] += 1
        census["by_last_message"][kind] = census["by_last_message"].get(kind, 0) + 1
        if kind not in RESTARTABLE:
            continue
        if cand.session_id in live:
            skipped.append((cand, "turn lease is live"))
        elif age < opts.min_age_s:
            skipped.append((cand, "too young (%.0fs)" % age))
        elif age > opts.max_age_s:
            skipped.append((cand, "too old (%.1fh)" % (age / 3600.0)))
        elif opts.sources and cand.source not in opts.sources:
            skipped.append((cand, "source %r not selected" % cand.source))
        else:
            picks.append(cand)
    census["markers_recent"] = _recent_markers(conn, now)
    return picks, skipped, census


def _recent_markers(conn: sqlite3.Connection, now: float, window_s: float = 86400.0) -> int:
    """Sessions carrying an interruption marker in the last day, wherever it sits.

    Non-zero with zero picks means the markers are being written and something
    else is answering (or restarting) them; zero means the marker pipeline
    itself is the problem.
    """
    try:
        return int(conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM messages WHERE active = 1 AND timestamp > ? "
            "  AND (content LIKE ? OR IFNULL(display_kind,'') = ?)",
            (now - window_s, _SENTINEL_LIKE, FAILED_TURN_KIND)).fetchone()[0])
    except (sqlite3.Error, TypeError):
        return 0


def fmt_census(census: dict) -> str:
    parts = " ".join("%s=%d" % (k, v) for k, v in sorted(census["by_last_message"].items()))
    return ("recover-interrupted: census %d session(s) [%s] leases_live=%d "
            "markers_24h=%d" % (census["sessions"], parts or "none",
                                census["leases_live"], census["markers_recent"]))


# -------------------------------------------------------------------- ledger

def read_ledger(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def attempts_in_ledger(ledger: dict, session_id: str, now: float, window_s: float) -> int:
    """Attempts inside the recovery window — an old entry must not blacklist a
    session forever, only for as long as it is still a recovery candidate."""
    entry = ledger.get(session_id)
    if not isinstance(entry, dict):
        return 0
    try:
        if now - float(entry.get("last_ts", 0)) > window_s:
            return 0
    except (TypeError, ValueError):
        return 0
    return int(entry.get("attempts", 0) or 0)


def record_attempt(ledger: dict, session_id: str, now: float, note: str) -> dict:
    entry = ledger.get(session_id)
    entry = entry if isinstance(entry, dict) else {}
    attempts = int(entry.get("attempts", 0) or 0) + 1
    record = {"attempts": attempts, "last_ts": now, "note": note}
    ledger[session_id] = record
    return record


def prune_ledger(ledger: dict, now: float, window_s: float) -> dict:
    keep = {}
    for sid, entry in ledger.items():
        try:
            fresh = now - float(entry.get("last_ts", 0)) <= window_s
        except (AttributeError, TypeError, ValueError):
            fresh = False
        if fresh:
            keep[sid] = entry
    return keep


def journal(data_dir: Path, record: dict, name: str = "restarts.jsonl") -> None:
    """Append-only audit trail; a journal that cannot be written never stops a recovery."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(data_dir / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _census_journal(opts: Options, now: float, census: dict, picks: int, skipped: int) -> None:
    """One line per run, including the runs that did nothing. "It never fires" is
    only diagnosable if every tick left a trace of what it looked at."""
    journal(opts.data_dir, {"ts": round(now, 3), "kind": "census",
                            "sessions": census["sessions"],
                            "by_last_message": census["by_last_message"],
                            "leases_live": census["leases_live"],
                            "markers_24h": census["markers_recent"],
                            "picks": picks, "skipped": skipped},
            name="recover.jsonl")


# --------------------------------------------------------------------- verdict

def verdict_gate(opts: Options) -> tuple[bool, str]:
    """May we add load right now? → (yes, reason)."""
    if not opts.require_free:
        return True, "not required"
    verdict = gate.fetch_verdict(opts.verdict_url, opts.verdict_timeout_s)
    if not verdict.get("ok"):
        return False, "verdict unreachable (%s)" % verdict.get("error", "?")
    if verdict.get("free"):
        return True, "free inflight=%s" % verdict.get("inflight")
    return False, "busy (%s, inflight=%s)" % (verdict.get("reason"), verdict.get("inflight"))


# --------------------------------------------------------------------- restart

def resolve_hermes(value: str) -> str:
    """Absolute path to the `hermes` launcher, `""` when it cannot be found.

    cron runs with a minimal PATH, so a bare `hermes` — which every interactive
    shell resolves without thinking — is simply not there. Fall back to the two
    places Hermes installs itself.
    """
    if os.sep in value:
        return value if os.path.isfile(value) and os.access(value, os.X_OK) else ""
    extra = os.pathsep.join((str(Path.home() / ".local" / "bin"),
                             str(Path.home() / ".hermes" / "bin")))
    return shutil.which(value) or shutil.which(value, path=extra) or ""


def restart_session(opts: Options, cand: Candidate) -> tuple[bool, str]:
    """Re-submit the continuation nudge into the session. → (ok, detail).

    ``--resume`` keeps the session id, its model/provider and its recorded cwd,
    so the resumed turn lands in the same transcript the user was reading.
    """
    exe = resolve_hermes(opts.hermes_bin)
    if not exe:
        return False, ("cannot find the hermes launcher (%r) — pass --hermes-bin"
                       % opts.hermes_bin)
    cmd = [exe, "chat", "--resume", cand.session_id,
           "--oneshot", "-Q", "--query-file", "-"]
    env = dict(os.environ, HERMES_HOME=str(opts.home))
    cwd = cand.cwd if cand.cwd and os.path.isdir(cand.cwd) else str(opts.home)
    try:
        proc = subprocess.run(cmd, input=NUDGE, capture_output=True, text=True,
                              timeout=opts.turn_timeout_s, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, "timed out after %.0fs" % opts.turn_timeout_s
    except OSError as exc:
        return False, "cannot run %r: %s" % (opts.hermes_bin, exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, "exit %d: %s" % (proc.returncode, _tail(out))
    resumed = re.search(r"session_id:\s*(\S+)", out)
    sid = resumed.group(1) if resumed else ""
    if sid and sid != cand.session_id:
        return False, "resumed a different session (%s)" % sid
    return True, _tail(out) or "ok"


def _tail(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text[-limit:]


# ------------------------------------------------------------------------ run

@contextlib.contextmanager
def run_lock(data_dir: Path):
    """One recovery at a time: a slow resumed turn must not let the next cron
    tick pick the same session. Yields False when another run holds the lock."""
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / "recover.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


def run(opts: Options) -> int:
    started = time.time()
    db_path = opts.home / "state.db"
    if not db_path.exists():
        print("recover-interrupted: no state database at %s" % db_path)
        return 1
    with run_lock(opts.data_dir) as acquired:
        if not acquired:
            print("recover-interrupted: another run holds %s, nothing to do"
                  % (opts.data_dir / "recover.lock"))
            return 0
        conn = open_db(opts.home)
        try:
            picks, skipped, census = scan(conn, opts, started)
            print(fmt_census(census))
            _census_journal(opts, started, census, len(picks), len(skipped))
            for cand, why in skipped:
                print("  skip   %s — %s [%s]" % (cand.brief(), why, cand.kind))
            if not picks:
                print("recover-interrupted: no session on an unanswered interruption")
                return 0

            allowed, why = verdict_gate(opts)
            if not allowed:
                print("recover-interrupted: %d session(s) waiting, but %s — not adding load"
                      % (len(picks), why))
                return 0

            ledger = read_ledger(opts.ledger_path)
            window = opts.max_age_s + opts.min_age_s
            ledger = prune_ledger(ledger, started, window * 4)
            done = 0
            for cand in picks:
                if done >= opts.max_restarts:
                    print("  hold   %s — per-run cap of %d reached" % (cand.brief(), opts.max_restarts))
                    continue
                used = attempts_in_ledger(ledger, cand.session_id, started, window)
                if used >= opts.max_attempts:
                    print("  hold   %s — %d attempts already, giving up" % (cand.brief(), used))
                    continue
                if opts.dry_run:
                    print("  dry    %s — would resume (attempt %d/%d)"
                          % (cand.brief(), used + 1, opts.max_attempts))
                    continue
                print("  restart %s — resuming (attempt %d/%d)…"
                      % (cand.brief(), used + 1, opts.max_attempts))
                ok, detail = restart_session(opts, cand)
                record_attempt(ledger, cand.session_id, time.time(),
                               "ok" if ok else detail[:300])
                done += 1
                print("           %s %s" % ("done:" if ok else "FAILED:", detail[:300]))
                journal(opts.data_dir, {
                    "ts": round(time.time(), 3), "kind": "restart", "marker": cand.kind,
                    "session": cand.session_id,
                    "source": cand.source, "title": cand.title, "ok": ok,
                    "attempt": used + 1, "interrupted_at": cand.interrupted_at,
                    "waited_before_s": cand.age_s, "detail": detail[:500],
                    "verdict": why, "dry_run": False})
            try:
                opts.data_dir.mkdir(parents=True, exist_ok=True)
                with open(opts.ledger_path, "w", encoding="utf-8") as fh:
                    json.dump(ledger, fh, indent=1, sort_keys=True)
            except OSError as exc:
                print("recover-interrupted: ledger not written (%s)" % exc)
            return 0
        finally:
            conn.close()


def main(argv: Optional[list] = None) -> int:
    default_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    ap = argparse.ArgumentParser(
        prog="recover_interrupted.py",
        description="Re-submit a continuation nudge into sessions left on "
                    "'Operation interrupted: waiting for model response' "
                    "(see the module docstring for the whole contract).")
    ap.add_argument("--home", default=default_home,
                    help="profile home holding state.db (default $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--hermes-bin", default=os.environ.get("HERMES_BIN", "hermes"))
    ap.add_argument("--verdict-url",
                    default=os.environ.get("GPU_GATE_VERDICT_URL", gate.Policy.url),
                    help="ia-free /free endpoint (default %(default)s)")
    ap.add_argument("--verdict-timeout-s", type=float, default=3.0)
    ap.add_argument("--min-age-s", type=float, default=180.0,
                    help="ignore a sentinel younger than this (default %(default)s)")
    ap.add_argument("--max-age-s", type=float, default=21600.0,
                    help="ignore an interruption older than this (default %(default)s)")
    ap.add_argument("--max", dest="max_restarts", type=int, default=2,
                    help="restarts per run (default %(default)s)")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="restarts per session inside the window (default %(default)s)")
    ap.add_argument("--sources", default="",
                    help="comma list of session sources to consider (default: all)")
    ap.add_argument("--no-require-free", dest="require_free", action="store_false",
                    help="restart even when the verdict service says busy/unreachable")
    ap.add_argument("--turn-timeout-s", type=float, default=1500.0)
    ap.add_argument("--dry-run", action="store_true", help="report, do not restart")
    args = ap.parse_args(argv)

    opts = Options(home=Path(args.home).expanduser(), hermes_bin=args.hermes_bin,
                   verdict_url=args.verdict_url, verdict_timeout_s=args.verdict_timeout_s,
                   min_age_s=args.min_age_s, max_age_s=args.max_age_s,
                   max_restarts=max(0, args.max_restarts), max_attempts=max(0, args.max_attempts),
                   require_free=args.require_free, sources=parse_sources(args.sources),
                   turn_timeout_s=args.turn_timeout_s, dry_run=args.dry_run)
    return run(opts)


if __name__ == "__main__":
    sys.exit(main())
