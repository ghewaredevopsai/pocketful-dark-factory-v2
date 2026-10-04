#!/usr/bin/env python3
"""Reviewer probes for stage 4: refunds, the refund floor on corrections, correction batches, imports, deadlines.
Reuses the stage-3 reviewer helpers (../../stage-3/review/probes3.py).

Usage: python3 probes4.py --base URL [--stage3-base URL] [--only NAME]
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "stage-3", "review"))
import probes3 as P  # noqa: E402
from probes3 import call, check, err, key, iso, parse, US, code  # noqa: E402

S3 = None
TOTAL = 12500


def setup(**kw):
    fx, T = P.base_fixture(dt.datetime.now(P.UTC))
    fx.update(kw)
    P.reset(fx)
    return P.toks(), T


def me(tok, **kw):
    return P.me(tok, **kw)[1]


def refund(tok, pid, body, k=None):
    return call("POST", "/payments/%s/refunds" % pid, body, token=tok, key=k or key())


def batch(tok, items, k=None):
    return call("POST", "/correction-batches", {"corrections": items}, token=tok, key=k or key())


def item(pid, rev, amount, eff, reason="r"):
    return {"payment_id": pid, "expected_revision": rev, "amount": amount, "effective_at": eff, "reason": reason}


def revs(tok, pid):
    return call("GET", "/payments/%s/revisions" % pid, token=tok)[1]["revisions"]


def total_now(t):
    return sum(me(t[h])["balance"] for h in t)


def settle(t, transfers):
    k = key()
    s, js = call("POST", "/settlements", {"transfers": transfers}, token=t["op"], key=k)
    assert s == 201, (s, js)
    js["_key"], js["_transfers"] = k, transfers
    return js


# ------------------------------------------------------------------ refunds

def p_refunds():
    t, (T0, T1, T2, T3) = setup()
    k = key()
    s, r = refund(t["bob"], "p_1", {"amount": 200}, k)
    check("refund 201 shape: bob->ada, refund_of p_1, links null, original note/visibility", s == 201 and r["from_user_id"] == "u_bob"
          and r["to_user_id"] == "u_ada" and r["amount"] == 200 and r["refund_of"] == "p_1" and r["request_id"] is None
          and r["authorization_id"] is None and r.get("settlement_id") is None and r["note"] == "t0" and r["visibility"] == "public"
          and parse(r["created_at"]).tzinfo is not None, (s, r))
    check("refund moved money (ada 10200, bob 2300)", me(t["ada"])["balance"] == 10200 and me(t["bob"])["balance"] == 2300)
    s2, r2 = refund(t["bob"], "p_1", {"amount": 200}, k)
    check("refund replay 200 identical, no second move", s2 == 200 and r2 == r and me(t["bob"])["balance"] == 2300, s2)
    err("refund same key different body", refund(t["bob"], "p_1", {"amount": 1}, k), 409, "idempotency_key_reuse")
    err("refund by sender 403", refund(t["ada"], "p_1", {"amount": 1}), 403, "forbidden")
    err("refund by third party 403", refund(t["cy"], "p_1", {"amount": 1}), 403, "forbidden")
    err("refund unknown 404", refund(t["bob"], "nope", {"amount": 1}), 404, "not_found")
    err("refund no token 401", call("POST", "/payments/p_1/refunds", {"amount": 1}, key=key()), 401, "unauthenticated")
    err("refund missing key 400", call("POST", "/payments/p_1/refunds", {"amount": 1}, token=t["bob"]), 400, "missing_idempotency_key")
    for bad in (0, -1, 1.5, "5", True, None, 1000000001):
        err("refund amount %r -> 422" % (bad,), refund(t["bob"], "p_1", {"amount": bad}), 422, "validation_failed")
    err("refund amount missing -> 422", refund(t["bob"], "p_1", {}), 422, "validation_failed")
    err("cumulative refunds above amount -> refund_exceeds_payment", refund(t["bob"], "p_1", {"amount": 301}), 422, "refund_exceeds_payment")
    s, r3 = refund(t["bob"], "p_1", {"amount": 300})
    check("refund the exact remainder 201", s == 201, (s, r3))
    err("then 1 more -> refund_exceeds_payment", refund(t["bob"], "p_1", {"amount": 1}), 422, "refund_exceeds_payment")
    err("refund of a refund -> invalid_refund_target", refund(t["ada"], r["payment_id"], {"amount": 1}), 422, "invalid_refund_target")
    err("refund-of-refund by its sender is 403 first", refund(t["bob"], r["payment_id"], {"amount": 1}), 403, "forbidden")
    err("correcting a refund -> linked_payment_immutable", P.corr(t["bob"], r["payment_id"], {"expected_revision": 1, "amount": 1, "effective_at": r["created_at"], "reason": "x"}), 422, "linked_payment_immutable")
    # cap follows the current corrected amount
    s, c = P.corr(t["ada"], "p_4", {"expected_revision": 1, "amount": 60, "effective_at": iso(T3), "reason": "fix"})
    err("refund above corrected amount (61 > 60) -> refund_exceeds_payment", refund(t["bob"], "p_4", {"amount": 61}), 422, "refund_exceeds_payment")
    s, r4 = refund(t["bob"], "p_4", {"amount": 60})
    check("refund up to corrected amount 201", s == 201, (s, r4))
    # correction floor at refunded
    s, rr = refund(t["ada"], "p_2", {"amount": 150})
    err("correction below refunded (149 < 150) -> refund_exceeds_payment",
        P.corr(t["bob"], "p_2", {"expected_revision": 1, "amount": 149, "effective_at": iso(T1), "reason": "x"}), 422, "refund_exceeds_payment")
    s, c2 = P.corr(t["bob"], "p_2", {"expected_revision": 1, "amount": 150, "effective_at": iso(T1), "reason": "x"})
    check("correction exactly to refunded 201", s == 201, (s, c2))
    err("refund on fully refunded payment -> refund_exceeds_payment", refund(t["ada"], "p_2", {"amount": 1}), 422, "refund_exceeds_payment")
    check("sum conserved after refunds and corrections", total_now(t) == TOTAL, total_now(t))
    # available, not total: a hold blocks the refund; key stays unclaimed
    av = me(t["bob"])["available"]
    s, h = call("POST", "/authorizations", {"to_handle": "ada", "amount": av}, token=t["bob"], key=key())
    s, p = call("POST", "/payments", {"to_handle": "bob", "amount": 50}, token=t["ada"], key=key())
    call("POST", "/authorizations", {"to_handle": "ada", "amount": 50}, token=t["bob"], key=key())  # bob available 0
    before = (me(t["bob"]), me(t["ada"]), len(call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"]))
    kf = key()
    err("refund with only held funds -> insufficient_funds", refund(t["bob"], p["payment_id"], {"amount": 10}, kf), 409, "insufficient_funds")
    check("failed refund changed nothing", (me(t["bob"]), me(t["ada"]), len(call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"])) == before)
    call("POST", "/authorizations/%s/void" % h["authorization_id"], token=t["bob"])
    s, r5 = refund(t["bob"], p["payment_id"], {"amount": 10}, kf)
    check("same key after 409 is a first use once funds are available", s == 201, (s, r5))
    s, act = call("GET", "/activity?limit=200", token=t["ada"])
    check("non-refund payments carry refund_of null", all(("refund_of" in x) and (x["refund_of"] is None) == (x["payment_id"] not in {r["payment_id"], r3["payment_id"], r4["payment_id"], rr["payment_id"], r5["payment_id"]}) for x in act["payments"]))


def p_refund_links():
    t, (T0, T1, T2, T3) = setup()
    # request payment
    s, rq = call("POST", "/requests", {"payer_handle": "ada", "amount": 400}, token=t["bob"], key=key())
    kp = key()
    s, pay = call("POST", "/requests/%s/pay" % rq["request_id"], {"visibility": "private"}, token=t["ada"], key=kp)
    s, r = refund(t["bob"], pay["payment_id"], {"amount": 400})
    check("full refund of a request payment: private, refund_of", s == 201 and r["visibility"] == "private" and r["refund_of"] == pay["payment_id"], (s, r))
    st = [x for x in call("GET", "/requests", token=t["ada"])[1]["requests"] if x["request_id"] == rq["request_id"]][0]
    check("refund does not reopen the request (still paid)", st["status"] == "paid" and st["payment_id"] == pay["payment_id"], st)
    err("paying the request again is still request_not_pending", call("POST", "/requests/%s/pay" % rq["request_id"], {}, token=t["ada"], key=key()), 409, "request_not_pending")
    s, rep = call("POST", "/requests/%s/pay" % rq["request_id"], {"visibility": "private"}, token=t["ada"], key=kp)
    check("original pay replay unchanged", s == 200 and rep == pay)
    check("private refund hidden from third party", r["payment_id"] not in [x["payment_id"] for x in call("GET", "/activity?limit=200", token=t["cy"])[1]["payments"]])
    # capture of a final-captured hold and of an open (nonfinal) hold
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key())
    s, cap = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 300, "final": False}, token=t["bob"], key=key())
    m0 = me(t["ada"])
    s, rc = refund(t["bob"], cap["payment_id"], {"amount": 100})
    m1 = me(t["ada"])
    au = [x for x in call("GET", "/authorizations", token=t["ada"])[1]["authorizations"] if x["authorization_id"] == a["authorization_id"]][0]
    check("refund of a capture 201, refund_of set, authorization_id null", s == 201 and rc["refund_of"] == cap["payment_id"] and rc["authorization_id"] is None, (s, rc))
    check("refund does not restore the hold (held unchanged, available +100)", m1["held"] == m0["held"] == 700 and m1["available"] == m0["available"] + 100
          and au["status"] == "open" and au["remaining_amount"] == 700 and au["captured_amount"] == 300, (m0, m1, au))
    s, cap2 = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key())
    check("capture of the rest still limited to the remainder 700", s == 201 and cap2["amount"] == 700, (s, cap2))
    err("refund exceeding capture amount", refund(t["bob"], cap["payment_id"], {"amount": 201}), 422, "refund_exceeds_payment")
    s, rc2 = refund(t["bob"], cap2["payment_id"], {"amount": 700})
    au = [x for x in call("GET", "/authorizations", token=t["ada"])[1]["authorizations"] if x["authorization_id"] == a["authorization_id"]][0]
    check("refund of a final capture leaves the authorization captured", s == 201 and au["status"] == "captured" and au["captured_amount"] == 1000 and me(t["ada"])["held"] == 0, au)
    # settlement member
    stl = settle(t, [{"from_handle": "ada", "to_handle": "bob", "amount": 100}, {"from_handle": "bob", "to_handle": "cy", "amount": 40}])
    m = stl["payments"][1]
    s, rs = refund(t["cy"], m["payment_id"], {"amount": 40})
    check("refund of a settlement member 201; refund has settlement_id null", s == 201 and rs.get("settlement_id") is None and rs["refund_of"] == m["payment_id"], (s, rs))
    s, again = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 100}, {"from_handle": "bob", "to_handle": "cy", "amount": 40}]}, token=t["op"], key=key())
    act = {x["payment_id"]: x for x in call("GET", "/activity?limit=200", token=t["cy"])[1]["payments"]}
    check("member keeps settlement_id; feed shows original member amounts", act[m["payment_id"]]["settlement_id"] == stl["settlement_id"] and act[m["payment_id"]]["amount"] == 40)
    check("sum conserved", total_now(t) == TOTAL, total_now(t))


def p_refund_history():
    t, (T0, T1, T2, T3) = setup()
    s, snap = P.stmt(t["bob"], limit=200)
    tok = snap["snapshot"]
    before_known = dt.datetime.now(P.UTC)
    time.sleep(0.01)
    s, r = refund(t["bob"], "p_4", {"amount": 30})
    R = parse(r["created_at"])
    st = P.full_stmt(t["bob"])
    e = [x for x in st["entries"] if x["payment"]["payment_id"] == r["payment_id"]]
    check("refund in statement: delta -30 for refunder, revision 1 at created_at", len(e) == 1 and e[0]["delta"] == -30 and e[0]["revision"] == 1
          and e[0]["effective_at"] == r["created_at"] and e[0]["payment"]["refund_of"] == "p_4", e)
    ea = [x for x in P.full_stmt(t["ada"])["entries"] if x["payment"]["payment_id"] == r["payment_id"]]
    check("refund in sender's statement with +30", len(ea) == 1 and ea[0]["delta"] == 30, ea)
    check("as_of just before refund excludes it; at it includes it",
          me(t["bob"], as_of=iso(R - US))["balance"] == 2500 and me(t["bob"], as_of=iso(R))["balance"] == 2470)
    check("known_at before the refund: refund unknown", me(t["bob"], known=iso(before_known))["balance"] == 2500)
    s, pg = P.stmt(t["bob"], snapshot=tok, limit=200)
    check("earlier snapshot unchanged by refund", pg["entries"] == snap["entries"] and pg["closing_balance"] == snap["closing_balance"])
    views = [(iso(T0 - US), None), (iso(T2), None), (iso(R), None), (iso(R + dt.timedelta(days=1)), iso(before_known))]
    check("sum = seeded total in historical views with a refund", all(sum(me(t[h], as_of=a, known=k)["balance"] for h in t) == TOTAL for a, k in views))


# ------------------------------------------------------------------ batches

def p_batch_matrix():
    t, (T0, T1, T2, T3) = setup()
    good = [item("p_1", 1, 400, iso(T0)), item("p_4", 1, 90, iso(T3))]
    err("batch no token 401", call("POST", "/correction-batches", {"corrections": good}, key=key()), 401, "unauthenticated")
    err("batch non-operator 403", batch(t["ada"], good), 403, "forbidden")
    err("batch missing key 400", call("POST", "/correction-batches", {"corrections": good}, token=t["op"]), 400, "missing_idempotency_key")
    s, js = call("POST", "/correction-batches", raw=b"", token=t["op"], key=key())
    check("batch empty body 400", s == 400 and code(js) == "malformed_request", (s, js))
    for name, body in (("missing corrections", {}), ("empty list", {"corrections": []}), ("33 items", {"corrections": [item("p_1", 1, 1, iso(T0))] * 33}),
                       ("non-object item", {"corrections": ["x"]}), ("duplicate payment_id", {"corrections": [item("p_1", 1, 400, iso(T0)), item("p_1", 1, 300, iso(T0))]}),
                       ("corrections not a list", {"corrections": {"a": 1}})):
        s, js = call("POST", "/correction-batches", body, token=t["op"], key=key())
        check("batch %s -> 422 (or 400 for a wrong JSON type)" % name, (s == 422 and code(js) == "validation_failed") or (s == 400 and name == "corrections not a list"), (s, js))
    err("item unknown payment 404", batch(t["op"], [item("p_1", 1, 400, iso(T0)), item("nope", 1, 1, iso(T0))]), 404, "not_found")
    for name, it in (("amount -1", item("p_1", 1, -1, iso(T0))), ("reason ''", item("p_1", 1, 1, iso(T0), "")), ("effective future", item("p_1", 1, 1, iso(dt.datetime.now(P.UTC) + dt.timedelta(minutes=5)))),
                     ("revision 0", item("p_1", 0, 1, iso(T0))), ("missing reason", {k: v for k, v in item("p_1", 1, 1, iso(T0)).items() if k != "reason"})):
        err("item %s -> 422" % name, batch(t["op"], [it]), 422, "validation_failed")
    err("item stale revision 409", batch(t["op"], [item("p_1", 2, 1, iso(T0))]), 409, "stale_revision")
    # precedence: item errors in input order
    err("precedence: stale item before later 404", batch(t["op"], [item("p_1", 2, 1, iso(T0)), item("nope", 1, 1, iso(T0))]), 409, "stale_revision")
    err("precedence: 404 before later stale", batch(t["op"], [item("nope", 1, 1, iso(T0)), item("p_1", 2, 1, iso(T0))]), 404, "not_found")
    err("precedence: 422 field error before later 404", batch(t["op"], [item("p_1", 1, -1, iso(T0)), item("nope", 1, 1, iso(T0))]), 422, "validation_failed")
    # captures and refunds immutable
    s, a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, token=t["ada"], key=key())
    s, cap = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, token=t["bob"], key=key())
    s, rf = refund(t["bob"], "p_1", {"amount": 50})
    err("batch with a capture -> linked_payment_immutable", batch(t["op"], [item(cap["payment_id"], 1, 1, cap["created_at"])]), 422, "linked_payment_immutable")
    err("batch with a refund -> linked_payment_immutable", batch(t["op"], [item(rf["payment_id"], 1, 1, rf["created_at"])]), 422, "linked_payment_immutable")
    err("batch below refunded -> refund_exceeds_payment", batch(t["op"], [item("p_1", 1, 49, iso(T0))]), 422, "refund_exceeds_payment")
    # settlements: completeness and identical instants
    stl = settle(t, [{"from_handle": "ada", "to_handle": "bob", "amount": 100}, {"from_handle": "bob", "to_handle": "cy", "amount": 40},
                     {"from_handle": "ada", "to_handle": "cy", "amount": 10}])
    m = [x["payment_id"] for x in stl["payments"]]
    C = parse(stl["committed_at"])
    err("single correction of a member stays linked_payment_immutable", P.corr(t["ada"], m[0], {"expected_revision": 1, "amount": 1, "effective_at": stl["committed_at"], "reason": "x"}), 422, "linked_payment_immutable")
    err("batch with 2 of 3 members -> incomplete_settlement", batch(t["op"], [item(m[0], 1, 90, stl["committed_at"]), item(m[1], 1, 40, stl["committed_at"])]), 422, "incomplete_settlement")
    err("item error (404) precedes incompleteness", batch(t["op"], [item(m[0], 1, 90, stl["committed_at"]), item("nope", 1, 1, iso(T0))]), 404, "not_found")
    err("incompleteness precedes funds", batch(t["op"], [item(m[1], 1, 1000000000, stl["committed_at"])]), 422, "incomplete_settlement")
    eff = iso(C - dt.timedelta(minutes=1))
    err("members with differing effective instants -> 422", batch(t["op"], [item(m[0], 1, 90, eff), item(m[1], 1, 40, eff), item(m[2], 1, 10, iso(C - dt.timedelta(minutes=2)))]), 422, "validation_failed")
    alt = (C - dt.timedelta(minutes=1)).astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat(timespec="microseconds")
    alt_z = iso(C - dt.timedelta(minutes=1)).replace("+00:00", "Z")
    kb = key()
    party = {m[0]: t["ada"], m[1]: t["bob"], m[2]: t["ada"]}
    prev = max(parse(r["recorded_at"]) for x in m for r in revs(party[x], x))
    s, b = batch(t["op"], [item(m[0], 1, 90, eff), item(m[1], 1, 40, alt), item(m[2], 1, 0, alt_z), item("p_4", 1, 95, iso(T3))], kb)
    check("complete batch with one instant spelled 3 ways 201", s == 201, (s, b))
    if s == 201:
        check("response: correction_batch_id, shared recorded_at, revisions in input order with batch id",
              isinstance(b["correction_batch_id"], str) and [r["payment_id"] for r in b["revisions"]] == m + ["p_4"]
              and all(r["recorded_at"] == b["recorded_at"] and r["correction_batch_id"] == b["correction_batch_id"] and r["revision"] == 2 for r in b["revisions"])
              and [r["effective_at"] for r in b["revisions"]][:3] == [eff, alt, alt_z], b)
        check("batch recorded_at strictly after every member's previous recorded_at", parse(b["recorded_at"]) > prev)
        rv = revs(t["cy"], m[2])
        check("revisions endpoint shows correction_batch_id; revision 1 has null", rv[1]["correction_batch_id"] == b["correction_batch_id"] and rv[0].get("correction_batch_id", "MISSING") is None, rv)
        s2, b2 = batch(t["op"], [item(m[0], 1, 90, eff), item(m[1], 1, 40, alt), item(m[2], 1, 0, alt_z), item("p_4", 1, 95, iso(T3))], kb)
        check("batch replay 200 identical", s2 == 200 and b2 == b, s2)
        err("batch key reuse with different body", batch(t["op"], [item("p_4", 2, 1, iso(T3))], kb), 409, "idempotency_key_reuse")
        s, stl_rep = call("POST", "/settlements", {"transfers": stl["_transfers"]}, token=t["op"], key=stl["_key"])
        orig = {k: v for k, v in stl.items() if not k.startswith("_")}
        check("settlement replay after the batch: 200 with the original receipt", s == 200 and stl_rep == orig, (s, stl_rep))
        act = {x["payment_id"]: x for x in call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"]}
        check("feed keeps original member amounts", act[m[0]]["amount"] == 100 and act[m[2]]["amount"] == 10 and act[m[0]]["settlement_id"] == stl["settlement_id"])
        sm = {e["payment"]["payment_id"]: e for e in P.full_stmt(t["cy"])["entries"]}
        check("statement uses the batch revisions (member to cy now 0, revision 2)", sm[m[2]]["revision"] == 2 and sm[m[2]]["delta"] == 0, sm.get(m[2]))
        err("single correction of the corrected member still linked", P.corr(t["ada"], m[0], {"expected_revision": 2, "amount": 1, "effective_at": eff, "reason": "x"}), 422, "linked_payment_immutable")
        old = me(t["ada"], known=iso(parse(b["recorded_at"]) - US))
        check("known_at before the batch shows pre-batch amounts", old["balance"] == me(t["ada"])["balance"] - (10 + 10 + 5), (old["balance"], me(t["ada"])["balance"]))
    check("sum conserved", total_now(t) == TOTAL, total_now(t))


def p_batch_funds():
    t, (T0, T1, T2, T3) = setup()
    # bob 2500: increasing p_2 (bob->ada) by 1500 and p_5 (cy->bob)... use two increases on bob's sent payment and a decrease of bob-received
    # combined: each item alone affordable, together not
    s, b1 = call("POST", "/payments", {"to_handle": "ada", "amount": 100}, token=t["bob"], key=key())
    s, b2 = call("POST", "/payments", {"to_handle": "cy", "amount": 100}, token=t["bob"], key=key())
    av = me(t["bob"])["available"]   # 2300
    snapshot_state = (me(t["ada"]), me(t["bob"]), revs(t["bob"], b1["payment_id"]), revs(t["bob"], b2["payment_id"]))
    kf = key()
    items = [item(b1["payment_id"], 1, 100 + av - 100, b1["created_at"]), item(b2["payment_id"], 1, 100 + 200, b2["created_at"])]
    err("items singly affordable but not together -> insufficient_funds", batch(t["op"], items, kf), 409, "insufficient_funds")
    check("rejected batch left balances and revisions unchanged", (me(t["ada"]), me(t["bob"]), revs(t["bob"], b1["payment_id"]), revs(t["bob"], b2["payment_id"])) == snapshot_state)
    s, ok = batch(t["op"], [item(b1["payment_id"], 1, 100 + av - 100, b1["created_at"])], kf)
    check("same key after rejection is a first use (unclaimed)", s == 201, (s, ok))
    # vice versa: an increase unaffordable alone, funded by a decrease of a payment bob received in the same batch
    av = me(t["bob"])["available"]
    s, c1 = call("POST", "/payments", {"to_handle": "bob", "amount": 1000}, token=t["ada"], key=key())  # bob receives 1000
    s, c2 = call("POST", "/payments", {"to_handle": "ada", "amount": 10}, token=t["bob"], key=key())
    av = me(t["bob"])["available"]
    err("single increase beyond available is insufficient", P.corr(t["bob"], c2["payment_id"], {"expected_revision": 1, "amount": 10 + av + 500, "effective_at": c2["created_at"], "reason": "x"}), 409, "insufficient_funds")
    # combined: bob pays +av+500 more on c2, but ada's payment c1 to bob is increased by 600 first? (debits ada, credits bob)
    s, b = batch(t["op"], [item(c2["payment_id"], 1, 10 + av + 500, c2["created_at"]), item(c1["payment_id"], 1, 1600, c1["created_at"])])
    check("combined batch affordable although its first item alone is not -> 201", s == 201, (s, b))
    check("bob available exactly 100 after", me(t["bob"])["available"] == 100, me(t["bob"]))
    # funds precede history: a batch that is both currently unaffordable and historically overdrawn -> insufficient_funds
    av = me(t["cy"])["available"]
    err("insufficient_funds precedes historical_overdraft", batch(t["op"], [item("p_3", 1, 0, iso(T1))]), 409, "historical_overdraft" if av >= 300 else "insufficient_funds")
    check("sum conserved", total_now(t) == TOTAL, total_now(t))


def p_batch_history():
    t, (T0, T1, T2, T3) = setup()
    call("POST", "/payments", {"to_handle": "cy", "amount": 300}, token=t["ada"], key=key())  # cy can afford a current debit of 300
    snap_s, snap = P.stmt(t["cy"], limit=200)
    before = (me(t["cy"]), revs(t["ada"], "p_3"))
    kh = key()
    err("batch reversing p_3 at T1 overdraws cy at T2 -> historical_overdraft", batch(t["op"], [item("p_3", 1, 0, iso(T1)), item("p_4", 1, 100, iso(T3))], kh), 409, "historical_overdraft")
    check("rejected batch: no revision, balances unchanged", (me(t["cy"]), revs(t["ada"], "p_3")) == before and revs(t["ada"], "p_4")[-1]["revision"] == 1)
    s, ok = batch(t["op"], [item("p_3", 1, 300, iso(T2)), item("p_5", 1, 300, iso(T2))], kh)
    check("same-instant combined boundary batch (p_3 and p_5 both at T2) -> 201 with the unclaimed key", s == 201, (s, ok))
    s, pg = P.stmt(t["cy"], snapshot=snap["snapshot"], limit=200)
    check("snapshot taken before the batch is frozen", pg["entries"] == snap["entries"] and pg["opening_balance"] == snap["opening_balance"])
    views = [(iso(T1), None), (iso(T2), None), (iso(T2 - US), iso(parse(ok["recorded_at"]) - US)) if s == 201 else (iso(T2), None)]
    check("sum = seeded total in every view after a batch", all(sum(me(t[h], as_of=a, known=k)["balance"] for h in t) == TOTAL for a, k in views))


def p_batch_races():
    t, (T0, T1, T2, T3) = setup()

    def go(i):
        if i % 3 == 0:
            return "batch", batch(t["op"], [item("p_1", 1, 400 - i, iso(T0)), item("p_4", 1, 90, iso(T3))])
        if i % 3 == 1:
            return "single", P.corr(t["ada"], "p_1", {"expected_revision": 1, "amount": 400 - i, "effective_at": iso(T0), "reason": "s"})
        return "batch1", batch(t["op"], [item("p_1", 1, 400 - i, iso(T0))])

    with cf.ThreadPoolExecutor(30) as ex:
        rs = list(ex.map(go, range(30)))
    wins = [r for r in rs if r[1][0] == 201]
    check("30 concurrent batches/singles sharing p_1 revision 1: exactly one 201, rest 409 stale_revision",
          len(wins) == 1 and all(r[1][0] == 409 and code(r[1][1]) == "stale_revision" for r in rs if r[1][0] != 201), sorted({(r[0], r[1][0]) for r in rs}))
    rv = revs(t["ada"], "p_1")
    check("p_1 has exactly two revisions", len(rv) == 2, rv)
    # refunds racing a correction to below their total
    s, p = call("POST", "/payments", {"to_handle": "bob", "amount": 100}, token=t["ada"], key=key())

    def go2(i):
        if i < 10:
            return refund(t["bob"], p["payment_id"], {"amount": 10})
        return P.corr(t["ada"], p["payment_id"], {"expected_revision": 1, "amount": 50, "effective_at": p["created_at"], "reason": "x"})

    with cf.ThreadPoolExecutor(11) as ex:
        rs = list(ex.map(go2, range(11)))
    nref = sum(1 for r in rs[:10] if r[0] == 201)
    corr_ok = rs[10][0] == 201
    rv = revs(t["ada"], p["payment_id"])
    check("refunds vs correction race: refunded total <= current amount (%d refunds, correction %s)" % (nref, rs[10][0]),
          nref * 10 <= rv[-1]["amount"] and (corr_ok == (rv[-1]["amount"] == 50)), (nref, rs[10], rv[-1]))
    check("sum conserved after races", total_now(t) == TOTAL, total_now(t))


def p_deadline():
    """Refunds and batch debits funded by a hold expiring mid-burst: affordable at their own recorded instant."""
    bad = 0
    for kind in ("refund", "batch"):
        users = [{"id": "u_%s" % u, "email": "%s@example.com" % u, "password": "correct horse", "display_name": u, "handle": u, "balance": b} for u, b in (("ada", 100 if kind == "batch" else 0), ("bob", 1000), ("op", 0))]
        P.reset({"currency": "JPY", "minor_units": 0, "authorization_ttl_seconds": 1, "users": users, "settlement_operator_ids": ["u_op"]})
        ta, tb, to = P.login("ada"), P.login("bob"), P.login("op")
        if kind == "refund":   # ada receives 40 x 1, then holds all 40
            pays = [call("POST", "/payments", {"to_handle": "ada", "amount": 1}, token=tb, key=key())[1] for _ in range(40)]
            s, ah = call("POST", "/authorizations", {"to_handle": "bob", "amount": 40}, token=ta, key=key())
        else:                  # ada sends 40 x 1 (60 left), then holds all 60
            pays = [call("POST", "/payments", {"to_handle": "bob", "amount": 1}, token=ta, key=key())[1] for _ in range(40)]
            s, ah = call("POST", "/authorizations", {"to_handle": "bob", "amount": 60}, token=ta, key=key())
        E = parse(ah["expires_at"])
        while dt.datetime.now(P.UTC) < E - dt.timedelta(milliseconds=30):
            time.sleep(0.002)
        if kind == "refund":
            one = lambda p: refund(ta, p["payment_id"], {"amount": 1})
        else:
            # increase debits sender ada by 1, effective at the hold deadline (before it: effective_at in the future -> 422)
            one = lambda p: batch(to, [item(p["payment_id"], 1, 2, ah["expires_at"])])

        def fn(p):   # keep trying across the deadline until accepted or 200 ms after it
            last = None
            while dt.datetime.now(P.UTC) < E + dt.timedelta(milliseconds=200):
                last = one(p)
                if last[0] == 201:
                    return last
                time.sleep(0.001)
            return last
        with cf.ThreadPoolExecutor(30) as ex:
            rs = list(ex.map(fn, pays))
        ok = [r[1] for r in rs if r[0] == 201]
        for o in ok:
            at = o["created_at"] if kind == "refund" else o["recorded_at"]
            if parse(at) < E:
                bad += 1
                print("  %s accepted at %s before hold deadline %s" % (kind, at, ah["expires_at"]))
            m = me(ta, as_of=at)
            if m["available"] < 0:
                bad += 1
                print("  %s at %s: historical available %d" % (kind, at, m["available"]))
        for at in (ah["created_at"], ah["expires_at"]):
            m = me(ta, as_of=at)
            if m["available"] < 0 or m["total"] < 0:
                bad += 1
                print("  %s: view at %s total %d available %d" % (kind, at, m["total"], m["available"]))
        codes = sorted({(s, code(js)) for s, js in rs if s != 201})
        print("  %s: %d accepted (all at/after expires_at), others %s" % (kind, len(ok), codes))
    check("refunds/batches funded by an expiring hold: none before the deadline, none historically unaffordable", bad == 0, bad)


# ------------------------------------------------------------------ imports

def p_imports():
    if S3:
        fx, (T0, T1, T2, T3) = P.base_fixture(dt.datetime.now(P.UTC))
        P.reset(fx, base=S3)
        t = P.toks(base=S3)
        stl = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 100}, {"from_handle": "bob", "to_handle": "cy", "amount": 40}]}, token=t["op"], key=key(), base=S3)[1]
        kc = key()
        s, c = call("POST", "/payments/p_1/corrections", {"expected_revision": 1, "amount": 450, "effective_at": iso(T0), "reason": "x"}, token=t["ada"], key=kc, base=S3)
        s, snap = call("GET", "/statement?limit=200", token=t["ada"], base=S3)
        s, exp = call("GET", "/_test/export", base=S3)
        s, js = call("POST", "/_test/import", exp)
        check("stage-3 export imports into stage 4", s == 204, (s, js))
        s, pg = P.stmt(t["ada"], snapshot=snap["snapshot"], limit=200)
        check("stage-3 snapshot pages identically after upgrade", s == 200 and pg["entries"] == snap["entries"] and pg["closing_balance"] == snap["closing_balance"], (s, pg))
        rv = revs(t["bob"], "p_1")
        check("stage-3 corrections kept (2 revisions, batch id null)", [r["revision"] for r in rv] == [1, 2] and rv[1].get("correction_batch_id") is None, rv)
        s, rp = P.corr(t["ada"], "p_1", {"expected_revision": 1, "amount": 450, "effective_at": iso(T0), "reason": "x"}, kc)
        check("stage-3 correction replay 200 after upgrade", s == 200 and rp["revision"] == 2, (s, rp))
        m = [x["payment_id"] for x in stl["payments"]]
        err("membership kept: single correction of member linked", P.corr(t["ada"], m[0], {"expected_revision": 1, "amount": 1, "effective_at": stl["committed_at"], "reason": "x"}), 422, "linked_payment_immutable")
        err("membership kept: incomplete batch", batch(t["op"], [item(m[0], 1, 50, stl["committed_at"])]), 422, "incomplete_settlement")
        s, b = batch(t["op"], [item(m[0], 1, 50, stl["committed_at"]), item(m[1], 1, 40, stl["committed_at"])])
        check("imported settlement correctable as a complete batch", s == 201, (s, b))
        s, r = refund(t["bob"], "p_1", {"amount": 450})
        check("imported corrected payment refundable up to corrected amount", s == 201, (s, r))
        check("sum conserved after stage-3 import + batch + refund", total_now(t) == TOTAL, total_now(t))
    # stage-4 round trip
    t, (T0, T1, T2, T3) = setup()
    stl = settle(t, [{"from_handle": "ada", "to_handle": "bob", "amount": 100}])
    kb, kr = key(), key()
    s, b = batch(t["op"], [item(stl["payments"][0]["payment_id"], 1, 70, stl["committed_at"]), item("p_4", 1, 80, iso(T3))], kb)
    s, r = refund(t["bob"], "p_1", {"amount": 25}, kr)
    s, snap = P.stmt(t["bob"], limit=200)
    views = {h: me(t[h], as_of=iso(T2), known=iso(parse(b["recorded_at"]) - US)) for h in t}
    s, exp = call("GET", "/_test/export")
    P.reset(P.base_fixture(dt.datetime.now(P.UTC))[0])
    s, _ = call("POST", "/_test/import", exp)
    check("stage-4 round trip: historical views identical", {h: me(t[h], as_of=iso(T2), known=iso(parse(b["recorded_at"]) - US)) for h in t} == views)
    s, b2 = batch(t["op"], [item(stl["payments"][0]["payment_id"], 1, 70, stl["committed_at"]), item("p_4", 1, 80, iso(T3))], kb)
    s3, r2 = refund(t["bob"], "p_1", {"amount": 25}, kr)
    check("batch and refund replays 200 identical after import", s == 200 and b2 == b and s3 == 200 and r2 == r, (s, s3))
    check("batch ids and refund links preserved", revs(t["ada"], "p_4")[1]["correction_batch_id"] == b["correction_batch_id"]
          and [x for x in call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"] if x["payment_id"] == r["payment_id"]][0]["refund_of"] == "p_1")
    s, pg = P.stmt(t["bob"], snapshot=snap["snapshot"], limit=200)
    check("snapshot survives stage-4 round trip", s == 200 and pg["entries"] == snap["entries"])
    err("refund cap still enforced after import (475 left of 500)", refund(t["bob"], "p_1", {"amount": 476}), 422, "refund_exceeds_payment")
    s, b3 = batch(t["op"], [item("p_4", 2, 81, iso(T3))])
    check("new batch after import recorded strictly after imported revisions", s == 201 and parse(b3["recorded_at"]) > parse(b["recorded_at"]), (s, b3))


def p_concurrency():
    t, (T0, T1, T2, T3) = setup()
    ps = [call("POST", "/payments", {"to_handle": "bob", "amount": 10}, token=t["ada"], key=key())[1] for _ in range(20)]
    k = key()

    def go(i):
        if i < 20:
            return refund(t["bob"], ps[i]["payment_id"], {"amount": 10})
        if i < 40:
            return refund(t["bob"], ps[0]["payment_id"], {"amount": 10}, k)
        return P.stmt(t["ada"], limit=5)

    with cf.ThreadPoolExecutor(50) as ex:
        rs = list(ex.map(go, range(50)))
    check("50 in flight (refunds, identical refund retries, statements): no 5xx", all(r[0] < 500 for r in rs), sorted({r[0] for r in rs}))
    same = [r for r in rs[20:40]] + [rs[0]]
    check("identical refund retries resolve to one refund", len({json.dumps(r[1], sort_keys=True) for r in rs[20:40] if r[0] in (200, 201)}) <= 2)
    check("sum conserved", total_now(t) == TOTAL, total_now(t))


PROBES = [p_refunds, p_refund_links, p_refund_history, p_batch_matrix, p_batch_funds, p_batch_history, p_batch_races, p_deadline, p_imports, p_concurrency]


def main():
    global S3
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--stage3-base", default=None)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    P.BASE, S3 = a.base, a.stage3_base
    for p in PROBES:
        if a.only and a.only not in p.__name__:
            continue
        try:
            p()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(p.__name__ + " crashed", False, repr(e))
    fails = [n for n, ok in P.RESULTS if not ok]
    print("TOTAL %d PASS %d FAIL %d" % (len(P.RESULTS), len(P.RESULTS) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
