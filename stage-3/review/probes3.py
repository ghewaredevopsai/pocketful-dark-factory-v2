#!/usr/bin/env python3
"""Reviewer probes for stage 3: as_of / known_at views, statements, snapshots, corrections, revisions,
historical holds, import chains.

Usage: python3 probes3.py --base URL [--stage1-base URL] [--stage2-base URL] [--only NAME]
Each probe resets the service. Prints PASS/FAIL per check and a TOTAL line; exit 1 on any failure.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import http.client
import json
import sys
import time
import urllib.parse

BASE = S1 = S2 = None
RESULTS = []
K = [0]
UTC = dt.timezone.utc


def call(method, path, body=None, token=None, key=None, raw=None, base=None, timeout=12):
    u = urllib.parse.urlsplit(base or BASE)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if key is not None:
        h["Idempotency-Key"] = key
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    txt = r.read()
    c.close()
    try:
        js = json.loads(txt) if txt else None
    except ValueError:
        js = txt
    return r.status, js


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -- " + str(detail)[:500]))
    sys.stdout.flush()


def code(js):
    return js.get("error", {}).get("code") if isinstance(js, dict) else None


def err(name, resp, status, c):
    s, js = resp
    check(name, s == status and code(js) == c, (s, js))


def key():
    K[0] += 1
    return "k3-%d-%d" % (time.time_ns(), K[0])


def iso(d):
    return d.astimezone(UTC).isoformat(timespec="microseconds")


def q(v):
    return urllib.parse.quote(v, safe="")


def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


US = dt.timedelta(microseconds=1)


def base_fixture(now):
    T0, T1, T2, T3 = now - dt.timedelta(hours=3), now - dt.timedelta(hours=2), now - dt.timedelta(minutes=90), now - dt.timedelta(hours=1)
    fx = {
        "currency": "EUR", "minor_units": 2, "authorization_ttl_seconds": 600,
        "users": [{"id": "u_" + h, "email": h + "@example.com", "password": "correct horse", "display_name": h.title(), "handle": h, "balance": b}
                  for h, b in (("ada", 10000), ("bob", 2500), ("cy", 300), ("op", 0))],
        # ada opening 10000+500-200+300+100 = 10700; bob 2500-500+200-300-100 = 1800; cy 300-300+300 = 300; op 0
        "payments": [
            {"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500, "note": "t0", "visibility": "public", "created_at": iso(T0)},
            {"id": "p_2", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 200, "note": "t1a", "visibility": "public", "created_at": iso(T1)},
            {"id": "p_3", "from_user_id": "u_ada", "to_user_id": "u_cy", "amount": 300, "note": "t1b", "visibility": "private", "created_at": iso(T1)},
            {"id": "p_5", "from_user_id": "u_cy", "to_user_id": "u_bob", "amount": 300, "note": "t2", "visibility": "public", "created_at": iso(T2)},
            {"id": "p_4", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 100, "note": "t3", "visibility": "public", "created_at": iso(T3)},
        ],
        "requests": [], "authorizations": [], "settlement_operator_ids": ["u_op"],
    }
    # balances above are post-payment: ada 10700-500+200-300-100=10000; bob 1800+500-200+300+100=2500; cy 0+300-300 = 0 -> set cy 0
    fx["users"][2]["balance"] = 0
    return fx, (T0, T1, T2, T3)


OPEN = {"ada": 10700, "bob": 1800, "cy": 0, "op": 0}
TOTAL = 12500


def reset(fx, base=None):
    s, js = call("POST", "/_test/reset", fx, base=base)
    assert s == 204, (s, js)


def login(h, base=None):
    s, js = call("POST", "/auth/login", {"email": h + "@example.com", "password": "correct horse"}, base=base)
    assert s == 200, (s, js)
    return js["token"]


def toks(base=None):
    return {h: login(h, base) for h in ("ada", "bob", "cy", "op")}


def me(tok, as_of=None, known=None):
    qs = []
    if as_of:
        qs.append("as_of=" + q(as_of))
    if known:
        qs.append("known_at=" + q(known))
    return call("GET", "/me" + ("?" + "&".join(qs) if qs else ""), token=tok)


def stmt(tok, **kw):
    qs = "&".join("%s=%s" % (k, q(str(v))) for k, v in kw.items())
    return call("GET", "/statement" + ("?" + qs if qs else ""), token=tok)


def full_stmt(tok, **kw):
    s, js = stmt(tok, limit=200, **kw)
    assert s == 200, (s, js)
    return js


def setup():
    now = dt.datetime.now(UTC)
    fx, T = base_fixture(now)
    reset(fx)
    return toks(), T


# ------------------------------------------------------------------ probes

def p_seeds():
    t, (T0, T1, T2, T3) = setup()
    for h in t:
        s, m = me(t[h], as_of=iso(T0 - dt.timedelta(days=1)))
        check("opening balance %s via as_of before history" % h, s == 200 and m["balance"] == OPEN[h] == m["total"], (s, m))
    now = dt.datetime.now(UTC)
    fx, _ = base_fixture(now)
    fx["payments"][0]["created_at"] = iso(now + dt.timedelta(minutes=5))
    err("seeded created_at in the future -> 422", call("POST", "/_test/reset", fx), 422, "validation_failed")
    check("unchanged after bad reset", me(t["ada"])[1]["balance"] == 10000)
    s, js = call("GET", "/activity", token=t["ada"])
    check("feed keeps seeded created_at and order", [p["payment_id"] for p in js["payments"]] in (["p_4", "p_5", "p_3", "p_2", "p_1"], ["p_4", "p_5", "p_2", "p_3", "p_1"])
          and next(p for p in js["payments"] if p["payment_id"] == "p_1")["created_at"] == iso(T0), [p["payment_id"] for p in js["payments"]])


def p_as_of():
    t, (T0, T1, T2, T3) = setup()
    exp = {  # balances just after each instant
        "before": OPEN,
        "T0": {"ada": 10200, "bob": 2300, "cy": 0, "op": 0},
        "T1": {"ada": 10100, "bob": 2100, "cy": 300, "op": 0},
        "T2": {"ada": 10100, "bob": 2400, "cy": 0, "op": 0},
        "T3": {"ada": 10000, "bob": 2500, "cy": 0, "op": 0},
    }
    views = [("before", T0 - US), ("T0", T0), ("T0+1us", T0 + US), ("T1-1us", T1 - US), ("T1", T1), ("T2", T2), ("T3", T3), ("future", dt.datetime.now(UTC) + dt.timedelta(days=30))]
    ref = {"T0+1us": "T0", "T1-1us": "T0", "future": "T3"}
    for name, at in views:
        got = {h: me(t[h], as_of=iso(at))[1]["balance"] for h in t}
        want = exp[ref.get(name, name)]
        check("as_of %s balances %s (inclusive), sum = seeded total" % (name, want), got == want and sum(got.values()) == TOTAL, got)
    for text in (iso(T1).replace("+00:00", "Z"), (T1.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))).isoformat(timespec="microseconds")):
        s, m = me(t["ada"], as_of=text)
        check("as_of echoed exactly and same instant in any offset (%s)" % text[-6:], s == 200 and m["as_of"] == text and m["balance"] == 10100, m)
    for bad in ("", "2026-09-24", "2026-09-24T13:20:00", "2026-09-24T13:20Z", "yesterday", "2026-13-01T00:00:00Z", "2026-09-24 13:20:00+00:00x"):
        err("as_of %r -> 422" % bad, me(t["ada"], as_of=bad) if bad else call("GET", "/me?as_of=", token=t["ada"]), 422, "validation_failed")
    s, m = me(t["ada"])
    check("no temporal params: current fields, no as_of key", s == 200 and "as_of" not in m and m["balance"] == 10000 and m["available"] == 10000)
    s, m = call("POST", "/auth/signup", {"email": "newbie@example.com", "password": "12345678", "display_name": "N"})
    nt = m["token"]
    call("POST", "/payments", {"to_handle": "newbie", "amount": 50}, token=t["ada"], key=key())
    check("signup wallet opens at 0 historically", me(nt, as_of=iso(T0))[1]["balance"] == 0 and me(nt, as_of=iso(dt.datetime.now(UTC) + dt.timedelta(hours=1)))[1]["balance"] == 50)


def p_statement():
    t, (T0, T1, T2, T3) = setup()
    js = full_stmt(t["ada"])
    ids = [e["payment"]["payment_id"] for e in js["entries"]]
    check("statement oldest first, ties by payment id (p_2 before p_3), only own payments", ids == ["p_1", "p_2", "p_3", "p_4"], ids)
    check("opening = opening balance, closing = current", js["opening_balance"] == 10700 and js["closing_balance"] == 10000, js)
    deltas = [e["delta"] for e in js["entries"]]
    check("deltas signed (-500, +200, -300, -100)", deltas == [-500, 200, -300, -100], deltas)
    check("opening + sum(delta) = closing; balance_after running", js["opening_balance"] + sum(deltas) == js["closing_balance"]
          and [e["balance_after"] for e in js["entries"]] == [10200, 10400, 10100, 10000])
    e = js["entries"][0]
    check("entry has revision/effective_at/recorded_at; payment.created_at kept", e["revision"] == 1 and e["effective_at"] == iso(T0) == e["recorded_at"] == e["payment"]["created_at"], e)
    check("snapshot token present", isinstance(js.get("snapshot"), str) and js["snapshot"])
    cy = full_stmt(t["cy"])
    check("cy statement excludes public payments of others", [x["payment"]["payment_id"] for x in cy["entries"]] == ["p_3", "p_5"], cy)
    # half-open window
    w = full_stmt(t["ada"], **{"from": iso(T1), "to": iso(T3)})
    check("window [T1,T3): includes T1 entries, excludes T3", [x["payment"]["payment_id"] for x in w["entries"]] == ["p_2", "p_3"], w)
    check("window opening = balance just before T1, closing = balance just before T3", w["opening_balance"] == 10200 and w["closing_balance"] == 10100, w)
    check("opening equals /me as_of(from - 1us); closing equals /me as_of(to - 1us)",
          w["opening_balance"] == me(t["ada"], as_of=iso(T1 - US))[1]["balance"] and w["closing_balance"] == me(t["ada"], as_of=iso(T3 - US))[1]["balance"])
    w2 = full_stmt(t["ada"], **{"from": iso(T1 + US), "to": iso(T3 + US)})
    check("window [T1+1us, T3+1us): only p_4", [x["payment"]["payment_id"] for x in w2["entries"]] == ["p_4"], w2)
    em = full_stmt(t["ada"], **{"from": iso(T1), "to": iso(T1)})
    check("empty window from == to: no entries, opening = closing", em["entries"] == [] and em["opening_balance"] == em["closing_balance"] == 10200, em)
    fut = full_stmt(t["ada"], to=iso(dt.datetime.now(UTC) + dt.timedelta(days=1)))
    check("future to accepted", fut["closing_balance"] == 10000)
    for kv in ({"from": "2026-09-24"}, {"to": ""}, {"known_at": "nope"}, {"limit": "0"}, {"offset": "-1"}, {"limit": "201"}):
        err("statement %s -> 422" % kv, stmt(t["ada"], **kv), 422, "validation_failed")
    err("statement no token 401", call("GET", "/statement"), 401, "unauthenticated")
    # pagination invariance
    pages, off = [], 0
    while True:
        s, pg = stmt(t["ada"], limit=1, offset=off)
        pages.append(pg)
        off += 1
        if not pg["has_more"]:
            break
    flat = [(x["payment"]["payment_id"], x["balance_after"]) for pg in pages for x in pg["entries"]]
    check("limit=1 pages give same entries and balance_after as full", flat == [(x["payment"]["payment_id"], x["balance_after"]) for x in js["entries"]], flat)
    check("every page reports full-window opening/closing", all(pg["opening_balance"] == 10700 and pg["closing_balance"] == 10000 for pg in pages))
    s, pg = stmt(t["ada"], limit=3, offset=3)
    check("final partial page has_more false", len(pg["entries"]) == 1 and pg["has_more"] is False, pg)
    s, pg = stmt(t["ada"], limit=2, offset=99)
    check("offset beyond end: empty, has_more false, balances kept", pg["entries"] == [] and pg["has_more"] is False and pg["closing_balance"] == 10000, pg)
    s, pg = stmt(t["ada"], limit=2, offset=0)
    check("limit 2 has_more true", pg["has_more"] is True and [x["balance_after"] for x in pg["entries"]] == [10200, 10400])


def p_snapshot():
    t, (T0, T1, T2, T3) = setup()
    s, first = stmt(t["ada"], limit=2)
    tok = first["snapshot"]
    full_before = [x for o in (0, 2) for x in stmt(t["ada"], snapshot=tok, limit=2, offset=o)[1]["entries"]]
    # writes after the snapshot: payment, correction, hold lifecycle
    call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t["ada"], key=key())
    call("POST", "/payments/p_1/corrections", {"expected_revision": 1, "amount": 400, "effective_at": iso(T0), "reason": "fix"}, token=t["ada"], key=key())
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 50}, token=t["ada"], key=key())
    call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 20, "final": False}, token=t["bob"], key=key())
    call("POST", "/authorizations/%s/void" % a["authorization_id"], token=t["ada"])
    pages = [stmt(t["ada"], snapshot=tok, limit=2, offset=o)[1] for o in (0, 2, 4)]
    after = [x for pg in pages for x in pg["entries"]]
    check("snapshot pages unchanged after payment, correction, capture, void", after == full_before and len(after) == 4, len(after))
    check("snapshot balances frozen (closing 10000, opening 10700)", all(pg["opening_balance"] == 10700 and pg["closing_balance"] == 10000 for pg in pages))
    check("snapshot has_more: true, false, false(beyond end)", [pg["has_more"] for pg in pages] == [True, False, False], [pg["has_more"] for pg in pages])
    live = full_stmt(t["ada"])
    check("a fresh statement sees the new state", live["closing_balance"] != 10000 and live["snapshot"] != tok)
    for extra in ({"from": iso(T0)}, {"to": iso(T3)}, {"known_at": iso(T3)}):
        err("snapshot + %s -> 422" % list(extra)[0], stmt(t["ada"], snapshot=tok, **extra), 422, "validation_failed")
    s, js = stmt(t["ada"], snapshot=tok, bogus="1")
    check("snapshot + unknown param ignored", s == 200, (s, js))
    err("another user's snapshot -> 404", stmt(t["bob"], snapshot=tok), 404, "not_found")
    err("unknown snapshot -> 404", stmt(t["ada"], snapshot="nope"), 404, "not_found")
    # export/import keeps tokens (team decision), reset clears
    s, exp = call("GET", "/_test/export")
    call("POST", "/_test/import", exp)
    s, js = stmt(t["ada"], snapshot=tok, limit=200)
    check("snapshot survives import", s == 200 and len(js["entries"]) == 4)
    fx, _ = base_fixture(dt.datetime.now(UTC))
    reset(fx)
    t2 = toks()
    err("token from before reset -> 404", stmt(t2["ada"], snapshot=tok), 404, "not_found")
    # concurrent writes while paging a snapshot
    s, f = stmt(t2["ada"], limit=200)
    tok2, ref = f["snapshot"], f

    def writer(i):
        if i % 3 == 0:
            return call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=t2["ada"], key=key())[0]
        if i % 3 == 1:
            return call("POST", "/payments", {"to_handle": "ada", "amount": 1}, token=t2["bob"], key=key())[0]
        return stmt(t2["ada"], snapshot=tok2, limit=200)[1] == ref

    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(writer, range(90)))
    snaps = [r for i, r in enumerate(rs) if i % 3 == 2]
    check("snapshot identical across 30 reads during 60 concurrent payments", all(snaps), snaps.count(False))


def corr(tok, pid, body, k=None):
    return call("POST", "/payments/%s/corrections" % pid, body, token=tok, key=k or key())


def p_corrections():
    t, (T0, T1, T2, T3) = setup()
    good = {"expected_revision": 1, "amount": 400, "effective_at": iso(T0), "reason": "corrected amount"}
    err("correction no token 401", call("POST", "/payments/p_1/corrections", good, key=key()), 401, "unauthenticated")
    err("correction missing key 400", call("POST", "/payments/p_1/corrections", good, token=t["ada"]), 400, "missing_idempotency_key")
    err("correction by receiver 403", corr(t["bob"], "p_1", good), 403, "forbidden")
    err("correction by third party 403", corr(t["cy"], "p_1", good), 403, "forbidden")
    err("correction unknown payment 404", corr(t["ada"], "nope", good), 404, "not_found")
    now = dt.datetime.now(UTC)
    bad = [("missing expected_revision", {k: v for k, v in good.items() if k != "expected_revision"}),
           ("missing amount", {k: v for k, v in good.items() if k != "amount"}),
           ("missing effective_at", {k: v for k, v in good.items() if k != "effective_at"}),
           ("missing reason", {k: v for k, v in good.items() if k != "reason"}),
           ("revision 0", dict(good, expected_revision=0)), ("revision -1", dict(good, expected_revision=-1)),
           ("revision 1.5", dict(good, expected_revision=1.5)), ("revision '1'", dict(good, expected_revision="1")),
           ("amount -1", dict(good, amount=-1)), ("amount 1e9+1", dict(good, amount=1000000001)), ("amount 1.5", dict(good, amount=1.5)),
           ("amount '5'", dict(good, amount="5")), ("amount true", dict(good, amount=True)),
           ("reason ''", dict(good, reason="")), ("reason 201", dict(good, reason="r" * 201)), ("reason 5", dict(good, reason=5)),
           ("effective_at naive", dict(good, effective_at="2026-09-20T12:00:00")), ("effective_at date", dict(good, effective_at="2026-09-20")),
           ("effective_at future", dict(good, effective_at=iso(now + dt.timedelta(minutes=10)))), ("effective_at ''", dict(good, effective_at="")),
           ("effective_at 5", dict(good, effective_at=5))]
    for name, b in bad:
        s, js = corr(t["ada"], "p_1", b)
        check("correction %s -> 422 validation_failed (or 400 for a wrong JSON type)" % name,
              (s == 422 and code(js) == "validation_failed") or (s == 400 and code(js) == "malformed_request" and name in ("revision '1'", "reason 5", "effective_at 5")), (s, js))
    check("no revision appended by invalid corrections", len(call("GET", "/payments/p_1/revisions", token=t["ada"])[1]["revisions"]) == 1)
    k = key()
    s, r2 = corr(t["ada"], "p_1", good, k)
    check("correction 201 shape", s == 201 and r2["payment_id"] == "p_1" and r2["revision"] == 2 and r2["amount"] == 400 and r2["effective_at"] == iso(T0)
          and r2["reason"] == "corrected amount" and parse(r2["recorded_at"]) > T0, (s, r2))
    m = {h: me(t[h])[1] for h in ("ada", "bob")}
    check("decrease debits receiver, credits sender (ada 10100, bob 2400)", m["ada"]["balance"] == 10100 and m["bob"]["balance"] == 2400, m)
    s, r3 = corr(t["ada"], "p_1", dict(good, expected_revision=2, amount=0, reason="reverse"))
    check("zero amount reverses (revision 3)", s == 201 and r3["revision"] == 3 and me(t["bob"])[1]["balance"] == 2000)
    check("recorded_at strictly increasing", parse(r3["recorded_at"]) > parse(r2["recorded_at"]))
    s, rp = corr(t["ada"], "p_1", good, k)
    check("replay after newer revisions -> 200 original revision 2", s == 200 and rp == r2, (s, rp))
    err("same key different body -> reuse", corr(t["ada"], "p_1", dict(good, reason="other"), k), 409, "idempotency_key_reuse")
    err("stale expected_revision -> 409 stale_revision", corr(t["ada"], "p_1", good), 409, "stale_revision")
    s, revs = call("GET", "/payments/p_1/revisions", token=t["bob"])
    check("revisions in order incl. rev 1 reason ''", s == 200 and [r["revision"] for r in revs["revisions"]] == [1, 2, 3] and revs["revisions"][0]["reason"] == ""
          and revs["revisions"][0]["amount"] == 500 and revs["revisions"][0]["effective_at"] == iso(T0), revs)
    err("third party revisions 404 (public payment)", call("GET", "/payments/p_1/revisions", token=t["cy"]), 404, "not_found")
    err("revisions no token 401", call("GET", "/payments/p_1/revisions"), 401, "unauthenticated")
    err("revisions unknown 404", call("GET", "/payments/nope/revisions", token=t["ada"]), 404, "not_found")
    s, js = call("GET", "/activity", token=t["ada"])
    p1 = next(p for p in js["payments"] if p["payment_id"] == "p_1")
    check("activity shows the original payment, no new feed items", p1["amount"] == 500 and len(js["payments"]) == 5, (p1, len(js["payments"])))
    st = full_stmt(t["ada"])
    e = st["entries"][0]
    check("statement uses selected revision 3 (amount 0, zero delta entry kept, not counted twice)", e["payment"]["payment_id"] == "p_1" and e["revision"] == 3
          and e["delta"] == 0 and e["payment"]["amount"] == 0 and len(st["entries"]) == 4 and st["closing_balance"] == 10500, st["entries"][0])
    # increase debits sender
    s, r = corr(t["ada"], "p_4", {"expected_revision": 1, "amount": 600, "effective_at": iso(T3), "reason": "more"})
    check("increase debits sender (ada -500, bob +500)", s == 201 and me(t["ada"])[1]["balance"] == 10000 and me(t["bob"])[1]["balance"] == 2500, (s, r))
    # request payments are correctable (team decision); original idempotent response unchanged
    s, rq = call("POST", "/requests", {"payer_handle": "ada", "amount": 10}, token=t["bob"], key=key())
    kp = key()
    s, pay = call("POST", "/requests/%s/pay" % rq["request_id"], {}, token=t["ada"], key=kp)
    s, r = corr(t["ada"], pay["payment_id"], {"expected_revision": 1, "amount": 5, "effective_at": pay["created_at"], "reason": "half"})
    s2, rep = call("POST", "/requests/%s/pay" % rq["request_id"], {}, token=t["ada"], key=kp)
    check("original pay response unchanged after correction", s2 == 200 and rep == pay and rep["amount"] == 10)
    for h in t:
        pass
    sums = sum(me(t[h])[1]["balance"] for h in t)
    check("sum of current balances = seeded total", sums == TOTAL, sums)


def p_known_at():
    t, (T0, T1, T2, T3) = setup()
    before = dt.datetime.now(UTC)
    s, r2 = corr(t["ada"], "p_1", {"expected_revision": 1, "amount": 100, "effective_at": iso(T0), "reason": "fix"})
    R = parse(r2["recorded_at"])
    for name, known, ada, bob in (("before correction recorded", R - US, 10200, 2300), ("at recorded_at (inclusive)", R, 10600, 1900), ("future", dt.datetime.now(UTC) + dt.timedelta(days=1), 10600, 1900)):
        a = me(t["ada"], as_of=iso(T0), known=iso(known))[1]
        b = me(t["bob"], as_of=iso(T0), known=iso(known))[1]
        check("known_at %s: as_of T0 ada %d bob %d, echo" % (name, ada, bob), a["balance"] == ada and b["balance"] == bob and a["known_at"] == iso(known), (a, b))
    a = me(t["ada"], known=iso(T0 - US))[1]
    check("known_at before any payment recorded: opening balances", a["balance"] == 10700 and a["available"] == 10700, a)
    a = me(t["ada"], known=iso(T1))[1]
    check("known_at T1 (p_1..p_3 known, p_4/p_5 not): ada 10100", a["balance"] == 10100, a)
    sums = {name: sum(me(t[h], as_of=iso(at), known=iso(kn))[1]["balance"] for h in t) for name, at, kn in
            (("T0/R-1", T0, R - US), ("T2/R", T2, R), ("now/T1", dt.datetime.now(UTC), T1), ("past/past", T0 - US, T0 - US))}
    check("sum = seeded total in every (as_of, known_at) view", all(v == TOTAL for v in sums.values()), sums)
    st_old = full_stmt(t["ada"], known_at=iso(R - US))
    st_new = full_stmt(t["ada"], known_at=iso(R))
    check("statement known_at selects revision 1 before / 2 at recorded_at", st_old["entries"][0]["revision"] == 1 and st_old["entries"][0]["delta"] == -500
          and st_new["entries"][0]["revision"] == 2 and st_new["entries"][0]["delta"] == -100 and st_old["known_at"] == iso(R - US), (st_old["entries"][0], st_new["entries"][0]))
    # effective time moves a payment between windows
    s, r = corr(t["ada"], "p_4", {"expected_revision": 1, "amount": 100, "effective_at": iso(T0 - dt.timedelta(minutes=30)), "reason": "backdate"})
    w = full_stmt(t["ada"], **{"from": iso(T1), "to": iso(T3 + US)})
    check("back-dated correction moves p_4 out of [T1,T3]", [x["payment"]["payment_id"] for x in w["entries"]] == ["p_2", "p_3"], w["entries"])
    w0 = full_stmt(t["ada"], to=iso(T0))
    check("... and into the earlier window, ordered by effective_at", [x["payment"]["payment_id"] for x in w0["entries"]] == ["p_4"] and w0["entries"][0]["effective_at"] == iso(T0 - dt.timedelta(minutes=30)), w0["entries"])
    w_k = full_stmt(t["ada"], to=iso(T0), known_at=iso(parse(r["recorded_at"]) - US))
    check("... but not as known before that correction", w_k["entries"] == [], w_k["entries"])
    payment_created_after = dt.datetime.now(UTC)
    s, p = call("POST", "/payments", {"to_handle": "bob", "amount": 7}, token=t["ada"], key=key())
    a = me(t["ada"], known=iso(parse(p["created_at"]) - US))[1]
    b = me(t["ada"])[1]
    check("payment not yet recorded contributes nothing under earlier known_at", b["balance"] - a["balance"] == -7, (a["balance"], b["balance"]))


def p_overdraft():
    t, (T0, T1, T2, T3) = setup()
    # cy received p_3 300 at T1, spent p_5 300 at T2 (cy ends 0). Give cy 300 now so a current debit is affordable.
    call("POST", "/payments", {"to_handle": "cy", "amount": 300}, token=t["ada"], key=key())
    snapshot_state = (me(t["ada"])[1], me(t["cy"])[1], call("GET", "/payments/p_3/revisions", token=t["ada"])[1], full_stmt(t["cy"])["entries"])
    k = key()
    err("decrease p_3 to 0 at T1: cy negative at T2 -> historical_overdraft", corr(t["ada"], "p_3", {"expected_revision": 1, "amount": 0, "effective_at": iso(T1), "reason": "r"}, k), 409, "historical_overdraft")
    after = (me(t["ada"])[1], me(t["cy"])[1], call("GET", "/payments/p_3/revisions", token=t["ada"])[1], full_stmt(t["cy"])["entries"])
    check("failure preserves balances, revisions, statements", after == snapshot_state)
    # same-instant boundary: move p_3 to exactly T2 (same instant as cy's spend) -> combined 0, allowed
    s, r = corr(t["ada"], "p_3", {"expected_revision": 1, "amount": 300, "effective_at": iso(T2), "reason": "same instant"}, k)
    check("same key reused after 409 (unclaimed) and effective_at == T2 combined boundary -> 201", s == 201 and r["revision"] == 2, (s, r))
    err("moving it 1us after T2 -> historical_overdraft", corr(t["ada"], "p_3", {"expected_revision": 2, "amount": 300, "effective_at": iso(T2 + US), "reason": "late"}), 409, "historical_overdraft")
    # insufficient_funds precedes historical_overdraft: drain cy now, then the same back-dated decrease
    call("POST", "/payments", {"to_handle": "bob", "amount": 300}, token=t["cy"], key=key())
    err("current unaffordable debit -> insufficient_funds (precedence over historical)", corr(t["ada"], "p_3", {"expected_revision": 2, "amount": 0, "effective_at": iso(T1), "reason": "r"}), 409, "insufficient_funds")
    # held funds count: ada places a hold leaving 50 available, then increases a payment by 51
    av = me(t["ada"])[1]["available"]
    call("POST", "/authorizations", {"to_handle": "bob", "amount": av - 50}, token=t["ada"], key=key())
    err("increase beyond available (held funds excluded) -> insufficient_funds", corr(t["ada"], "p_4", {"expected_revision": 1, "amount": 151, "effective_at": iso(T3), "reason": "r"}), 409, "insufficient_funds")
    s, r = corr(t["ada"], "p_4", {"expected_revision": 1, "amount": 150, "effective_at": iso(T3), "reason": "r"})
    check("increase exactly available -> 201", s == 201, (s, r))
    check("available exactly 0 after", me(t["ada"])[1]["available"] == 0)
    # historical available: hold created now; back-dated increase in the past is fine for total but a debit at a time when hold existed
    tot = sum(me(t[h])[1]["balance"] for h in t)
    check("sum still seeded total", tot == TOTAL, tot)


def p_races():
    t, (T0, T1, T2, T3) = setup()
    body = lambda i: {"expected_revision": 1, "amount": 400 - i, "effective_at": iso(T0), "reason": "race %d" % i}
    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(lambda i: corr(t["ada"], "p_1", body(i)), range(30)))
    st = sorted(r[0] for r in rs)
    check("30 concurrent corrections on revision 1: exactly one 201, rest 409 stale_revision", st.count(201) == 1 and st.count(409) == 29
          and all(code(r[1]) == "stale_revision" for r in rs if r[0] == 409), st)
    revs = call("GET", "/payments/p_1/revisions", token=t["ada"])[1]["revisions"]
    win = next(r[1] for r in rs if r[0] == 201)
    check("one revision 2 recorded, money matches it", len(revs) == 2 and revs[1]["amount"] == win["amount"] and me(t["bob"])[1]["balance"] == 2500 - (500 - win["amount"]))
    k = key()
    with cf.ThreadPoolExecutor(20) as ex:
        rs = list(ex.map(lambda i: corr(t["ada"], "p_4", {"expected_revision": 1, "amount": 90, "effective_at": iso(T3), "reason": "same"}, k), range(20)))
    st = sorted(r[0] for r in rs)
    check("20 identical correction retries: one 201, 19 x 200 same body", st.count(201) == 1 and st.count(200) == 19 and len({json.dumps(r[1], sort_keys=True) for r in rs}) == 1, st)
    check("sum seeded total after races", sum(me(t[h])[1]["balance"] for h in t) == TOTAL)


def p_linked():
    t, (T0, T1, T2, T3) = setup()
    s, stl = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 10}, {"from_handle": "bob", "to_handle": "cy", "amount": 5}]}, token=t["op"], key=key())
    m = stl["payments"][0]
    err("settlement member correction -> linked_payment_immutable", corr(t["ada"], m["payment_id"], {"expected_revision": 1, "amount": 1, "effective_at": m["created_at"], "reason": "r"}), 422, "linked_payment_immutable")
    revs = call("GET", "/payments/%s/revisions" % m["payment_id"], token=t["ada"])[1]["revisions"]
    check("settlement member revision 1 at committed_at", revs[0]["effective_at"] == revs[0]["recorded_at"] == stl["committed_at"], revs)
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 30}, token=t["ada"], key=key())
    s, cap = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key())
    err("capture correction -> linked_payment_immutable", corr(t["ada"], cap["payment_id"], {"expected_revision": 1, "amount": 1, "effective_at": cap["created_at"], "reason": "r"}), 422, "linked_payment_immutable")
    err("receiver correcting a capture still 403 first", corr(t["bob"], cap["payment_id"], {"expected_revision": 1, "amount": 1, "effective_at": cap["created_at"], "reason": "r"}), 403, "forbidden")
    st = full_stmt(t["bob"])
    caps = [e for e in st["entries"] if e["payment"]["payment_id"] == cap["payment_id"]]
    check("capture appears exactly once in statement with its link", len(caps) == 1 and caps[0]["payment"]["authorization_id"] == a["authorization_id"], caps)
    check("statement has only payments (no hold/release entries)", all("payment_id" in e["payment"] for e in st["entries"]) and len(st["entries"]) == len(set(e["payment"]["payment_id"] for e in st["entries"])))


def p_holds():
    fx, _ = base_fixture(dt.datetime.now(UTC))
    fx["authorization_ttl_seconds"] = 3
    reset(fx)
    t = toks()
    t_before = dt.datetime.now(UTC)
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key())
    C = parse(a["created_at"])
    check("new hold closed_at null", a["closed_at"] is None, a)
    s, c1 = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 300, "final": False}, token=t["bob"], key=key())
    X1 = parse(c1["created_at"])
    s, v = call("POST", "/authorizations/%s/void" % a["authorization_id"], token=t["ada"])
    V = parse(v["closed_at"])
    check("void closed_at = event time after capture", V > X1 and v["status"] == "voided", v)
    exp = parse(a["expires_at"])

    def view(at, known=None):
        m = me(t["ada"], as_of=iso(at), known=iso(known) if known else None)[1]
        return (m["total"], m["held"], m["available"])

    check("as_of before creation: held 0", view(C - US) == (10000, 0, 10000), view(C - US))
    check("as_of creation (inclusive): held 1000", view(C) == (10000, 1000, 9000), view(C))
    check("as_of nonfinal capture: total -300, held 700", view(X1) == (9700, 700, 9000), view(X1))
    check("as_of void: held 0", view(V) == (9700, 0, 9700), view(V))
    check("known_at before void, as_of just before deadline: still held 700", view(exp - US, V - US) == (9700, 700, 9000), view(exp - US, V - US))
    check("known_at before void, as_of at deadline: released (expiry at expires_at)", view(exp, V - US) == (9700, 0, 9700), view(exp, V - US))
    check("known_at before creation: hold unknown, capture unknown", view(X1, C - US) == (10000, 0, 10000), view(X1, C - US))
    check("known_at before capture: capture not applied, hold 1000", view(V - US, X1 - US) == (10000, 1000, 9000), view(V - US, X1 - US))
    m = me(t["ada"], known=iso(C))[1]
    check("known_at only (as_of = now): hold known, runs to deadline", m["held"] == 1000 and m["available"] == 9000, m)
    # expiry by the clock
    s, b = call("POST", "/authorizations", {"to_handle": "bob", "amount": 500}, token=t["ada"], key=key())
    E = parse(b["expires_at"])
    check("future as_of beyond deadline releases open hold", view(E + dt.timedelta(seconds=1)) == (9700, 0, 9700) and view(E - US)[1] == 500, (view(E - US), view(E + dt.timedelta(seconds=1))))
    while dt.datetime.now(UTC) <= E + dt.timedelta(milliseconds=200):
        time.sleep(0.05)
    lst = {x["authorization_id"]: x for x in call("GET", "/authorizations", token=t["ada"])[1]["authorizations"]}
    check("expired hold closed_at = expires_at", lst[b["authorization_id"]]["status"] == "expired" and lst[b["authorization_id"]]["closed_at"] == b["expires_at"], lst[b["authorization_id"]])
    check("after expiry, historical view at deadline-1us still held, at deadline released", view(E - US)[1] == 500 and view(E)[1] == 0)
    check("current /me: held 0 (released once)", me(t["ada"])[1]["held"] == 0 and me(t["ada"])[1]["available"] == 9700)
    # final capture closes at capture time
    s, c = call("POST", "/authorizations", {"to_handle": "bob", "amount": 400}, token=t["ada"], key=key())
    s, cp = call("POST", "/authorizations/%s/capture" % c["authorization_id"], {"amount": 100}, token=t["bob"], key=key())
    F = parse(cp["created_at"])
    lst = {x["authorization_id"]: x for x in call("GET", "/authorizations", token=t["ada"])[1]["authorizations"]}
    check("final capture closed_at = capture time", parse(lst[c["authorization_id"]]["closed_at"]) == F, lst[c["authorization_id"]])
    check("as_of final capture: remainder released same instant", view(F) == (9600, 0, 9600) and view(F - US)[1] == 400, (view(F - US), view(F)))
    # seeded holds: open created at reset (or created_at), closed seeded holds hold nothing
    fx, _ = base_fixture(dt.datetime.now(UTC))
    now = dt.datetime.now(UTC)
    fx["authorizations"] = [
        {"id": "a_o", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 2000, "status": "open", "expires_at": iso(now + dt.timedelta(hours=2))},
        {"id": "a_c", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 3000, "status": "open", "expires_at": iso(now + dt.timedelta(hours=2)), "created_at": iso(now - dt.timedelta(minutes=30))},
        {"id": "a_x", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 4000, "status": "voided", "expires_at": iso(now + dt.timedelta(hours=2))},
        {"id": "a_e", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 4000, "status": "expired", "expires_at": iso(now - dt.timedelta(hours=2))},
    ]
    reset(fx)
    t = toks()
    m_now = me(t["ada"])[1]
    check("seeded open holds held now (5000)", m_now["held"] == 5000 and m_now["available"] == 5000, m_now)
    m_45 = me(t["ada"], as_of=iso(now - dt.timedelta(minutes=45)))[1]
    m_20 = me(t["ada"], as_of=iso(now - dt.timedelta(minutes=20)))[1]
    check("seeded hold with created_at -30min: held 0 at -45min, 3000 at -20min", m_45["held"] == 0 and m_20["held"] == 3000, (m_45, m_20))
    lst = {x["authorization_id"]: x for x in call("GET", "/authorizations", token=t["ada"])[1]["authorizations"]}
    check("seeded expired closed_at = expires_at; open closed_at null", lst["a_e"]["closed_at"] == lst["a_e"]["expires_at"] and lst["a_o"]["closed_at"] is None, lst)
    sm = full_stmt(t["ada"])
    check("statement unaffected by holds", [e["payment"]["payment_id"] for e in sm["entries"]] == ["p_1", "p_2", "p_3", "p_4"])


def p_hold_overdraft():
    """A back-dated increase that leaves total fine but makes available negative while a hold existed."""
    fx, _ = base_fixture(dt.datetime.now(UTC))
    reset(fx)
    t = toks()
    # bob: total 2500. Hold 2400 of bob's money now; then bob pays ada 50 (available 100 -> 50).
    s, a = call("POST", "/authorizations", {"to_handle": "ada", "amount": 2400}, token=t["bob"], key=key())
    s, p = call("POST", "/payments", {"to_handle": "ada", "amount": 50}, token=t["bob"], key=key())
    call("POST", "/authorizations/%s/void" % a["authorization_id"], token=t["bob"])   # now bob available 2450
    # increase p by 60 effective at its own time: currently affordable (2450), but at p's time available was 50 -> -10
    err("increase that overdraws historical available during a hold -> historical_overdraft",
        corr(t["bob"], p["payment_id"], {"expected_revision": 1, "amount": 110, "effective_at": p["created_at"], "reason": "r"}), 409, "historical_overdraft")
    s, r = corr(t["bob"], p["payment_id"], {"expected_revision": 1, "amount": 100, "effective_at": p["created_at"], "reason": "r"})
    check("increase to exactly available-at-the-time (0 left) -> 201", s == 201, (s, r))


def p_import_chains():
    if S1:
        fx, (T0, T1, T2, T3) = base_fixture(dt.datetime.now(UTC))
        s1fx = json.loads(json.dumps(fx))
        for k in ("authorizations", "authorization_ttl_seconds"):
            s1fx.pop(k)
        for p in s1fx["payments"]:
            p.pop("created_at")
        reset(s1fx, base=S1)
        t = toks(base=S1)
        s, p = call("POST", "/payments", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key(), base=S1)
        s, exp = call("GET", "/_test/export", base=S1)
        s, js = call("POST", "/_test/import", exp)
        check("stage-1 export imports into stage-3", s == 204, (s, js))
        m = me(t["ada"])[1]
        check("stage-1 tokens valid; current balance", m["balance"] == 9000, m)
        st = full_stmt(t["ada"])
        check("imported statement closes at current balance and sums", st["opening_balance"] + sum(e["delta"] for e in st["entries"]) == st["closing_balance"] == 9000, st)
        s, r = corr(t["ada"], p["payment_id"], {"expected_revision": 1, "amount": 900, "effective_at": p["created_at"], "reason": "imported fix"})
        check("imported stage-1 payment correctable", s == 201 and me(t["ada"])[1]["balance"] == 9100, (s, r))
        check("sum seeded total after stage-1 import + correction", sum(me(t[h])[1]["balance"] for h in t) == TOTAL)
    if S2:
        fx, _ = base_fixture(dt.datetime.now(UTC))
        s2fx = json.loads(json.dumps(fx))
        for p in s2fx["payments"]:
            p.pop("created_at")
        s2fx["authorization_ttl_seconds"] = 600
        reset(s2fx, base=S2)
        t = toks(base=S2)
        s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key(), base=S2)
        s, c = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 400, "final": False}, token=t["bob"], key=key(), base=S2)
        s, b = call("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, token=t["ada"], key=key(), base=S2)
        call("POST", "/authorizations/%s/void" % b["authorization_id"], token=t["ada"], base=S2)
        s, exp = call("GET", "/_test/export", base=S2)
        s, js = call("POST", "/_test/import", exp)
        check("stage-2 export imports into stage-3", s == 204, (s, js))
        m = me(t["ada"])[1]
        check("stage-2 holds imported: total 9600, held 600", (m["total"], m["held"], m["available"]) == (9600, 600, 9000), m)
        C = parse(a["created_at"])
        h = me(t["ada"], as_of=iso(C - US))[1]
        check("historical before imported hold: held 0, total 10000", (h["total"], h["held"]) == (10000, 0), h)
        h = me(t["ada"], as_of=iso(parse(c["created_at"])))[1]
        check("historical at imported capture: total 9600, held 600", (h["total"], h["held"]) == (9600, 600), h)
        err("imported capture is linked", corr(t["ada"], c["payment_id"], {"expected_revision": 1, "amount": 1, "effective_at": c["created_at"], "reason": "r"}), 422, "linked_payment_immutable")
        s, js = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key())
        check("imported open hold capturable after upgrade", s == 201 and js["amount"] == 600, (s, js))
    # stage-3 round trip
    fx, (T0, T1, T2, T3) = base_fixture(dt.datetime.now(UTC))
    reset(fx)
    t = toks()
    k = key()
    s, r2 = corr(t["ada"], "p_1", {"expected_revision": 1, "amount": 450, "effective_at": iso(T0), "reason": "x"}, k)
    s, first = stmt(t["ada"], limit=200)
    views = {h: me(t[h], as_of=iso(T1), known=iso(T2))[1] for h in t}
    s, exp = call("GET", "/_test/export")
    reset(fx)
    s, _ = call("POST", "/_test/import", exp)
    check("stage-3 round trip: historical views identical", {h: me(t[h], as_of=iso(T1), known=iso(T2))[1] for h in t} == views)
    s, rp = corr(t["ada"], "p_1", {"expected_revision": 1, "amount": 450, "effective_at": iso(T0), "reason": "x"}, k)
    check("correction replay after import -> 200 original", s == 200 and rp == r2, (s, rp))
    check("revisions preserved", [r["revision"] for r in call("GET", "/payments/p_1/revisions", token=t["bob"])[1]["revisions"]] == [1, 2])
    s, js = call("POST", "/_test/import", exp)
    check("repeat import no duplication", len(full_stmt(t["ada"])["entries"]) == 4)


PROBES = [p_seeds, p_as_of, p_statement, p_snapshot, p_corrections, p_known_at, p_overdraft, p_races, p_linked, p_holds, p_hold_overdraft, p_import_chains]


def main():
    global BASE, S1, S2
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--stage1-base", default=None)
    ap.add_argument("--stage2-base", default=None)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    BASE, S1, S2 = a.base, a.stage1_base, a.stage2_base
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
