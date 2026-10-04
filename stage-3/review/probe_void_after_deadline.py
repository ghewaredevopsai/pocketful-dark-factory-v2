"""Void vs clock expiry: can a void close a hold (status voided) at a closed_at at or after its expires_at?
Usage: python3 probe_void_after_deadline.py <base> [attempts]"""
import datetime as dt, json, sys, time, urllib.request, urllib.error
B = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 40
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
users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": 100000} for u in ("ada", "bob")]
call("POST", "/_test/reset", {"currency": "JPY", "minor_units": 0, "authorization_ttl_seconds": 1, "users": users})
ta = call("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})[1]["token"]
hits = 0
for i in range(N):
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 10}, ta, "v%d-%d" % (time.time_ns(), i))
    E = P(a["expires_at"])
    target = E - dt.timedelta(microseconds=300 + (i % 10) * 60)
    while dt.datetime.now(U) < target:
        pass
    s, v = call("POST", "/authorizations/%s/void" % a["authorization_id"], tok=ta)
    if s == 200 and P(v["closed_at"]) >= E:
        hits += 1
        print("hold %s: status %s closed_at %s >= expires_at %s (+%.3f ms)" % (a["authorization_id"], v["status"], v["closed_at"], a["expires_at"], (P(v["closed_at"]) - E).total_seconds() * 1000))
print("RESULT: %d of %d voids closed at/after expires_at" % (hits, N))
sys.exit(1 if hits else 0)
