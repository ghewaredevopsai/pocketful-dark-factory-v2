#!/usr/bin/env python3
"""Reviewer probes for stage 2 API: holds, captures, voids, clock expiry, races, upgrade, Accept negotiation.

Usage: python3 probes2.py --base http://HOST:PORT [--stage1-base http://HOST:PORT] [--only NAME]
Each probe resets the service. Prints PASS/FAIL per check and a TOTAL line; exit 1 on any failure.
"""
import argparse
import concurrent.futures as cf
import datetime
import http.client
import json
import random
import sys
import time
import urllib.parse

BASE = None
S1 = None
RESULTS = []
K = [0]


def call(method, path, body=None, token=None, key=None, raw=None, headers=None, base=None, timeout=12):
    u = urllib.parse.urlsplit(base or BASE)
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
    ok = s == status and (code(js) in c if isinstance(c, tuple) else code(js) == c)
    check(name, ok, (s, js))


def key():
    K[0] += 1
    return "k2-%d-%d" % (time.time_ns(), K[0])


def iso_in(seconds):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)).isoformat()


def fixture(**kw):
    fx = {
        "currency": "EUR", "minor_units": 2, "authorization_ttl_seconds": 600,
        "users": [{"id": "u_%s" % h, "email": "%s@example.com" % h, "password": "correct horse",
                   "display_name": h.title(), "handle": h, "balance": b}
                  for h, b in (("ada", 10000), ("bob", 2500), ("cy", 0), ("op", 0))],
        "payments": [], "requests": [], "authorizations": [], "settlement_operator_ids": ["u_op"],
    }
    fx.update(kw)
    return fx


def reset(fx, base=None):
    s, js, _ = call("POST", "/_test/reset", fx, base=base)
    assert s == 204, (s, js)


def login(h, base=None):
    s, js, _ = call("POST", "/auth/login", {"email": h + "@example.com", "password": "correct horse"}, base=base)
    assert s == 200, (s, js)
    return js["token"]


def toks(base=None):
    return {h: login(h, base) for h in ("ada", "bob", "cy", "op")}


def me(tok):
    return call("GET", "/me", token=tok)[1]


def wallets(t):
    return {h: me(tok) for h, tok in t.items()}


def invariants(name, t, total):
    w = wallets(t)
    ok = (sum(x["total"] for x in w.values()) == total and all(x["available"] >= 0 and x["held"] >= 0 for x in w.values())
          and all(x["balance"] == x["total"] == x["available"] + x["held"] for x in w.values()))
    check(name, ok, {h: (x["total"], x["available"], x["held"]) for h, x in w.items()})
    return w


def auths(tok, q=""):
    return call("GET", "/authorizations" + q, token=tok)[1]["authorizations"]


def auth_of(tok, aid):
    return [a for a in auths(tok, "?limit=200") if a["authorization_id"] == aid][0]


# ------------------------------------------------------------------ probes

def p_seeded():
    fx = fixture()
    fx["authorizations"] = [
        {"id": "a_open", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 2000, "note": "deposit",
         "visibility": "public", "status": "open", "expires_at": iso_in(7200)},
        {"id": "a_past", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 3000, "note": "old",
         "visibility": "private", "status": "open", "expires_at": iso_in(-3600)},
        {"id": "a_cap", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 100, "note": "",
         "visibility": "public", "status": "captured", "expires_at": iso_in(3600)},
        {"id": "a_void", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 100, "note": "",
         "visibility": "public", "status": "voided", "expires_at": iso_in(3600)},
    ]
    reset(fx)
    t = toks()
    m = me(t["ada"])
    check("/me with seeded open hold", m["balance"] == 10000 and m["total"] == 10000 and m["held"] == 2000 and m["available"] == 8000, m)
    check("captured/voided seeded hold nothing", me(t["bob"])["held"] == 0)
    st = {a["authorization_id"]: a for a in auths(t["ada"])}
    check("seeded past-expiry open listed as expired", st["a_past"]["status"] == "expired" and st["a_past"]["remaining_amount"] == 0, st.get("a_past"))
    check("seeded open remaining_amount", st["a_open"]["remaining_amount"] == 2000 and st["a_open"]["captured_amount"] == 0, st["a_open"])
    check("status=open excludes expired", [a["authorization_id"] for a in auths(t["ada"], "?status=open")] == ["a_open"])
    check("status=expired lists it", [a["authorization_id"] for a in auths(t["ada"], "?status=expired")] == ["a_past"])
    check("expires_at offset preserved as instant", datetime.datetime.fromisoformat(st["a_open"]["expires_at"]).tzinfo is not None)
    check("open hold not in feed", all(p.get("authorization_id") is None for p in call("GET", "/activity", token=t["ada"])[1]["payments"]))
    err("third party sees nothing", (200, {"x": 1}, None) if auths(t["cy"]) == [] else (500, auths(t["cy"]), None), 200, None) if False else check("third party lists none", auths(t["cy"]) == [])
    err("held funds not spendable by payment", call("POST", "/payments", {"to_handle": "cy", "amount": 8001}, token=t["ada"], key=key()), 409, "insufficient_funds")
    s, _, _ = call("POST", "/payments", {"to_handle": "cy", "amount": 8000}, token=t["ada"], key=key())
    check("available exactly spendable", s == 201)
    # holds above balance -> 422, unchanged
    bad = fixture()
    bad["authorizations"] = [{"id": "a1", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 2000, "status": "open", "expires_at": iso_in(7200)},
                             {"id": "a2", "from_user_id": "u_bob", "to_user_id": "u_cy", "amount": 501, "status": "open", "expires_at": iso_in(7200)}]
    err("seeded holds > balance -> 422", call("POST", "/_test/reset", bad), 422, "validation_failed")
    check("unchanged after bad reset", me(t["ada"])["available"] == 0 and me(t["ada"])["held"] == 2000)
    ok = fixture()
    ok["authorizations"] = [{"id": "a1", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 2500, "status": "open", "expires_at": iso_in(7200)},
                            {"id": "a2", "from_user_id": "u_bob", "to_user_id": "u_cy", "amount": 99999, "status": "open", "expires_at": iso_in(-7200)}]
    s, js, _ = call("POST", "/_test/reset", ok)
    check("seeded holds == balance ok; expired ones not counted", s == 204, (s, js))
    for ttl in (0, -5, 1.5, "600", True):
        s, js, _ = call("POST", "/_test/reset", fixture(authorization_ttl_seconds=ttl))
        check("ttl %r rejected 4xx" % (ttl,), s in (400, 422), (s, js))
    fx = fixture()
    del fx["authorizations"], fx["authorization_ttl_seconds"]
    reset(fx)
    t = toks()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=key())
    d = datetime.datetime.fromisoformat(a["expires_at"]) - datetime.datetime.fromisoformat(a["created_at"])
    check("default ttl 600 when omitted", s == 201 and d.total_seconds() == 600, (s, d))


def p_authorize():
    reset(fixture(authorization_ttl_seconds=900))
    t = toks()
    k = key()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 2000, "note": "deposit", "visibility": "private"}, token=t["ada"], key=k)
    check("authorize 201 shape", s == 201 and a["status"] == "open" and a["captured_amount"] == 0 and a["remaining_amount"] == 2000
          and a["payment_id"] is None and a["payment_ids"] == [] and a["from_handle"] == "ada" and a["to_user_id"] == "u_bob"
          and a["currency"] == "EUR" and a["visibility"] == "private" and a["note"] == "deposit", (s, a))
    d = datetime.datetime.fromisoformat(a["expires_at"]) - datetime.datetime.fromisoformat(a["created_at"])
    check("expires_at = created_at + ttl", d.total_seconds() == 900, d)
    s2, a2, _ = call("POST", "/authorizations", {"amount": 2000, "visibility": "private", "note": "deposit", "to_handle": "bob"}, token=t["ada"], key=k)
    check("authorize replay 200 identical", s2 == 200 and a2 == a, s2)
    err("authorize same key diff body", call("POST", "/authorizations", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=k), 409, "idempotency_key_reuse")
    m = me(t["ada"])
    check("held 2000 once", m["held"] == 2000 and m["available"] == 8000 and m["total"] == 10000, m)
    check("authorization not in anyone's feed", all(p["amount"] != 2000 for who in t for p in call("GET", "/activity", token=t[who])[1]["payments"]))
    err("authorize > available", call("POST", "/authorizations", {"to_handle": "bob", "amount": 8001}, token=t["ada"], key=key()), 409, "insufficient_funds")
    err("authorize self", call("POST", "/authorizations", {"to_handle": "ada", "amount": 1}, token=t["ada"], key=key()), 422, "self_payment")
    err("authorize unknown", call("POST", "/authorizations", {"to_handle": "zz", "amount": 1}, token=t["ada"], key=key()), 404, "not_found")
    for b in ({"to_handle": "bob", "amount": 0}, {"to_handle": "bob", "amount": 1000000001}, {"to_handle": "bob", "amount": 1.5},
              {"to_handle": "bob", "amount": "5"}, {"to_handle": "bob", "amount": 1, "note": "x" * 201},
              {"to_handle": "bob", "amount": 1, "visibility": "secret"}, {"to_handle": "bob", "amount": 1, "note": None}, {"amount": 1}):
        err("authorize invalid %s -> 422" % json.dumps(b)[:60], call("POST", "/authorizations", b, token=t["ada"], key=key()), 422, "validation_failed")
    err("authorize missing key", call("POST", "/authorizations", {"to_handle": "bob", "amount": 1}, token=t["ada"]), 400, "missing_idempotency_key")
    err("authorize no token", call("POST", "/authorizations", {"to_handle": "bob", "amount": 1}, key=key()), 401, "unauthenticated")
    err("held not spendable by authorize", call("POST", "/authorizations", {"to_handle": "cy", "amount": 8001}, token=t["ada"], key=key()), 409, "insufficient_funds")
    s, rq, _ = call("POST", "/requests", {"payer_handle": "ada", "amount": 8001}, token=t["bob"], key=key())
    err("held not spendable by request pay", call("POST", "/requests/%s/pay" % rq["request_id"], {}, token=t["ada"], key=key()), 409, "insufficient_funds")
    err("held not spendable by settlement net debit", call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 8001}]}, token=t["op"], key=key()), 409, "insufficient_funds")
    s, _, _ = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "cy", "amount": 8001}, {"from_handle": "cy", "to_handle": "ada", "amount": 1}]}, token=t["op"], key=key())
    check("settlement net debit within available commits", s == 201, s)
    # list
    for q in ("direction=up", "status=paid", "limit=0", "limit=201", "offset=-1", "limit=1e1"):
        err("GET /authorizations?%s -> 422" % q, call("GET", "/authorizations?" + q, token=t["ada"]), 422, "validation_failed")
    call("POST", "/authorizations", {"to_handle": "ada", "amount": 1}, token=t["bob"], key=key())
    out = auths(t["ada"], "?direction=outgoing")
    inc = auths(t["ada"], "?direction=incoming")
    check("direction filters", all(a["from_user_id"] == "u_ada" for a in out) and len(inc) == 1 and inc[0]["from_user_id"] == "u_bob", (len(out), len(inc)))
    al = auths(t["ada"])
    check("newest first", [datetime.datetime.fromisoformat(a["created_at"]) for a in al] == sorted([datetime.datetime.fromisoformat(a["created_at"]) for a in al], reverse=True))
    s, js, _ = call("GET", "/authorizations?limit=1", token=t["ada"])
    check("limit/has_more", len(js["authorizations"]) == 1 and js["has_more"] is True, js)
    check("third party sees no authorizations", auths(t["cy"]) == [])


def p_capture():
    reset(fixture())
    t = toks()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 2000, "note": "dep", "visibility": "private"}, token=t["ada"], key=key())
    aid = a["authorization_id"]
    err("capture by payer 403", call("POST", "/authorizations/%s/capture" % aid, {}, token=t["ada"], key=key()), 403, "forbidden")
    err("capture by third party 403", call("POST", "/authorizations/%s/capture" % aid, {}, token=t["cy"], key=key()), 403, "forbidden")
    err("capture unknown 404", call("POST", "/authorizations/nope/capture", {}, token=t["bob"], key=key()), 404, "not_found")
    err("capture amount 0", call("POST", "/authorizations/%s/capture" % aid, {"amount": 0}, token=t["bob"], key=key()), 422, "validation_failed")
    err("capture amount 1.5", call("POST", "/authorizations/%s/capture" % aid, {"amount": 1.5}, token=t["bob"], key=key()), 422, "validation_failed")
    err("capture > remaining", call("POST", "/authorizations/%s/capture" % aid, {"amount": 2001}, token=t["bob"], key=key()), 422, "capture_exceeds_authorization")
    err("capture missing key", call("POST", "/authorizations/%s/capture" % aid, {}, token=t["bob"]), 400, "missing_idempotency_key")
    s, js, _ = call("POST", "/authorizations/%s/capture" % aid, {"final": "no"}, token=t["bob"], key=key())
    check("final non-boolean 4xx", s in (400, 422), (s, js))
    # partial, non-final
    k1 = key()
    s, p1, _ = call("POST", "/authorizations/%s/capture" % aid, {"amount": 700, "final": False}, token=t["bob"], key=k1)
    check("nonfinal capture 201 payment shape", s == 201 and p1["amount"] == 700 and p1["authorization_id"] == aid and p1["request_id"] is None
          and p1["note"] == "dep" and p1["visibility"] == "private" and p1["from_user_id"] == "u_ada" and p1["to_user_id"] == "u_bob"
          and p1.get("settlement_id") is None and p1["currency"] == "EUR", (s, p1))
    a1 = auth_of(t["ada"], aid)
    check("after nonfinal: open, captured 700, remaining 1300", a1["status"] == "open" and a1["captured_amount"] == 700 and a1["remaining_amount"] == 1300
          and a1["payment_ids"] == [p1["payment_id"]] and a1["payment_id"] == p1["payment_id"], a1)
    m = me(t["ada"])
    check("payer: total 9300 held 1300 available 8000", (m["total"], m["held"], m["available"]) == (9300, 1300, 8000), m)
    check("receiver credited", me(t["bob"])["total"] == 3200)
    s, r1, _ = call("POST", "/authorizations/%s/capture" % aid, {"amount": 700, "final": False}, token=t["bob"], key=k1)
    check("capture replay 200 identical, no move", s == 200 and r1 == p1 and me(t["bob"])["total"] == 3200, s)
    err("capture replay changed body", call("POST", "/authorizations/%s/capture" % aid, {"amount": 700}, token=t["bob"], key=k1), 409, "idempotency_key_reuse")
    err("capture > new remaining", call("POST", "/authorizations/%s/capture" % aid, {"amount": 1301, "final": False}, token=t["bob"], key=key()), 422, "capture_exceeds_authorization")
    # final partial releases remainder
    s, p2, _ = call("POST", "/authorizations/%s/capture" % aid, {"amount": 300}, token=t["bob"], key=key())
    a2 = auth_of(t["bob"], aid)
    check("final partial: captured, cumulative 1000, remaining 0, payment_ids both", s == 201 and a2["status"] == "captured" and a2["captured_amount"] == 1000
          and a2["remaining_amount"] == 0 and a2["payment_ids"] == [p1["payment_id"], p2["payment_id"]] and a2["payment_id"] == p2["payment_id"], a2)
    m = me(t["ada"])
    check("remainder released same step", (m["total"], m["held"], m["available"]) == (9000, 0, 9000), m)
    err("capture after final -> not_open", call("POST", "/authorizations/%s/capture" % aid, {}, token=t["bob"], key=key()), 409, "authorization_not_open")
    s, r1, _ = call("POST", "/authorizations/%s/capture" % aid, {"amount": 700, "final": False}, token=t["bob"], key=k1)
    check("old replay after close still 200 original", s == 200 and r1 == p1, s)
    err("void captured -> not_open", call("POST", "/authorizations/%s/void" % aid, token=t["ada"]), 409, "authorization_not_open")
    feed_cy = [p["payment_id"] for p in call("GET", "/activity", token=t["cy"])[1]["payments"]]
    feed_ada = [p["payment_id"] for p in call("GET", "/activity", token=t["ada"])[1]["payments"]]
    check("private capture hidden from third party, seen by payer", p1["payment_id"] not in feed_cy and p1["payment_id"] in feed_ada)
    # default capture: {} captures remainder
    s, b, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 500}, token=t["ada"], key=key())
    kd = key()
    s, pd, _ = call("POST", "/authorizations/%s/capture" % b["authorization_id"], {}, token=t["bob"], key=kd)
    check("{} captures full remainder", s == 201 and pd["amount"] == 500 and auth_of(t["bob"], b["authorization_id"])["status"] == "captured", (s, pd))
    err("{} vs {amount:500} reuse", call("POST", "/authorizations/%s/capture" % b["authorization_id"], {"amount": 500}, token=t["bob"], key=kd), 409, "idempotency_key_reuse")
    # final:false capturing entire remainder closes
    s, c, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 400}, token=t["ada"], key=key())
    call("POST", "/authorizations/%s/capture" % c["authorization_id"], {"amount": 400, "final": False}, token=t["bob"], key=key())
    check("final:false with full remainder closes", auth_of(t["bob"], c["authorization_id"])["status"] == "captured")
    s, c2, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 400}, token=t["ada"], key=key())
    s, pc, _ = call("POST", "/authorizations/%s/capture" % c2["authorization_id"], raw=b"", token=t["bob"], key=key())
    check("empty capture body captures remainder", s in (201, 400) and (s == 400 or pc["amount"] == 400), (s, pc))
    check("payments without authorization carry authorization_id null",
          all(("authorization_id" in p) for p in call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"]))
    s, pp, _ = call("POST", "/payments", {"to_handle": "cy", "amount": 1}, token=t["ada"], key=key())
    check("direct payment authorization_id null", pp.get("authorization_id", "MISSING") is None, pp)
    invariants("sum conserved after captures", t, 12500)


def p_void():
    reset(fixture())
    t = toks()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key())
    aid = a["authorization_id"]
    err("void by receiver 403", call("POST", "/authorizations/%s/void" % aid, token=t["bob"]), 403, "forbidden")
    err("void by third 403", call("POST", "/authorizations/%s/void" % aid, token=t["cy"]), 403, "forbidden")
    err("void unknown 404", call("POST", "/authorizations/nope/void", token=t["ada"]), 404, "not_found")
    call("POST", "/authorizations/%s/capture" % aid, {"amount": 250, "final": False}, token=t["bob"], key=key())
    s, v, _ = call("POST", "/authorizations/%s/void" % aid, token=t["ada"])
    check("void partial: voided, captured kept, remaining 0", s == 200 and v["status"] == "voided" and v["captured_amount"] == 250 and v["remaining_amount"] == 0 and len(v["payment_ids"]) == 1, (s, v))
    m = me(t["ada"])
    check("void releases only remainder", (m["total"], m["held"], m["available"]) == (9750, 0, 9750), m)
    s, v2, _ = call("POST", "/authorizations/%s/void" % aid, token=t["ada"])
    check("void twice 200, no double release", s == 200 and v2["status"] == "voided" and me(t["ada"])["held"] == 0 and me(t["ada"])["available"] == 9750, (s, v2))
    err("capture voided -> not_open", call("POST", "/authorizations/%s/capture" % aid, {}, token=t["bob"], key=key()), 409, "authorization_not_open")
    invariants("sum conserved after void", t, 12500)


def p_expiry():
    reset(fixture(authorization_ttl_seconds=2))
    t = toks()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 3000}, token=t["ada"], key=key())
    s, b, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key())
    kc = key()
    s, pc, _ = call("POST", "/authorizations/%s/capture" % b["authorization_id"], {"amount": 400, "final": False}, token=t["bob"], key=kc)
    s, c, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 500}, token=t["ada"], key=key())
    call("POST", "/authorizations/%s/void" % c["authorization_id"], token=t["ada"])
    check("before expiry: held 3600", me(t["ada"])["held"] == 3600, me(t["ada"]))
    err("expired-capture-precheck: still open now", (200, None, None) if auth_of(t["ada"], a["authorization_id"])["status"] == "open" else (0, None, None), 200, None) if False else None
    exp = datetime.datetime.fromisoformat(a["expires_at"])
    while datetime.datetime.now(datetime.timezone.utc) <= exp + datetime.timedelta(milliseconds=150):
        time.sleep(0.05)
    # first read after the deadline, no write in between
    m = me(t["ada"])
    check("after expiry /me releases remainders (total 9600, held 0, available 9600)", (m["total"], m["held"], m["available"]) == (9600, 0, 9600), m)
    st = {x["authorization_id"]: x for x in auths(t["bob"])}
    check("listed expired with remaining 0", st[a["authorization_id"]]["status"] == "expired" and st[a["authorization_id"]]["remaining_amount"] == 0, st[a["authorization_id"]])
    check("partially captured hold expired keeps capture records", st[b["authorization_id"]]["status"] == "expired" and st[b["authorization_id"]]["captured_amount"] == 400
          and st[b["authorization_id"]]["payment_ids"] == [pc["payment_id"]], st[b["authorization_id"]])
    check("voided stays voided after deadline", st[c["authorization_id"]]["status"] == "voided")
    check("status=open empty after expiry", auths(t["bob"], "?status=open") == [])
    check("status=expired has both", sorted(x["authorization_id"] for x in auths(t["bob"], "?status=expired")) == sorted([a["authorization_id"], b["authorization_id"]]))
    err("capture expired -> authorization_expired", call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key()), 409, ("authorization_expired",))
    err("void expired -> not_open", call("POST", "/authorizations/%s/void" % a["authorization_id"], token=t["ada"]), 409, "authorization_not_open")
    s, r, _ = call("POST", "/authorizations/%s/capture" % b["authorization_id"], {"amount": 400, "final": False}, token=t["bob"], key=kc)
    check("capture replay after expiry 200 original", s == 200 and r == pc, s)
    m = me(t["ada"])
    check("no double release after expiry + capture/void attempts", (m["held"], m["available"]) == (0, 9600), m)
    s, _, _ = call("POST", "/payments", {"to_handle": "cy", "amount": 9600}, token=t["ada"], key=key())
    check("released funds spendable", s == 201)
    invariants("sum conserved after expiry", t, 12500)
    # expiry visible through export too
    reset(fixture(authorization_ttl_seconds=1))
    t = toks()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, token=t["ada"], key=key())
    time.sleep(1.3)
    s, exp_js, _ = call("GET", "/_test/export")
    s, _, _ = call("POST", "/_test/import", exp_js)
    check("after export/import of expired hold: held 0", me(t["ada"])["held"] == 0 and auth_of(t["ada"], a["authorization_id"])["status"] == "expired")


def p_expiry_after_import():
    reset(fixture(authorization_ttl_seconds=3))
    t = toks()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 777}, token=t["ada"], key=key())
    s, exp_js, _ = call("GET", "/_test/export")
    reset(fixture())
    s, _, _ = call("POST", "/_test/import", exp_js)
    check("import restores open hold", me(t["ada"])["held"] == 777 and auth_of(t["ada"], a["authorization_id"])["status"] == "open")
    deadline = datetime.datetime.fromisoformat(a["expires_at"])
    while datetime.datetime.now(datetime.timezone.utc) <= deadline + datetime.timedelta(milliseconds=150):
        time.sleep(0.05)
    check("imported hold expires by the clock", me(t["ada"])["held"] == 0 and auth_of(t["ada"], a["authorization_id"])["status"] == "expired")


def p_export_import():
    reset(fixture())
    t = toks()
    ka, kc = key(), key()
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=ka)
    s, p, _ = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 100, "final": False}, token=t["bob"], key=kc)
    s, exp, _ = call("GET", "/_test/export")
    before = wallets(t)
    call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key())
    s, _, _ = call("POST", "/_test/import", exp)
    check("import 204", s == 204)
    check("wallets restored incl. held", wallets(t) == before, wallets(t))
    s, r, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=ka)
    check("authorize replay after import 200 original", s == 200 and r == a, s)
    s, r, _ = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 100, "final": False}, token=t["bob"], key=kc)
    check("capture replay after import 200 original, no move", s == 200 and r == p and me(t["bob"])["total"] == before["bob"]["total"], s)
    s, pf, _ = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key())
    check("capture remainder after import", s == 201 and pf["amount"] == 900, (s, pf))
    s, x, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=key())
    ids = [y["authorization_id"] for y in auths(t["ada"], "?limit=200")]
    check("new authorization id unique after import", len(ids) == len(set(ids)))
    bad = json.loads(json.dumps(exp))
    bad["state"]["authorizations"][0]["captured_amount"] = 5000
    err("import invalid authorization state -> 422", call("POST", "/_test/import", bad), 422, "validation_failed")
    invariants("sum conserved export/import", t, 12500)


def p_upgrade():
    if not S1:
        return
    fx = fixture()
    for k in ("authorizations", "authorization_ttl_seconds"):
        del fx[k]
    fx["requests"] = [{"id": "rq_1", "requester_id": "u_bob", "payer_id": "u_ada", "amount": 1200, "note": "taxi", "status": "pending"}]
    reset(fx, base=S1)
    t = toks(base=S1)
    kp = key()
    s, p, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 300}, token=t["ada"], key=kp, base=S1)
    kf = key()
    call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["cy"], key=kf, base=S1)  # 409
    s, exp, _ = call("GET", "/_test/export", base=S1)
    reset(fixture())
    s, js, _ = call("POST", "/_test/import", exp)
    check("stage-1 export imports into stage-2", s == 204, (s, js))
    m = me(t["ada"])
    check("old token valid, held 0, total/available = balance", m.get("balance") == 9700 and m["total"] == 9700 and m["available"] == 9700 and m["held"] == 0, m)
    s, r, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 300}, token=t["ada"], key=kp)
    check("stage-1 receipt replays 200 identical after upgrade", s == 200 and {k: v for k, v in r.items() if k != "authorization_id"} == p, (s, r))
    s, r, _ = call("POST", "/requests/rq_1/pay", {}, token=t["ada"], key=key())
    check("imported pending request payable", s == 201, (s, r))
    call("POST", "/payments", {"to_handle": "cy", "amount": 5}, token=t["ada"], key=key())
    s, r, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["cy"], key=kf)
    check("stage-1 failed key reusable after upgrade", s == 201, (s, r))
    check("stage-1 password login after upgrade", login("bob") is not None)
    s, a, _ = call("POST", "/authorizations", {"to_handle": "bob", "amount": 10}, token=t["ada"], key=key())
    d = datetime.datetime.fromisoformat(a["expires_at"]) - datetime.datetime.fromisoformat(a["created_at"])
    check("upgraded service default ttl 600", s == 201 and d.total_seconds() == 600, (s, a))


def p_races():
    users = [{"id": "u%d" % i, "email": "u%d@example.com" % i, "password": "correct horse", "display_name": "U", "handle": "u%d" % i, "balance": 1000} for i in range(6)]
    users.append({"id": "u_op", "email": "op@example.com", "password": "correct horse", "display_name": "Op", "handle": "op", "balance": 0})
    reset({"currency": "JPY", "minor_units": 0, "users": users, "settlement_operator_ids": ["u_op"], "authorization_ttl_seconds": 600})
    t = {u["handle"]: login(u["handle"]) for u in users}
    total = 6000

    def inv(name):
        w = [me(t["u%d" % i]) for i in range(6)]
        ok = sum(x["total"] for x in w) == total and all(x["available"] >= 0 and x["held"] >= 0 and x["total"] == x["available"] + x["held"] for x in w)
        check(name, ok, [(x["total"], x["available"], x["held"]) for x in w])

    # 1) identical capture retries
    s, a, _ = call("POST", "/authorizations", {"to_handle": "u1", "amount": 600}, token=t["u0"], key=key())
    k = key()
    with cf.ThreadPoolExecutor(40) as ex:
        rs = list(ex.map(lambda _: call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 50, "final": False}, token=t["u1"], key=k), range(40)))
    st = sorted(r[0] for r in rs)
    check("40 identical capture retries: one 201, 39 x 200 same body", st.count(201) == 1 and st.count(200) == 39 and len({json.dumps(r[1], sort_keys=True) for r in rs}) == 1, st)
    check("captured once", auth_of(t["u1"], a["authorization_id"])["captured_amount"] == 50)
    # 2) distinct-key nonfinal captures of 100 racing: remaining 550 -> exactly 5 succeed, never exceed
    with cf.ThreadPoolExecutor(40) as ex:
        rs = list(ex.map(lambda _: call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 100, "final": False}, token=t["u1"], key=key()), range(40)))
    st = [r[0] for r in rs]
    ax = auth_of(t["u1"], a["authorization_id"])
    check("racing captures: 5 x 201, rest 422, cumulative 550 <= 600", st.count(201) == 5 and st.count(422) == 35 and ax["captured_amount"] == 550 and ax["remaining_amount"] == 50, (sorted(st), ax["captured_amount"]))
    # 3) capture vs void vs payer spending vs final capture
    s, b, _ = call("POST", "/authorizations", {"to_handle": "u3", "amount": 400}, token=t["u2"], key=key())
    bid = b["authorization_id"]
    ops = ([("cap", None)] * 15 + [("capf", None)] * 5 + [("void", None)] * 5 + [("pay", None)] * 15 + [("auth", None)] * 10)
    random.Random(3).shuffle(ops)

    def run(op):
        kind = op[0]
        if kind == "cap":
            return kind, call("POST", "/authorizations/%s/capture" % bid, {"amount": 30, "final": False}, token=t["u3"], key=key())
        if kind == "capf":
            return kind, call("POST", "/authorizations/%s/capture" % bid, {"amount": 30}, token=t["u3"], key=key())
        if kind == "void":
            return kind, call("POST", "/authorizations/%s/void" % bid, token=t["u2"])
        if kind == "pay":
            return kind, call("POST", "/payments", {"to_handle": "u4", "amount": 60}, token=t["u2"], key=key())
        return kind, call("POST", "/authorizations", {"to_handle": "u5", "amount": 70}, token=t["u2"], key=key())

    with cf.ThreadPoolExecutor(50) as ex:
        rs = list(ex.map(run, ops))
    check("mixed hold race: no 5xx", all(r[1][0] < 500 for r in rs), sorted({r[1][0] for r in rs}))
    bx = auth_of(t["u2"], bid)
    caps = sum(r[1][1]["amount"] for r in rs if r[0] in ("cap", "capf") and r[1][0] == 201)
    check("hold closed, captured_amount equals sum of 201 captures, <= 400", bx["status"] in ("captured", "voided") and bx["captured_amount"] == caps <= 400, (bx["status"], bx["captured_amount"], caps))
    inv("mixed hold race: sum conserved, available >= 0, total = available + held")
    # 4) capture vs clock expiry at the deadline
    reset({"currency": "JPY", "minor_units": 0, "users": users, "settlement_operator_ids": ["u_op"], "authorization_ttl_seconds": 2})
    t.update({u["handle"]: login(u["handle"]) for u in users})
    s, c, _ = call("POST", "/authorizations", {"to_handle": "u1", "amount": 1000}, token=t["u0"], key=key())
    deadline = datetime.datetime.fromisoformat(c["expires_at"])
    while datetime.datetime.now(datetime.timezone.utc) < deadline - datetime.timedelta(milliseconds=120):
        time.sleep(0.01)

    def capx(_):
        r = call("POST", "/authorizations/%s/capture" % c["authorization_id"], {"amount": 10, "final": False}, token=t["u1"], key=key())
        return r, datetime.datetime.now(datetime.timezone.utc)

    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(capx, range(120)))
    st = [r[0][0] for r in rs]
    cx = auth_of(t["u0"], c["authorization_id"])
    n201 = st.count(201)
    late = [r for r in rs if r[0][0] == 201 and datetime.datetime.fromisoformat(r[0][1]["created_at"]) >= deadline]
    check("capture/expiry race: only 201 or 409 authorization_expired", set(st) <= {201, 409} and all(code(r[0][1]) == "authorization_expired" for r in rs if r[0][0] == 409), sorted(set(st)))
    check("no capture created at/after expires_at", not late, [r[0][1]["created_at"] for r in late][:3])
    w0, w1 = me(t["u0"]), me(t["u1"])
    check("after race: captured %d x 10, held 0, released exactly once" % n201, cx["status"] == "expired" and cx["captured_amount"] == 10 * n201
          and w0["held"] == 0 and w0["total"] == 1000 - 10 * n201 and w1["total"] == 1000 + 10 * n201, (cx["status"], cx["captured_amount"], w0, w1["total"]))


def p_html():
    reset(fixture())
    t = toks()
    for path in ("/", "/split", "/signup", "/login"):
        s, js, h = call("GET", path, headers={"Accept": "text/html"})
        check("GET %s html" % path, s == 200 and "text/html" in h.get("Content-Type", ""), (s, h.get("Content-Type")))
    for path in ("/requests", "/authorizations"):
        s, js, h = call("GET", path, headers={"Accept": "text/html,application/xhtml+xml"})
        check("GET %s Accept html -> html" % path, s == 200 and "text/html" in h.get("Content-Type", ""), (s, h.get("Content-Type")))
        s, js, h = call("GET", path, token=t["ada"])
        check("GET %s no Accept -> JSON" % path, s == 200 and "application/json" in h.get("Content-Type", "") and isinstance(js, dict), (s, h.get("Content-Type")))
        s, js, h = call("GET", path, token=t["ada"], headers={"Accept": "application/json"})
        check("GET %s Accept json -> JSON" % path, s == 200 and isinstance(js, dict))
        err("GET %s no token JSON 401" % path, call("GET", path), 401, "unauthenticated")


PROBES = [p_seeded, p_authorize, p_capture, p_void, p_expiry, p_expiry_after_import, p_export_import, p_upgrade, p_races, p_html]


def main():
    global BASE, S1
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--stage1-base", default=None)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    BASE, S1 = a.base, a.stage1_base
    for p in PROBES:
        if a.only and a.only not in p.__name__:
            continue
        try:
            p()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(p.__name__ + " crashed", False, repr(e))
    fails = [n for n, ok in RESULTS if not ok]
    print("TOTAL %d PASS %d FAIL %d" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
