"""Capture vs clock expiry: can a capture be recorded at or after its hold's expires_at, and what does stage-3 history
then say? Retries up to N holds; captures are fired across the deadline from 30 threads.
Usage: python3 probe_capture_after_deadline.py <base> [attempts]"""
import concurrent.futures as cf, datetime as dt, json, sys, time, urllib.parse, urllib.request, urllib.error
B = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
U = dt.timezone.utc
def call(m, p, body=None, tok=None, key=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    if key: h["Idempotency-Key"] = key
    r = urllib.request.Request(B + p, json.dumps(body).encode() if body is not None else None, h, method=m)
    try:
        with urllib.request.urlopen(r, timeout=10) as x:
            t = x.read(); return x.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
P = lambda s: dt.datetime.fromisoformat(s)
q = lambda s: urllib.parse.quote(s, safe="")
K = [0]
def key():
    K[0] += 1; return "cad-%d-%d" % (time.time_ns(), K[0])
for attempt in range(1, N + 1):
    users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": 100000} for u in ("ada", "bob")]
    call("POST", "/_test/reset", {"currency": "JPY", "minor_units": 0, "authorization_ttl_seconds": 1, "users": users})
    ta = call("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})[1]["token"]
    tb = call("POST", "/auth/login", {"email": "bob@example.com", "password": "correct horse"})[1]["token"]
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, ta, key())
    E = P(a["expires_at"])
    while dt.datetime.now(U) < E - dt.timedelta(milliseconds=60):
        time.sleep(0.002)
    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(lambda i: call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 5, "final": False}, tb, key()), range(120)))
    late = [r[1] for r in rs if r[0] == 201 and P(r[1]["created_at"]) >= E]
    if not late:
        print("attempt %d: no capture at/after expires_at (%d captures before it)" % (attempt, sum(r[0] == 201 for r in rs)))
        continue
    L = late[0]
    au = [x for x in call("GET", "/authorizations", tok=ta)[1]["authorizations"] if x["authorization_id"] == a["authorization_id"]][0]
    print("attempt %d: %d capture(s) recorded at/after expires_at" % (attempt, len(late)))
    print("  expires_at          %s" % a["expires_at"])
    print("  late capture        %s  (+%.3f ms) payment %s amount %d" % (L["created_at"], (P(L["created_at"]) - E).total_seconds() * 1000, L["payment_id"], L["amount"]))
    print("  authorization       status %s captured_amount %d closed_at %s" % (au["status"], au["captured_amount"], au.get("closed_at", "(stage 2: no closed_at)")))
    for name, at in (("as_of expires_at", E), ("as_of late capture", P(L["created_at"])), ("now", None)):
        m = call("GET", "/me" + ("?as_of=" + q(at.isoformat()) if at else ""), tok=ta)[1]
        print("  ada %-20s total %d held %d available %d" % (name, m["total"], m["held"], m["available"]))
    st = call("GET", "/statement?limit=200", tok=tb)[1]
    if isinstance(st, dict) and "entries" in st:  # stage 3 only
        print("  bob statement: capture entries after expires_at: %d" % sum(1 for e in st["entries"] if P(e["effective_at"]) >= E))
    print("RESULT: capture accepted at/after the hold's expires_at")
    sys.exit(1)
print("RESULT: no late capture in %d attempts" % N)
