"""Own tests for the stage-1 service. Run: python3 test_service.py [base_url]

Without an argument the server is started in-process on a free port.
"""
import http.client
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

BASE = sys.argv.pop(1) if len(sys.argv) > 1 and sys.argv[1].startswith("http") else None

if BASE is None:
    import server
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE = "http://127.0.0.1:%d" % srv.server_address[1]
HOST = urlsplit(BASE).netloc


def call(method, path, body=None, token=None, key=None, raw=None, headers=None):
    c = http.client.HTTPConnection(HOST, timeout=10)
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    if key is not None:
        h["Idempotency-Key"] = key
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    text = r.read()
    c.close()
    return r.status, (json.loads(text) if text else None), r.getheader("Content-Type")


def user(handle, balance, uid=None):
    return {"id": uid or "u_" + handle, "email": handle + "@example.com", "password": "correct horse",
            "display_name": handle.title(), "handle": handle, "balance": balance}


def fixture(users=None, currency="EUR", minor_units=2, payments=None, requests=None, ops=None):
    fx = {"currency": currency, "minor_units": minor_units,
          "users": users or [user("ada", 10000), user("bob", 2500), user("cy", 500)],
          "payments": payments or [], "requests": requests or []}
    if ops is not None:
        fx["settlement_operator_ids"] = ops
    return fx


def reset(fx=None):
    s, b, _ = call("POST", "/_test/reset", fx or fixture())
    assert s == 204, (s, b)


def login(handle):
    s, b, _ = call("POST", "/auth/login", {"email": handle + "@example.com", "password": "correct horse"})
    assert s == 200, (s, b)
    return b["token"]


def balances(tokens):
    return {h: call("GET", "/me", token=t)[1]["balance"] for h, t in tokens.items()}


class Base(unittest.TestCase):
    fx = None

    def setUp(self):
        reset(self.fx() if self.fx else None)
        self.t = {h: login(h) for h in ("ada", "bob", "cy")}

    def err(self, resp, status, code):
        self.assertEqual(resp[0], status, resp)
        self.assertEqual(resp[1]["error"]["code"], code, resp)
        self.assertIn("message", resp[1]["error"])


class Runtime(Base):
    def test_health_and_content_type(self):
        s, b, ct = call("GET", "/health")
        self.assertEqual((s, b), (200, {"status": "ok"}))
        self.assertEqual(ct, "application/json; charset=utf-8")

    def test_unknown_route_is_404_with_error_body(self):
        self.err(call("GET", "/nope", token=self.t["ada"]), 404, "not_found")

    def test_me(self):
        s, b, _ = call("GET", "/me", token=self.t["ada"])
        self.assertEqual(b, {"user_id": "u_ada", "display_name": "Ada", "handle": "ada",
                             "balance": 10000, "total": 10000, "available": 10000, "held": 0,
                             "currency": "EUR", "minor_units": 2})

    def test_auth_errors(self):
        self.err(call("GET", "/me"), 401, "unauthenticated")
        self.err(call("GET", "/me", token="nope"), 401, "unauthenticated")
        self.err(call("GET", "/me", headers={"Authorization": "Basic abc"}), 401, "unauthenticated")


class Reset(Base):
    def test_negative_balance_rejected_state_unchanged(self):
        call("POST", "/payments", {"to_handle": "bob", "amount": 1}, self.t["ada"], "k")
        self.err(call("POST", "/_test/reset", fixture(users=[user("ada", -1)])), 422, "validation_failed")
        self.assertEqual(call("GET", "/me", token=self.t["ada"])[1]["balance"], 9999)

    def test_malformed_fixtures(self):
        bads = [
            fixture(minor_units=1), fixture(users=[user("ada", 1), user("ada", 2, uid="u_x")]),
            fixture(users=[user("Ada", 1)]), fixture(users=[dict(user("ada", 1), balance="1")]),
            fixture(payments=[{"id": "p", "from_user_id": "u_zz", "to_user_id": "u_bob", "amount": 1}]),
            fixture(requests=[{"id": "r", "requester_id": "u_bob", "payer_id": "u_zz", "amount": 1}]),
            fixture(users=[user("ada", 1), dict(user("bob", 1), email="ada@example.com")]),
            fixture(users=[user("ada", 1), user("bob", 1, uid="u_ada")]),
        ]
        for fx in bads:
            s = call("POST", "/_test/reset", fx)[0]
            self.assertTrue(400 <= s < 500, (s, fx))
        self.assertEqual(call("GET", "/me", token=self.t["ada"])[1]["balance"], 10000)
        self.err(call("POST", "/_test/reset", raw=b"{nope"), 400, "malformed_request")

    def test_reset_invalidates_old_tokens(self):
        old = self.t["ada"]
        reset()
        self.err(call("GET", "/me", token=old), 401, "unauthenticated")

    def test_seeded_state_readable(self):
        fx = fixture(
            payments=[{"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500,
                       "note": "coffee", "visibility": "public"},
                      {"id": "p_2", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 7,
                       "note": "secret", "visibility": "private"}],
            requests=[{"id": "rq_1", "requester_id": "u_bob", "payer_id": "u_ada", "amount": 1200,
                       "note": "taxi", "status": "pending"},
                      {"id": "rq_2", "requester_id": "u_bob", "payer_id": "u_ada", "amount": 500,
                       "note": "x", "status": "paid", "payment_id": "p_1"}])
        reset(fx)
        t = {h: login(h) for h in ("ada", "bob", "cy")}
        self.assertEqual(balances(t), {"ada": 10000, "bob": 2500, "cy": 500})  # not replayed
        ids = [p["payment_id"] for p in call("GET", "/activity", token=t["cy"])[1]["payments"]]
        self.assertEqual(ids, ["p_1"])
        ids = [p["payment_id"] for p in call("GET", "/activity", token=t["bob"])[1]["payments"]]
        self.assertEqual(ids, ["p_2", "p_1"])
        reqs = call("GET", "/requests", token=t["ada"])[1]["requests"]
        self.assertEqual({r["request_id"]: (r["status"], r["payment_id"]) for r in reqs},
                         {"rq_1": ("pending", None), "rq_2": ("paid", "p_1")})
        self.assertEqual(call("GET", "/requests", token=t["cy"])[1]["requests"], [])
        s, p, _ = call("POST", "/requests/rq_1/pay", {}, t["ada"], "k")
        self.assertEqual(s, 201)
        self.assertEqual(balances(t), {"ada": 8800, "bob": 3700, "cy": 500})
        self.err(call("POST", "/requests/rq_2/pay", {}, t["ada"], "k2"), 409, "request_not_pending")


class Auth(Base):
    def test_signup_login_and_derived_handle(self):
        s, b, _ = call("POST", "/auth/signup", {"email": "Dee.Dee+X@Example.com", "password": "12345678",
                                                "display_name": "Dee"})
        self.assertEqual(s, 201, b)
        self.assertEqual(set(b), {"user_id", "display_name", "token"})
        me = call("GET", "/me", token=b["token"])[1]
        self.assertEqual((me["handle"], me["balance"]), ("dee_dee_x", 0))
        s, b2, _ = call("POST", "/auth/login", {"email": "Dee.Dee+X@Example.com", "password": "12345678"})
        self.assertEqual((s, b2["user_id"]), (200, b["user_id"]))
        self.assertEqual(call("GET", "/me", token=b["token"])[0], 200)  # both tokens valid
        # can receive money and be asked immediately
        self.assertEqual(call("POST", "/payments", {"to_handle": "dee_dee_x", "amount": 5},
                              self.t["ada"], "a")[0], 201)
        self.assertEqual(call("POST", "/requests", {"payer_handle": "dee_dee_x", "amount": 5},
                              self.t["ada"], "a")[0], 201)

    def test_handle_truncated_to_20(self):
        s, b, _ = call("POST", "/auth/signup", {"email": "a" * 30 + "@x.io", "password": "12345678",
                                                "display_name": "A"})
        self.assertEqual(call("GET", "/me", token=b["token"])[1]["handle"], "a" * 20)

    def test_signup_errors(self):
        ok = {"email": "new@example.com", "password": "12345678", "display_name": "N"}
        self.err(call("POST", "/auth/signup", dict(ok, email="ada@example.com")), 409, "email_taken")
        self.err(call("POST", "/auth/signup", dict(ok, password="1234567")), 422, "validation_failed")
        self.err(call("POST", "/auth/signup", dict(ok, email="nope")), 422, "validation_failed")
        self.err(call("POST", "/auth/signup", dict(ok, email="ada@other.org")), 409, "handle_taken")
        self.err(call("POST", "/auth/login", {"email": "ada@other.org", "password": "correct horse"}),
                 401, "unauthenticated")  # no account was created
        self.err(call("POST", "/auth/signup", dict(ok, email=5)), 400, "malformed_request")
        self.err(call("POST", "/auth/signup", raw=b"{"), 400, "malformed_request")

    def test_login_errors(self):
        self.err(call("POST", "/auth/login", {"email": "ada@example.com", "password": "wrong horse"}),
                 401, "unauthenticated")
        self.err(call("POST", "/auth/login", {"email": "zz@example.com", "password": "correct horse"}),
                 401, "unauthenticated")

    def test_password_not_stored_plaintext(self):
        dump = json.dumps(call("GET", "/_test/export")[1])
        self.assertNotIn("correct horse", dump)

    def test_concurrent_logins_fast(self):
        import time
        t0 = time.time()
        with ThreadPoolExecutor(50) as ex:
            res = list(ex.map(lambda _: call("POST", "/auth/login", {"email": "ada@example.com",
                                                                     "password": "correct horse"})[0],
                              range(50)))
        self.assertEqual(res, [200] * 50)
        self.assertLess(time.time() - t0, 5)


class EmptyBody(Base):
    """Regression: an empty or non-object body is 400 malformed_request (ruling fd77ec53)."""

    def test_empty_and_non_object_bodies(self):
        a, op = self.t["ada"], self.t["ada"]
        rq = call("POST", "/requests", {"payer_handle": "ada", "amount": 5}, self.t["bob"], "r")[1]
        authed = ["/payments", "/requests", "/splits", "/settlements"]
        public = ["/auth/signup", "/auth/login", "/_test/reset", "/_test/import"]
        for raw in (b"", b"[]", b"5", b"null", b'"x"'):
            for path in authed:
                with self.subTest(path=path, raw=raw):
                    s, b, _ = call("POST", path, raw=raw, token=a, key="k1",
                                   headers={"Content-Type": "application/json"})
                    if path == "/settlements":
                        self.assertIn(s, (400, 403))  # ada is not an operator here
                    else:
                        self.err((s, b, None), 400, "malformed_request")
            for path in public:
                with self.subTest(path=path, raw=raw):
                    self.err(call("POST", path, raw=raw), 400, "malformed_request")
            if raw:
                self.err(call("POST", "/requests/%s/pay" % rq["request_id"], raw=raw, token=a, key="k1"),
                         400, "malformed_request")
        self.assertEqual(balances(self.t), {"ada": 10000, "bob": 2500, "cy": 500})
        # pay's body is optional: empty is {} (same value as {} for idempotency)
        path = "/requests/%s/pay" % rq["request_id"]
        s1, p1, _ = call("POST", path, raw=b"", token=a, key="P")
        s2, p2, _ = call("POST", path, {}, a, "P")
        self.assertEqual((s1, s2, p1["visibility"], p1), (201, 200, "public", p2))


class Payments(Base):
    def test_payment_body_and_balances(self):
        s, p, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1500, "note": "dinner",
                                             "visibility": "public"}, self.t["ada"], "k1")
        self.assertEqual(s, 201)
        for k, v in {"from_user_id": "u_ada", "from_handle": "ada", "to_user_id": "u_bob", "to_handle": "bob",
                     "amount": 1500, "currency": "EUR", "note": "dinner", "visibility": "public",
                     "request_id": None, "settlement_id": None}.items():
            self.assertEqual(p[k], v)
        self.assertRegex(p["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$")
        self.assertLessEqual(len(p["payment_id"]), 64)
        self.assertEqual(balances(self.t), {"ada": 8500, "bob": 4000, "cy": 500})

    def test_defaults_and_numeric_forms(self):
        for i, amt in enumerate([1000, 1000.0, 1e3]):
            s, p, _ = call("POST", "/payments", {"to_handle": "bob", "amount": amt}, self.t["ada"], "n%d" % i)
            self.assertEqual((s, p["amount"], p["note"], p["visibility"]), (201, 1000, "", "public"))
        s, p, _ = call("POST", "/payments", raw=b'{"to_handle":"bob","amount":1E3}', token=self.t["ada"], key="e")
        self.assertEqual((s, p["amount"]), (201, 1000))

    def test_payment_errors(self):
        a = self.t["ada"]
        cases = [
            ({"to_handle": "bob", "amount": 0}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": 1000000001}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": 1.5}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": "5"}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": True}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": None}, 422, "validation_failed"),
            ({"to_handle": "bob"}, 422, "validation_failed"),
            ({"amount": 5}, 422, "validation_failed"),
            ({"to_handle": "ada", "amount": 5}, 422, "self_payment"),
            ({"to_handle": "bob", "amount": 5, "note": "x" * 201}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": 5, "note": None}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": 5, "note": 5}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": 5, "visibility": "friends"}, 422, "validation_failed"),
            ({"to_handle": "bob", "amount": 5, "visibility": None}, 422, "validation_failed"),
            ({"to_handle": "zed", "amount": 5}, 404, "not_found"),
            ({"to_handle": 7, "amount": 5}, 400, "malformed_request"),
            ({"to_handle": "bob", "amount": 10001}, 409, "insufficient_funds"),
        ]
        for i, (body, st, code) in enumerate(cases):
            with self.subTest(body=body):
                self.err(call("POST", "/payments", body, a, "e%d" % i), st, code)
        self.err(call("POST", "/payments", raw=b"[1]", token=a, key="x"), 400, "malformed_request")
        self.err(call("POST", "/payments", raw=b"{bad", token=a, key="x"), 400, "malformed_request")
        self.err(call("POST", "/payments", raw=b'{"to_handle":"bob","amount":NaN}', token=a, key="x"),
                 400, "malformed_request")
        for amt in (b"1e999999999", b"-1e999999999", b"1e-999999999", b"1" + b"0" * 5000, b"0.5"):
            self.err(call("POST", "/payments", raw=b'{"to_handle":"bob","amount":%s}' % amt, token=a, key="x"),
                     422, "validation_failed")
        deep = b'{"a":' + b"[" * 100000 + b"]" * 100000 + b"}"
        self.err(call("POST", "/payments", raw=deep, token=a, key="x"), 400, "malformed_request")
        self.assertEqual(balances(self.t), {"ada": 10000, "bob": 2500, "cy": 500})
        self.assertEqual(call("GET", "/activity", token=a)[1]["payments"], [])

    def test_note_exactly_200_and_unicode_verbatim(self):
        note = "  ünïcødé 💸 <b>&amp;</b> \u200b " + "x" * 160
        note = note[:200]
        s, p, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 1, "note": note}, self.t["ada"], "u")
        self.assertEqual((s, p["note"]), (201, note))
        feed = call("GET", "/activity", token=self.t["bob"])[1]["payments"]
        self.assertEqual(feed[0]["note"], note)

    def test_exact_balance_payment_allowed(self):
        self.assertEqual(call("POST", "/payments", {"to_handle": "ada", "amount": 500}, self.t["cy"], "k")[0], 201)
        self.err(call("POST", "/payments", {"to_handle": "ada", "amount": 1}, self.t["cy"], "k2"),
                 409, "insufficient_funds")


class Idempotency(Base):
    def test_header_rules(self):
        a = self.t["ada"]
        body = {"to_handle": "bob", "amount": 5}
        self.err(call("POST", "/payments", body, a), 400, "missing_idempotency_key")
        self.err(call("POST", "/payments", body, a, ""), 400, "missing_idempotency_key")
        self.err(call("POST", "/payments", body, a, "k" * 256), 422, "validation_failed")
        self.assertEqual(call("POST", "/payments", body, a, "k" * 255)[0], 201)
        self.err(call("POST", "/payments", body, None, "k" * 256), 401, "unauthenticated")
        self.err(call("POST", "/payments", body, None), 401, "unauthenticated")

    def test_replay_reuse_and_scope(self):
        a, b = self.t["ada"], self.t["bob"]
        s1, p1, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 5, "note": "n"}, a, "K")
        s2, p2, _ = call("POST", "/payments", raw=b'{ "note":"n",  "amount":5.0, "to_handle":"bob"}', token=a, key="K")
        self.assertEqual((s1, s2, p1), (201, 200, p2))
        self.err(call("POST", "/payments", {"to_handle": "bob", "amount": 6}, a, "K"), 409, "idempotency_key_reuse")
        # invalid body with a claimed key is still reuse (§7 last paragraph)
        self.err(call("POST", "/payments", {"to_handle": "bob", "amount": -1}, a, "K"), 409, "idempotency_key_reuse")
        # other user, same key: independent
        self.assertEqual(call("POST", "/payments", {"to_handle": "ada", "amount": 5, "note": "n"}, b, "K")[0], 201)
        # same key, same body, other path: a new request
        self.assertEqual(call("POST", "/requests", {"payer_handle": "bob", "amount": 5, "note": "n"}, a, "K")[0], 201)
        self.assertEqual(balances(self.t), {"ada": 10000, "bob": 2500, "cy": 500})

    def test_failed_key_reusable(self):
        a = self.t["cy"]
        self.err(call("POST", "/payments", {"to_handle": "bob", "amount": 501}, a, "F"), 409, "insufficient_funds")
        self.assertEqual(call("POST", "/payments", {"to_handle": "bob", "amount": 1}, a, "F")[0], 201)

    def test_concurrent_identical_one_effect(self):
        a = self.t["ada"]
        body = {"to_handle": "bob", "amount": 100}
        with ThreadPoolExecutor(30) as ex:
            res = list(ex.map(lambda _: call("POST", "/payments", body, a, "C"), range(30)))
        self.assertEqual(sorted(r[0] for r in res), [200] * 29 + [201])
        self.assertEqual(len({json.dumps(r[1], sort_keys=True) for r in res}), 1)
        self.assertEqual(balances(self.t)["ada"], 9900)

    def test_concurrent_failures_do_not_claim(self):
        c = self.t["cy"]
        with ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda _: call("POST", "/payments", {"to_handle": "bob", "amount": 501}, c, "Z")[0],
                              range(20)))
        self.assertEqual(res, [409] * 20)
        self.assertEqual(call("POST", "/payments", {"to_handle": "bob", "amount": 500}, c, "Z")[0], 201)

    def test_pay_replay_after_paid(self):
        rq = call("POST", "/requests", {"payer_handle": "ada", "amount": 300}, self.t["bob"], "r")[1]
        path = "/requests/%s/pay" % rq["request_id"]
        s1, p1, _ = call("POST", path, {"visibility": "private"}, self.t["ada"], "P")
        s2, p2, _ = call("POST", path, {"visibility": "private"}, self.t["ada"], "P")
        self.assertEqual((s1, s2, p1), (201, 200, p2))
        self.assertEqual((p1["request_id"], p1["visibility"]), (rq["request_id"], "private"))
        self.err(call("POST", path, {}, self.t["ada"], "P"), 409, "idempotency_key_reuse")
        self.err(call("POST", path, {}, self.t["ada"], "P2"), 409, "request_not_pending")
        self.assertEqual(balances(self.t), {"ada": 9700, "bob": 2800, "cy": 500})

    def test_concurrent_pay_distinct_keys_moves_once(self):
        rq = call("POST", "/requests", {"payer_handle": "ada", "amount": 300}, self.t["bob"], "r")[1]
        path = "/requests/%s/pay" % rq["request_id"]
        with ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda i: call("POST", path, {}, self.t["ada"], "k%d" % i)[0], range(20)))
        self.assertEqual(sorted(res), [201] + [409] * 19)
        self.assertEqual(balances(self.t), {"ada": 9700, "bob": 2800, "cy": 500})


class Concurrency(Base):
    def test_burst_conserves_and_never_negative(self):
        users = [user("u%d" % i, 1000) for i in range(10)]
        reset(fixture(users=users))
        toks = {u["handle"]: login(u["handle"]) for u in users}
        import random
        rnd = random.Random(7)
        jobs = []
        for i in range(400):
            a, b = rnd.sample(range(10), 2)
            jobs.append(("u%d" % a, "u%d" % b, rnd.randint(1, 400), "j%d" % i))

        def go(j):
            return call("POST", "/payments", {"to_handle": j[1], "amount": j[2]}, toks[j[0]], j[3])[0]

        with ThreadPoolExecutor(50) as ex:
            res = list(ex.map(go, jobs))
        self.assertTrue(set(res) <= {201, 409}, set(res))
        bal = balances(toks)
        self.assertEqual(sum(bal.values()), 10000)
        self.assertTrue(all(v >= 0 for v in bal.values()))


class Requests(Base):
    def mk(self, amount=1200, payer="ada", requester="bob", key="r"):
        s, r, _ = call("POST", "/requests", {"payer_handle": payer, "amount": amount, "note": "taxi"},
                       self.t[requester], key)
        self.assertEqual(s, 201, r)
        return r

    def test_create_body_and_errors(self):
        r = self.mk()
        for k, v in {"requester_id": "u_bob", "requester_handle": "bob", "payer_id": "u_ada", "payer_handle": "ada",
                     "amount": 1200, "currency": "EUR", "note": "taxi", "status": "pending",
                     "payment_id": None}.items():
            self.assertEqual(r[k], v)
        b = self.t["bob"]
        self.err(call("POST", "/requests", {"payer_handle": "bob", "amount": 5}, b, "1"), 422, "self_request")
        self.err(call("POST", "/requests", {"payer_handle": "ada", "amount": 0}, b, "2"), 422, "validation_failed")
        self.err(call("POST", "/requests", {"payer_handle": "ada", "amount": 10 ** 9 + 1}, b, "3"), 422,
                 "validation_failed")
        self.err(call("POST", "/requests", {"payer_handle": "ada", "amount": 5, "note": "x" * 201}, b, "4"), 422,
                 "validation_failed")
        self.err(call("POST", "/requests", {"payer_handle": "zz", "amount": 5}, b, "5"), 404, "not_found")
        # exceeding payer balance is fine
        self.mk(amount=10 ** 9, payer="cy", requester="bob", key="6")

    def test_short_payer_can_pay_later(self):
        r = self.mk(amount=600, payer="cy", requester="bob")
        path = "/requests/%s/pay" % r["request_id"]
        self.err(call("POST", path, {}, self.t["cy"], "p"), 409, "insufficient_funds")
        call("POST", "/payments", {"to_handle": "cy", "amount": 100}, self.t["ada"], "top")
        self.assertEqual(call("POST", path, {}, self.t["cy"], "p")[0], 201)  # failed key reusable

    def test_pay_permissions(self):
        r = self.mk()
        path = "/requests/%s/pay" % r["request_id"]
        self.err(call("POST", path, {}, self.t["bob"], "p"), 403, "forbidden")
        self.err(call("POST", path, {}, self.t["cy"], "p"), 403, "forbidden")
        self.err(call("POST", "/requests/nope/pay", {}, self.t["ada"], "p"), 404, "not_found")
        self.err(call("POST", path, {"visibility": "x"}, self.t["ada"], "p"), 422, "validation_failed")

    def test_decline_cancel(self):
        r = self.mk()
        d = "/requests/%s/decline" % r["request_id"]
        c = "/requests/%s/cancel" % r["request_id"]
        self.err(call("POST", d, None, self.t["bob"]), 403, "forbidden")
        self.err(call("POST", c, None, self.t["ada"]), 403, "forbidden")
        s, b, _ = call("POST", d, None, self.t["ada"])
        self.assertEqual((s, b["status"]), (200, "declined"))
        self.assertEqual(call("POST", d, None, self.t["ada"])[1]["status"], "declined")
        self.err(call("POST", c, None, self.t["bob"]), 409, "request_not_pending")
        self.err(call("POST", "/requests/%s/pay" % r["request_id"], {}, self.t["ada"], "p"), 409,
                 "request_not_pending")
        r2 = self.mk(key="r2")
        c2 = "/requests/%s/cancel" % r2["request_id"]
        self.assertEqual(call("POST", c2, None, self.t["bob"])[1]["status"], "cancelled")
        self.assertEqual(call("POST", c2, None, self.t["bob"])[0], 200)
        self.err(call("POST", "/requests/%s/decline" % r2["request_id"], None, self.t["ada"]), 409,
                 "request_not_pending")
        r3 = self.mk(key="r3")
        call("POST", "/requests/%s/pay" % r3["request_id"], {}, self.t["ada"], "p3")
        self.err(call("POST", "/requests/%s/decline" % r3["request_id"], None, self.t["ada"]), 409,
                 "request_not_pending")
        self.err(call("POST", "/requests/%s/cancel" % r3["request_id"], None, self.t["bob"]), 409,
                 "request_not_pending")
        self.err(call("POST", "/requests/nope/cancel", None, self.t["bob"]), 404, "not_found")

    def test_list_filters_and_paging(self):
        ids = [self.mk(amount=i + 1, key="k%d" % i)["request_id"] for i in range(5)]
        self.mk(payer="bob", requester="ada", key="out")
        s, b, _ = call("GET", "/requests?direction=incoming&limit=2", token=self.t["ada"])
        self.assertEqual([r["request_id"] for r in b["requests"]], ids[::-1][:2])
        self.assertTrue(b["has_more"])
        b = call("GET", "/requests?direction=incoming&limit=2&offset=4", token=self.t["ada"])[1]
        self.assertEqual(([r["request_id"] for r in b["requests"]], b["has_more"]), ([ids[0]], False))
        self.assertEqual(len(call("GET", "/requests?direction=outgoing", token=self.t["ada"])[1]["requests"]), 1)
        self.assertEqual(len(call("GET", "/requests?status=pending&x=1", token=self.t["ada"])[1]["requests"]), 6)
        self.assertEqual(len(call("GET", "/requests?status=paid", token=self.t["ada"])[1]["requests"]), 0)
        self.assertEqual(call("GET", "/requests", token=self.t["cy"])[1], {"requests": [], "has_more": False})
        for qs in ("limit=0", "limit=201", "limit=1e9", "limit=4.0", "limit=+4", "offset=-1", "limit=",
                   "direction=sideways", "status=open", "offset=abc"):
            self.err(call("GET", "/requests?" + qs, token=self.t["ada"]), 422, "validation_failed")
        self.assertEqual(call("GET", "/requests?limit=200&offset=0", token=self.t["ada"])[0], 200)


class Feed(Base):
    def test_visibility_rules(self):
        call("POST", "/payments", {"to_handle": "bob", "amount": 1, "visibility": "private"}, self.t["ada"], "a")
        call("POST", "/payments", {"to_handle": "cy", "amount": 2}, self.t["ada"], "b")
        r = call("POST", "/requests", {"payer_handle": "ada", "amount": 3}, self.t["bob"], "r")[1]
        feed = lambda h: [p["amount"] for p in call("GET", "/activity", token=self.t[h])[1]["payments"]]
        self.assertEqual(feed("ada"), [2, 1])
        self.assertEqual(feed("bob"), [2, 1])
        self.assertEqual(feed("cy"), [2])
        call("POST", "/requests/%s/pay" % r["request_id"], {"visibility": "private"}, self.t["ada"], "p")
        self.assertEqual(feed("cy"), [2])
        self.assertEqual(feed("bob"), [3, 2, 1])

    def test_paging_same_second_consistent(self):
        for i in range(25):
            call("POST", "/payments", {"to_handle": "bob", "amount": 1, "note": str(i)}, self.t["ada"], "k%d" % i)
        seen = []
        for off in range(0, 25, 7):
            seen += [p["note"] for p in call("GET", "/activity?limit=7&offset=%d" % off,
                                             token=self.t["cy"])[1]["payments"]]
        self.assertEqual(seen, [str(i) for i in range(24, -1, -1)])
        self.err(call("GET", "/activity?limit=201", token=self.t["cy"]), 422, "validation_failed")


class Splits(Base):
    def test_rounding_table(self):
        for amount, shares in [(1000, [334, 333, 333]), (1, [1, 0, 0]), (10, [4, 3, 3]), (999, [333, 333, 333])]:
            s, b, _ = call("POST", "/splits", {"amount": amount, "participant_handles": ["ada", "bob", "cy"]},
                           self.t["ada"], "s%d" % amount)
            self.assertEqual(s, 201)
            self.assertEqual([x["amount"] for x in b["shares"]], shares)
            self.assertEqual([r["payer_handle"] for r in b["requests"]], ["bob", "cy"])
            self.assertEqual([r["amount"] for r in b["requests"]], shares[1:])

    def test_five_way_and_order(self):
        users = [user(h, 100) for h in ("a", "b", "c", "d", "e")]
        reset(fixture(users=users))
        t = login("a")
        b = call("POST", "/splits", {"amount": 5, "participant_handles": ["a", "b", "c", "d", "e"]}, t, "x")[1]
        self.assertEqual([x["amount"] for x in b["shares"]], [1] * 5)
        b = call("POST", "/splits", {"amount": 7, "participant_handles": ["e", "d", "c", "b", "a"]}, t, "y")[1]
        self.assertEqual([(x["handle"], x["amount"]) for x in b["shares"]],
                         [("e", 2), ("d", 2), ("c", 1), ("b", 1), ("a", 1)])
        self.assertEqual([r["payer_handle"] for r in b["requests"]], ["e", "d", "c", "b"])

    def test_zero_share_request_and_caller_omitted(self):
        b = call("POST", "/splits", {"amount": 1, "participant_handles": ["bob", "cy"], "note": "z"},
                 self.t["ada"], "z")[1]
        self.assertEqual([(r["payer_handle"], r["amount"]) for r in b["requests"]], [("bob", 1), ("cy", 0)])
        rid = b["requests"][1]["request_id"]
        self.assertEqual(call("POST", "/requests/%s/pay" % rid, {}, self.t["cy"], "p")[0], 201)

    def test_caller_only_and_errors(self):
        s, b, _ = call("POST", "/splits", {"amount": 50, "participant_handles": ["ada"]}, self.t["ada"], "o")
        self.assertEqual((s, b["requests"], b["shares"], b["note"]), (201, [], [{"handle": "ada", "amount": 50}], ""))
        a = self.t["ada"]
        self.err(call("POST", "/splits", {"amount": 5, "participant_handles": []}, a, "1"), 422, "validation_failed")
        self.err(call("POST", "/splits", {"amount": 5, "participant_handles": ["bob", "bob"]}, a, "2"), 422,
                 "validation_failed")
        self.err(call("POST", "/splits", {"amount": 0, "participant_handles": ["bob"]}, a, "3"), 422,
                 "validation_failed")
        self.err(call("POST", "/splits", {"amount": 5, "participant_handles": ["bob"], "note": "x" * 201}, a, "4"),
                 422, "validation_failed")
        self.err(call("POST", "/splits", {"amount": 5, "participant_handles": ["bob", "zz"]}, a, "5"), 404,
                 "not_found")
        self.err(call("POST", "/splits", {"amount": 5}, a, "6"), 422, "validation_failed")

    def test_split_not_in_feed_and_paid_in_full_conserves(self):
        b = call("POST", "/splits", {"amount": 1000, "participant_handles": ["ada", "bob", "cy"]},
                 self.t["ada"], "s")[1]
        self.assertEqual(call("GET", "/activity", token=self.t["ada"])[1]["payments"], [])
        for r in b["requests"]:
            self.assertEqual(call("POST", "/requests/%s/pay" % r["request_id"], {},
                                  self.t[r["payer_handle"]], "p")[0], 201)
        bal = balances(self.t)
        self.assertEqual(bal, {"ada": 10666, "bob": 2167, "cy": 167})
        self.assertEqual(sum(bal.values()), 13000)


class ExportImport(Base):
    def test_round_trip_preserves_everything(self):
        a = self.t["ada"]
        p = call("POST", "/payments", {"to_handle": "bob", "amount": 5}, a, "K")[1]
        call("POST", "/payments", {"to_handle": "bob", "amount": 9999}, self.t["cy"], "F")  # fails
        exp = call("GET", "/_test/export")[1]
        self.assertEqual((exp["track"], exp["format_version"]), ("pocketful", 1))
        call("POST", "/payments", {"to_handle": "bob", "amount": 7}, a, "K2")  # after export
        reset(fixture(users=[user("zed", 1)]))
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)  # replacement, no duplicates
        self.assertEqual(balances(self.t), {"ada": 9995, "bob": 2505, "cy": 500})
        s, b, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 5}, a, "K")
        self.assertEqual((s, b), (200, p))
        self.assertEqual(call("POST", "/payments", {"to_handle": "bob", "amount": 1}, self.t["cy"], "F")[0], 201)
        self.assertEqual(len(call("GET", "/activity", token=a)[1]["payments"]), 2)
        self.assertEqual(call("POST", "/auth/login", {"email": "zed@example.com",
                                                      "password": "correct horse"})[0], 401)
        login("ada")

    def test_import_errors_leave_state(self):
        exp = call("GET", "/_test/export")[1]
        call("POST", "/payments", {"to_handle": "bob", "amount": 5}, self.t["ada"], "K")
        for body in ({}, dict(exp, track="tablekeeper"), dict(exp, format_version=2), {"track": "pocketful",
                     "format_version": 1}, dict(exp, state={"x": 1}), dict(exp, state=[])):
            self.err(call("POST", "/_test/import", body), 422, "validation_failed")
        self.err(call("POST", "/_test/import", raw=b"{"), 400, "malformed_request")
        self.assertEqual(balances(self.t)["ada"], 9995)


class Settlements(Base):
    fx = staticmethod(lambda: fixture(ops=["u_cy"]))

    def test_netting_and_body(self):
        tr = [{"from_handle": "bob", "to_handle": "ada", "amount": 3000},
              {"from_handle": "ada", "to_handle": "bob", "amount": 1000, "note": "n", "visibility": "private"}]
        s, b, _ = call("POST", "/settlements", {"transfers": tr}, self.t["cy"], "S")
        self.assertEqual(s, 201, b)  # bob has 2500 < 3000 but nets to >= 0
        self.assertEqual([p["amount"] for p in b["payments"]], [3000, 1000])
        self.assertTrue(all(p["settlement_id"] == b["settlement_id"] and p["created_at"] == b["committed_at"]
                            and p["request_id"] is None for p in b["payments"]))
        self.assertEqual(balances(self.t), {"ada": 12000, "bob": 500, "cy": 500})
        self.assertEqual(call("POST", "/settlements", {"transfers": tr}, self.t["cy"], "S")[1], b)
        self.assertEqual(balances(self.t)["ada"], 12000)
        # private member hidden from operator cy
        self.assertEqual([p["amount"] for p in call("GET", "/activity", token=self.t["cy"])[1]["payments"]], [3000])

    def test_errors(self):
        c = self.t["cy"]
        ok = {"from_handle": "ada", "to_handle": "bob", "amount": 1}
        self.err(call("POST", "/settlements", {"transfers": [ok]}, None, "a"), 401, "unauthenticated")
        self.err(call("POST", "/settlements", {"transfers": [ok]}, self.t["ada"], "a"), 403, "forbidden")
        self.err(call("POST", "/settlements", {"transfers": [ok]}, c), 400, "missing_idempotency_key")
        self.err(call("POST", "/settlements", {"transfers": []}, c, "1"), 422, "validation_failed")
        self.err(call("POST", "/settlements", {"transfers": [ok] * 33}, c, "2"), 422, "validation_failed")
        self.err(call("POST", "/settlements", {"transfers": "x"}, c, "3"), 422, "validation_failed")
        self.err(call("POST", "/settlements", {}, c, "4"), 422, "validation_failed")
        self.err(call("POST", "/settlements", {"transfers": [ok, dict(ok, to_handle="zz"),
                                                             dict(ok, to_handle="ada")]}, c, "5"), 404, "not_found")
        self.err(call("POST", "/settlements", {"transfers": [ok, dict(ok, to_handle="ada"),
                                                             dict(ok, to_handle="zz")]}, c, "6"), 422, "self_payment")
        self.err(call("POST", "/settlements", {"transfers": [dict(ok, amount=99999), dict(ok, amount=0)]}, c, "7"),
                 422, "validation_failed")
        self.err(call("POST", "/settlements", {"transfers": [dict(ok, from_handle="cy", amount=501)]}, c, "8"),
                 409, "insufficient_funds")
        self.assertEqual(call("POST", "/settlements", {"transfers": [ok] * 32}, c, "8")[0], 201)  # key unclaimed
        self.assertEqual(balances(self.t), {"ada": 9968, "bob": 2532, "cy": 500})

    def test_members_survive_export_import(self):
        b = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 5}]},
                 self.t["cy"], "S")[1]
        exp = call("GET", "/_test/export")[1]
        reset()
        call("POST", "/_test/import", exp)
        self.assertEqual(call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob",
                                                                      "amount": 5}]}, self.t["cy"], "S"), (200, b,
                         "application/json; charset=utf-8"))
        feed = call("GET", "/activity", token=self.t["ada"])[1]["payments"]
        self.assertEqual(feed[0]["settlement_id"], b["settlement_id"])


def me(tok):
    return call("GET", "/me", token=tok)[1]


def iso_in(seconds):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)).isoformat()


class Authorizations(Base):
    def auth(self, amount=2000, frm="ada", to="bob", key="a", **extra):
        s, a, _ = call("POST", "/authorizations", dict({"to_handle": to, "amount": amount}, **extra),
                       self.t[frm], key)
        self.assertEqual(s, 201, a)
        return a

    def test_create_body_and_me(self):
        a = self.auth(note="deposit", visibility="private")
        for k, v in {"from_user_id": "u_ada", "from_handle": "ada", "to_user_id": "u_bob", "to_handle": "bob",
                     "amount": 2000, "captured_amount": 0, "remaining_amount": 2000, "currency": "EUR",
                     "note": "deposit", "visibility": "private", "status": "open", "payment_id": None,
                     "payment_ids": []}.items():
            self.assertEqual(a[k], v, k)
        import datetime
        c, e = (datetime.datetime.fromisoformat(a[f]) for f in ("created_at", "expires_at"))
        self.assertEqual((e - c).total_seconds(), 600)
        self.assertEqual(me(self.t["ada"]), {"user_id": "u_ada", "display_name": "Ada", "handle": "ada",
                                             "balance": 10000, "total": 10000, "available": 8000, "held": 2000,
                                             "currency": "EUR", "minor_units": 2})
        self.assertEqual(call("GET", "/activity", token=self.t["ada"])[1]["payments"], [])
        # held funds cannot fund payments, authorizations or settlements
        self.err(call("POST", "/payments", {"to_handle": "cy", "amount": 8001}, self.t["ada"], "p"),
                 409, "insufficient_funds")
        self.err(call("POST", "/authorizations", {"to_handle": "cy", "amount": 8001}, self.t["ada"], "x"),
                 409, "insufficient_funds")
        rq = call("POST", "/requests", {"payer_handle": "ada", "amount": 8001}, self.t["cy"], "r")[1]
        self.err(call("POST", "/requests/%s/pay" % rq["request_id"], {}, self.t["ada"], "p"),
                 409, "insufficient_funds")
        self.assertEqual(call("POST", "/payments", {"to_handle": "cy", "amount": 8000}, self.t["ada"], "p2")[0], 201)
        self.assertEqual(me(self.t["ada"])["available"], 0)

    def test_create_errors(self):
        a = self.t["ada"]
        for i, (body, st, code) in enumerate([
                ({"to_handle": "bob", "amount": 0}, 422, "validation_failed"),
                ({"to_handle": "bob", "amount": 10 ** 9 + 1}, 422, "validation_failed"),
                ({"to_handle": "bob", "amount": "5"}, 422, "validation_failed"),
                ({"to_handle": "ada", "amount": 5}, 422, "self_payment"),
                ({"to_handle": "bob", "amount": 5, "note": "x" * 201}, 422, "validation_failed"),
                ({"to_handle": "bob", "amount": 5, "visibility": "x"}, 422, "validation_failed"),
                ({"to_handle": "zz", "amount": 5}, 404, "not_found"),
                ({"to_handle": "bob", "amount": 10001}, 409, "insufficient_funds")]):
            self.err(call("POST", "/authorizations", body, a, "e%d" % i), st, code)
        self.err(call("POST", "/authorizations", {"to_handle": "bob", "amount": 5}, a), 400, "missing_idempotency_key")
        self.err(call("POST", "/authorizations", raw=b"", token=a, key="z"), 400, "malformed_request")

    def test_full_capture_default(self):
        a = self.auth()
        s, p, _ = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, self.t["bob"], "c")
        self.assertEqual(s, 201, p)
        self.assertEqual((p["amount"], p["authorization_id"], p["request_id"], p["from_handle"], p["to_handle"]),
                         (2000, a["authorization_id"], None, "ada", "bob"))
        self.assertEqual((me(self.t["ada"])["total"], me(self.t["ada"])["held"], me(self.t["bob"])["total"]),
                         (8000, 0, 4500))
        got = call("GET", "/authorizations", token=self.t["ada"])[1]["authorizations"][0]
        self.assertEqual((got["status"], got["captured_amount"], got["payment_id"], got["payment_ids"],
                          got["remaining_amount"]), ("captured", 2000, p["payment_id"], [p["payment_id"]], 0))
        self.assertEqual(call("GET", "/activity", token=self.t["cy"])[1]["payments"][0]["payment_id"],
                         p["payment_id"])
        self.err(call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, self.t["bob"], "c2"),
                 409, "authorization_not_open")
        s2, p2, _ = call("POST", "/authorizations/%s/capture" % a["authorization_id"], raw=b"", token=self.t["bob"],
                         key="c")
        self.assertEqual((s2, p2), (200, p))  # replay after close, empty body == {}
        self.err(call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 2000},
                      self.t["bob"], "c"), 409, "idempotency_key_reuse")
        self.err(call("POST", "/authorizations/%s/void" % a["authorization_id"], None, self.t["ada"]),
                 409, "authorization_not_open")

    def test_partial_final_releases_remainder(self):
        a = self.auth()
        call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 1500}, self.t["bob"], "c")
        m = me(self.t["ada"])
        self.assertEqual((m["total"], m["available"], m["held"]), (8500, 8500, 0))
        self.assertNotIn("payments", m)

    def test_extended_mode(self):
        a = self.auth()
        path = "/authorizations/%s/capture" % a["authorization_id"]
        p1 = call("POST", path, {"amount": 700, "final": False}, self.t["bob"], "1")[1]
        m = me(self.t["ada"])
        self.assertEqual((m["total"], m["held"], m["available"]), (9300, 1300, 8000))
        self.err(call("POST", path, {"amount": 1301, "final": False}, self.t["bob"], "2"), 422,
                 "capture_exceeds_authorization")
        self.err(call("POST", path, {"amount": 0}, self.t["bob"], "2"), 422, "validation_failed")
        self.err(call("POST", path, {"final": "no"}, self.t["bob"], "2"), 400, "malformed_request")
        p2 = call("POST", path, {"amount": 300, "final": False}, self.t["bob"], "3")[1]
        got = call("GET", "/authorizations?status=open", token=self.t["bob"])[1]["authorizations"][0]
        self.assertEqual((got["captured_amount"], got["remaining_amount"], got["payment_id"], got["payment_ids"]),
                         (1000, 1000, p2["payment_id"], [p1["payment_id"], p2["payment_id"]]))
        p3 = call("POST", path, {"final": False}, self.t["bob"], "4")[1]  # whole remainder closes it
        self.assertEqual(p3["amount"], 1000)
        got = call("GET", "/authorizations", token=self.t["bob"])[1]["authorizations"][0]
        self.assertEqual((got["status"], got["captured_amount"], got["remaining_amount"]), ("captured", 2000, 0))
        self.assertEqual(sum(me(self.t[h])["total"] for h in self.t), 13000)

    def test_void_partial_and_permissions(self):
        a = self.auth()
        aid = a["authorization_id"]
        self.err(call("POST", "/authorizations/%s/capture" % aid, {}, self.t["ada"], "c"), 403, "forbidden")
        self.err(call("POST", "/authorizations/%s/capture" % aid, {}, self.t["cy"], "c"), 403, "forbidden")
        self.err(call("POST", "/authorizations/%s/void" % aid, None, self.t["bob"]), 403, "forbidden")
        self.err(call("POST", "/authorizations/%s/void" % aid, None, self.t["cy"]), 403, "forbidden")
        self.err(call("POST", "/authorizations/nope/void", None, self.t["ada"]), 404, "not_found")
        self.err(call("POST", "/authorizations/nope/capture", {}, self.t["bob"], "c"), 404, "not_found")
        call("POST", "/authorizations/%s/capture" % aid, {"amount": 500, "final": False}, self.t["bob"], "c")
        s, v, _ = call("POST", "/authorizations/%s/void" % aid, None, self.t["ada"])
        self.assertEqual((s, v["status"], v["captured_amount"], v["remaining_amount"], len(v["payment_ids"])),
                         (200, "voided", 500, 0, 1))
        self.assertEqual(call("POST", "/authorizations/%s/void" % aid, None, self.t["ada"])[0], 200)
        m = me(self.t["ada"])
        self.assertEqual((m["total"], m["held"]), (9500, 0))
        self.err(call("POST", "/authorizations/%s/capture" % aid, {}, self.t["bob"], "c9"), 409,
                 "authorization_not_open")
        self.assertEqual(call("GET", "/authorizations", token=self.t["cy"])[1],
                         {"authorizations": [], "has_more": False})

    def test_expiry_by_clock(self):
        import time
        reset(dict(fixture(), authorization_ttl_seconds=1))
        t = {h: login(h) for h in ("ada", "bob")}
        a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 3000}, t["ada"], "a")[1]
        call("POST", "/authorizations/%s/capture" % a["authorization_id"], {"amount": 1000, "final": False},
             t["bob"], "c")
        self.assertEqual(me(t["ada"])["held"], 2000)
        time.sleep(1.2)
        m = me(t["ada"])
        self.assertEqual((m["total"], m["held"], m["available"]), (9000, 0, 9000))
        got = call("GET", "/authorizations?status=expired", token=t["ada"])[1]["authorizations"]
        self.assertEqual([(x["status"], x["captured_amount"], x["remaining_amount"]) for x in got],
                         [("expired", 1000, 0)])
        self.assertEqual(call("GET", "/authorizations?status=open", token=t["ada"])[1]["authorizations"], [])
        self.err(call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, t["bob"], "c2"), 409,
                 "authorization_expired")
        self.err(call("POST", "/authorizations/%s/void" % a["authorization_id"], None, t["ada"]), 409,
                 "authorization_not_open")
        self.assertEqual(me(t["ada"])["held"], 0)  # released once

    def test_list_filters(self):
        a1 = self.auth(amount=1, key="1")
        a2 = self.auth(amount=2, frm="bob", to="ada", key="2")
        ids = lambda qs: [x["authorization_id"] for x in
                          call("GET", "/authorizations" + qs, token=self.t["ada"])[1]["authorizations"]]
        self.assertEqual(ids(""), [a2["authorization_id"], a1["authorization_id"]])
        self.assertEqual(ids("?direction=outgoing"), [a1["authorization_id"]])
        self.assertEqual(ids("?direction=incoming"), [a2["authorization_id"]])
        self.assertEqual(ids("?limit=1&offset=1"), [a1["authorization_id"]])
        for qs in ("?direction=x", "?status=pending", "?limit=0", "?offset=-1"):
            self.err(call("GET", "/authorizations" + qs, token=self.t["ada"]), 422, "validation_failed")

    def test_concurrent_capture_void_payment_race(self):
        for round_ in range(5):
            reset()
            t = {h: login(h) for h in ("ada", "bob", "cy")}
            a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 6000}, t["ada"], "a")[1]
            aid = a["authorization_id"]
            jobs = ([("POST", "/authorizations/%s/capture" % aid, {"amount": 1000, "final": False}, t["bob"], "c%d" % i)
                     for i in range(8)]
                    + [("POST", "/authorizations/%s/void" % aid, None, t["ada"], None)]
                    + [("POST", "/payments", {"to_handle": "cy", "amount": 1500}, t["ada"], "p%d" % i)
                       for i in range(5)])
            with ThreadPoolExecutor(14) as ex:
                res = list(ex.map(lambda j: call(*j)[0], jobs))
            self.assertTrue(set(res) <= {200, 201, 409, 422}, res)
            ms = {h: me(t[h]) for h in t}
            self.assertEqual(sum(m["total"] for m in ms.values()), 13000)
            self.assertTrue(all(m["available"] >= 0 and m["held"] >= 0 for m in ms.values()), ms)
            got = call("GET", "/authorizations", token=t["ada"])[1]["authorizations"][0]
            self.assertLessEqual(got["captured_amount"], 6000)
            self.assertEqual(ms["ada"]["held"], got["remaining_amount"])

    def test_concurrent_identical_capture_once(self):
        a = self.auth()
        path = "/authorizations/%s/capture" % a["authorization_id"]
        with ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda _: call("POST", path, {"amount": 100, "final": False}, self.t["bob"], "K"),
                              range(20)))
        self.assertEqual(sorted(r[0] for r in res), [200] * 19 + [201])
        self.assertEqual(me(self.t["bob"])["total"], 2600)

    def test_settlement_uses_available(self):
        reset(fixture(ops=["u_cy"]))
        t = {h: login(h) for h in ("ada", "bob", "cy")}
        call("POST", "/authorizations", {"to_handle": "cy", "amount": 2000}, t["bob"], "a")
        self.err(call("POST", "/settlements", {"transfers": [{"from_handle": "bob", "to_handle": "ada",
                                                              "amount": 501}]}, t["cy"], "s"), 409,
                 "insufficient_funds")
        self.assertEqual(call("POST", "/settlements", {"transfers": [{"from_handle": "bob", "to_handle": "ada",
                                                                      "amount": 500}]}, t["cy"], "s")[0], 201)


class SeededAuthorizations(unittest.TestCase):
    def test_seeded_holds(self):
        fx = dict(fixture(), authorizations=[
            {"id": "a_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 2000, "note": "deposit",
             "visibility": "public", "status": "open", "expires_at": iso_in(3600)},
            {"id": "a_2", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 1000,
             "status": "open", "expires_at": iso_in(-3600)},
            {"id": "a_3", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 100,
             "status": "voided", "expires_at": iso_in(3600)},
            {"id": "a_4", "from_user_id": "u_bob", "to_user_id": "u_cy", "amount": 0,
             "status": "open", "expires_at": iso_in(-7000)}])
        reset(fx)
        ada, bob = login("ada"), login("bob")
        m = me(ada)
        self.assertEqual((m["balance"], m["total"], m["available"], m["held"]), (10000, 10000, 8000, 2000))
        st = {x["authorization_id"]: x["status"] for x in
              call("GET", "/authorizations", token=ada)[1]["authorizations"]}
        self.assertEqual(st, {"a_1": "open", "a_2": "expired", "a_3": "voided"})
        got = {x["authorization_id"]: x for x in call("GET", "/authorizations", token=ada)[1]["authorizations"]}
        self.assertEqual((got["a_2"]["expires_at"], got["a_2"]["closed_at"]), (fx["authorizations"][1]["expires_at"],) * 2)
        self.assertEqual(me(bob)["held"], 0)
        s, p, _ = call("POST", "/authorizations/a_1/capture", {}, bob, "k")
        self.assertEqual((s, p["amount"]), (201, 2000))
        # M1: generated ids never collide with seeded ids
        self.assertNotIn(p["payment_id"], ("a_1", "a_2", "a_3", "u_ada", "u_bob", "u_cy"))

    def test_seeded_holds_over_balance_rejected(self):
        reset()
        tok = login("cy")
        fx = dict(fixture(), authorizations=[
            {"id": "a_1", "from_user_id": "u_cy", "to_user_id": "u_bob", "amount": 400, "status": "open",
             "expires_at": iso_in(3600)},
            {"id": "a_2", "from_user_id": "u_cy", "to_user_id": "u_bob", "amount": 101, "status": "open",
             "expires_at": iso_in(3600)}])
        s, b, _ = call("POST", "/_test/reset", fx)
        self.assertEqual((s, b["error"]["code"]), (422, "validation_failed"))
        self.assertEqual(me(tok)["balance"], 500)  # unchanged
        fx["authorizations"][1]["expires_at"] = iso_in(-3600)  # expired holds do not count
        reset(fx)
        for bad in ({"authorization_ttl_seconds": 0}, {"authorization_ttl_seconds": 1.5},
                    {"authorization_ttl_seconds": -5}):
            self.assertEqual(call("POST", "/_test/reset", dict(fixture(), **bad))[0], 422)
        self.assertEqual(call("POST", "/_test/reset", dict(fixture(), authorization_ttl_seconds="60"))[0], 400)

    def test_m1_generated_ids_skip_seeded(self):
        fx = fixture(ops=["u_cy"], payments=[
            {"id": "p_%d" % i, "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 1} for i in range(1, 6)]
            + [{"id": "st_7", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 1, "settlement_id": "st_9"}],
            requests=[{"id": "rq_%d" % i, "requester_id": "u_bob", "payer_id": "u_ada", "amount": 1}
                      for i in range(1, 12)])
        reset(fx)
        cy, ada = login("cy"), login("ada")
        seen = {"p_1", "p_2", "p_3", "p_4", "p_5", "st_7", "st_9"} | {"rq_%d" % i for i in range(1, 12)}
        for i in range(12):
            b = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]},
                     cy, "s%d" % i)[1]
            new = {b["settlement_id"], b["payments"][0]["payment_id"]}
            self.assertFalse(new & seen, new)
            seen |= new
            for kind, path, body in (("r", "/requests", {"payer_handle": "bob", "amount": 1}),
                                     ("a", "/authorizations", {"to_handle": "bob", "amount": 1}),
                                     ("s", "/splits", {"amount": 2, "participant_handles": ["ada", "bob"]})):
                b = call("POST", path, body, ada, kind + str(i))[1]
                new = {b.get("request_id") or b.get("authorization_id") or b.get("split_id")}
                self.assertFalse(new & seen, new)
                seen |= new

    def test_m2_reset_150_distinct_passwords_fast(self):
        import time
        users = [dict(user("u%d" % i, 10), password="secret password %d" % i) for i in range(150)]
        t0 = time.time()
        reset(fixture(users=users))
        took = time.time() - t0
        self.assertLess(took, 3, took)
        s, b, _ = call("POST", "/auth/login", {"email": "u149@example.com", "password": "secret password 149"})
        self.assertEqual(s, 200)
        self.assertEqual(call("POST", "/auth/login", {"email": "u149@example.com",
                                                      "password": "secret password 148"})[0], 401)


class Stage1Import(unittest.TestCase):
    """A stage-1 export (this team's format, no authorizations/ttl/n/authorization_id) imports."""
    STAGE1 = None

    def stage1_export(self):
        import os, subprocess, socket, time
        here = os.path.dirname(os.path.abspath(__file__))
        s1 = os.path.join(here, "..", "stage-1", "server.py")
        if not os.path.exists(s1):
            self.skipTest("stage-1 source not present")
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        proc = subprocess.Popen([sys.executable, s1], env=dict(os.environ, PORT=str(port)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = "127.0.0.1:%d" % port
            for _ in range(100):
                try:
                    http.client.HTTPConnection(base, timeout=1).request("GET", "/health"); break
                except OSError:
                    time.sleep(0.05)

            def c1(method, path, body=None, token=None, key=None, raw=None):
                c = http.client.HTTPConnection(base, timeout=10)
                h = {}
                if token: h["Authorization"] = "Bearer " + token
                if key: h["Idempotency-Key"] = key
                c.request(method, path, body=raw if raw is not None else (json.dumps(body) if body is not None else None),
                          headers=h)
                r = c.getresponse(); t = r.read()
                return r.status, json.loads(t) if t else None
            c1("POST", "/_test/reset", fixture())
            tok = c1("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})[1]["token"]
            bob = c1("POST", "/auth/login", {"email": "bob@example.com", "password": "correct horse"})[1]["token"]
            pay = c1("POST", "/payments", {"to_handle": "bob", "amount": 250, "note": "lost"}, tok, "LOST")[1]
            rq = c1("POST", "/requests", {"payer_handle": "ada", "amount": 300}, bob, "R")[1]
            exp = c1("GET", "/_test/export")[1]
            return exp, tok, pay, rq
        finally:
            proc.terminate(); proc.wait()

    def test_stage1_export_imports(self):
        exp, tok, pay, rq = self.stage1_export()
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        m = me(tok)  # same token still valid
        self.assertEqual((m["balance"], m["total"], m["available"], m["held"]), (9750, 9750, 9750, 0))
        s, b, _ = call("POST", "/payments", {"to_handle": "bob", "amount": 250, "note": "lost"}, tok, "LOST")
        self.assertEqual((s, b), (200, pay))
        self.assertEqual(me(tok)["balance"], 9750)
        self.assertEqual(call("POST", "/requests/%s/pay" % rq["request_id"], {}, tok, "P")[0], 201)
        self.assertEqual(call("POST", "/auth/login", {"email": "ada@example.com", "password": "correct horse"})[0],
                         200)
        self.assertEqual(call("GET", "/authorizations", token=tok)[1], {"authorizations": [], "has_more": False})
        a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 5}, tok, "A")[1]
        import datetime
        c, e = (datetime.datetime.fromisoformat(a[f]) for f in ("created_at", "expires_at"))
        self.assertEqual((e - c).total_seconds(), 600)
        self.assertNotIn(a["authorization_id"], (pay["payment_id"], rq["request_id"]))


class Stage2RoundTrip(Base):
    def test_auth_state_round_trips(self):
        a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 2000}, self.t["ada"], "A")[1]
        path = "/authorizations/%s/capture" % a["authorization_id"]
        p = call("POST", path, {"amount": 500, "final": False}, self.t["bob"], "C")[1]
        exp = call("GET", "/_test/export")[1]
        reset()
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        self.assertEqual(call("POST", "/authorizations", {"to_handle": "bob", "amount": 2000}, self.t["ada"], "A")[1:2],
                         (a,))
        self.assertEqual(call("POST", path, {"amount": 500, "final": False}, self.t["bob"], "C")[:2], (200, p))
        m = me(self.t["ada"])
        self.assertEqual((m["total"], m["held"]), (9500, 1500))
        got = call("GET", "/authorizations", token=self.t["ada"])[1]["authorizations"][0]
        self.assertEqual((got["captured_amount"], got["payment_ids"], got["expires_at"]),
                         (500, [p["payment_id"]], a["expires_at"]))


class Html(Base):
    def test_negotiation(self):
        for path in ("/", "/split", "/signup", "/login"):
            s, _, ct = call_raw("GET", path, {})
            self.assertEqual((s, ct), (200, "text/html; charset=utf-8"), path)
        for path in ("/requests", "/authorizations"):
            self.assertEqual(call_raw("GET", path, {"Accept": "text/html,application/xhtml+xml"})[2],
                             "text/html; charset=utf-8")
            for accept in (None, "application/json", "*/*"):
                h = {"Authorization": "Bearer " + self.t["ada"]}
                if accept:
                    h["Accept"] = accept
                s, body, ct = call_raw("GET", path, h)
                self.assertEqual((s, ct), (200, "application/json; charset=utf-8"), (path, accept))
            self.err(call("GET", path), 401, "unauthenticated")


def call_raw(method, path, headers):
    c = http.client.HTTPConnection(HOST, timeout=10)
    c.request(method, path, headers=headers)
    r = c.getresponse()
    return r.status, r.read(), r.getheader("Content-Type")


# ---------------------------------------------------------------- stage 3

from urllib.parse import quote


def q(**kw):
    return "?" + "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in kw.items())


def ts_after(iso_text, micro):
    import datetime
    return (datetime.datetime.fromisoformat(iso_text) + datetime.timedelta(microseconds=micro)).isoformat()


def corr(tok, pid, key="c", **body):
    return call("POST", "/payments/%s/corrections" % pid, body, tok, key)


class Seeds(unittest.TestCase):
    def test_seeded_created_at_and_future(self):
        past = iso_in(-7200)
        fx = fixture(payments=[{"id": "p_old", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500,
                                "created_at": past},
                               {"id": "p_now", "from_user_id": "u_bob", "to_user_id": "u_cy", "amount": 100}])
        reset(fx)
        ada, bob = login("ada"), login("bob")
        feed = call("GET", "/activity", token=bob)[1]["payments"]
        self.assertEqual([(p["payment_id"], p["created_at"]) for p in feed][1], ("p_old", past))
        self.assertEqual(me(ada)["balance"], 10000)  # not replayed
        self.assertEqual(call("GET", "/me" + q(as_of=iso_in(-10000)), token=ada)[1]["balance"], 10500)  # opening
        self.assertEqual(call("GET", "/me" + q(as_of=past), token=ada)[1]["balance"], 10000)  # inclusive
        new = call("POST", "/payments", {"to_handle": "bob", "amount": 1}, ada, "k")[1]
        self.assertGreater(new["created_at"], feed[0]["created_at"])
        bad = fixture(users=[user("zed", 5)])
        bad["payments"] = []
        bad = fixture(payments=[{"id": "p_f", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 1,
                                 "created_at": iso_in(3600)}])
        s, b, _ = call("POST", "/_test/reset", bad)
        self.assertEqual((s, b["error"]["code"]), (422, "validation_failed"))
        self.assertEqual(me(ada)["balance"], 9999)  # unchanged


class AsOf(Base):
    def test_as_of_rules(self):
        a = self.t["ada"]
        self.assertNotIn("as_of", me(a))
        p1 = call("POST", "/payments", {"to_handle": "bob", "amount": 300}, a, "1")[1]
        p2 = call("POST", "/payments", {"to_handle": "bob", "amount": 200}, a, "2")[1]
        at = lambda t: call("GET", "/me" + q(as_of=t), token=a)[1]
        self.assertEqual(at(p1["created_at"])["balance"], 9700)
        self.assertEqual(at(ts_after(p1["created_at"], -1))["balance"], 10000)
        self.assertEqual(at(p2["created_at"])["balance"], 9500)
        later = iso_in(3600)
        b = at(later)
        self.assertEqual((b["balance"], b["total"], b["available"], b["held"], b["as_of"]), (9500, 9500, 9500, 0, later))
        z = "2026-01-01T00:00:00Z"
        self.assertEqual(at(z)["as_of"], z)
        for bad in ("", "2026-09-24", "2026-09-24T13:20:00", "yesterday", "2026-09-24T25:00:00+00:00"):
            self.err(call("GET", "/me" + q(as_of=bad), token=a), 422, "validation_failed")
            self.err(call("GET", "/me" + q(known_at=bad), token=a), 422, "validation_failed")
        # an unencoded '+' arrives as a space and is still accepted
        s, body, _ = call("GET", "/me?as_of=2030-01-01T00:00:00+00:00", token=a)
        self.assertEqual((s, body["balance"]), (200, 9500))


class Statement(Base):
    def test_window_order_and_balances(self):
        a, b = self.t["ada"], self.t["bob"]
        p = [call("POST", "/payments", {"to_handle": "bob", "amount": 100 * (i + 1)}, a, "k%d" % i)[1]
             for i in range(5)]
        call("POST", "/payments", {"to_handle": "ada", "amount": 50}, b, "x")
        call("POST", "/payments", {"to_handle": "cy", "amount": 7}, b, "y")  # public, not ada's
        full = call("GET", "/statement", token=a)[1]
        self.assertEqual([e["delta"] for e in full["entries"]], [-100, -200, -300, -400, -500, 50])
        run = full["opening_balance"]
        for e in full["entries"]:
            run += e["delta"]
            self.assertEqual(e["balance_after"], run)
            self.assertEqual((e["revision"], e["effective_at"], e["recorded_at"]),
                             (1, e["payment"]["created_at"], e["payment"]["created_at"]))
        self.assertEqual((full["opening_balance"], full["closing_balance"], full["has_more"]), (10000, 8550, False))
        self.assertTrue(full["snapshot"])
        # half-open window [p1, p3)
        w = call("GET", "/statement" + q(**{"from": p[1]["created_at"], "to": p[3]["created_at"]}), token=a)[1]
        self.assertEqual([e["payment"]["payment_id"] for e in w["entries"]], [p[1]["payment_id"], p[2]["payment_id"]])
        self.assertEqual((w["opening_balance"], w["closing_balance"]), (9900, 9400))
        # pagination does not change balances
        pg = call("GET", "/statement" + q(limit=2, offset=2), token=a)[1]
        self.assertEqual(pg["entries"], full["entries"][2:4])
        self.assertEqual((pg["opening_balance"], pg["closing_balance"], pg["has_more"]), (10000, 8550, True))
        last = call("GET", "/statement" + q(limit=4, offset=4), token=a)[1]
        self.assertEqual((len(last["entries"]), last["has_more"]), (2, False))
        beyond = call("GET", "/statement" + q(limit=4, offset=40), token=a)[1]
        self.assertEqual((beyond["entries"], beyond["has_more"]), ([], False))
        self.assertEqual(call("GET", "/statement", token=self.t["cy"])[1]["entries"][0]["delta"], 7)
        for bad in (dict(limit=0), dict(offset=-1), dict(to="x"), {"from": "2026-01-01"},
                    {"from": iso_in(10), "to": iso_in(-10)}):
            self.err(call("GET", "/statement" + q(**bad), token=a), 422, "validation_failed")
        self.err(call("GET", "/statement"), 401, "unauthenticated")

    def test_snapshots(self):
        a = self.t["ada"]
        p = call("POST", "/payments", {"to_handle": "bob", "amount": 100}, a, "1")[1]
        first = call("GET", "/statement" + q(limit=1), token=a)[1]
        tok = first["snapshot"]
        call("POST", "/payments", {"to_handle": "bob", "amount": 5}, a, "2")
        corr(a, p["payment_id"], expected_revision=1, amount=10, effective_at=p["created_at"], reason="fix")
        again = call("GET", "/statement" + q(snapshot=tok, limit=1, offset=0), token=a)[1]
        self.assertEqual((again["entries"], again["opening_balance"], again["closing_balance"], again["has_more"]),
                         (first["entries"], 10000, 9900, False))
        self.assertEqual(call("GET", "/statement" + q(snapshot=tok, offset=1), token=a)[1]["entries"], [])
        for extra in ({"from": iso_in(-5)}, {"to": iso_in(5)}, {"known_at": iso_in(0)}):
            self.err(call("GET", "/statement" + q(snapshot=tok, **extra), token=a), 422, "validation_failed")
        self.err(call("GET", "/statement" + q(snapshot=tok), token=self.t["bob"]), 404, "not_found")
        self.err(call("GET", "/statement" + q(snapshot="nope"), token=a), 404, "not_found")
        reset()
        a = login("ada")
        self.err(call("GET", "/statement" + q(snapshot=tok), token=a), 404, "not_found")


class Corrections(Base):
    def pay(self, frm="ada", to="bob", amount=100, key="p"):
        return call("POST", "/payments", {"to_handle": to, "amount": amount}, self.t[frm], key)[1]

    def test_increase_decrease_revisions_and_feed(self):
        a = self.t["ada"]
        p = self.pay(amount=100)
        pid = p["payment_id"]
        s, r2, _ = corr(a, pid, "c1", expected_revision=1, amount=150, effective_at=p["created_at"], reason="tip")
        self.assertEqual((s, r2["revision"], r2["amount"], r2["effective_at"], r2["reason"], r2["payment_id"]),
                         (201, 2, 150, p["created_at"], "tip", pid))
        self.assertEqual((me(a)["balance"], me(self.t["bob"])["balance"]), (9850, 2650))
        s, r3, _ = corr(a, pid, "c2", expected_revision=2, amount=0, effective_at=p["created_at"], reason="refund")
        self.assertEqual((me(a)["balance"], me(self.t["bob"])["balance"]), (10000, 2500))
        self.assertGreater(r3["recorded_at"], r2["recorded_at"])
        revs = call("GET", "/payments/%s/revisions" % pid, token=self.t["bob"])[1]["revisions"]
        self.assertEqual([(r["revision"], r["amount"], r["reason"]) for r in revs], [(1, 100, ""), (2, 150, "tip"), (3, 0, "refund")])
        self.assertEqual((revs[0]["effective_at"], revs[0]["recorded_at"]), (p["created_at"], p["created_at"]))
        self.err(call("GET", "/payments/%s/revisions" % pid, token=self.t["cy"]), 404, "not_found")
        self.err(call("GET", "/payments/%s/revisions" % pid), 401, "unauthenticated")
        self.err(call("GET", "/payments/nope/revisions", token=a), 404, "not_found")
        feed = call("GET", "/activity", token=a)[1]["payments"]
        self.assertEqual([(x["payment_id"], x["amount"]) for x in feed], [(pid, 100)])  # original
        st = call("GET", "/statement", token=a)[1]
        self.assertEqual([(e["revision"], e["delta"], e["payment"]["amount"]) for e in st["entries"]], [(3, 0, 0)])
        # replay returns the original revision 2 even after revision 3
        s, again, _ = corr(a, pid, "c1", expected_revision=1, amount=150, effective_at=p["created_at"], reason="tip")
        self.assertEqual((s, again), (200, r2))
        self.err(corr(a, pid, "c1", expected_revision=1, amount=151, effective_at=p["created_at"], reason="tip"),
                 409, "idempotency_key_reuse")
        self.err(corr(a, pid, "c9", expected_revision=2, amount=5, effective_at=p["created_at"], reason="x"),
                 409, "stale_revision")
        # known_at selects older revisions; before the payment it contributes nothing
        kn = lambda t: call("GET", "/me" + q(known_at=t), token=a)[1]
        self.assertEqual(kn(r2["recorded_at"])["balance"], 9850)
        self.assertEqual(kn(ts_after(r2["recorded_at"], -1))["balance"], 9900)
        self.assertEqual(kn(ts_after(p["created_at"], -1))["balance"], 10000)
        self.assertEqual(kn(r2["recorded_at"])["known_at"], r2["recorded_at"])
        st2 = call("GET", "/statement" + q(known_at=r2["recorded_at"]), token=a)[1]
        self.assertEqual([(e["revision"], e["delta"]) for e in st2["entries"]], [(2, -150)])
        self.assertEqual(st2["known_at"], r2["recorded_at"])

    def test_effective_time_moves_entry_between_windows(self):
        a = self.t["ada"]
        p = self.pay(amount=100)
        earlier = iso_in(-3600)
        corr(a, p["payment_id"], "c", expected_revision=1, amount=100, effective_at=earlier, reason="backdate")
        st = call("GET", "/statement" + q(**{"from": iso_in(-60)}), token=a)[1]
        self.assertEqual((st["entries"], st["opening_balance"]), ([], 9900))
        self.assertEqual(call("GET", "/me" + q(as_of=iso_in(-1800)), token=a)[1]["balance"], 9900)
        self.assertEqual(call("GET", "/me" + q(as_of=iso_in(-1800), known_at=p["created_at"]), token=a)[1]["balance"],
                         10000)

    def test_validation_and_permissions(self):
        a = self.t["ada"]
        p = self.pay()
        pid = p["payment_id"]
        ok = dict(expected_revision=1, amount=50, effective_at=p["created_at"], reason="r")
        for i, bad in enumerate([dict(ok, expected_revision=0), dict(ok, expected_revision="1"), dict(ok, amount=-1),
                                 dict(ok, amount=10 ** 9 + 1), dict(ok, amount=1.5), dict(ok, reason=""),
                                 dict(ok, reason="x" * 201), dict(ok, reason=5), dict(ok, effective_at=iso_in(60)),
                                 dict(ok, effective_at="2026-09-20"), dict(ok, effective_at="2026-09-20T12:00:00")]
                                + [{k: v for k, v in ok.items() if k != f} for f in ok]):
            self.err(corr(a, pid, "v%d" % i, **bad), 422, "validation_failed")
        self.err(corr(self.t["bob"], pid, "x", **ok), 403, "forbidden")
        self.err(corr(self.t["cy"], pid, "x", **ok), 403, "forbidden")
        self.err(corr(a, "nope", "x", **ok), 404, "not_found")
        self.err(call("POST", "/payments/%s/corrections" % pid, ok, None, "x"), 401, "unauthenticated")
        self.err(call("POST", "/payments/%s/corrections" % pid, ok, a), 400, "missing_idempotency_key")
        self.assertEqual(corr(a, pid, "v0", **ok)[0], 201)  # failed key was not claimed
        self.assertEqual(corr(a, pid, "reason200", **dict(ok, expected_revision=2, reason="x" * 200))[0], 201)

    def test_insufficient_then_historical_overdraft(self):
        reset(fixture(users=[user("ada", 1000), user("bob", 0), user("cy", 0)]))
        t = {h: login(h) for h in ("ada", "bob", "cy")}
        p1 = call("POST", "/payments", {"to_handle": "bob", "amount": 500}, t["ada"], "1")[1]
        call("POST", "/payments", {"to_handle": "cy", "amount": 500}, t["ada"], "2")
        call("POST", "/payments", {"to_handle": "ada", "amount": 300}, t["cy"], "3")
        before = (me(t["ada"]), call("GET", "/payments/%s/revisions" % p1["payment_id"], token=t["ada"])[1])
        # current debit of 400 > ada's 300 available -> insufficient_funds first
        self.err(corr(t["ada"], p1["payment_id"], "k", expected_revision=1, amount=900, effective_at=p1["created_at"],
                      reason="x"), 409, "insufficient_funds")
        # affordable now (200 <= 300) but ada would be -200 after the second payment -> historical_overdraft
        self.err(corr(t["ada"], p1["payment_id"], "k", expected_revision=1, amount=700, effective_at=p1["created_at"],
                      reason="x"), 409, "historical_overdraft")
        after = (me(t["ada"]), call("GET", "/payments/%s/revisions" % p1["payment_id"], token=t["ada"])[1])
        self.assertEqual(before, after)
        # the same correction effective after the second payment is fine; the key was never claimed
        s, r, _ = corr(t["ada"], p1["payment_id"], "k", expected_revision=1, amount=700, effective_at=iso_in(0),
                       reason="x")
        self.assertEqual(s, 201, r)
        self.assertEqual(sum(me(t[h])["balance"] for h in t), 1000)
        # receiver side: decreasing debits bob, who spent it
        p4 = call("POST", "/payments", {"to_handle": "cy", "amount": 700}, t["bob"], "4")[1]
        self.err(corr(t["ada"], p1["payment_id"], "k2", expected_revision=2, amount=0, effective_at=p1["created_at"],
                      reason="x"), 409, "insufficient_funds")

    def test_held_funds_block_correction_and_history_available(self):
        reset(fixture(users=[user("ada", 1000), user("bob", 0), user("cy", 0)]))
        t = {h: login(h) for h in ("ada", "bob", "cy")}
        p1 = call("POST", "/payments", {"to_handle": "bob", "amount": 100}, t["ada"], "1")[1]
        call("POST", "/authorizations", {"to_handle": "cy", "amount": 850}, t["ada"], "a")
        # 100 more for bob: ada has 50 available now -> insufficient
        self.err(corr(t["ada"], p1["payment_id"], "k", expected_revision=1, amount=200, effective_at=p1["created_at"],
                      reason="x"), 409, "insufficient_funds")
        self.assertEqual(corr(t["ada"], p1["payment_id"], "k", expected_revision=1, amount=150,
                              effective_at=p1["created_at"], reason="x")[0], 201)

    def test_combined_instant_boundary(self):
        T = iso_in(-3600)
        reset(fixture(users=[user("x", 100), user("y", 100), user("z", 100)], payments=[
            {"id": "p1", "from_user_id": "u_x", "to_user_id": "u_z", "amount": 100, "created_at": T},
            {"id": "p2", "from_user_id": "u_y", "to_user_id": "u_x", "amount": 100, "created_at": T},
            {"id": "p3", "from_user_id": "u_z", "to_user_id": "u_x", "amount": 100, "created_at": iso_in(-60)}]))
        x = login("x")
        self.assertEqual(call("GET", "/me" + q(as_of=iso_in(-7200)), token=x)[1]["balance"], 0)  # opening
        # x's balance at T is 0 - 90 + 100 = 10 when both movements at T are combined
        s, r, _ = corr(x, "p1", "k", expected_revision=1, amount=90, effective_at=T, reason="partial")
        self.assertEqual(s, 201, r)

    def test_linked_payments_immutable(self):
        reset(fixture(ops=["u_cy"]))
        t = {h: login(h) for h in ("ada", "bob", "cy")}
        sp = call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 5}]},
                  t["cy"], "s")[1]["payments"][0]
        self.err(corr(t["ada"], sp["payment_id"], "k", expected_revision=1, amount=1, effective_at=sp["created_at"],
                      reason="x"), 422, "linked_payment_immutable")
        revs = call("GET", "/payments/%s/revisions" % sp["payment_id"], token=t["ada"])[1]["revisions"]
        self.assertEqual(revs[0]["effective_at"], sp["created_at"])
        a = call("POST", "/authorizations", {"to_handle": "bob", "amount": 50}, t["ada"], "a")[1]
        cp = call("POST", "/authorizations/%s/capture" % a["authorization_id"], {}, t["bob"], "c")[1]
        self.err(corr(t["ada"], cp["payment_id"], "k", expected_revision=1, amount=1, effective_at=cp["created_at"],
                      reason="x"), 422, "linked_payment_immutable")

    def test_concurrent_same_expected_revision(self):
        a = self.t["ada"]
        p = self.pay(amount=100)
        with ThreadPoolExecutor(20) as ex:
            res = list(ex.map(lambda i: corr(a, p["payment_id"], "k%d" % i, expected_revision=1, amount=100 + i,
                                             effective_at=p["created_at"], reason="r")[0], range(20)))
        self.assertEqual(sorted(res), [201] + [409] * 19)
        self.assertEqual(sum(me(self.t[h])["balance"] for h in self.t), 13000)


class HistoricalHolds(Base):
    def test_hold_lifecycle_history(self):
        a = self.t["ada"]
        before = iso_in(0)
        h = call("POST", "/authorizations", {"to_handle": "bob", "amount": 2000}, a, "a")[1]
        self.assertIsNone(h["closed_at"])
        c = call("POST", "/authorizations/%s/capture" % h["authorization_id"], {"amount": 500, "final": False},
                 self.t["bob"], "c")[1]
        mid = iso_in(0)
        v = call("POST", "/authorizations/%s/void" % h["authorization_id"], None, a)[1]
        self.assertTrue(v["closed_at"])
        view = lambda **kw: call("GET", "/me" + q(**kw), token=a)[1]
        m = view(as_of=before)
        self.assertEqual((m["total"], m["held"], m["available"]), (10000, 0, 10000))
        m = view(as_of=h["created_at"])
        self.assertEqual((m["total"], m["held"], m["available"]), (10000, 2000, 8000))
        m = view(as_of=c["created_at"])
        self.assertEqual((m["total"], m["held"], m["available"]), (9500, 1500, 8000))
        m = view(as_of=v["closed_at"])
        self.assertEqual((m["total"], m["held"], m["available"]), (9500, 0, 9500))
        # not yet knowing the void, the hold runs on until its deadline
        m = view(as_of=iso_in(60), known_at=mid)
        self.assertEqual((m["held"], m["available"]), (1500, 8000))
        m = view(as_of=ts_after(h["expires_at"], 0), known_at=mid)
        self.assertEqual(m["held"], 0)
        m = view(known_at=mid)  # as_of = now
        self.assertEqual(m["held"], 1500)
        st = call("GET", "/statement", token=a)[1]
        self.assertEqual([(e["payment"]["payment_id"], e["payment"]["authorization_id"]) for e in st["entries"]],
                         [(c["payment_id"], h["authorization_id"])])

    def test_expiry_closed_at(self):
        import time
        reset(dict(fixture(), authorization_ttl_seconds=1))
        a = login("ada")
        h = call("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, a, "a")[1]
        time.sleep(1.1)
        got = call("GET", "/authorizations", token=a)[1]["authorizations"][0]
        self.assertEqual((got["status"], got["closed_at"]), ("expired", h["expires_at"]))
        m = call("GET", "/me" + q(as_of=ts_after(h["expires_at"], -1)), token=a)[1]
        self.assertEqual(m["held"], 100)
        m = call("GET", "/me" + q(as_of=h["expires_at"]), token=a)[1]
        self.assertEqual(m["held"], 0)


class Stage3Import(Stage1Import):
    def test_stage1_export_history(self):
        exp, tok, pay, rq = self.stage1_export()
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        st = call("GET", "/statement", token=tok)[1]
        self.assertEqual((st["opening_balance"], st["closing_balance"], [e["delta"] for e in st["entries"]]),
                         (10000, 9750, [-250]))
        s, r, _ = corr(tok, pay["payment_id"], "k", expected_revision=1, amount=200, effective_at=pay["created_at"],
                       reason="fix")
        self.assertEqual(s, 201, r)
        exp3 = call("GET", "/_test/export")[1]
        reset()
        self.assertEqual(call("POST", "/_test/import", exp3)[0], 204)
        self.assertEqual(len(call("GET", "/payments/%s/revisions" % pay["payment_id"], token=tok)[1]["revisions"]), 2)
        self.assertEqual(call("GET", "/statement" + q(snapshot=st["snapshot"]), token=tok)[1]["closing_balance"], 9750)
        self.assertEqual(me(tok)["balance"], 9800)

    def test_stage2_export_with_captures(self):
        import os, subprocess, socket, time
        here = os.path.dirname(os.path.abspath(__file__))
        s2 = os.path.join(here, "..", "stage-2", "server.py")
        if not os.path.exists(s2):
            self.skipTest("stage-2 source not present")
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        proc = subprocess.Popen([sys.executable, s2], env=dict(os.environ, PORT=str(port)), cwd=os.path.dirname(s2),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        global HOST
        saved = HOST
        try:
            HOST = "127.0.0.1:%d" % port
            for _ in range(100):
                try:
                    http.client.HTTPConnection(HOST, timeout=1).request("GET", "/health"); break
                except OSError:
                    time.sleep(0.05)
            reset()
            ada, bob = login("ada"), login("bob")
            h = call("POST", "/authorizations", {"to_handle": "bob", "amount": 2000}, ada, "a")[1]
            c = call("POST", "/authorizations/%s/capture" % h["authorization_id"], {"amount": 500, "final": False},
                     bob, "c")[1]
            exp = call("GET", "/_test/export")[1]
        finally:
            HOST = saved
            proc.terminate(); proc.wait()
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        m = me(ada)
        self.assertEqual((m["total"], m["held"], m["available"]), (9500, 1500, 8000))
        m = call("GET", "/me" + q(as_of=c["created_at"]), token=ada)[1]
        self.assertEqual((m["total"], m["held"]), (9500, 1500))
        m = call("GET", "/me" + q(as_of=ts_after(h["created_at"], -1)), token=ada)[1]
        self.assertEqual((m["total"], m["held"]), (10000, 0))
        self.assertEqual(call("POST", "/authorizations/%s/capture" % h["authorization_id"], {"amount": 500, "final": False},
                              bob, "c")[:2], (200, c))
        self.err(corr(ada, c["payment_id"], "k", expected_revision=1, amount=1, effective_at=c["created_at"],
                      reason="x"), 422, "linked_payment_immutable")

    def err(self, resp, status, code):
        self.assertEqual((resp[0], resp[1]["error"]["code"]), (status, code), resp)


class SignupHistory(Base):
    """Regression (coordinator 1b39fa08): accounts created by signup open at zero in every historical read."""

    def test_signup_user_history(self):
        b = call("POST", "/auth/signup", {"email": "new@example.com", "password": "correct horse",
                                          "display_name": "N"})[1]
        tok = b["token"]
        for path in ("/me" + q(as_of="2026-10-04T00:00:00Z"), "/me" + q(known_at=iso_in(0)),
                     "/me" + q(as_of=iso_in(3600), known_at=iso_in(0))):
            s, m, _ = call("GET", path, token=tok)
            self.assertEqual((s, m["balance"], m["available"], m["held"]), (200, 0, 0, 0), path)
        s, st, _ = call("GET", "/statement", token=tok)
        self.assertEqual((s, st["entries"], st["opening_balance"], st["closing_balance"]), (200, [], 0, 0))
        self.assertEqual(call("GET", "/statement" + q(snapshot=st["snapshot"], limit=1), token=tok)[0], 200)
        p = call("POST", "/payments", {"to_handle": "new", "amount": 300}, self.t["ada"], "k")[1]
        s, st2, _ = call("GET", "/statement", token=tok)
        self.assertEqual((st2["opening_balance"], st2["closing_balance"], [e["delta"] for e in st2["entries"]]),
                         (0, 300, [300]))
        self.assertEqual(call("GET", "/me" + q(as_of=ts_after(p["created_at"], -1)), token=tok)[1]["balance"], 0)
        call("POST", "/payments", {"to_handle": "bob", "amount": 100}, tok, "k")
        call("POST", "/authorizations", {"to_handle": "bob", "amount": 50}, tok, "a")
        self.assertEqual(corr(tok, call("GET", "/activity", token=tok)[1]["payments"][0]["payment_id"], "c",
                              expected_revision=1, amount=90, effective_at=iso_in(0), reason="r")[0], 201)
        exp = call("GET", "/_test/export")[1]
        reset()
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        m = call("GET", "/me" + q(as_of=iso_in(3600)), token=tok)[1]
        self.assertEqual((m["balance"], m["held"]), (210, 0))  # hold expires by its deadline in that view
        self.assertEqual(me(tok)["held"], 50)
        st3 = call("GET", "/statement", token=tok)[1]
        self.assertEqual((st3["opening_balance"], st3["closing_balance"]), (0, 210))
        self.assertEqual(call("GET", "/statement" + q(snapshot=st2["snapshot"]), token=tok)[1]["closing_balance"], 300)


class Stage2ImportHolds(unittest.TestCase):
    """Regression (coordinator 5e818548, D2/D3): imported stage-2 holds that are closed, or whose deadline is not
    after creation, contribute nothing to any view; held and available are never negative."""

    def stage2_export(self, fx, actions=None):
        import os, subprocess, socket, time
        global HOST
        here = os.path.dirname(os.path.abspath(__file__))
        s2 = os.path.join(here, "..", "stage-2", "server.py")
        if not os.path.exists(s2):
            self.skipTest("stage-2 source not present")
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        proc = subprocess.Popen([sys.executable, s2], env=dict(os.environ, PORT=str(port)), cwd=os.path.dirname(s2),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        saved = HOST
        try:
            HOST = "127.0.0.1:%d" % port
            for _ in range(100):
                try:
                    http.client.HTTPConnection(HOST, timeout=1).request("GET", "/health"); break
                except OSError:
                    time.sleep(0.05)
            reset(fx)
            out = actions() if actions else None
            return call("GET", "/_test/export")[1], out
        finally:
            HOST = saved
            proc.terminate(); proc.wait()

    def views(self, tok, instants):
        for t in instants:
            m = call("GET", "/me" + q(as_of=t), token=tok)[1]
            self.assertGreaterEqual(m["held"], 0, t)
            self.assertGreaterEqual(m["available"], 0, t)
            self.assertEqual(m["available"], m["total"] - m["held"], t)
            yield m

    def hold_fx(self, **hold):
        return dict(fixture(users=[user("ada", 1000), user("bob", 0), user("cy", 0)]),
                    authorizations=[dict({"id": "a_1", "from_user_id": "u_ada", "to_user_id": "u_bob",
                                          "amount": 300}, **hold)])

    def test_d2_seeded_expired_hold_with_future_deadline(self):
        exp, _ = self.stage2_export(self.hold_fx(status="expired", expires_at=iso_in(3600)))
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        tok = login("ada")
        self.assertEqual(me(tok)["held"], 0)
        for m in self.views(tok, [iso_in(-3600), iso_in(5), iso_in(1800), iso_in(7200)]):
            self.assertEqual((m["held"], m["available"]), (0, 1000))

    def test_d3_open_hold_with_past_deadline(self):
        exp, _ = self.stage2_export(self.hold_fx(status="open", expires_at=iso_in(-3600)))
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        tok = login("ada")
        a = call("GET", "/authorizations", token=tok)[1]["authorizations"][0]
        for m in self.views(tok, [iso_in(-7200), iso_in(-1800), ts_after(a["created_at"], -1), a["created_at"],
                                  iso_in(0), iso_in(3600)]):
            self.assertEqual((m["held"], m["available"]), (0, 1000))

    def test_voided_and_open_api_holds(self):
        def acts():
            ada, bob = login("ada"), login("bob")
            v = call("POST", "/authorizations", {"to_handle": "bob", "amount": 200}, ada, "v")[1]
            call("POST", "/authorizations/%s/void" % v["authorization_id"], None, ada)
            o = call("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, ada, "o")[1]
            return v, o
        exp, (v, o) = self.stage2_export(fixture(users=[user("ada", 1000), user("bob", 0), user("cy", 0)]), acts)
        self.assertEqual(call("POST", "/_test/import", exp)[0], 204)
        tok = login("ada")
        got = {a["authorization_id"]: a for a in call("GET", "/authorizations", token=tok)[1]["authorizations"]}
        self.assertTrue(got[v["authorization_id"]]["closed_at"])
        self.assertIsNone(got[o["authorization_id"]]["closed_at"])
        held = [m["held"] for m in self.views(tok, [ts_after(v["created_at"], -1), v["created_at"],
                                                     ts_after(o["created_at"], -1), o["created_at"], iso_in(0),
                                                     ts_after(o["expires_at"], 0)])]
        self.assertEqual(held, [0, 0, 0, 100, 100, 0])
        self.assertEqual(me(tok)["held"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=1)
