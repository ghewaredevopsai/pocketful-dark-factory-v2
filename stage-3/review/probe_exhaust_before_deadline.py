"""Adjudication of the probes2 p_races section-4 dispute: 120 nonfinal captures of 10 on a 1000 hold can exhaust it
before the deadline; then the remaining 20 are correctly 409 authorization_not_open and the hold ends 'captured'.
Usage: python3 probe_exhaust_before_deadline.py <base>"""
import concurrent.futures as cf, datetime as dt, json, sys, time, urllib.request, urllib.error
B = sys.argv[1]
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
users = [{"id": "u%d" % i, "email": "u%d@example.com" % i, "password": "correct horse", "display_name": "U", "handle": "u%d" % i, "balance": 1000} for i in range(2)]
call("POST", "/_test/reset", {"currency": "JPY", "minor_units": 0, "users": users, "authorization_ttl_seconds": 2})
t0 = call("POST", "/auth/login", {"email": "u0@example.com", "password": "correct horse"})[1]["token"]
t1 = call("POST", "/auth/login", {"email": "u1@example.com", "password": "correct horse"})[1]["token"]
s, a = call("POST", "/authorizations", {"to_handle": "u1", "amount": 1000}, t0, "k-a")
E = dt.datetime.fromisoformat(a["expires_at"])
with cf.ThreadPoolExecutor(30) as ex:   # start at once, well before the 2 s deadline
    rs = list(ex.map(lambda i: call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 10, "final": False}, t1, "k-%d" % i), range(120)))
codes = {}
for s, b in rs:
    k = "%d %s" % (s, (b.get("error") or {}).get("code", "")) if isinstance(b, dict) else str(s)
    codes[k] = codes.get(k, 0) + 1
au = [x for x in call("GET", "/authorizations", tok=t0)[1]["authorizations"] if x["authorization_id"] == a["authorization_id"]][0]
late = [b for s, b in rs if s == 201 and dt.datetime.fromisoformat(b["created_at"]) >= E]
print("responses:", codes)
print("authorization: status %s captured_amount %d remaining %d closed_at %s (expires_at %s)" % (au["status"], au["captured_amount"], au["remaining_amount"], au["closed_at"], a["expires_at"]))
print("captures at/after expires_at:", len(late))
ok = codes.get("201 ", 0) == 100 and codes.get("409 authorization_not_open", 0) == 20 and au["status"] == "captured" and au["captured_amount"] == 1000 and not late
print("RESULT:", "correct behaviour (exhausted before deadline)" if ok else "UNEXPECTED")
sys.exit(0 if ok else 1)
