"""Late-stamp pattern in other handlers. (1) payments and (2) new holds funded by a hold that expires mid-burst: every
accepted write must be affordable in the historical view at its own recorded instant. (3) corrections whose
effective_at is the client's 'now': every accepted correction has effective_at <= its recorded_at.
Usage: python3 probe_late_stamp_other.py <base> [rounds]"""
import concurrent.futures as cf, datetime as dt, json, sys, time, urllib.parse, urllib.request, urllib.error
B = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 5
U = dt.timezone.utc; P = dt.datetime.fromisoformat; q = lambda s: urllib.parse.quote(s, safe="")
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
K = [0]
def key():
    K[0] += 1; return "ls-%d-%d" % (time.time_ns(), K[0])
bad = 0; checks = 0
for rnd in range(N):
    for kind in ("payment", "authorization"):
        users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": b} for u, b in (("ada", 1000), ("bob", 0))]
        call("POST", "/_test/reset", {"currency": "JPY", "minor_units": 0, "authorization_ttl_seconds": 1, "users": users})
        ta = call("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})[1]["token"]
        s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 900}, ta, key())
        E = P(a["expires_at"])
        while dt.datetime.now(U) < E - dt.timedelta(milliseconds=40):
            time.sleep(0.002)
        path = "/payments" if kind == "payment" else "/authorizations"
        with cf.ThreadPoolExecutor(30) as ex:
            rs = list(ex.map(lambda i: call("POST", path, {"to_handle": "bob", "amount": 50}, ta, key()), range(60)))
        ok = [b for s, b in rs if s == 201]
        codes = sorted({(s, (b.get("error") or {}).get("code")) for s, b in rs if s != 201})
        for b in ok:
            m = call("GET", "/me?as_of=" + q(b["created_at"]), tok=ta)[1]
            checks += 1
            if m["available"] < 0 or m["total"] < 0:
                bad += 1
                print("FAIL %s %s at %s: historical total %d held %d available %d" % (kind, b.get("payment_id") or b.get("authorization_id"), b["created_at"], m["total"], m["held"], m["available"]))
        print("round %d %-13s %d accepted (%d before expires_at), others %s" % (rnd, kind, len(ok), sum(P(b["created_at"]) < E for b in ok), codes))
    # corrections with effective_at = client now
    users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": 100000} for u in ("ada", "bob")]
    call("POST", "/_test/reset", {"currency": "JPY", "minor_units": 0, "users": users})
    ta = call("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})[1]["token"]
    pays = [call("POST", "/payments", {"to_handle": "bob", "amount": 100}, ta, key())[1] for _ in range(30)]
    def corr(p):
        return call("POST", "/payments/%s/corrections" % p["payment_id"], {"expected_revision": 1, "amount": 90, "reason": "r",
                    "effective_at": dt.datetime.now(U).isoformat()}, ta, key())
    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(corr, pays))
    for s, b in rs:
        if s == 201:
            checks += 1
            if P(b["effective_at"]) > P(b["recorded_at"]):
                bad += 1; print("FAIL correction effective_at %s after recorded_at %s" % (b["effective_at"], b["recorded_at"]))
    print("round %d corrections   %s" % (rnd, sorted({(s, (b.get("error") or {}).get("code") if s != 201 else "") for s, b in rs})))
print("TOTAL %d FAIL %d" % (checks, bad))
sys.exit(1 if bad else 0)
