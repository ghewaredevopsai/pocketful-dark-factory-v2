"""Own browser tests for the stage-2 UI. Needs Playwright (python) with Chromium.

Run: <python-with-playwright> test_ui.py [base_url]
Without a base URL the server is started in-process on a free port.
"""
import datetime
import json
import os
import sys
import threading
import time
import unittest
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv.pop(1) if len(sys.argv) > 1 and sys.argv[1].startswith("http") else None
SHOTS = os.environ.get("SHOTS")  # directory for screenshots, optional

if BASE is None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def http(method, path, body=None, token=None, key=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if key:
        req.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            t = r.read()
            return r.status, json.loads(t) if t else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def user(handle, balance):
    return {"id": "u_" + handle, "email": handle + "@example.com", "password": "correct horse",
            "display_name": handle.title(), "handle": handle, "balance": balance}


def fixture(currency="EUR", mu=2, **extra):
    fx = {"currency": currency, "minor_units": mu,
          "users": [user("ada", 10000), user("bob", 2500), user("cy", 500)]}
    fx.update(extra)
    return fx


def reset(fx=None):
    assert http("POST", "/_test/reset", fx or fixture())[0] == 204


def token(handle):
    return http("POST", "/auth/login", {"email": handle + "@example.com", "password": "correct horse"})[1]["token"]


def sel(t):
    return "[data-testid='%s']" % t


class UI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        reset()
        self.ctx = self.browser.new_context(base_url=BASE)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(5000)

    def tearDown(self):
        self.ctx.close()

    def login(self, handle="ada"):
        p = self.page
        p.goto("/login")
        p.fill(sel("login-email"), handle + "@example.com")
        p.fill(sel("login-password"), "correct horse")
        p.click(sel("login-submit"))
        p.wait_for_selector(sel("current-user"))

    def amount(self, testid):
        return self.page.get_attribute(sel(testid), "data-amount")

    def wait_amount(self, testid, value):
        self.page.wait_for_selector("%s[data-amount='%s']" % (sel(testid), value))

    def pay(self, handle="bob", amount="15.00", note=None):
        p = self.page
        p.wait_for_selector(sel("pay-submit"))
        p.fill(sel("pay-handle"), handle)
        p.fill(sel("pay-amount"), amount)
        if note is not None:
            p.fill(sel("pay-note"), note)
        p.click(sel("pay-submit"))

    def feed_count(self, handle="ada"):
        return len(http("GET", "/activity", token=token(handle))[1]["payments"])

    # ---- formatting and parsing

    def test_decimal_parsing_rules(self):
        self.login()
        for typed, expect_minor in (("15", 1500), ("15.5", 1550), ("0.01", 1)):
            before = int(self.amount("wallet-balance") or 10000)
            self.pay(amount=typed, note=typed)
            self.wait_amount("wallet-balance", before - expect_minor)
        before = self.amount("wallet-balance")
        for bad in ("15.005", "abc", "1,5", "-3", ""):
            self.pay(amount=bad, note="x" + bad)
            self.page.wait_for_selector(sel("pay-error"))
            self.assertEqual(self.amount("wallet-balance"), before)

    def test_jpy_and_bhd_formatting(self):
        for cur, mu, typed, minor, text in (("JPY", 0, "1200", 1200, "8800 JPY"),
                                            ("BHD", 3, "1.234", 1234, "8.766 BHD")):
            reset(fixture(cur, mu))
            self.login()
            self.page.goto("/")
            self.wait_amount("wallet-balance", 10000)
            self.pay(amount=typed)
            self.wait_amount("wallet-balance", 10000 - minor)
            self.assertEqual(self.page.text_content(sel("wallet-balance")).strip(), text)
            self.assertEqual(self.page.text_content(sel("wallet-available")).strip(), text)
            self.pay(amount=typed + "1" if mu == 0 else typed + "5", note="n")
            if mu == 0:
                self.pay(amount="1.5", note="m")
            self.page.wait_for_selector(sel("pay-error"))

    # ---- same-key retries

    def test_double_submit_and_change(self):
        self.login()
        self.pay()
        self.wait_amount("wallet-balance", 8500)
        self.page.click(sel("pay-submit"))
        self.page.wait_for_timeout(400)
        self.assertEqual(self.amount("wallet-balance"), "8500")
        self.assertIsNone(self.page.query_selector(sel("pay-error")))
        self.assertEqual(self.page.input_value(sel("pay-amount")), "15.00")  # values kept
        self.page.fill(sel("pay-note"), "changed")
        self.page.click(sel("pay-submit"))
        self.wait_amount("wallet-balance", 7000)
        self.assertEqual(self.feed_count(), 2)

    def test_lost_response_is_uncertain_then_retry_once(self):
        self.login()
        p = self.page
        lost = {"n": 0}

        def drop_after_commit(route):
            if route.request.method == "POST" and lost["n"] == 0:
                lost["n"] += 1
                route.fetch()          # reaches the server and commits
                route.abort()          # but the browser never sees the response
            else:
                route.continue_()
        p.route("**/payments", drop_after_commit)
        self.pay(amount="20.00")
        p.wait_for_selector(sel("pay-uncertain"))
        self.assertTrue(p.text_content(sel("pay-uncertain")).strip())
        self.assertIsNone(p.query_selector(sel("pay-error")))
        p.click(sel("pay-submit"))  # same body, same key
        self.wait_amount("wallet-balance", 8000)
        p.wait_for_selector(sel("pay-uncertain"), state="detached")
        self.assertIsNone(p.query_selector(sel("pay-error")))
        self.assertEqual(self.feed_count(), 1)

    def test_lost_response_before_commit_retry_pays(self):
        self.login()
        p = self.page
        state = {"n": 0}

        def drop(route):
            if route.request.method == "POST" and state["n"] == 0:
                state["n"] += 1
                route.abort()
            else:
                route.continue_()
        p.route("**/payments", drop)
        self.pay(amount="20.00")
        p.wait_for_selector(sel("pay-uncertain"))
        self.assertEqual(self.amount("wallet-balance"), "10000")
        p.click(sel("pay-submit"))
        self.wait_amount("wallet-balance", 8000)
        self.assertEqual(self.feed_count(), 1)

    def test_refused_payment_keeps_inputs_and_refreshes(self):
        self.login("cy")
        p = self.page
        p.goto("/")
        self.wait_amount("wallet-balance", 500)
        http("POST", "/payments", {"to_handle": "bob", "amount": 400}, token("cy"), "other")  # another client
        self.pay(amount="2.00", note="lunch")
        p.wait_for_selector(sel("pay-error"))
        self.wait_amount("wallet-balance", 100)
        self.assertEqual((p.input_value(sel("pay-handle")), p.input_value(sel("pay-amount")),
                          p.input_value(sel("pay-note"))), ("bob", "2.00", "lunch"))

    # ---- latest refresh wins

    def test_latest_refresh_wins_out_of_order(self):
        self.login()
        p = self.page
        p.goto("/")
        self.wait_amount("wallet-balance", 10000)
        calls = {"n": 0}

        def slow_first(route):
            calls["n"] += 1
            if calls["n"] == 1:
                resp = route.fetch()          # read the OLD state now ...
                time.sleep(1.0)               # ... and deliver it late
                route.fulfill(response=resp)
            else:
                route.continue_()
        p.route("**/me", slow_first)
        p.click(sel("wallet-refresh"))        # read 1 (old state, delayed)
        p.wait_for_timeout(150)
        http("POST", "/payments", {"to_handle": "bob", "amount": 300}, token("ada"), "x")
        p.click(sel("wallet-refresh"))        # read 2 (new state, fast)
        self.wait_amount("wallet-balance", 9700)
        p.wait_for_timeout(1500)              # read 1 arrives now and must be ignored
        self.assertEqual(self.amount("wallet-balance"), "9700")

    def test_refresh_keeps_pay_form(self):
        self.login()
        p = self.page
        p.goto("/")
        p.fill(sel("pay-handle"), "bob")
        p.fill(sel("pay-amount"), "3.00")
        p.click(sel("wallet-refresh"))
        p.wait_for_timeout(300)
        self.assertEqual(p.input_value(sel("pay-amount")), "3.00")

    # ---- requests

    def test_request_cancelled_elsewhere(self):
        rid = http("POST", "/requests", {"payer_handle": "ada", "amount": 100}, token("bob"), "r")[1]["request_id"]
        self.login()
        p = self.page
        p.goto("/requests")
        p.wait_for_selector(sel("request-pay-" + rid))
        http("POST", "/requests/%s/cancel" % rid, None, token("bob"))
        p.click(sel("request-pay-" + rid))
        p.wait_for_selector(sel("request-error"))
        p.wait_for_selector(sel("request-pay-" + rid), state="detached")
        self.assertEqual(p.get_attribute(sel("request-item-" + rid), "data-status"), "cancelled")

    def test_request_form_on_wallet(self):
        self.login()
        p = self.page
        p.fill(sel("request-handle"), "bob")
        p.fill(sel("request-amount"), "4.50")
        p.click(sel("request-submit"))
        p.wait_for_selector(sel("request-success"))
        got = http("GET", "/requests", token=token("bob"))[1]["requests"]
        self.assertEqual([(r["amount"], r["payer_handle"]) for r in got], [(450, "bob")])
        p.fill(sel("request-handle"), "bob")
        p.fill(sel("request-amount"), "1.001")
        p.click(sel("request-submit"))
        p.wait_for_selector(sel("request-error"))

    # ---- split

    def test_split_preview_matches_server_and_bhd(self):
        reset(fixture("BHD", 3))
        self.login()
        p = self.page
        p.goto("/split")
        p.fill(sel("split-amount"), "0.010")
        p.fill(sel("split-handles"), "bob, ada ,cy")
        p.wait_for_selector(sel("split-preview"))
        shown = [p.text_content(sel("split-share-" + h)).strip() for h in ("bob", "ada", "cy")]
        self.assertEqual(shown, ["0.004 BHD", "0.003 BHD", "0.003 BHD"])
        p.click(sel("split-submit"))
        p.wait_for_selector(sel("split-success"))
        reqs = {r["payer_handle"]: r["amount"] for r in http("GET", "/requests", token=token("ada"))[1]["requests"]}
        self.assertEqual(reqs, {"bob": 4, "cy": 3})

    # ---- holds

    def test_holds_flow(self):
        exp = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).isoformat()
        reset(fixture(authorizations=[{"id": "a_1", "from_user_id": "u_ada", "to_user_id": "u_bob",
                                       "amount": 2000, "status": "open", "expires_at": exp}]))
        self.login()
        p = self.page
        p.goto("/")
        self.wait_amount("wallet-available", 8000)
        self.assertEqual(self.amount("wallet-balance"), "10000")
        self.assertEqual(self.amount("wallet-held"), "2000")
        self.assertEqual(p.text_content(sel("wallet-available")).strip(), "80.00 EUR")
        p.fill(sel("authorize-handle"), "cy")
        p.fill(sel("authorize-amount"), "80.01")
        p.click(sel("authorize-submit"))
        p.wait_for_selector(sel("authorize-error"))
        p.goto("/authorizations")
        p.wait_for_selector(sel("authorization-item-a_1"))
        self.assertIsNotNone(p.query_selector(sel("authorization-void-a_1")))
        self.assertIsNone(p.query_selector(sel("authorization-capture-a_1")))
        self.assertEqual(p.text_content(sel("authorization-expires-a_1")).strip(),
                         http("GET", "/authorizations", token=token("ada"))[1]["authorizations"][0]["expires_at"])
        # bob captures part of it
        p.click(sel("logout-button"))
        p.wait_for_selector(sel("current-user"), state="detached")
        self.login("bob")
        p.goto("/authorizations")
        self.assertEqual(p.input_value(sel("authorization-capture-amount-a_1")), "20.00")
        p.fill(sel("authorization-capture-amount-a_1"), "15.00")
        p.click(sel("authorization-capture-a_1"))
        p.wait_for_selector("%s[data-status='captured']" % sel("authorization-item-a_1"))
        self.assertEqual(p.text_content(sel("authorization-captured-a_1")).strip(), "15.00 EUR")
        self.wait_amount("wallet-balance", 4000)
        self.assertIsNone(p.query_selector(sel("wallet-held")))
        p.click(sel("logout-button"))
        self.login("ada")
        p.goto("/")
        self.wait_amount("wallet-available", 8500)
        self.assertIsNone(p.query_selector(sel("wallet-held")))

    def test_authorize_then_void_and_empty(self):
        self.login("cy")
        p = self.page
        p.goto("/authorizations")
        p.wait_for_selector(sel("empty-authorizations"))
        p.fill(sel("authorize-handle"), "bob")
        p.fill(sel("authorize-amount"), "2.00")
        p.click(sel("authorize-submit"))
        self.wait_amount("wallet-held", 200)
        self.assertEqual(self.amount("wallet-available"), "300")
        aid = http("GET", "/authorizations", token=token("cy"))[1]["authorizations"][0]["authorization_id"]
        p.wait_for_selector(sel("authorization-void-" + aid))
        p.click(sel("authorization-void-" + aid))
        p.wait_for_selector("%s[data-status='voided']" % sel("authorization-item-" + aid))
        p.wait_for_selector(sel("wallet-held"), state="detached")
        # capture refused after it was voided elsewhere -> authorization-error
        a2 = http("POST", "/authorizations", {"to_handle": "ada", "amount": 100}, token("bob"), "k")[1]["authorization_id"]
        p.click(sel("logout-button"))
        self.login("ada")
        p.goto("/authorizations")
        p.wait_for_selector(sel("authorization-capture-" + a2))
        http("POST", "/authorizations/%s/void" % a2, None, token("bob"))
        p.click(sel("authorization-capture-" + a2))
        p.wait_for_selector(sel("authorization-error"))
        p.wait_for_selector(sel("authorization-capture-" + a2), state="detached")

    # ---- upgrade from stage 1 (export/import between browser requests)

    def test_session_and_pending_retry_survive_import(self):
        self.login()
        p = self.page
        drop = {"n": 0}

        def lose(route):
            if route.request.method == "POST" and drop["n"] == 0:
                drop["n"] += 1
                route.fetch()
                route.abort()
            else:
                route.continue_()
        p.route("**/payments", lose)
        self.pay(amount="12.00", note="before upgrade")
        p.wait_for_selector(sel("pay-uncertain"))
        snap = http("GET", "/_test/export")[1]
        reset(fixture(users=[user("zed", 1)]))       # destination had other data
        self.assertEqual(http("POST", "/_test/import", snap)[0], 204)
        p.click(sel("pay-submit"))                  # same key/body: recovers the original payment
        self.wait_amount("wallet-balance", 8800)
        p.wait_for_selector(sel("pay-uncertain"), state="detached")
        self.assertEqual(self.feed_count(), 1)
        p.goto("/requests")                         # still signed in after navigation
        p.wait_for_selector(sel("current-user"))

    # ---- layout

    def test_375px_no_horizontal_scroll_and_labels(self):
        rid = http("POST", "/requests", {"payer_handle": "ada", "amount": 123456, "note": "x" * 200},
                   token("bob"), "r")[1]["request_id"]
        http("POST", "/payments", {"to_handle": "bob", "amount": 9999, "note": "y" * 200}, token("ada"), "p")
        http("POST", "/authorizations", {"to_handle": "ada", "amount": 100}, token("bob"), "a")
        self.ctx.close()
        self.ctx = self.browser.new_context(base_url=BASE, viewport={"width": 375, "height": 800})
        self.page = self.ctx.new_page()
        self.login()
        for route, anchor in (("/", "activity-list"), ("/requests", "request-item-" + rid), ("/split", "split-submit"),
                              ("/authorizations", "authorization-list"), ("/login", "login-submit"),
                              ("/signup", "signup-submit")):
            self.page.goto(route)
            self.page.wait_for_selector(sel(anchor))
            self.page.wait_for_timeout(200)
            sw, cw = self.page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            self.assertLessEqual(sw, cw, route)
            unlabeled = self.page.evaluate("""[...document.querySelectorAll('input,select')]
                .filter(e => !(e.labels && e.labels.length)).map(e => e.dataset.testid)""")
            self.assertEqual(unlabeled, [], route)
            self.assertNotRegex(self.page.inner_text("body"), r"\b(null|undefined|NaN)\b", route)
            if SHOTS:
                self.page.screenshot(path=os.path.join(SHOTS, "m%s.png" % route.replace("/", "_")), full_page=True)

    def test_desktop_screenshots(self):
        if not SHOTS:
            self.skipTest("SHOTS not set")
        http("POST", "/payments", {"to_handle": "bob", "amount": 2500, "note": "dinner"}, token("ada"), "p")
        http("POST", "/payments", {"to_handle": "cy", "amount": 120, "visibility": "private"}, token("bob"), "p")
        http("POST", "/requests", {"payer_handle": "ada", "amount": 1200, "note": "taxi"}, token("bob"), "r")
        http("POST", "/authorizations", {"to_handle": "bob", "amount": 2000, "note": "deposit"}, token("ada"), "a")
        self.ctx.close()
        self.ctx = self.browser.new_context(base_url=BASE, viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.login()
        for route, anchor in (("/", "activity-list"), ("/requests", "incoming-list"), ("/split", "split-submit"),
                              ("/authorizations", "authorization-list"), ("/login", "login-submit")):
            self.page.goto(route)
            self.page.wait_for_selector(sel(anchor))
            if route == "/split":
                self.page.fill(sel("split-amount"), "30.00")
                self.page.fill(sel("split-handles"), "ada,bob,cy")
            self.page.wait_for_timeout(300)
            self.page.screenshot(path=os.path.join(SHOTS, "d%s.png" % route.replace("/", "_")), full_page=True)

    def test_m3_closed_holds_do_not_say_expired(self):
        a1 = http("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, token("ada"), "a1")[1]["authorization_id"]
        a2 = http("POST", "/authorizations", {"to_handle": "bob", "amount": 100}, token("ada"), "a2")[1]["authorization_id"]
        http("POST", "/authorizations/%s/capture" % a1, {}, token("bob"), "c")
        http("POST", "/authorizations/%s/void" % a2, None, token("ada"))
        self.login()
        p = self.page
        p.goto("/authorizations")
        for aid, word in ((a1, "Collected"), (a2, "Released")):
            p.wait_for_selector(sel("authorization-item-" + aid))
            text = p.inner_text(sel("authorization-item-" + aid))
            self.assertNotIn("Expire", text)
            self.assertIn(word, text)
            self.assertTrue(p.text_content(sel("authorization-expires-" + aid)).strip())

    def test_m4_feed_follows_pay_form_on_phones(self):
        http("POST", "/payments", {"to_handle": "bob", "amount": 100}, token("ada"), "p")
        self.ctx.close()
        self.ctx = self.browser.new_context(base_url=BASE, viewport={"width": 375, "height": 800})
        self.page = self.ctx.new_page()
        self.login()
        p = self.page
        p.wait_for_selector(sel("activity-list"))
        top = lambda t: p.eval_on_selector(sel(t), "e => e.getBoundingClientRect().top + window.scrollY")
        self.assertLess(top("activity-list"), top("request-submit"))
        self.assertLess(top("pay-submit"), top("activity-list"))
        self.assertLess(top("activity-list"), 1100)
        for t in ("request-handle", "authorize-handle", "authorize-submit"):
            self.assertIsNotNone(p.query_selector(sel(t)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
