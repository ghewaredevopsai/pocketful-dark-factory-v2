#!/usr/bin/env python3
"""Reviewer probes for stage 1 (stdlib only).

Usage: python3 probes.py --base http://127.0.0.1:8080 [--only NAME]
Each probe resets the service itself. Prints one line per check and a final PASS/FAIL count; exit 1 on any fail.
"""
import argparse
import concurrent.futures as cf
import datetime
import http.client
import json
import sys
import time
import urllib.parse

BASE = None
RESULTS = []


def call(method, path, body=None, token=None, key=None, raw=None, headers=None, timeout=12):
    u = urllib.parse.urlsplit(BASE)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if key is not None:
        h["Idempotency-Key"] = key
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    txt = r.read()
    c.close()
    try:
        js = json.loads(txt) if txt else None
    except ValueError:
        js = txt
    return r.status, js, dict(r.getheaders())


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -- " + str(detail)[:400]))


def code(js):
    return js.get("error", {}).get("code") if isinstance(js, dict) else None


def err(name, resp, status, c):
    s, js, _ = resp
    check(name, s == status and code(js) == c, (s, js))


def fixture(**kw):
    fx = {
        "currency": "EUR", "minor_units": 2,
        "users": [
            {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada",
             "handle": "ada", "balance": 10000},
            {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob",
             "handle": "bob", "balance": 2500},
            {"id": "u_cy", "email": "cy@example.com", "password": "correct horse", "display_name": "Cy",
             "handle": "cy", "balance": 0},
            {"id": "u_op", "email": "op@example.com", "password": "correct horse", "display_name": "Op",
             "handle": "op", "balance": 0},
        ],
        "payments": [{"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500,
                      "note": "coffee", "visibility": "public"}],
        "requests": [{"id": "rq_1", "requester_id": "u_bob", "payer_id": "u_ada", "amount": 1200,
                      "note": "taxi", "status": "pending"}],
        "settlement_operator_ids": ["u_op"],
    }
    fx.update(kw)
    return fx


def reset(fx=None):
    s, js, _ = call("POST", "/_test/reset", fx or fixture())
    assert s == 204, (s, js)


def login(email):
    s, js, _ = call("POST", "/auth/login", {"email": email, "password": "correct horse"})
    assert s == 200, (s, js)
    return js["token"]


def toks():
    return {h: login(h + "@example.com") for h in ("ada", "bob", "cy", "op")}


def balances(t):
    return {h: call("GET", "/me", token=tok)[1]["balance"] for h, tok in t.items()}


K = [0]


def key():
    K[0] += 1
    return "k-%d-%d" % (time.time_ns(), K[0])


# ------------------------------------------------------------------ probes

def p_health_reset():
    s, js, h = call("GET", "/health")
    check("health 200 ok", s == 200 and js == {"status": "ok"}, (s, js))
    check("content-type json utf-8", "application/json" in h.get("Content-Type", "") and "utf-8" in h.get("Content-Type", "").lower(), h)
    reset()
    t = toks()
    check("seeded balances", balances(t) == {"ada": 10000, "bob": 2500, "cy": 0, "op": 0})
    s, js, _ = call("GET", "/me", token=t["ada"])
    check("/me shape", js == {"user_id": "u_ada", "display_name": "Ada", "handle": "ada", "balance": 10000,
                              "currency": "EUR", "minor_units": 2}, js)
    # negative balance fixture -> 422 and state unchanged
    fx = fixture()
    fx["users"][0]["balance"] = -1
    err("reset negative balance 422", call("POST", "/_test/reset", fx), 422, "validation_failed")
    check("state unchanged after bad reset", call("GET", "/me", token=t["ada"])[1].get("balance") == 10000)
    err("reset invalid json 400", call("POST", "/_test/reset", raw=b"{bad"), 400, "malformed_request")
    for mu, cur in ((0, "JPY"), (3, "BHD")):
        reset(fixture(currency=cur, minor_units=mu))
        tok = login("ada@example.com")
        js = call("GET", "/me", token=tok)[1]
        check("minor_units %d currency %s" % (mu, cur), js["minor_units"] == mu and js["currency"] == cur, js)
    fx = fixture(minor_units=1)
    err("minor_units 1 -> 422", call("POST", "/_test/reset", fx), 422, "validation_failed")
    # repeated reset drops new users and tokens
    reset()
    s, js, _ = call("POST", "/auth/signup", {"email": "zed@example.com", "password": "12345678", "display_name": "Z"})
    ztok = js["token"]
    reset()
    err("token gone after reset", call("GET", "/me", token=ztok), 401, "unauthenticated")


def p_reset_many_passwords():
    users = [{"id": "u%d" % i, "email": "u%d@example.com" % i, "password": "password-%d" % i,
              "display_name": "U", "handle": "u%d" % i, "balance": 1} for i in range(100)]
    t0 = time.time()
    s, js, _ = call("POST", "/_test/reset", {"currency": "EUR", "minor_units": 2, "users": users}, timeout=30)
    dt = time.time() - t0
    check("reset 100 users distinct passwords 204 within 10 s (took %.2fs)" % dt, s == 204 and dt < 10, (s, dt))


def p_auth():
    reset()
    s, js, _ = call("POST", "/auth/signup", {"email": "Dan.Smith+x@Example.com", "password": "12345678", "display_name": "Dan"})
    check("signup 201", s == 201 and set(js) >= {"user_id", "display_name", "token"}, (s, js))
    me = call("GET", "/me", token=js["token"])[1]
    check("derived handle dan_smith_x", me["handle"] == "dan_smith_x" and me["balance"] == 0, me)
    s, js2, _ = call("POST", "/auth/signup", {"email": "abcdefghijklmnopqrstuvwxyz@x.io", "password": "12345678", "display_name": "L"})
    check("derived handle truncated to 20", call("GET", "/me", token=js2["token"])[1]["handle"] == "abcdefghijklmnopqrst")
    err("email taken", call("POST", "/auth/signup", {"email": "ada@example.com", "password": "12345678", "display_name": "x"}), 409, "email_taken")
    err("handle taken", call("POST", "/auth/signup", {"email": "ada@other.com", "password": "12345678", "display_name": "x"}), 409, "handle_taken")
    s, js, _ = call("POST", "/auth/login", {"email": "ada@other.com", "password": "12345678"})
    check("handle_taken created no account", s == 401, (s, js))
    err("short password", call("POST", "/auth/signup", {"email": "e@x.io", "password": "1234567", "display_name": "x"}), 422, "validation_failed")
    err("bad email", call("POST", "/auth/signup", {"email": "nope", "password": "12345678", "display_name": "x"}), 422, "validation_failed")
    err("wrong password", call("POST", "/auth/login", {"email": "ada@example.com", "password": "wrong pass"}), 401, "unauthenticated")
    err("unknown email", call("POST", "/auth/login", {"email": "who@example.com", "password": "correct horse"}), 401, "unauthenticated")
    err("password wrong type 400", call("POST", "/auth/signup", {"email": "e@x.io", "password": 12345678, "display_name": "x"}), 400, "malformed_request")
    err("signup bad json 400", call("POST", "/auth/signup", raw=b"{"), 400, "malformed_request")
    t1, t2 = login("ada@example.com"), login("ada@example.com")
    check("two tokens both valid", call("GET", "/me", token=t1)[0] == 200 and call("GET", "/me", token=t2)[0] == 200)
    err("no token", call("GET", "/me"), 401, "unauthenticated")
    err("bad scheme", call("GET", "/me", headers={"Authorization": "Basic abc"}), 401, "unauthenticated")
    err("unknown token", call("GET", "/me", token="nope"), 401, "unauthenticated")
    err("payments no token", call("POST", "/payments", {"to_handle": "bob", "amount": 1}, key="x"), 401, "unauthenticated")
    # new user receives immediately and can be asked
    s, js, _ = call("POST", "/auth/signup", {"email": "neo@example.com", "password": "12345678", "display_name": "Neo"})
    s1 = call("POST", "/payments", {"to_handle": "neo", "amount": 5}, token=t1, key=key())[0]
    s2 = call("POST", "/requests", {"payer_handle": "neo", "amount": 5}, token=t1, key=key())[0]
    check("new user can receive and be asked", s1 == 201 and s2 == 201, (s1, s2))
    # export must not hold plaintext password
    exp = json.dumps(call("GET", "/_test/export")[1])
    check("no plaintext password in export", "correct horse" not in exp and "12345678" not in exp)


def p_payments():
    reset()
    t = toks()
    k = key()
    s, js, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1500, "note": "dinner \U0001F37D é\u0000 <b>", "visibility": "private"}, token=t["ada"], key=k)
    check("payment 201 shape", s == 201 and js["from_handle"] == "ada" and js["to_user_id"] == "u_bob" and js["amount"] == 1500
          and js["currency"] == "EUR" and js["visibility"] == "private" and js["request_id"] is None, (s, js))
    check("note verbatim", js["note"] == "dinner \U0001F37D é\u0000 <b>", js.get("note"))
    ts = datetime.datetime.fromisoformat(js["created_at"])
    check("created_at has offset", ts.tzinfo is not None and len(js["payment_id"]) <= 64, js["created_at"])
    check("balances moved", balances(t) == {"ada": 8500, "bob": 4000, "cy": 0, "op": 0})
    s, js2, _ = call("POST", "/payments", {"visibility": "private", "amount": 1500, "to_handle": "bob", "note": "dinner \U0001F37D é\u0000 <b>"}, token=t["ada"], key=k)
    check("replay 200 identical (key order differs)", s == 200 and js2 == js, (s, js2))
    s, js3, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1.5e3, "note": "dinner \U0001F37D é\u0000 <b>", "visibility": "private"}, token=t["ada"], key=k)
    check("replay with 1500.0 is same value", s == 200, (s, js3))
    err("same key different body", call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=k), 409, "idempotency_key_reuse")
    err("same key invalid body -> reuse", call("POST", "/payments", {"to_handle": "bob", "amount": -5}, token=t["ada"], key=k), 409, "idempotency_key_reuse")
    check("no double move", balances(t)["ada"] == 8500)
    s, js, _ = call("POST", "/payments", {"to_handle": "ada", "amount": 1}, token=t["bob"], key=k)
    check("same key other user independent", s == 201, (s, js))
    s, js, _ = call("POST", "/requests", {"payer_handle": "bob", "amount": 1}, token=t["ada"], key=k)
    check("same key other path is first use", s == 201, (s, js))
    for a in (1e3, 1000.0):
        s, js, _ = call("POST", "/payments", {"to_handle": "cy", "amount": a}, token=t["ada"], key=key())
        check("amount %r accepted as 1000" % a, s == 201 and js["amount"] == 1000 and isinstance(js["amount"], int), (s, js))
    s, js, _ = call("POST", "/payments", raw=b'{"to_handle":"cy","amount":1E1}', token=t["ada"], key=key())
    check("amount 1E1 accepted", s == 201 and js["amount"] == 10, (s, js))
    for bad_amt in (0, -1, 1000000001, 1.5, "10", True, None, [1], {"a": 1}):
        err("amount %r -> 422" % (bad_amt,), call("POST", "/payments", {"to_handle": "cy", "amount": bad_amt}, token=t["ada"], key=key()), 422, "validation_failed")
    err("amount missing -> 422", call("POST", "/payments", {"to_handle": "cy"}, token=t["ada"], key=key()), 422, "validation_failed")
    err("to_handle missing -> 422", call("POST", "/payments", {"amount": 1}, token=t["ada"], key=key()), 422, "validation_failed")
    err("to_handle wrong type -> 400", call("POST", "/payments", {"to_handle": 5, "amount": 1}, token=t["ada"], key=key()), 400, "malformed_request")
    err("note null -> 422", call("POST", "/payments", {"to_handle": "cy", "amount": 1, "note": None}, token=t["ada"], key=key()), 422, "validation_failed")
    err("note 201 chars -> 422", call("POST", "/payments", {"to_handle": "cy", "amount": 1, "note": "x" * 201}, token=t["ada"], key=key()), 422, "validation_failed")
    s, js, _ = call("POST", "/payments", {"to_handle": "cy", "amount": 1, "note": "é" * 200}, token=t["ada"], key=key())
    check("note 200 non-ascii chars ok", s == 201, (s, js))
    for v in ("PUBLIC", None, 1, ""):
        err("visibility %r -> 422" % (v,), call("POST", "/payments", {"to_handle": "cy", "amount": 1, "visibility": v}, token=t["ada"], key=key()), 422, "validation_failed")
    err("self payment", call("POST", "/payments", {"to_handle": "ada", "amount": 1}, token=t["ada"], key=key()), 422, "self_payment")
    err("unknown handle", call("POST", "/payments", {"to_handle": "nobody", "amount": 1}, token=t["ada"], key=key()), 404, "not_found")
    before = balances(t)
    err("insufficient", call("POST", "/payments", {"to_handle": "bob", "amount": before["cy"] + 1}, token=t["cy"], key=key()), 409, "insufficient_funds")
    check("failed payment no trace", balances(t) == before)
    err("missing key", call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"]), 400, "missing_idempotency_key")
    err("empty key", call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=""), 400, "missing_idempotency_key")
    err("key 256 chars", call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key="k" * 256), 422, "validation_failed")
    s, js, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key="k" * 255)
    check("key 255 chars ok", s == 201, (s, js))
    err("bad json", call("POST", "/payments", raw=b'{"to_handle":', token=t["ada"], key=key()), 400, "malformed_request")
    err("array body", call("POST", "/payments", raw=b'[1]', token=t["ada"], key=key()), 400, "malformed_request")
    s, js, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1, "extra": {"x": [1]}}, token=t["ada"], key=key())
    check("unknown field ignored", s == 201, (s, js))
    # failed key is reusable
    k2 = key()
    want = balances(t)["cy"] + 5
    err("fail first", call("POST", "/payments", {"to_handle": "bob", "amount": want}, token=t["cy"], key=k2), 409, "insufficient_funds")
    call("POST", "/payments", {"to_handle": "cy", "amount": 5}, token=t["ada"], key=key())
    s, js, _ = call("POST", "/payments", {"to_handle": "bob", "amount": want}, token=t["cy"], key=k2)
    check("key reusable after 4xx (later succeeds)", s == 201, (s, js))
    check("sum conserved", sum(balances(t).values()) == 12500, balances(t))


def p_overflow():
    big = 2 ** 53 - 10
    fx = fixture()
    fx["users"][1]["balance"] = big
    fx["users"][0]["balance"] = 10 ** 9
    fx["payments"] = []
    fx["requests"] = []
    reset(fx)
    t = toks()
    s, js, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 10}, token=t["ada"], key=key())
    check("credit to exactly 2^53 ok", s == 201, (s, js))
    s, js, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=key())
    check("credit beyond 2^53 rejected 4xx, no 5xx", 400 <= s < 500, (s, js))
    b = balances(t)
    check("2^53 balance exact", b["bob"] == 2 ** 53 and b["ada"] == 10 ** 9 - 10, b)


def p_requests():
    reset()
    t = toks()
    k = key()
    s, rq, _ = call("POST", "/requests", {"payer_handle": "cy", "amount": 700, "note": "taxi"}, token=t["bob"], key=k)
    check("request 201 shape, exceeds balance ok", s == 201 and rq["status"] == "pending" and rq["payer_id"] == "u_cy"
          and rq["requester_handle"] == "bob" and rq["payment_id"] is None and rq["currency"] == "EUR", (s, rq))
    err("self request", call("POST", "/requests", {"payer_handle": "bob", "amount": 1}, token=t["bob"], key=key()), 422, "self_request")
    err("request unknown handle", call("POST", "/requests", {"payer_handle": "zz", "amount": 1}, token=t["bob"], key=key()), 404, "not_found")
    err("request amount 0", call("POST", "/requests", {"payer_handle": "cy", "amount": 0}, token=t["bob"], key=key()), 422, "validation_failed")
    err("request note long", call("POST", "/requests", {"payer_handle": "cy", "amount": 1, "note": "n" * 201}, token=t["bob"], key=key()), 422, "validation_failed")
    rid = rq["request_id"]
    err("pay by requester 403", call("POST", "/requests/%s/pay" % rid, {}, token=t["bob"], key=key()), 403, "forbidden")
    err("pay by third party 403", call("POST", "/requests/%s/pay" % rid, {}, token=t["ada"], key=key()), 403, "forbidden")
    err("pay unknown 404", call("POST", "/requests/nope/pay", {}, token=t["cy"], key=key()), 404, "not_found")
    kp = key()
    err("pay while short 409", call("POST", "/requests/%s/pay" % rid, {"visibility": "private"}, token=t["cy"], key=kp), 409, "insufficient_funds")
    check("request still pending", [r for r in call("GET", "/requests", token=t["cy"])[1]["requests"] if r["request_id"] == rid][0]["status"] == "pending")
    call("POST", "/payments", {"to_handle": "cy", "amount": 700}, token=t["ada"], key=key())
    s, pay, _ = call("POST", "/requests/%s/pay" % rid, {"visibility": "private"}, token=t["cy"], key=kp)
    check("pay after funds arrive 201 (same key reused)", s == 201 and pay["request_id"] == rid and pay["visibility"] == "private"
          and pay["from_user_id"] == "u_cy" and pay["to_user_id"] == "u_bob" and pay["amount"] == 700 and pay["note"] == "taxi", (s, pay))
    s, pay2, _ = call("POST", "/requests/%s/pay" % rid, {"visibility": "private"}, token=t["cy"], key=kp)
    check("pay replay 200 same body", s == 200 and pay2 == pay, (s, pay2))
    err("pay replay {} vs visibility body -> reuse", call("POST", "/requests/%s/pay" % rid, {}, token=t["cy"], key=kp), 409, "idempotency_key_reuse")
    err("pay again new key -> not pending", call("POST", "/requests/%s/pay" % rid, {}, token=t["cy"], key=key()), 409, "request_not_pending")
    lst = call("GET", "/requests", token=t["bob"])[1]["requests"]
    r = [x for x in lst if x["request_id"] == rid][0]
    check("request paid carries payment_id", r["status"] == "paid" and r["payment_id"] == pay["payment_id"], r)
    check("balances after pay", balances(t) == {"ada": 9300, "bob": 3200, "cy": 0, "op": 0}, balances(t))
    err("decline paid 409", call("POST", "/requests/%s/decline" % rid, token=t["cy"]), 409, "request_not_pending")
    err("cancel paid 409", call("POST", "/requests/%s/cancel" % rid, token=t["bob"]), 409, "request_not_pending")
    # decline / cancel
    s, r2, _ = call("POST", "/requests", {"payer_handle": "ada", "amount": 5}, token=t["bob"], key=key())
    r2id = r2["request_id"]
    err("decline by requester 403", call("POST", "/requests/%s/decline" % r2id, token=t["bob"]), 403, "forbidden")
    s, js, _ = call("POST", "/requests/%s/decline" % r2id, token=t["ada"])
    check("decline 200", s == 200 and js["status"] == "declined", (s, js))
    s, js, _ = call("POST", "/requests/%s/decline" % r2id, token=t["ada"])
    check("decline twice 200", s == 200 and js["status"] == "declined", (s, js))
    err("cancel declined 409", call("POST", "/requests/%s/cancel" % r2id, token=t["bob"]), 409, "request_not_pending")
    err("pay declined 409", call("POST", "/requests/%s/pay" % r2id, {}, token=t["ada"], key=key()), 409, "request_not_pending")
    s, r3, _ = call("POST", "/requests", {"payer_handle": "ada", "amount": 5}, token=t["bob"], key=key())
    r3id = r3["request_id"]
    err("cancel by payer 403", call("POST", "/requests/%s/cancel" % r3id, token=t["ada"]), 403, "forbidden")
    s, js, _ = call("POST", "/requests/%s/cancel" % r3id, token=t["bob"])
    check("cancel 200", s == 200 and js["status"] == "cancelled", (s, js))
    check("cancel twice 200", call("POST", "/requests/%s/cancel" % r3id, token=t["bob"])[0] == 200)
    err("decline cancelled 409", call("POST", "/requests/%s/decline" % r3id, token=t["ada"]), 409, "request_not_pending")
    err("decline unknown 404", call("POST", "/requests/nope/decline", token=t["ada"]), 404, "not_found")
    # replay of pay after the request changed is still the original
    s, r4, _ = call("POST", "/requests", {"payer_handle": "ada", "amount": 5}, token=t["bob"], key=key())
    k4 = key()
    s, p4, _ = call("POST", "/requests/%s/pay" % r4["request_id"], raw=b"", token=t["ada"], key=k4)
    check("pay with empty body 201 public", s == 201 and p4["visibility"] == "public", (s, p4))
    # listing
    s, js, _ = call("GET", "/requests", token=t["op"])
    check("operator sees no foreign requests", s == 200 and js["requests"] == [] and js["has_more"] is False, js)
    inc = call("GET", "/requests?direction=incoming", token=t["ada"])[1]["requests"]
    out = call("GET", "/requests?direction=outgoing", token=t["ada"])[1]["requests"]
    check("direction filters", all(r["payer_id"] == "u_ada" for r in inc) and all(r["requester_id"] == "u_ada" for r in out) and len(inc) >= 4, (len(inc), len(out)))
    allr = call("GET", "/requests", token=t["ada"])[1]["requests"]
    check("newest first", [r["created_at"] for r in allr] == sorted([r["created_at"] for r in allr], reverse=True) or
          [datetime.datetime.fromisoformat(r["created_at"]) for r in allr] == sorted([datetime.datetime.fromisoformat(r["created_at"]) for r in allr], reverse=True))
    st = call("GET", "/requests?status=declined", token=t["ada"])[1]["requests"]
    check("status filter", [r["request_id"] for r in st] == [r2id], st)
    for q in ("direction=sideways", "status=open", "limit=0", "limit=201", "limit=1e1", "limit=4.0", "limit=+4", "offset=-1", "limit=", "offset=x"):
        err("GET /requests?%s -> 422" % q, call("GET", "/requests?" + q, token=t["ada"]), 422, "validation_failed")
    s, js, _ = call("GET", "/requests?limit=2&offset=0&bogus=1", token=t["ada"])
    check("limit 2 has_more", s == 200 and len(js["requests"]) == 2 and js["has_more"] is True, js)
    n = len(allr)
    s, js, _ = call("GET", "/requests?limit=%d&offset=0" % n, token=t["ada"])
    check("has_more false at exact end", js["has_more"] is False and len(js["requests"]) == n, js)
    s, js, _ = call("GET", "/requests?offset=1000", token=t["ada"])
    check("offset beyond end empty", js == {"requests": [], "has_more": False}, js)


def p_feed():
    reset()
    t = toks()
    call("POST", "/payments", {"to_handle": "bob", "amount": 1, "visibility": "private", "note": "secret"}, token=t["ada"], key=key())
    call("POST", "/payments", {"to_handle": "cy", "amount": 2}, token=t["ada"], key=key())
    s, rq, _ = call("POST", "/requests", {"payer_handle": "bob", "amount": 3, "note": "req"}, token=t["cy"], key=key())
    for who, see_private in (("ada", True), ("bob", True), ("cy", False), ("op", False)):
        items = call("GET", "/activity", token=t[who])[1]["payments"]
        notes = [p["note"] for p in items]
        check("feed %s private=%s" % (who, see_private), ("secret" in notes) == see_private and "req" not in notes and "coffee" in notes, notes)
    priv = [p for p in call("GET", "/activity", token=t["bob"])[1]["payments"] if p["note"] == "secret"][0]
    check("private seen as private by receiver", priv["visibility"] == "private")
    items = call("GET", "/activity", token=t["ada"])[1]["payments"]
    check("feed newest first", [p["note"] for p in items][:3] == ["", "secret", "coffee"], [p["note"] for p in items])
    check("feed has settlement_id null", all(p.get("settlement_id", "MISSING") is None for p in items), items)
    for q in ("limit=0", "limit=201", "offset=-1", "limit=1e2"):
        err("activity %s -> 422" % q, call("GET", "/activity?" + q, token=t["ada"]), 422, "validation_failed")
    s, js, _ = call("GET", "/activity?limit=1&offset=1", token=t["ada"])
    check("activity paging", s == 200 and len(js["payments"]) == 1 and js["payments"][0]["note"] == "secret" and js["has_more"] is True, js)
    # paging through same-second burst gives no dup/gap
    for i in range(30):
        call("POST", "/payments", {"to_handle": "bob", "amount": 1, "note": str(i)}, token=t["ada"], key=key())
    seen = []
    off = 0
    while True:
        js = call("GET", "/activity?limit=7&offset=%d" % off, token=t["cy"])[1]
        seen += [p["payment_id"] for p in js["payments"]]
        off += 7
        if not js["has_more"]:
            break
    check("paging no dup/gap", len(seen) == len(set(seen)) == 32, len(seen))


def p_splits():
    reset()
    t = toks()
    for amt, n, exp in ((1000, 3, [334, 333, 333]), (1, 3, [1, 0, 0]), (10, 3, [4, 3, 3]), (999, 3, [333, 333, 333])):
        hs = ["ada", "bob", "cy"][:n]
        s, js, _ = call("POST", "/splits", {"amount": amt, "participant_handles": hs, "note": "d"}, token=t["ada"], key=key())
        check("split %d/%d shares" % (amt, n), s == 201 and [x["amount"] for x in js["shares"]] == exp
              and [x["handle"] for x in js["shares"]] == hs and [r["amount"] for r in js["requests"]] == exp[1:]
              and [r["payer_handle"] for r in js["requests"]] == hs[1:] and all(r["requester_id"] == "u_ada" and r["status"] == "pending" for r in js["requests"]), (s, js))
    s, js, _ = call("POST", "/splits", {"amount": 5, "participant_handles": ["ada", "bob", "cy", "op", "zed"][:4] + []}, token=t["ada"], key=key())
    check("split 5/4", [x["amount"] for x in js["shares"]] == [2, 1, 1, 1], js)
    s, js, _ = call("POST", "/splits", {"amount": 1, "participant_handles": ["cy", "bob", "ada"]}, token=t["ada"], key=key())
    check("order changes extra unit; zero share request created", [x["amount"] for x in js["shares"]] == [1, 0, 0]
          and [r["amount"] for r in js["requests"]] == [1, 0], js)
    zero = [r for r in js["requests"] if r["amount"] == 0][0]
    s, js0, _ = call("POST", "/requests/%s/pay" % zero["request_id"], {}, token=t["bob"], key=key())
    check("paying a zero-share request ok", s == 201 and js0["amount"] == 0, (s, js0))
    s, js, _ = call("POST", "/splits", {"amount": 10, "participant_handles": ["bob", "cy"]}, token=t["ada"], key=key())
    check("caller omitted -> requests for all", [x["amount"] for x in js["shares"]] == [5, 5] and len(js["requests"]) == 2, js)
    s, js, _ = call("POST", "/splits", {"amount": 10, "participant_handles": ["ada"]}, token=t["ada"], key=key())
    check("caller-only split valid", s == 201 and js["requests"] == [] and js["shares"] == [{"handle": "ada", "amount": 10}], (s, js))
    check("split not in feed", all(p["amount"] not in (334,) for p in call("GET", "/activity", token=t["ada"])[1]["payments"]))
    err("split empty", call("POST", "/splits", {"amount": 10, "participant_handles": []}, token=t["ada"], key=key()), 422, "validation_failed")
    err("split dup", call("POST", "/splits", {"amount": 10, "participant_handles": ["bob", "bob"]}, token=t["ada"], key=key()), 422, "validation_failed")
    err("split unknown", call("POST", "/splits", {"amount": 10, "participant_handles": ["bob", "zz"]}, token=t["ada"], key=key()), 404, "not_found")
    err("split amount 0", call("POST", "/splits", {"amount": 0, "participant_handles": ["bob"]}, token=t["ada"], key=key()), 422, "validation_failed")
    err("split note long", call("POST", "/splits", {"amount": 1, "participant_handles": ["bob"], "note": "x" * 201}, token=t["ada"], key=key()), 422, "validation_failed")
    err("split missing handles", call("POST", "/splits", {"amount": 1}, token=t["ada"], key=key()), 422, "validation_failed")
    k = key()
    s, a, _ = call("POST", "/splits", {"amount": 7, "participant_handles": ["bob", "cy"]}, token=t["ada"], key=k)
    s2, b, _ = call("POST", "/splits", {"amount": 7, "participant_handles": ["bob", "cy"]}, token=t["ada"], key=k)
    check("split replay 200 identical, no new requests", s == 201 and s2 == 200 and a == b, (s, s2))
    # pay all split requests; conservation
    for who in ("bob", "cy"):
        for r in call("GET", "/requests?direction=incoming&status=pending&limit=200", token=t[who])[1]["requests"]:
            call("POST", "/requests/%s/pay" % r["request_id"], {}, token=t[who], key=key())
    check("sum conserved after paying splits", sum(balances(t).values()) == 12500, balances(t))


def p_settlements():
    reset()
    t = toks()
    body = {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 100},
                          {"from_handle": "bob", "to_handle": "cy", "amount": 50, "visibility": "private", "note": "n"}]}
    err("settlement no token 401", call("POST", "/settlements", body, key=key()), 401, "unauthenticated")
    err("settlement non-operator 403", call("POST", "/settlements", body, token=t["ada"], key=key()), 403, "forbidden")
    err("settlement missing key 400", call("POST", "/settlements", body, token=t["op"]), 400, "missing_idempotency_key")
    k = key()
    s, st, _ = call("POST", "/settlements", body, token=t["op"], key=k)
    check("settlement 201", s == 201 and len(st["payments"]) == 2 and all(p["settlement_id"] == st["settlement_id"] and p["created_at"] == st["committed_at"] and p["request_id"] is None for p in st["payments"])
          and st["payments"][0]["from_handle"] == "ada" and st["payments"][1]["visibility"] == "private" and st["payments"][0]["note"] == "", (s, st))
    check("settlement balances", balances(t) == {"ada": 9900, "bob": 2550, "cy": 50, "op": 0}, balances(t))
    s, st2, _ = call("POST", "/settlements", body, token=t["op"], key=k)
    check("settlement replay 200 identical", s == 200 and st2 == st, s)
    check("no double settlement", balances(t)["ada"] == 9900)
    feed_op = [p["note"] for p in call("GET", "/activity", token=t["op"])[1]["payments"]]
    check("operator does not see private member", "n" not in feed_op, feed_op)
    feed_cy = call("GET", "/activity", token=t["cy"])[1]["payments"]
    check("receiver sees private member", any(p.get("settlement_id") == st["settlement_id"] and p["visibility"] == "private" for p in feed_cy))
    # net affordability: cy has 50, chain cy->ada 60 funded by bob->cy 20 in same batch
    s, js, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "cy", "to_handle": "ada", "amount": 60},
                                                         {"from_handle": "bob", "to_handle": "cy", "amount": 20}]}, token=t["op"], key=key())
    check("net-affordable batch commits", s == 201, (s, js))
    before = balances(t)
    kf = key()
    err("unaffordable 409", call("POST", "/settlements", {"transfers": [{"from_handle": "cy", "to_handle": "ada", "amount": 11},
                                                                        {"from_handle": "ada", "to_handle": "bob", "amount": 1}]}, token=t["op"], key=kf), 409, "insufficient_funds")
    check("unaffordable changes nothing", balances(t) == before)
    s, js, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]}, token=t["op"], key=kf)
    check("failed settlement key reusable", s == 201, (s, js))
    before = balances(t)
    nfeed = len(call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"])
    cases = [
        ("first entry error wins (404 before later 422)", [{"from_handle": "ada", "to_handle": "zz", "amount": 1}, {"from_handle": "ada", "to_handle": "ada", "amount": 1}], 404, "not_found"),
        ("first entry error wins (422 self before later 404)", [{"from_handle": "ada", "to_handle": "ada", "amount": 1}, {"from_handle": "ada", "to_handle": "zz", "amount": 1}], 422, "self_payment"),
        ("entry error before funds", [{"from_handle": "cy", "to_handle": "ada", "amount": 10 ** 9}, {"from_handle": "ada", "to_handle": "bob", "amount": 0}], 422, "validation_failed"),
        ("empty transfers", [], 422, "validation_failed"),
        ("33 transfers", [{"from_handle": "ada", "to_handle": "bob", "amount": 1}] * 33, 422, "validation_failed"),
        ("bad visibility", [{"from_handle": "ada", "to_handle": "bob", "amount": 1, "visibility": "x"}], 422, "validation_failed"),
        ("note too long", [{"from_handle": "ada", "to_handle": "bob", "amount": 1, "note": "x" * 201}], 422, "validation_failed"),
        ("entry not object", ["x"], 422, "validation_failed"),
    ]
    for name, tr, stc, c in cases:
        err("settlement " + name, call("POST", "/settlements", {"transfers": tr}, token=t["op"], key=key()), stc, c)
    err("settlement transfers missing", call("POST", "/settlements", {}, token=t["op"], key=key()), 422, "validation_failed")
    s, js, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}] * 32}, token=t["op"], key=key())
    check("32 transfers ok", s == 201 and len(js["payments"]) == 32, s)
    after = balances(t)
    check("failed settlements left no trace", after["ada"] == before["ada"] - 32 and len(call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"]) == nfeed + 32)
    check("sum conserved", sum(after.values()) == 12500, after)
    # operator ordinary permissions unchanged
    err("operator cannot pay others' request", call("POST", "/requests/rq_1/pay", {}, token=t["op"], key=key()), 403, "forbidden")


def p_seeded_settlement_id_collision():
    fx = fixture()
    fx["payments"] = [{"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500, "note": "old",
                       "visibility": "public", "settlement_id": "st_%d" % i} for i in range(1, 2)]
    fx["payments"] = [dict(fx["payments"][0], id="p_%d" % i, settlement_id="st_%d" % i) for i in range(1, 61)]
    reset(fx)
    t = toks()
    s, js, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]}, token=t["op"], key=key())
    seeded_sids = {"st_%d" % i for i in range(1, 61)}
    check("new settlement_id does not collide with a seeded settlement_id", s == 201 and js["settlement_id"] not in seeded_sids, js.get("settlement_id") if isinstance(js, dict) else js)
    s, js2, _ = call("POST", "/splits", {"amount": 3, "participant_handles": ["bob"]}, token=t["ada"], key=key())
    check("ids <= 64 chars", all(len(x) <= 64 for x in (js["settlement_id"], js2["split_id"])))


def p_seeded_readable():
    fx = fixture()
    fx["payments"].append({"id": "p_priv", "from_user_id": "u_bob", "to_user_id": "u_cy", "amount": 1, "note": "hidden",
                           "visibility": "private", "created_at": "2026-09-24T19:00:00+02:00"})
    fx["requests"].append({"id": "rq_paid", "requester_id": "u_ada", "payer_id": "u_bob", "amount": 1, "note": "",
                           "status": "paid", "payment_id": "p_1"})
    reset(fx)
    t = toks()
    ada = call("GET", "/activity", token=t["ada"])[1]["payments"]
    check("seeded private hidden from third party", "p_priv" not in [p["payment_id"] for p in ada])
    cy = call("GET", "/activity", token=t["cy"])[1]["payments"]
    pp = [p for p in cy if p["payment_id"] == "p_priv"]
    check("seeded private visible to receiver with offset ts", pp and datetime.datetime.fromisoformat(pp[0]["created_at"]) == datetime.datetime.fromisoformat("2026-09-24T19:00:00+02:00"), pp)
    rq = call("GET", "/requests", token=t["bob"])[1]["requests"]
    ids = {r["request_id"]: r for r in rq}
    check("seeded requests readable with statuses", ids.get("rq_1", {}).get("status") == "pending" and ids.get("rq_paid", {}).get("status") == "paid", rq)
    err("pay seeded paid -> not pending", call("POST", "/requests/rq_paid/pay", {}, token=t["bob"], key=key()), 409, "request_not_pending")
    s, js, _ = call("POST", "/requests/rq_1/pay", {}, token=t["ada"], key=key())
    check("pay seeded pending request", s == 201 and js["request_id"] == "rq_1" and js["amount"] == 1200, (s, js))
    check("balances not replayed", balances(t) == {"ada": 8800, "bob": 3700, "cy": 0, "op": 0}, balances(t))


def p_export_import():
    reset()
    t = toks()
    k = key()
    s, p1, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 10}, token=t["ada"], key=k)
    kf = key()
    call("POST", "/payments", {"to_handle": "bob", "amount": 10 ** 6}, token=t["cy"], key=kf)  # fails, 409
    ks = key()
    s, st1, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 5}]}, token=t["op"], key=ks)
    s, exp, _ = call("GET", "/_test/export")
    check("export shape", s == 200 and exp["track"] == "pocketful" and exp["format_version"] == 1 and isinstance(exp["state"], dict), exp.get("track") if isinstance(exp, dict) else exp)
    bal = balances(t)
    act = call("GET", "/activity", token=t["ada"])[1]
    # mutate after export, then import
    call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=key())
    s, exp2, _ = call("GET", "/_test/export")
    check("export snapshot not changed by later writes", exp != exp2)
    s, _, _ = call("POST", "/_test/import", exp)
    check("import 204", s == 204, s)
    check("balances restored, old tokens valid", balances(t) == bal, balances(t))
    check("activity identical after import", call("GET", "/activity", token=t["ada"])[1] == act)
    s, r, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 10}, token=t["ada"], key=k)
    check("receipt replay after import 200 identical", s == 200 and r == p1, (s, r))
    s, r, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 5}]}, token=t["op"], key=ks)
    check("settlement replay after import", s == 200 and r == st1, s)
    err("operator kept after import", call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 5}]}, token=t["ada"], key=key()), 403, "forbidden")
    call("POST", "/payments", {"to_handle": "cy", "amount": 10 ** 6}, token=t["ada"], key=key()) if False else None
    s, r, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["cy"], key=kf)
    check("failed key reusable after import", s == 201, (s, r))
    check("login works after import", login("bob@example.com") is not None)
    s, _, _ = call("POST", "/_test/import", exp)
    s, _, _ = call("POST", "/_test/import", exp)
    check("repeat import no duplication", len(call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"]) == len(act["payments"]) and balances(t) == bal)
    # new ids after import don't collide
    s, pn, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=key())
    ids = [p["payment_id"] for p in call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"]]
    check("new id after import unique", len(ids) == len(set(ids)), ids)
    before = balances(t)
    for name, body, stc, c in (("wrong track", dict(exp, track="tablekeeper"), 422, "validation_failed"),
                               ("wrong version", dict(exp, format_version=2), 422, "validation_failed"),
                               ("missing state", {"track": "pocketful", "format_version": 1}, 422, "validation_failed"),
                               ("state garbage", dict(exp, state={"x": 1}), 422, "validation_failed"),
                               ("state not object", dict(exp, state=[1]), 422, "validation_failed")):
        err("import " + name, call("POST", "/_test/import", body), stc, c)
    err("import bad json", call("POST", "/_test/import", raw=b"{nope"), 400, "malformed_request")
    check("failed imports changed nothing", balances(t) == before)
    # import removes previous credentials
    s, js, _ = call("POST", "/auth/signup", {"email": "late@example.com", "password": "12345678", "display_name": "L"})
    late = js["token"]
    call("POST", "/_test/import", exp)
    err("post-export token gone after import", call("GET", "/me", token=late), 401, "unauthenticated")
    reset()
    err("old tokens gone after reset", call("GET", "/me", token=t["ada"]), 401, "unauthenticated")


def p_concurrency():
    users = [{"id": "u%d" % i, "email": "u%d@example.com" % i, "password": "correct horse", "display_name": "U",
              "handle": "u%d" % i, "balance": 1000} for i in range(10)]
    users.append({"id": "u_op", "email": "op@example.com", "password": "correct horse", "display_name": "Op", "handle": "op", "balance": 0})
    reset({"currency": "JPY", "minor_units": 0, "users": users, "settlement_operator_ids": ["u_op"]})
    t = {u["handle"]: login(u["email"]) for u in users}
    total = 10000
    # 1) 50 identical retries
    k = key()
    with cf.ThreadPoolExecutor(50) as ex:
        rs = list(ex.map(lambda _: call("POST", "/payments", {"to_handle": "u1", "amount": 7}, token=t["u0"], key=k), range(50)))
    st = sorted(r[0] for r in rs)
    check("50 identical retries: one 201, rest 200 same body", st.count(201) == 1 and st.count(200) == 49 and len({json.dumps(r[1], sort_keys=True) for r in rs}) == 1, st)
    check("moved once", call("GET", "/me", token=t["u0"])[1]["balance"] == 993)
    # 2) drain: 50 concurrent distinct payments of 100 from u2 (balance 1000) -> exactly 10 succeed
    with cf.ThreadPoolExecutor(50) as ex:
        rs = list(ex.map(lambda i: call("POST", "/payments", {"to_handle": "u%d" % (3 + i % 7), "amount": 100}, token=t["u2"], key=key()), range(50)))
    st = [r[0] for r in rs]
    check("drain: exactly 10 x 201, 40 x 409, no 5xx", st.count(201) == 10 and st.count(409) == 40, sorted(st))
    check("drained to 0", call("GET", "/me", token=t["u2"])[1]["balance"] == 0)
    # 3) concurrent pay of the same request via different keys -> money moves once
    s, rq, _ = call("POST", "/requests", {"payer_handle": "u4", "amount": 50}, token=t["u5"], key=key())
    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(lambda i: call("POST", "/requests/%s/pay" % rq["request_id"], {}, token=t["u4"], key=key()), range(30)))
    st = [r[0] for r in rs]
    check("concurrent pay distinct keys: one 201, rest 409 request_not_pending", st.count(201) == 1 and st.count(409) == 29, sorted(st))
    # 4) mixed random burst + global conservation + no negatives
    import random
    rnd = random.Random(7)
    ops = []
    for i in range(400):
        a, b = rnd.sample(range(10), 2)
        kind = rnd.random()
        if kind < 0.6:
            ops.append(("POST", "/payments", {"to_handle": "u%d" % b, "amount": rnd.randint(1, 400)}, "u%d" % a))
        elif kind < 0.8:
            ops.append(("SETTLE", "/settlements", {"transfers": [{"from_handle": "u%d" % a, "to_handle": "u%d" % b, "amount": rnd.randint(1, 300)},
                                                                 {"from_handle": "u%d" % b, "to_handle": "u%d" % rnd.choice([x for x in range(10) if x != b]), "amount": rnd.randint(1, 300)}]}, "op"))
        else:
            ops.append(("GET", "/activity?limit=200", None, "u%d" % a))

    def run(op):
        m, path, body, who = op
        if m == "GET":
            return call("GET", path, token=t[who])
        return call("POST", path, body, token=t[who], key=key())

    with cf.ThreadPoolExecutor(50) as ex:
        rs = list(ex.map(run, ops))
    st = [r[0] for r in rs]
    check("mixed burst: no 5xx", all(s < 500 for s in st), sorted(set(st)))
    bals = [call("GET", "/me", token=t["u%d" % i])[1]["balance"] for i in range(10)]
    check("mixed burst: sum conserved and none negative", sum(bals) == total and min(bals) >= 0, bals)
    # replay-check: activity sum from exported state consistent
    acts = call("GET", "/activity?limit=200", token=t["u0"])[1]
    check("feed readable after burst", isinstance(acts.get("payments"), list))
    # concurrent signups with same email -> exactly one 201
    with cf.ThreadPoolExecutor(20) as ex:
        rs = list(ex.map(lambda i: call("POST", "/auth/signup", {"email": "race@example.com", "password": "12345678", "display_name": "R"}), range(20)))
    st = [r[0] for r in rs]
    check("concurrent same-email signup: one 201", st.count(201) == 1 and st.count(409) == 19, sorted(st))
    # latency under 50 concurrent logins
    t0 = time.time()
    with cf.ThreadPoolExecutor(50) as ex:
        rs = list(ex.map(lambda i: call("POST", "/auth/login", {"email": "u%d@example.com" % (i % 10), "password": "correct horse"}), range(50)))
    dt = time.time() - t0
    check("50 concurrent logins all 200 in < 5 s (%.2fs)" % dt, all(r[0] == 200 for r in rs) and dt < 5)


def p_http_edges():
    reset()
    t = toks()
    s, js, _ = call("GET", "/nope", token=t["ada"])
    check("unknown path 4xx json error", 400 <= s < 500 and code(js), (s, js))
    s, js, _ = call("POST", "/payments", raw=b'{"to_handle":"bob","amount":1e400}', token=t["ada"], key=key())
    check("huge exponent amount 422 no 5xx", s == 422, (s, js))
    s, js, _ = call("POST", "/payments", raw=b'{"to_handle":"bob","amount":NaN}', token=t["ada"], key=key())
    check("NaN amount 4xx", s in (400, 422), (s, js))
    s, js, _ = call("POST", "/payments", raw=b'\xff\xfe', token=t["ada"], key=key())
    check("non-utf8 body 400", s == 400, (s, js))
    s, js, _ = call("POST", "/payments", raw=("[" * 100000).encode(), token=t["ada"], key=key())
    check("deep nesting 400 no 5xx", s == 400, (s, js))


PROBES = [p_health_reset, p_auth, p_payments, p_overflow, p_requests, p_feed, p_splits, p_settlements,
          p_seeded_settlement_id_collision, p_seeded_readable, p_export_import, p_concurrency, p_http_edges,
          p_reset_many_passwords]


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    BASE = a.base
    for p in PROBES:
        if a.only and a.only not in p.__name__:
            continue
        try:
            p()
        except Exception as e:  # a crashed probe is a failure
            check(p.__name__ + " crashed", False, repr(e))
    fails = [n for n, ok in RESULTS if not ok]
    print("TOTAL %d PASS %d FAIL %d" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
