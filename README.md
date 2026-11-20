# gpu-gate — a priority gate for Hermes Agent

Run your background Hermes sessions **only when the inference server is actually
free**, so a cron job does not queue itself in front of the interactive session
you are waiting on.

`gpu-gate` is a [Hermes Agent](https://hermes-agent.nousresearch.com) plugin: no
core modification, no proxy, no fork. It hooks the `llm_execution` middleware,
asks a small HTTP service whether the engine has room, waits if it does not, and
lets the request through when its budget runs out. On top of the gate it adds
**pauses** (hold a session, a mode or everything, for a fixed time or until you
say otherwise) and **slowness detection** (decode tokens/s and prompt-cache reuse
per call, with an optional automatic pause when a session degrades), plus a cron
that resurrects sessions a torn-down turn left for dead.

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
| `pause_max_wait_s` | `300` | how long a *timed* pause may hold a turn that is already inside the middleware; past that the turn proceeds and the pause stays armed for the next one |
| `indefinite_max_s` | `21600` | safety net: an indefinite pause stops holding a waiting turn after this long (the pause itself remains, until `/gpu-prio resume`) |
| `perf_enabled` | `true` | sample decode speed and prompt-cache reuse on every provider call (see *Measuring slowness*) |
| `perf_max_bytes` | `2000000` | `perf.jsonl` ceiling before rotation; `0` turns the journal off |
| `perf_decode_tps_min` | `12.0` | below this decoded tokens/s a call counts as slow |
| `perf_cache_ratio_min` | `0.25` | below this prompt-cache reuse ratio a call counts as slow |
| `perf_min_output_tokens` | `64` | a shorter answer carries no signal; not judged |
| `perf_min_prompt_tokens` | `2000` | a prompt this small has nothing to reuse; the cache ratio is not judged |
| `perf_consecutive` | `3` | slow calls in a row, same session, before anything is done about it |
| `auto_pause_enabled` | `false` | actually pause the session when the streak trips (**off by default** — read `perf.jsonl` first) |
| `auto_pause_min_s` / `auto_pause_max_s` | `20` / `90` | the pause length is drawn uniformly between these |
| `auto_pause_per_hour` | `6` | per-session cap; past it a streak is journalled, not acted on |

**Keep the budgets under your watchdogs**, otherwise the wait is cut before it
ends: gateway inactivity `agent.gateway_timeout` (1800 s by default), cron
inactivity (600 s), and any external launcher's `max_task_minutes` — that time is
**shared** between waiting and working.

## Pausing a session

A mode says *how politely* a session waits. A pause says *do not run at all yet* —
for a fixed time, or until you say so. Useful when the engine is about to be
restarted, when a deploy is running, or when one session keeps producing junk and
you want it stopped mid-fall rather than merely slowed.

```
/gpu-prio pause                      # this session, indefinitely
/gpu-prio pause 30m                  # this session, 30 minutes
/gpu-prio pause all 2h deploy        # every gated session, with a reason
/gpu-prio pause ultralow 45s         # every ultralow session
/gpu-prio pauses                     # what is armed, with what is left
/gpu-prio resume                     # lift this session's pause
/gpu-prio resume all
```

Same from the shell: `hermes gpu-prio pause all 10m maintenance`,
`hermes gpu-prio resume all`, `hermes gpu-prio pauses`.

A duration is `45s` / `30m` / `2h` / `until 15:30`; bare `pause` means indefinite.
Scopes are this session (default), `all`, or a mode name.

**A timed pause and an indefinite one behave differently, on purpose:**

| | turn already inside the middleware | next turn |
|---|---|---|
| timed (`30m`) | held in place, up to `pause_max_wait_s` (300 s), then it proceeds and the pause stays armed for the next iteration | waits for the deadline |
| indefinite | held up to `indefinite_max_s`, then proceeds | **refused** at `pre_llm_call`, before any inference |

The indefinite case refuses because that is the only place Hermes lets a plugin
say *no* cleanly. The refusal tells the user their message was **not** run and
will not be replayed, so nothing is silently swallowed: a hand brake is supposed
to be visible. A timed pause is a hold, not a refusal, so it never drops a turn —
if it cannot be honoured within `pause_max_wait_s` the turn goes through and the
journal records `pause:pause_overrun`.

Every pause is journalled (`decisions.jsonl`, `kind: paused_turn` for refusals,
`outcome: pause:…` for holds) with its scope, reason and remaining time, so
"why did nothing run at 3 a.m." has an answer.

## Measuring slowness

`/gpu-prio perf` answers "is the engine actually slow right now, or does it just
feel slow". Every provider call is sampled through the `post_api_request` hook —
no core change, no proxy — and written to `perf.jsonl`:

```json
{"ts": 1762660000.1, "session": "local-febfef62…", "model": "qwen3", "iter": 4,
 "duration_s": 21.4, "ttft_s": 1.8, "output_tokens": 512, "input_tokens": 30120,
 "cached_tokens": 29440, "decode_tps": 26.1, "cache_ratio": 0.977}
```

`decode_tps` is output tokens over the time *after* the first chunk arrived, so
queueing and prefill do not pollute it; a non-streaming call falls back to the
whole duration and is marked `approx`. `cache_ratio` is reused prompt tokens over
prompt tokens — the number that tells you a session is re-sending a cold prompt.

A call counts as **slow** when `decode_tps < perf_decode_tps_min` **or**
`cache_ratio < perf_cache_ratio_min`, with two guards so the rule cannot fire on
noise: an answer shorter than `perf_min_output_tokens` carries no decode signal,
and a prompt under `perf_min_prompt_tokens` has nothing to reuse. One slow call
is a hiccup; `perf_consecutive` in a row in the same session is a trend.

When a trend trips, the default action is **to write it down and do nothing**
(`auto_pause_enabled: false`). The streak lands in `perf.jsonl` as
`kind: slow_streak`. Turn the auto-pause on once you have seen what your normal
looks like, and the same streak pauses the session for a duration drawn
uniformly between `auto_pause_min_s` and `auto_pause_max_s` — random on purpose,
so a fleet of sessions that all went slow together does not wake up together and
go slow together again. `auto_pause_per_hour` caps the escalation per session;
past it a streak is logged as `auto_pause_throttled` instead of stacking pauses.

The engine's own view arrives with the verdict: the `/free` service reports
`decode_tps`, `prefill_tps`, `cache_hit_ratio` and `queue_wait_longest_seconds`
straight from the server's metrics, and those are journalled next to the client
samples. When the two disagree, the client is right about what *it* experienced
and the engine is right about *why*.

## Warm or cold: is this session's context already resident?

A cold call pays prefill for the whole prompt; a warm one pays almost nothing. On
this engine (Halogen / llama.cpp, 4 slots, 262 144 ctx) a 30 k-token prompt costs
~23 s cold and ~0.5 s warm — a 40× difference, and the cold one *evicts* someone
else's entry to make room. So the useful question before a turn is not "is the GPU
free" but "does this session still have its context on the card".

**The engine cannot answer it.** There is no API to ask whether a given prefix is
resident: `/cache` publishes only aggregates (`entries 24/24`, `hit_rate`,
`evicted`), `/slots` shows slots, not keys. What the engine *does* give you is the
answer after the fact, in the response you already have:

| call | `usage.prompt_tokens_details` | engine counters |
|---|---|---|
| cold | **key absent entirely** | `misses +1`, `evicted +2` |
| warm | `cached_tokens: 1379` (100 %) | `hits +1`, `evicted +0` |

So warmth is *inferred*, and the inference needs one number per call that only the
caller has. That is the whole design:

1. **gpu-gate** reads `cached_tokens` / `prompt_tokens` off every call it already
   samples, and POSTs them to the arbiter (`POST /warmth`, fire-and-forget on a
   daemon thread — a dead arbiter costs a dropped report, never a stalled turn).
2. **ia-free** is the arbiter because it is the one place that sees all 2-3 Hermes
   instances. They never talk to each other; they all talk to the gate.
3. The gate answers `warmth` on `/free?session=<id>&tokens=<n>`.

The verdict is `hot` only when three things hold at once: the last call's ratio was
above `warm_ratio_min`, it was less than `warm_grace_s` ago, **and** the engine's
global `cache_evicted_total` has not moved since. That third term is what makes it
correct across instances — nothing tells session A that instance B just prefilled
40 k tokens, but both read the same eviction counter.

```
/gpu-prio perf
warmth: hot · age 12s · cache 99% · 28500 tok · evictable 45000 tok · needs 28500 · room: yes · → go
warmth: unknown · age ? · cache ? · ? tok · evictable 45000 tok · needs 40000 · room: yes · → cold-room
warmth: evicted · age 5s · cache 90% · 30000 tok · +3 evicted since · needs 40000 · room: no · → cold-no-room
```

`evictable` answers "is there anything cold enough to throw away": the footprint of
sessions last seen more than `protect_age_s` ago. Anything younger is counted as
`protected` — a running session's context is not spare room, and evicting it is how
you turn one slow session into two. A warm session needs nothing: its space is
already taken, so it gets `room: yes` unconditionally.

`advice` is the raw "do we go or not" answer — `go`, `cold-room`, `cold-no-room`,
`cold`. **It is advisory: `/free` does not consult it yet.** Nothing here changes an
admission decision; the table has to prove itself against real traffic first. The
knobs live on the arbiter (`IA_FREE_WARM_GRACE_S=90`, `IA_FREE_WARM_RATIO_MIN=0.5`,
`IA_FREE_PROTECT_AGE_S=60`, `IA_FREE_WARM_TTL_S=900`, `IA_FREE_WARM_MAX_SESSIONS=200`)
and the table is persisted in `state.json`, so a gate restart does not erase what
the instances reported.

Two honest limits. The grace window is a guess: a session idle 2 minutes is probably
gone from a two-dozen-entry cache, but "probably" is all the evidence supports. And
the engine's cache holds only a couple of dozen entries (`max_entries` 24 on 0.18.15,
28 on 0.20.7) against a pool that was already 99.6 % full — with more live sessions
than that across all instances, entries leave no matter how careful the admission is,
and warmth becomes a description of the thrash rather than a way to avoid it.

One trap worth naming, because an engine upgrade walked straight into it: every
counter used here is monotonic *per engine generation*. A restart resets them, and a
decremented eviction counter reads as "nobody evicted anything" — so a session that
was warm right before the restart stays warm forever. The arbiter therefore keeps an
`engine_epoch`, bumped when `requests_total` goes backwards or when any session's
reported eviction counter is above the engine's current one (the baseline-free case:
arbiter restarted *after* the engine). A bump wipes the warmth table. Nothing survives
a restart, and pretending otherwise is how a cold session gets told it has nothing to
reload.

## Recovering interrupted turns

A gated session spends most of its life *inside* the middleware, waiting. From the
turn loop's point of view an API call has been in flight the whole time, so
anything that tears the session down while it waits — a desktop or backend
shutdown, a `/stop`, a message typed into a session that is already busy — ends
the turn with a marker instead of an answer. There are two, and a recovery that
only knows one of them misses half the deaths:

```
Operation interrupted: waiting for model response (726.6s elapsed).   ← an interrupt landed
Your request was not processed. Send it again…                        ← display_kind=failed_turn:
                                                                        the liveness watchdog
                                                                        aborted a stalled turn
```

The second one is the shape the gate itself produces: a wait longer than
`agent.turn_liveness.timeout_s` (600 s) is aborted as "no progress", because the
watchdog cannot tell waiting from hanging. That is why `pause_max_wait_s` and
`max_wait_s` are kept well under it.

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
| the **last meaningful message** must be one of the two markers | a session answered afterwards is healthy |
| machinery rows are skipped, not treated as answers | a compaction handoff or a `process_complete` notice lands *after* the marker and would hide it forever (`hidden`, `internal_notification`, `process_complete`, `async_delegation_complete` — Hermes' own `_MACHINERY_DISPLAY_KINDS`) |
| a session ending on a bare user prompt or a tool result is left alone | that is an abandoned session or a turn in flight, not a death |
| no unexpired row in `session_turn_leases` | somebody is working on it right now |
| `--min-age-s` (180 s) | a marker that just landed may still be finishing |
| `--max-age-s` (6 h) | no resurrecting week-old dead sessions |
| `--max` (2) restarts per run | recovery must not become the load spike it recovers from |
| the verdict must say `free` | refuses to add load when the engine is busy **or unreadable** (`--no-require-free` to override) |
| `--max-attempts` (2) per session | a session that keeps dying is left alone, not restarted forever |
| `recover.lock` | one recovery at a time, even if a resumed turn outlives the tick |

It re-submits a **nudge**, not the original prompt: the interrupted turn's user
message, tool calls and results are already in the transcript, so re-sending the
prompt would ask for the same work twice. The nudge says to pick up where it
stopped and not redo completed steps.

**Every run reports what it looked at.** A recovery that silently finds nothing is
indistinguishable from a recovery that is broken — which is exactly how this script
spent weeks matching zero sessions while three were sitting dead in the database.
Each tick prints a census and appends it to `recover.jsonl`:

```
recover-interrupted: census 362 session(s) [answered=332 failed_turn=3 mid_flight=2
  unanswered_user=25] leases_live=1 markers_24h=3
  skip   …0c101a9d3455e14312    local     24562min  Audit plan_lan.dot [failed_turn] too old (409.4h)
```

`markers_24h` with zero picks means the markers are being written and something
else is answering them; `markers_24h: 0` means the marker pipeline itself is the
problem. Either way the next person does not have to guess.

Its own state lives under the profile home, in `plugin-data/gpu-gate/`:
`restarts.json` (the attempt ledger), `restarts.jsonl` (one line per restart),
`recover.jsonl` (one census line per run) and `recover.lock`. The state database
is only ever opened read-only. `--dry-run` prints what would be restarted,
`--help` lists every knob.

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
python3 test_gate.py         #  32 assertions: pure core logic
python3 test_middleware.py   # 104 assertions: Hermes glue, fake verdict service on a local socket
python3 test_pause.py        #  42 assertions: pause scopes, durations, expiry, jitter
python3 test_perf.py         #  44 assertions: sampling, the slow rule, the streak, the cap
python3 test_recover.py      #  41 assertions: interrupted-turn recovery on a throwaway state.db
python3 test_warmth.py       #  44 assertions: cache-warmth report out, verdict back
```

Fully offline, no network, no GPU: the `admit()` matrix (three modes, turn entry
vs iteration, unreachable gate, unreachable engine), the wait loop on a fake
clock (slot after N slices, budget exhausted, exploding probe), `base_url`
filtering, tag lifecycle (TTL, per-session isolation, removal), priority
resolution (tag > cwd > platform > unattended > default), settings parsing, the
journal round-trip and rotation. For the pause layer: duration parsing (`30m`,
`until 15:30`, `indef`, garbage), scope precedence (session > mode > `all`),
expiry, the indefinite safety net, the jitter window, and the middleware actually
holding a turn then releasing it on `resume`. For the perf layer: that
`decode_tps` excludes time-to-first-token, that a short answer or a small prompt
is never judged, the streak needing `perf_consecutive` in a row, the per-hour cap
turning into `throttled` instead of escalating, and the auto-pause being **off**
by default. For the recovery cron: every shape a session can end on (sentinel,
watchdog `failed_turn`, marker buried under a compaction row, answered, bare
prompt, tool row, archived, leased, too young, too old), the census counts, the
per-run cap, the per-session attempt breaker and the verdict throttle.

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
  distinguish "working" from "waiting"; hence the budgets. A plugin cannot stamp
  the activity clock — `llm_execution` is handed request data, not the agent —
  so a long wait *will* be aborted at `agent.turn_liveness.timeout_s` if you let
  it run that long. Keep `max_wait_s`, `max_wait_unattended_s` and
  `pause_max_wait_s` under it.
- **A pause holds at the next provider call, not inside the current one.** It
  cannot reach into a request already in flight, and a timed pause stops holding a
  turn after `pause_max_wait_s` — a pause that outlives its budget lets the turn
  through and waits for the next one rather than being killed by a watchdog.
- **`perf` can only measure what the provider reports.** No `usage` in the
  response means no sample at all; no `cached_tokens` means `cache_ratio` is
  `null` and the cache rule simply does not judge that call. Non-streaming calls
  have no time-to-first-token, so their `decode_tps` includes prefill and is
  marked `approx`.
- **Recovery resumes a session in a second process.** `hermes chat --resume` on a
  session that is *also* open in the desktop means two processes on one
  transcript; the turn lease blocks most of that, but a session whose lease
  lapsed while you were reading it can be nudged under you. `--min-age-s` is the
  blunt instrument keeping that rare.
- A tag is **per profile**: Hermes profiles are islands on purpose.

## License

MIT
