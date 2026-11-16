# gpu-gate — a priority gate for Hermes Agent

Run your background Hermes sessions **only when the inference server is actually
free**, so a cron job does not queue itself in front of the interactive session
you are waiting on.

`gpu-gate` is a [Hermes Agent](https://hermes-agent.nousresearch.com) plugin: no
core modification, no proxy, no fork. It hooks the `llm_execution` middleware,
asks a small HTTP service whether the engine has room, waits if it does not, and
lets the request through when its budget runs out.

```
Hermes turn ── iteration ──▶ [ gpu-gate ] ──▶ provider call ──▶ inference server
                                 │
                                 └── GET /free ──▶ verdict service
```

## Why

Self-hosted inference boxes are usually single-slot. Everything submitted to one
competes for the same GPU: the chat you are driving, the nightly mail triage,
the document importer, the batch job you queued at 3 a.m. Hermes has no notion of
"this session is background noise, let it wait" — every turn grabs the model as
soon as it starts.

The gate adds that notion, inside the agent process, in code, not in a prompt.

## Semantics

| Mode | Turn entry (first iteration) | Later iterations |
|---|---|---|
| `normal` | nothing | nothing |
| `low` | `free`: at most `max_inflight` (1) request in flight | `free` |
| `ultralow` | `idle_ok`: engine quiet for `idle_min_s` (30 s) | `free` |

If the slot is not open, **we wait**, in `poll_slice_s` slices, up to
`max_wait_s`. **At the end of the budget we proceed anyway.** A task that waits
forever gets killed by a watchdog (gateway inactivity, a launcher's
`max_task_minutes`) before it ever gets its slot — a late turn beats a turn
killed with nothing to show for it.

Quiet is required **at turn entry only**. Asking for it at every iteration would
be absurd: the session's own previous request resets the quiet clock, so a
20-iteration turn would wait 20 × 30 s.

## The verdict service

The gate only needs one HTTP `GET` returning JSON:

```json
{
  "free": true,
  "reason": "free",
  "idle_for_s": 41.2,
  "idle_ok": true,
  "signals": { "inflight": 0 }
}
```

| Field | Used for |
|---|---|
| `free` | the `low` gate: the engine still accepts a request |
| `idle_for_s` | seconds since the engine went from busy to fully idle |
| `idle_ok` | `idle_for_s >= idle_min_s`, precomputed server-side (optional — the gate falls back to `idle_for_s`) |
| `reason` | free-form; `engine_unreachable` tells the gate that waiting is pointless |
| `signals.inflight` | display only |

Anything else is ignored, and an older service without `idle_for_s` still works:
`ultralow` then falls back to `free`. If the service is unreachable, times out,
or returns garbage, the gate is **neutral** — it never freezes the agent.

A typical implementation reads the engine's own metrics (for llama.cpp-style
servers: `requests_processing`, plus a `busy_for_seconds` / `requests_total` pair
to derive the idle transition) and answers `free = inflight <= 1`.

## How it hooks Hermes

| Seam | Fires | Can refuse? |
|---|---|---|
| `pre_llm_call` | once per **turn** | yes, but a vetoed turn is withheld and never replayed |
| `pre_api_request` | per **iteration** | no — return value discarded, observability only |
| **`llm_execution` middleware** | per **iteration**, wraps the real provider call | **can wait, cannot refuse** |

`llm_execution` is the only per-iteration seam that can block, and it is not
capped by `plugins.hook_callback_timeout` (30 s), which is what makes a long wait
possible. Its chain is fail-open — a callback that raises without calling
`next_call` gets the request executed anyway — which matches the "wait, then
proceed" semantics exactly.

## Install

```bash
./install.sh                 # current profile
./install.sh myprofile       # ~/.hermes/profiles/myprofile
```

Then **restart the gateway / the desktop**: `hermes plugins enable` hot-reloads a
plugin's commands and hooks but *not* its middleware, so a running gateway keeps
serving without the gate until it restarts.

Verify:

```bash
hermes gpu-prio status            # verdict, budgets, rules, untagged default
hermes gpu-prio ls                # tags in place
hermes gpu-prio log               # recent decisions
hermes logs | grep gpu-gate       # gate activity
```

To confirm the middleware is wired in a fresh process (before any restart):

```bash
python3 -c "import model_tools; from hermes_cli.plugins import has_middleware; \
            print(has_middleware('llm_execution'))"
```

Inside a session: `/gpu-prio low`, `/gpu-prio ultralow`, `/gpu-prio normal`,
`/gpu-prio auto` (drop the tag), `/gpu-prio status`, `/gpu-prio log`.

## How a session's mode is chosen

First rule that answers wins:

1. **explicit tag** — `/gpu-prio low` in the session, or
   `hermes gpu-prio set <session> ultralow` from outside;
2. **`mode_by_cwd`** — a working-directory prefix rule, longest prefix wins. A
   background launcher runs its sessions in a dedicated directory, which is the
   one thing the gateway can still see about a one-shot (its `os.environ` markers
   are invisible to the process that serves the turn);
3. **`mode_by_platform`** — a per-platform rule. This is what puts **every cron**
   in `ultralow`, existing and future, without tagging anything one by one (a
   cron session reports `platform="cron"`);
4. **unattended session** → `unattended_mode` (`low` by default). Three markers,
   any one is enough: the launcher env (`HERMES_SINGLE_QUERY_SESSION`,
   `HERMES_CRON_SESSION`), a running cron execution, or — the one that matters
   under a gateway — the **finite admission flag** of the turn
   (`gateway.session_finite.finite_turn_required()`). A session spawned by a
   script (`hermes chat -q --oneshot`) is served by the *running gateway*, which
   never sees the launcher's environment; the admission does carry the one-shot
   fact, so a scripted turn is gated while a person answering in that same
   session later is not. The journal records which marker fired
   (`auto:unattended:env|cron|finite`).
5. **`default_mode`** of the profile (`normal` by default). Setting it to `low`
   makes **every untagged session** — including the interactive one you are typing
   in — wait for a free slot; a session that must never wait gets an explicit
   `/gpu-prio normal` tag or a `mode_by_platform` / `mode_by_cwd` exception.

```yaml
# config.yaml — every cron in ultralow, everything else untouched
plugins:
  entries:
    gpu-gate:
      settings:
        verdict_url: http://192.0.2.10:8765/free
        engine_prefixes: [http://192.0.2.10]
        mode_by_platform:
          cron: ultralow
```

A hand-set tag on a cron session still wins: the platform rule only supplies a
default.

Tags live in the plugin state (`~/.hermes/plugin-data/…/state.json`), are scoped
**per profile**, and expire after `tag_ttl_days`.

## Settings

Under `plugins.entries.gpu-gate.settings` in the profile's `config.yaml`:

| Key | Default | Role |
|---|---|---|
| `enabled` | `true` | disables the gate without uninstalling it |
| `verdict_url` | `http://127.0.0.1:8765/free` | the verdict service |
| `engine_prefixes` | loopback + LAN prefixes | only gates requests whose `base_url` starts with one of these; a session on a remote model is never slowed down |
| `idle_min_s` | `30` | quiet required by `ultralow` at turn entry |
| `max_wait_s` | `1200` | wait budget of an attended session |
| `max_wait_unattended_s` | `420` | budget of an unattended session (tighter watchdogs) |
| `poll_slice_s` | `5` | probe period while waiting, also the maximum reaction delay to a `/stop` |
| `verdict_timeout_s` / `verdict_cache_s` | `2` / `2` | HTTP timeout, lifetime of the verdict shared between sessions |
| `default_mode` / `unattended_mode` | `normal` / `low` | default modes |
| `mode_by_platform` | *(empty)* | platform → mode, e.g. `{cron: ultralow}`; takes precedence over `unattended_mode` |
| `mode_by_cwd` | *(empty)* | path prefix → mode, e.g. `{"/srv/ia-queue": "ultralow"}`; wins over `mode_by_platform` |
| `auto_low_platforms` | *(empty)* | legacy alias of `mode_by_platform` that maps those platforms to `low` |
| `tag_ttl_days` | `7` | tag lifetime |
| `journal_max_bytes` | `1000000` | decision journal ceiling before rotation; `0` turns it off |

**Keep the budgets under your watchdogs**, otherwise the wait is cut before it
ends: gateway inactivity `agent.gateway_timeout` (1800 s by default), cron
inactivity (600 s), and any external launcher's `max_task_minutes` — that time is
**shared** between waiting and working.

## Recovering interrupted turns

A gated session spends most of its life *inside* the middleware, waiting. From the
turn loop's point of view an API call has been in flight the whole time, so
anything that tears the session down while it waits — a desktop or backend
shutdown, a `/stop`, a message typed into a session that is already busy — ends
the turn with a sentinel instead of an answer:

```
Operation interrupted: waiting for model response (726.6s elapsed).
```

The user message is then sitting there unanswered and nothing retries it.
`recover_interrupted.py` is a standalone cron that finds those sessions and
re-submits a short continuation nudge (`--resume`, same session id, same model,
same cwd), so the turn picks up where it stopped instead of waiting for you:

```cron
*/10 * * * * /usr/bin/python3 ~/.hermes/plugins/gpu-gate/recover_interrupted.py \
    --verdict-url http://192.0.2.10:8765/free \
    >> ~/.hermes/logs/gpu-gate-recover.log 2>&1

# another profile: prefix the line with HERMES_HOME=~/.hermes/profiles/<name>
```

What it will and will not touch:

| Guard | Why |
|---|---|
| the **last active message** must be the sentinel | a session answered afterwards is healthy |
| no unexpired row in `session_turn_leases` | somebody is working on it right now |
| `--min-age-s` (180 s) | a sentinel that just landed may still be finishing |
| `--max-age-s` (6 h) | no resurrecting week-old dead sessions |
| `--max` (2) restarts per run | recovery must not become the load spike it recovers from |
| the verdict must say `free` | refuses to add load when the engine is busy **or unreadable** (`--no-require-free` to override) |
| `--max-attempts` (2) per session | a session that keeps dying is left alone, not restarted forever |
| `recover.lock` | one recovery at a time, even if a resumed turn outlives the tick |

It re-submits a **nudge**, not the original prompt: the interrupted turn's user
message, tool calls and results are already in the transcript, so re-sending the
prompt would ask for the same work twice. The nudge says to pick up where it
stopped and not redo completed steps.

Its own state lives under the profile home, in `plugin-data/gpu-gate/`:
`restarts.json` (the attempt ledger), `restarts.jsonl` (one line per restart) and
`recover.lock`. The state database is only ever opened read-only. `--dry-run`
prints what would be restarted, `--help` lists every knob.

## Decision journal

`~/.hermes/plugin-data/gpu-gate/decisions.jsonl`, one JSON line per decision:

```json
{"ts": 1762660000.123, "session": "local-febfef62…", "platform": "cron",
 "mode": "ultralow", "origin": "auto:platform:cron", "entry": true, "iter": 1,
 "waited_s": 45.0, "budget_s": 420.0, "outcome": "admitted:quiet",
 "model": "my-model", "engine": "http://192.0.2.10:4000/v1",
 "verdict": {"ok": true, "free": true, "idle_for_s": 31.4, "idle_ok": true,
             "reason": "idle", "inflight": 0}, "pid": 736504}
```

Recorded: **every turn entry** of a gated session (this is what answers "which
session was running in which mode"), plus any iteration that really waited or
forced its way through on budget exhaustion. A fast iteration inside an already
open turn is not written — otherwise a 90-iteration turn would bury the journal
for nothing.

```bash
hermes gpu-prio log              # last 20 decisions
hermes gpu-prio log 50           # 50
hermes gpu-prio log local-febf   # filtered on a session
```

The gate also writes to the Hermes log (`hermes logs | grep gpu-gate`): `INFO`
when it waited, `DEBUG` for each fast pass. The JSONL is the audit trail, the
Hermes log the supplement; the journal rotates at `journal_max_bytes`, keeping
its tail.

## Tests

```bash
python3 test_gate.py         # 32 assertions: pure core logic
python3 test_middleware.py   # 59 assertions: Hermes glue, fake verdict service on a local socket
python3 test_recover.py      # 38 assertions: interrupted-turn recovery on a throwaway state.db
```

Fully offline, no network, no GPU: the `admit()` matrix (three modes, turn entry
vs iteration, unreachable gate, unreachable engine), the wait loop on a fake
clock (slot after N slices, budget exhausted, exploding probe), `base_url`
filtering, tag lifecycle (TTL, per-session isolation, removal), priority
resolution (tag > cwd > platform > unattended > default), settings parsing, the
journal round-trip and rotation, and — for the recovery cron — candidate
selection (answered / archived / leased / too young / too old), the per-run cap,
the per-session attempt breaker and the verdict throttle.

## Assumed limits

- **The middleware cannot refuse, only wait.** The `llm_execution` chain is
  fail-open on the Hermes side. A clean "refuse the turn" only exists in
  `pre_llm_call` (veto), and a vetoed turn is not replayed — not what we want here.
- **`max 1 request in flight` is a ceiling, not a mutex.** Two concurrent `low`
  sessions each see the other at 1 and both pass; a third one blocks. Real
  serialisation needs a lease on the verdict service, not this counter.
- **Auxiliary calls are not gated** (titling, compression, vision): they do not go
  through `llm_execution`. Small calls, deliberately out of scope.
- **A waiting turn is an inactive turn.** The inactivity watchdog does not
  distinguish "working" from "waiting"; hence the budgets.
- A tag is **per profile**: Hermes profiles are islands on purpose.

## License

MIT
