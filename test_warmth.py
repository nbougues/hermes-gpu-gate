"""Offline: warmth.py — the caller half of cache-warmth (report out, verdict back).

A stub HTTP server stands in for `ia-free`; nothing leaves the machine.
"""

import importlib.util
import json
import os
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gpu_warmth", os.path.join(_HERE, "warmth.py"))
warmth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(warmth)

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + "%-58s got=%r want=%r" % (label, got, want))
    if not ok:
        FAILS.append(label)


# ------------------------------------------------------------- arbitre simulé
RECEIVED = []
REPLY = {"warm": True, "reason": "hot", "age_s": 3.0, "ratio": 0.98,
         "prompt_tokens": 30000, "evicted_since": 0, "reclaimable_tokens": 12000}
BEHAVIOUR = {"mode": "ok"}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
        RECEIVED.append((self.path, body))
        mode = BEHAVIOUR["mode"]
        if mode == "hang":
            time.sleep(5)
            return
        if mode == "garbage":
            payload = b"not json at all"
        elif mode == "error":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "session is required"}).encode())
            return
        else:
            payload = json.dumps(REPLY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *a):  # noqa: A002 - signature fixed by the base class
        pass


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
SERVER.daemon_threads = True
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % SERVER.server_address[1]


def row(**over):
    out = {"session": "s1", "prompt_tokens": 30000, "cached_tokens": 28000,
           "model": "local", "decode_tps": 38.0}
    out.update(over)
    return out


print("endpoint: deriving the report URL from the verdict URL")
check("free → warmth", warmth.endpoint(BASE + "/free"), BASE + "/warmth")
check("trailing slash", warmth.endpoint(BASE + "/free/"), BASE + "/warmth")
check("no path at all", warmth.endpoint("http://h:8765"), "http://h:8765/warmth")
check("empty url", warmth.endpoint(""), "")
check("garbage url", warmth.endpoint("not a url"), "")
check("None url", warmth.endpoint(None), "")

print("payload: what one call can say about its own context")
body = warmth.payload(row(), 44410)
check("session", body["session"], "s1")
check("prompt tokens", body["prompt_tokens"], 30000)
check("cached tokens", body["cached_tokens"], 28000)
check("engine eviction counter rides along", body["evicted_total"], 44410)
check("tag defaults to the model", body["tag"], "local")
check("agent identifies this instance", bool(body["agent"] and ":" in body["agent"]), True)
check("no eviction signal = key absent",
      "evicted_total" in warmth.payload(row()), False)
check("a call with no prompt tokens says nothing", warmth.payload(row(prompt_tokens=0)), None)
check("no prompt_tokens key at all", warmth.payload({"session": "s"}), None)
check("non-numeric prompt tokens", warmth.payload(row(prompt_tokens="x")), None)
check("cold call keeps 0, not missing", warmth.payload(row(cached_tokens=0))["cached_tokens"], 0)
check("cached never exceeds prompt",
      warmth.payload(row(cached_tokens=99999))["cached_tokens"], 30000)
check("missing cached = cold", warmth.payload(row(cached_tokens=None))["cached_tokens"], 0)
check("non-dict row", warmth.payload(None), None)

print("post: the wire, against a stub arbiter")
RECEIVED.clear()
view = warmth.post(BASE + "/warmth", warmth.payload(row(), 1))
check("reply is handed back", view.get("reason"), "hot")
check("posted to /warmth", RECEIVED[0][0], "/warmth")
check("body arrived as JSON", RECEIVED[0][1]["session"], "s1")
check("unreachable arbiter returns None", warmth.post("http://127.0.0.1:1/warmth", {}), None)
check("empty url returns None", warmth.post("", {}), None)
BEHAVIOUR["mode"] = "garbage"
check("non-JSON reply returns None", warmth.post(BASE + "/warmth", {}), None)
BEHAVIOUR["mode"] = "error"
check("4xx returns None, not an exception", warmth.post(BASE + "/warmth", {}), None)
BEHAVIOUR["mode"] = "hang"
start = time.time()
check("timeout is honoured", warmth.post(BASE + "/warmth", {}, timeout=0.3), None)
check("and it actually gave up quickly", time.time() - start < 2.0, True)
BEHAVIOUR["mode"] = "ok"

print("spawn: fire-and-forget, and the reply is kept for later")
warmth._views.clear()
warmth.spawn(BASE + "/warmth", warmth.payload(row(), 7))
deadline = time.time() + 3.0
while time.time() < deadline and not warmth.last("s1"):
    time.sleep(0.05)
check("the report went out on its own thread", warmth.last("s1").get("reason"), "hot")
check("stamped for display", isinstance(warmth.last("s1").get("_at"), float), True)
check("another session is not polluted", warmth.last("other"), {})
check("spawn on an empty url is a no-op", warmth.spawn("", {"session": "x"}), None)
warmth.note("s2", {"reason": "cold"})
check("note keeps a copy", warmth.last("s2")["reason"], "cold")
warmth.note("s2", {"reason": "changed"})
check("last() is the newest", warmth.last("s2")["reason"], "changed")

print("fmt_view: one line a human reads")
check("nothing reported yet", warmth.fmt_view({}).startswith("warmth: never reported"), True)
line = warmth.fmt_view({"reason": "hot", "age_s": 12.0, "ratio": 0.98,
                        "prompt_tokens": 30000, "evicted_since": 0,
                        "reclaimable_tokens": 20000})
check("states the reason", "hot" in line, True)
check("shows the cache ratio", "98%" in line, True)
check("shows the footprint", "30000 tok" in line, True)
check("zero evictions is not shown as a problem", "evicted since" in line, False)
check("shows what could be evicted", "evictable 20000 tok" in line, True)
angry = warmth.fmt_view({"reason": "evicted", "age_s": 5.0, "ratio": 0.9,
                         "prompt_tokens": 30000, "evicted_since": 3,
                         "needs_tokens": 40000, "room_for_cold": False,
                         "advice": "cold-no-room"})
check("evictions are called out", "+3 evicted since" in angry, True)
check("the room verdict is shown", "room: no" in angry, True)
check("the advice is the last word", angry.rstrip().endswith("→ cold-no-room"), True)

SERVER.shutdown()
SERVER.server_close()
print()
print("FAILURES: %d" % len(FAILS))
for name in FAILS:
    print("  - " + name)
raise SystemExit(1 if FAILS else 0)
