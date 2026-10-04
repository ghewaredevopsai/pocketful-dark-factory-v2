"""Where does the product place a seeded open hold's creation, relative to the reset request window?
Usage: python3 probe_seeded_hold_instant.py <base>"""
import datetime as dt, json, sys, urllib.parse, urllib.request
B = sys.argv[1]
def call(m, p, body=None, tok=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(B + p, json.dumps(body).encode() if body is not None else None, h, method=m)
    with urllib.request.urlopen(r) as x:
        t = x.read(); return json.loads(t) if t else None
U = dt.timezone.utc
now = dt.datetime.now(U)
users = [{"id": "u_%s" % h, "email": "%s@example.com" % h, "password": "pw-%s-xyz" % h, "display_name": h, "handle": h, "balance": 1000} for h in ("ada", "bob", "cy", "dee", "eve", "fay")]
fx = {"currency": "EUR", "minor_units": 2, "users": users,
      "authorizations": [{"id": "a_s0", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 16, "status": "open", "expires_at": (now + dt.timedelta(hours=2)).isoformat()}]}
t0 = dt.datetime.now(U); call("POST", "/_test/reset", fx); t1 = dt.datetime.now(U)
tok = call("POST", "/auth/login", {"email": "ada@example.com", "password": "pw-ada-xyz"})["token"]
a = call("GET", "/authorizations", tok=tok)["authorizations"][0]
C = dt.datetime.fromisoformat(a["created_at"])
print("reset request window %s .. %s (%.0f ms); seeded hold created_at %s (%.1f ms after send)" % (t0.isoformat(), t1.isoformat(), (t1 - t0).total_seconds() * 1000, a["created_at"], (C - t0).total_seconds() * 1000))
q = lambda s: urllib.parse.quote(s, safe="")
for name, asof, known in (("as_of=C known=C", C, C), ("as_of=C known=C-1us", C, C - dt.timedelta(microseconds=1)), ("as_of=reset end known=reset end", t1, t1), ("as_of=reset start", t0, None)):
    path = "/me?as_of=" + q(asof.isoformat()) + ("&known_at=" + q(known.isoformat()) if known else "")
    m = call("GET", path, tok=tok)
    print("%-32s held=%d available=%d" % (name, m["held"], m["available"]))
