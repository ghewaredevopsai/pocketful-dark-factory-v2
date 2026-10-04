"""D2/D3 regression: holds SEEDED closed on stage 2 (captured / voided / expired, incl. expired with a future deadline)
must hold nothing in any stage-3 historical view after import; a seeded open hold holds from reset.
Usage: python3 probe_s2_seeded_closed_holds.py <stage-2 base> <stage-3 base>"""
import datetime as dt, json, sys, urllib.parse, urllib.request
S2, S3 = sys.argv[1], sys.argv[2]
def call(b, m, p, body=None, tok=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(b + p, json.dumps(body).encode() if body is not None else None, h, method=m)
    with urllib.request.urlopen(r) as x:
        t = x.read(); return json.loads(t) if t else None
U = dt.timezone.utc; now = dt.datetime.now(U); H = dt.timedelta(hours=2)
users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": b} for u, b in (("ada", 1000), ("bob", 0))]
auths = [{"id": "a_cap", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 900, "status": "captured", "captured_amount": 900, "expires_at": (now + H).isoformat()},
         {"id": "a_void", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 900, "status": "voided", "expires_at": (now + H).isoformat()},
         {"id": "a_exp_past", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 900, "status": "expired", "expires_at": (now - H).isoformat()},
         {"id": "a_exp_future", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 900, "status": "expired", "expires_at": (now + H).isoformat()},
         {"id": "a_open", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 100, "status": "open", "expires_at": (now + H).isoformat()}]
call(S2, "POST", "/_test/reset", {"currency": "EUR", "minor_units": 2, "users": users, "authorizations": auths})
tok = call(S2, "POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})["token"]
created = {a["authorization_id"]: a["created_at"] for a in call(S2, "GET", "/authorizations?limit=200", tok=tok)["authorizations"]}
call(S3, "POST", "/_test/import", call(S2, "GET", "/_test/export"))
q = lambda s: urllib.parse.quote(s, safe="")
C = dt.datetime.fromisoformat(created["a_open"])
bad = 0
for name, at in (("before reset", C - dt.timedelta(seconds=1)), ("at reset", C), ("+1 h", C + dt.timedelta(hours=1)), ("+3 h (after deadlines)", C + dt.timedelta(hours=3))):
    m = call(S3, "GET", "/me?as_of=" + q(at.isoformat()), tok=tok)
    want_held = 0 if name in ("before reset", "+3 h (after deadlines)") else 100
    ok = m["held"] == want_held and m["available"] == m["total"] - m["held"] >= 0
    bad += not ok
    print(("PASS " if ok else "FAIL ") + "%-24s held %d (want %d) total %d available %d" % (name, m["held"], want_held, m["total"], m["available"]))
print("TOTAL 4 FAIL %d" % bad)
