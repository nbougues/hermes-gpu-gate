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

Detection is deliberately narrow: the **last active message** of the session
must be that sentinel. A session that got a real answer afterwards is healthy
and is never touched.

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
    last_prompt: str = ""
    reason: str = field(default="", repr=False)

    def brief(self) -> str:
        return "%-22s %-8s %6.0fmin  %s" % (
            "…" + self.session_id[-18:], self.source or "-", self.age_s / 60.0,
            (self.title or "(untitled)")[:48])


#: the last active message of the session must be the sentinel, so a session that
#: was answered afterwards (by hand or by an earlier recovery) never matches
_SQL_CANDIDATES = """
SELECT s.id, IFNULL(s.source,''), IFNULL(s.title,''), IFNULL(s.cwd,''), m.timestamp,
       (SELECT u.content FROM messages u
         WHERE u.session_id = s.id AND u.role = 'user' AND u.active = 1 AND u.id < m.id
         ORDER BY u.id DESC LIMIT 1)
  FROM sessions s
  JOIN messages m ON m.id = (SELECT id FROM messages
                              WHERE session_id = s.id AND active = 1
                              ORDER BY id DESC LIMIT 1)
 WHERE m.role = 'assistant' AND m.content LIKE ?
   AND IFNULL(s.archived, 0) = 0
 ORDER BY m.timestamp ASC
"""


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


def find_candidates(conn: sqlite3.Connection, opts: Options, now: float) -> tuple[list, list]:
    """→ (restartable candidates oldest-first, skipped list of (Candidate, why))."""
    live = live_session_ids(conn, now)
    picks: list[Candidate] = []
    skipped: list[tuple[Candidate, str]] = []
    for row in conn.execute(_SQL_CANDIDATES, (_SENTINEL_LIKE,)):
        # positional: the script must not depend on a row factory
        age = now - float(row[4] or 0)
        cand = Candidate(session_id=str(row[0]), source=str(row[1]), title=str(row[2]),
                         cwd=str(row[3]), interrupted_at=float(row[4] or 0),
                         age_s=age, last_prompt=str(row[5] or ""))
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
    return picks, skipped


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


def journal(data_dir: Path, record: dict) -> None:
    """Append-only audit trail; a journal that cannot be written never stops a recovery."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(data_dir / "restarts.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


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
            picks, skipped = find_candidates(conn, opts, started)
            for cand, why in skipped:
                print("  skip   %s — %s" % (cand.brief(), why))
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
                    "ts": round(time.time(), 3), "session": cand.session_id,
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
