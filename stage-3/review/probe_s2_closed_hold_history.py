"""Historical held for holds created through the stage-2 API that were already closed (final capture / clock expiry /
void) when the stage-2 export was taken, after import into stage 3.
Usage: python3 probe_s2_closed_hold_history.py <stage-2 base> <stage-3 base>"""
import datetime as dt, json, sys, time, urllib.parse, urllib.request
S2, S3 = sys.argv[1], sys.argv[2]
def call(b, m, p, body=None, tok=None, key=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    if key: h["Idempotency-Key"] = key
    r = urllib.request.Request(b + p, json.dumps(body).encode() if body is not None else None, h, method=m)
    with urllib.request.urlopen(r) as x:
        t = x.read(); return json.loads(t) if t else None
users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": 10000} for u in ("ada", "bob")]
call(S2, "POST", "/_test/reset", {"currency": "EUR", "minor_units": 2, "authorization_ttl_seconds": 2, "users": users})
ta = call(S2, "POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})["token"]
tb = call(S2, "POST", "/auth/login", {"email": "bob@example.com", "password": "correct horse"})["token"]
cap = call(S2, "POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, ta, "k1")
p = call(S2, "POST", "/authorizations/%s/capture" % cap["authorization_id"], {"amount": 300}, tb, "k2")   # final: releases 700
vd = call(S2, "POST", "/authorizations", {"to_handle": "bob", "amount": 400}, ta, "k5")
pv = call(S2, "POST", "/authorizations/%s/capture" % vd["authorization_id"], {"amount": 100, "final": False}, tb, "k6")
call(S2, "POST", "/authorizations/%s/void" % vd["authorization_id"], tok=ta)                         # voided after a nonfinal capture
exp = call(S2, "POST", "/authorizations", {"to_handle": "bob", "amount": 500}, ta, "k3")
time.sleep(2.3)                                                                                          # expires by the clock
export = call(S2, "GET", "/_test/export")
call(S3, "POST", "/_test/import", export)
q = lambda s: urllib.parse.quote(s, safe="")
P = lambda s: dt.datetime.fromisoformat(s)
US = dt.timedelta(microseconds=1)
def view(at):
    m = call(S3, "GET", "/me?as_of=" + q(at.isoformat()), tok=ta); return (m["total"], m["held"], m["available"])
C1, X, CV, XV = P(cap["created_at"]), P(p["created_at"]), P(vd["created_at"]), P(pv["created_at"])
C2, E = P(exp["created_at"]), P(exp["expires_at"])
rows = [("final-captured hold: between creation and capture", C1 + (X - C1) / 2, (10000, 1000, 9000)),
        ("after final capture, before the voided hold", X + (CV - X) / 2, (9700, 0, 9700)),
        ("voided hold: between creation and its nonfinal capture", CV + (XV - CV) / 2, (9700, 400, 9300)),
        ("voided hold: at its capture (latest known event) closed", XV, (9600, 0, 9600)),
        ("expired hold: before its deadline", C2 + (E - C2) / 2, (9600, 500, 9100)),
        ("expired hold: at expires_at released", E, (9600, 0, 9600))]
bad = 0
for name, at, want in rows:
    got = view(at)
    print(("PASS " if got == want else "FAIL ") + "%-55s want (total, held, available) %s got %s" % (name, want, got))
    bad += got != want
print("TOTAL %d FAIL %d" % (len(rows), bad))
