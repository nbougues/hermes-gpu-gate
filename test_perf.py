"""Offline: perf.py — what a provider call's payload turns into, and when it is "slow"."""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gpu_perf", os.path.join(_HERE, "perf.py"))
perf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(perf)

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + "%-58s got=%r want=%r" % (label, got, want))
    if not ok:
        FAILS.append(label)


def payload(**over):
    row = {
        "session_id": "s1", "platform": "cli", "model": "local", "provider": "openai",
        "base_url": "http://10.0.0.5:8731/v1", "api_call_count": 3,
        "started_at": 1000.0, "api_duration": 20.0, "first_chunk_at": 1002.0,
        "usage": {"input_tokens": 30000, "output_tokens": 500, "cached_tokens": 28000},
        "finish_reason": "stop",
    }
    row.update(over)
    return row


print("sample: one provider call → metrics")
row = perf.sample(payload())
check("decode rate excludes the time to first chunk", row["decode_tps"], round(500 / 18.0, 2))
check("ttft measured", row["ttft_s"], 2.0)
check("cache ratio", row["cache_ratio"], round(28000 / 30000, 4))
check("not flagged as approx", row["approx"], False)
check("session kept", row["session"], "s1")
check("iteration kept", row["iter"], 3)

check("no usage at all", perf.sample(payload(usage=None))["decode_tps"], None)
check("zero output tokens", perf.sample(payload(usage={"input_tokens": 10})).get("decode_tps"), None)
check("no duration = no sample", perf.sample(payload(api_duration=0)), None)
check("missing duration = no sample", perf.sample(payload(api_duration=None)), None)
check("non-streaming call falls back to the whole duration",
      perf.sample(payload(first_chunk_at=None))["decode_tps"], round(500 / 20.0, 2))
check("…and says it is an approximation", perf.sample(payload(first_chunk_at=None))["approx"], True)
check("provider alias (completion_tokens)",
      perf.sample(payload(usage={"prompt_tokens": 1000, "completion_tokens": 100,
                                 "cache_read_input_tokens": 500}))["cache_ratio"], 0.5)
check("first chunk after the whole call is not a ttft",
      perf.sample(payload(first_chunk_at=1020.0))["approx"], True)

print("\nwhat counts as slow")
fast = perf.sample(payload())
slow = perf.sample(payload(api_duration=100.0, first_chunk_at=1002.0))   # ~6 t/s
cold = perf.sample(payload(usage={"input_tokens": 30000, "output_tokens": 500,
                                  "cached_tokens": 0}))
tiny = perf.sample(payload(usage={"input_tokens": 30000, "output_tokens": 8,
                                  "cached_tokens": 0}))
small = perf.sample(payload(usage={"input_tokens": 300, "output_tokens": 500,
                                   "cached_tokens": 0}))
check("a normal call is not slow", perf.slow_reason(fast, 12.0, 0.25, 64, 2000), "")
check("slow decode is caught", perf.slow_reason(slow, 12.0, 0.25, 64, 2000), perf.SLOW_DECODE)
check("an empty prompt cache is caught", perf.slow_reason(cold, 12.0, 0.25, 64, 2000), perf.SLOW_CACHE)
check("a tiny answer carries no signal", perf.slow_reason(tiny, 12.0, 0.25, 64, 2000), "")
check("a small prompt has nothing to cache", perf.slow_reason(small, 12.0, 0.25, 64, 2000), "")
check("decode wins over cache when both are bad",
      perf.slow_reason(perf.sample(payload(api_duration=100.0, first_chunk_at=1002.0,
                                           usage={"input_tokens": 30000,
                                                   "output_tokens": 500})),
                       12.0, 0.25, 64, 2000), perf.SLOW_DECODE)

print("\nthe streak: one slow call is noise")
tracker = perf.SlowTracker(decode_tps_min=12.0, cache_ratio_min=0.25,
                           min_output_tokens=64, min_prompt_tokens=2000,
                           consecutive=3, max_per_hour=2)
check("1st slow: nothing", tracker.observe(slow), None)
check("2nd slow: nothing", tracker.observe(slow), None)
decision = tracker.observe(slow)
check("3rd slow: a pause is due", decision["kind"], "pause")
check("…with the reason", decision["reason"], perf.SLOW_DECODE)
check("…and the streak", decision["streak"], 3)
check("streak resets after firing", tracker.observe(slow), None)
check("a fast call breaks the streak", (tracker.observe(slow), tracker.observe(fast),
                                        tracker.observe(slow))[2], None)
# streak is back to 1 here; two more slow calls fire again, and the hour's cap (2) is reached
fired = [tracker.observe(slow) for _ in range(2)]
check("2nd firing still pauses", fired[1]["kind"], "pause")
later = [tracker.observe(slow) for _ in range(3)]
check("cap reached: throttled instead of escalating", later[2]["kind"], "throttled")
check("snapshot reports the streak", tracker.snapshot("s1")["reason"], perf.SLOW_DECODE)
check("configure re-reads thresholds",
      (tracker.configure(1000.0, 0.25, 64, 2000, 1, 6),
       tracker.observe(perf.sample(payload(api_duration=100.0, first_chunk_at=1002.0))))[1]["kind"],
      "pause")

print("\naggregation")
rows = [{"decode_tps": 10, "cache_ratio": 0.5, "ttft_s": 1.0, "session": "a"},
        {"decode_tps": 30, "cache_ratio": 0.9, "ttft_s": 2.0, "session": "b"},
        {"decode_tps": 50, "cache_ratio": None, "ttft_s": None, "session": "b"},
        {"kind": "auto_pause", "session": "a"}]
summary = perf.summarise(rows)
check("counts only the calls", summary["calls"], 4)
check("only rows with a rate count", summary["with_tps"], 3)
check("distinct sessions", summary["sessions"], 2)
check("p50", summary["tps_p50"], 30)
check("min", summary["tps_min"], 10)
check("max", summary["tps_max"], 50)
check("cache mean ignores None", summary["cache_mean"], round((0.5 + 0.9) / 2, 10))
check("empty input is safe", perf.summarise([])["calls"], 0)
check("formatting survives empty", "decode t/s" in perf.fmt_summary(perf.summarise([])), True)
check("journal file name", perf.JOURNAL_NAME, "perf.jsonl")

print("\nengine note")
perf.note_engine("s1", {"ok": True, "free": True, "inflight": 1, "decode_tps": 38.2})
check("last verdict is kept per session", perf.engine_note("s1")["decode_tps"], 38.2)
check("another session has none", perf.engine_note("nope"), {})
perf.note_engine("s2", {"ok": False, "error": "boom"})
check("a failed verdict is not kept", perf.engine_note("s2"), {})

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
