#!/usr/bin/env python3
"""Reviewer UI probes for stage 2 in a real browser (Python Playwright + Chromium).

Usage: python ui_review.py --base http://127.0.0.1:18281 [--shots DIR] [--only NAME]
Every request to a host other than --base is aborted and recorded (network blocked for external assets).
Prints PASS/FAIL per check and a TOTAL line; exit 1 on any failure.
"""
import argparse
import datetime
import json
import sys
import threading
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = None
SHOTS = None
RESULTS = []
EXTERNAL = []
K = [0]


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -- " + str(detail)[:400]))
    sys.stdout.flush()


def api(method, path, body=None, token=None, key=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if key:
        h["Idempotency-Key"] = key
    req = urllib.request.Request(BASE + path, json.dumps(body).encode() if body is not None else None, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def key():
    K[0] += 1
    return "ui-%d-%d" % (time.time_ns(), K[0])


def iso_in(s):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=s)).isoformat()


LONG = "x" * 200


def fixture(currency="EUR", mu=2):
    return {
        "currency": currency, "minor_units": mu, "authorization_ttl_seconds": 600,
        "users": [
            {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada Lovelace-Byron the Countess", "handle": "ada", "balance": 10000},
            {"id": "u_bob", "email": "bob@example.com", "password": "correct horse", "display_name": "Bob", "handle": "bob", "balance": 2500},
            {"id": "u_cy", "email": "cy@example.com", "password": "correct horse", "display_name": "Cy", "handle": "abcdefghijklmnopqrst", "balance": 0},
        ],
        "payments": [
            {"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500, "note": "coffee ☕", "visibility": "public"},
            {"id": "p_2", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 1, "note": LONG, "visibility": "private"},
            {"id": "p_3", "from_user_id": "u_bob", "to_user_id": "u_cy", "amount": 7, "note": "", "visibility": "public"},
        ],
        "requests": [
            {"id": "rq_in", "requester_id": "u_bob", "payer_id": "u_ada", "amount": 1200, "note": "taxi", "status": "pending"},
            {"id": "rq_out", "requester_id": "u_ada", "payer_id": "u_bob", "amount": 300, "note": LONG, "status": "pending"},
            {"id": "rq_paid", "requester_id": "u_ada", "payer_id": "u_bob", "amount": 50, "note": "", "status": "paid", "payment_id": "p_2"},
        ],
        "authorizations": [
            {"id": "a_out", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 2000, "note": "deposit", "visibility": "public", "status": "open", "expires_at": iso_in(7200)},
            {"id": "a_in", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 1000, "note": LONG, "visibility": "private", "status": "open", "expires_at": iso_in(7200)},
            {"id": "a_cap", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 400, "note": "", "visibility": "public", "status": "captured", "captured_amount": 300, "expires_at": iso_in(7200)},
            {"id": "a_void", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 100, "note": "", "visibility": "public", "status": "voided", "expires_at": iso_in(7200)},
            {"id": "a_exp", "from_user_id": "u_bob", "to_user_id": "u_ada", "amount": 100, "note": "", "visibility": "public", "status": "open", "expires_at": iso_in(-7200)},
        ],
    }


def reset(fx):
    s, js = api("POST", "/_test/reset", fx)
    assert s == 204, (s, js)


def token(email):
    return api("POST", "/auth/login", {"email": email, "password": "correct horse"})[1]["token"]


class Browser:
    def __init__(self, pw):
        self.b = pw.chromium.launch()

    def page(self, width=1280, height=800):
        ctx = self.b.new_context(viewport={"width": width, "height": height})
        base_host = urllib.parse.urlsplit(BASE).netloc

        def gate(route):
            u = urllib.parse.urlsplit(route.request.url)
            if u.scheme in ("data", "blob") or u.netloc == base_host:
                return route.continue_()
            EXTERNAL.append(route.request.url)
            return route.abort()

        ctx.route("**/*", gate)
        p = ctx.new_page()
        p.on("pageerror", lambda e: EXTERNAL.append("JS ERROR: %s" % e))
        return p

    def login(self, p, email="ada@example.com"):
        p.goto(BASE + "/login")
        p.get_by_test_id("login-email").fill(email)
        p.get_by_test_id("login-password").fill("correct horse")
        p.get_by_test_id("login-submit").click()
        p.get_by_test_id("wallet-available").wait_for()


def tid(p, t):
    return p.get_by_test_id(t)


def text(p, t):
    return tid(p, t).inner_text().strip()


def shot(p, name):
    if SHOTS:
        p.screenshot(path="%s/%s.png" % (SHOTS, name), full_page=True)


LAYOUT_JS = """() => {
  const vw = document.documentElement.clientWidth;
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (el.closest('.sr-only, .skip')) continue;
    if (r.right > vw + 1 || r.left < -1) bad.push((el.getAttribute('data-testid') || el.tagName + '.' + el.className) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return {scrollW: document.documentElement.scrollWidth, bodyScrollW: document.body.scrollWidth, vw, bad: bad.slice(0, 8), nbad: bad.length};
}"""

LABEL_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (el.type === 'hidden' || !el.getBoundingClientRect().width) continue;
    const lab = (el.labels && el.labels.length && el.labels[0].innerText.trim()) || el.getAttribute('aria-label') || '';
    const visible = el.labels && el.labels.length && el.labels[0].getBoundingClientRect().width > 0;
    if (!lab || !visible) out.push(el.getAttribute('data-testid') || el.name);
  }
  return out;
}"""

CONTRAST_JS = """() => {
  function rgb(s) { const m = s.match(/[\\d.]+/g); return m ? m.map(Number) : [0,0,0,0]; }
  function lum(c) { const a = c.slice(0,3).map(v => { v /= 255; return v <= .03928 ? v/12.92 : Math.pow((v+.055)/1.055, 2.4); });
    return .2126*a[0] + .7152*a[1] + .0722*a[2]; }
  function bg(el) {
    while (el) { const cs = getComputedStyle(el);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') { const m = cs.backgroundImage.match(/#[0-9a-f]{6}|rgba?\\([^)]*\\)/i);
        if (m) { if (m[0][0] === '#') { const h = m[0]; return [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16), 1]; } return rgb(m[0]); } }
      const c = rgb(cs.backgroundColor); if (c.length < 4 || c[3] > 0.5) return c; el = el.parentElement; }
    return [255,255,255,1];
  }
  const worst = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; const seen = new Set();
  while ((n = walker.nextNode())) {
    const el = n.parentElement; if (!n.textContent.trim() || seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
    if (el.closest('.sr-only, .skip')) continue;
    const cs = getComputedStyle(el); if (cs.visibility === 'hidden') continue;
    const fg = rgb(cs.color), b = bg(el);
    const L1 = lum(fg), L2 = lum(b); const ratio = (Math.max(L1,L2) + .05) / (Math.min(L1,L2) + .05);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    if (ratio < need) worst.push(n.textContent.trim().slice(0, 30) + ' ' + ratio.toFixed(2) + '<' + need);
  }
  return worst.slice(0, 10);
}"""

FOCUS_JS = """() => { const el = document.activeElement; if (!el || el === document.body) return null;
  const cs = getComputedStyle(el);
  return {tag: el.tagName, tid: el.getAttribute('data-testid'), outline: cs.outlineStyle + ' ' + cs.outlineWidth, shadow: cs.boxShadow}; }"""


def layout_checks(p, label):
    for js, nm in ((LABEL_JS, "every visible input has a visible label"),):
        out = p.evaluate(js)
        check("%s: %s" % (label, nm), out == [], out)
    lay = p.evaluate(LAYOUT_JS)
    check("%s: no horizontal overflow (scrollWidth %d vs %d, %d elements beyond viewport)" % (label, lay["scrollW"], lay["vw"], lay["nbad"]),
          lay["scrollW"] <= lay["vw"] and lay["nbad"] == 0, lay["bad"])
    low = p.evaluate(CONTRAST_JS)
    check("%s: text contrast meets WCAG AA" % label, low == [], low)


# ------------------------------------------------------------------ probes

def u_routes_and_testids(br):
    reset(fixture())
    for w, h in ((375, 812), (1280, 800)):
        p = br.page(w, h)
        br.login(p)
        tag = "%dpx" % w
        p.wait_for_selector("[data-testid=activity-list]")
        check("%s /: current-user has display name" % tag, "Ada Lovelace" in text(p, "current-user"))
        check("%s /: current-handle exactly 'ada'" % tag, tid(p, "current-handle").inner_text() == "ada", tid(p, "current-handle").inner_text())
        av, bal, held = tid(p, "wallet-available"), tid(p, "wallet-balance"), tid(p, "wallet-held")
        check("%s /: wallet-available 80.00 EUR data-amount 8000" % tag, av.inner_text().strip() == "80.00 EUR" and av.get_attribute("data-amount") == "8000", av.inner_text())
        check("%s /: wallet-balance 100.00 EUR (total)" % tag, bal.inner_text().strip() == "100.00 EUR" and bal.get_attribute("data-amount") == "10000")
        check("%s /: wallet-held 20.00 EUR" % tag, held.inner_text().strip() == "20.00 EUR" and held.get_attribute("data-amount") == "2000")
        fs = p.evaluate("""() => ['wallet-available','wallet-balance','wallet-held'].map(t => parseFloat(getComputedStyle(document.querySelector('[data-testid='+t+']')).fontSize))""")
        check("%s /: available is the headline (largest) amount %s" % (tag, fs), fs[0] > fs[1] * 1.5 and fs[0] > fs[2] * 1.5, fs)
        for t in ("pay-handle", "pay-amount", "pay-note", "pay-visibility", "pay-submit", "request-handle", "request-amount", "request-note",
                  "request-submit", "authorize-handle", "authorize-amount", "authorize-note", "authorize-visibility", "authorize-submit",
                  "wallet-refresh", "logout-button", "activity-list"):
            check("%s /: %s visible" % (tag, t), tid(p, t).is_visible())
        opts = p.evaluate("() => [...document.querySelector('[data-testid=pay-visibility]').options].map(o => o.value)")
        check("%s /: pay-visibility options public/private" % tag, sorted(opts) == ["private", "public"], opts)
        for t in ("pay-error", "request-error", "authorize-error", "empty-activity", "auth-error"):
            check("%s /: %s absent initially" % (tag, t), tid(p, t).count() == 0)
        ids = p.evaluate("() => [...document.querySelector('[data-testid=activity-list]').children].map(c => c.getAttribute('data-testid'))")
        check("%s /: feed newest first, own private visible, no request/hold items" % tag, ids == ["activity-item-p_3", "activity-item-p_2", "activity-item-p_1"], ids)
        check("%s /: activity-amount exact" % tag, text(p, "activity-amount-p_1") == "5.00 EUR" and text(p, "activity-amount-p_2") == "0.01 EUR")
        check("%s /: activity-note exact, empty note present" % tag, tid(p, "activity-note-p_1").inner_text() == "coffee ☕" and tid(p, "activity-note-p_3").count() == 1 and tid(p, "activity-note-p_3").inner_text() == "")
        par = text(p, "activity-parties-p_3")
        check("%s /: parties contain both handles" % tag, "bob" in par and "abcdefghijklmnopqrst" in par, par)
        check("%s /: data-visibility" % tag, tid(p, "activity-item-p_2").get_attribute("data-visibility") == "private" and tid(p, "activity-item-p_1").get_attribute("data-visibility") == "public")
        layout_checks(p, "%s /" % tag)
        shot(p, "wallet-%d" % w)
        # requests
        p.get_by_role("link", name="Requests").click()
        p.wait_for_selector("[data-testid=incoming-list]")
        p.wait_for_selector("[data-testid=request-item-rq_in]")
        check("%s /requests via nav link" % tag, p.url.endswith("/requests"))
        check("%s /requests: incoming pending has pay+decline, no cancel" % tag, tid(p, "request-pay-rq_in").is_visible() and tid(p, "request-decline-rq_in").is_visible() and tid(p, "request-cancel-rq_in").count() == 0)
        check("%s /requests: outgoing pending has cancel only" % tag, tid(p, "request-cancel-rq_out").is_visible() and tid(p, "request-pay-rq_out").count() == 0)
        check("%s /requests: paid has no buttons, data-status paid" % tag, tid(p, "request-item-rq_paid").get_attribute("data-status") == "paid" and tid(p, "request-cancel-rq_paid").count() == 0)
        check("%s /requests: request-amount exact" % tag, text(p, "request-amount-rq_in") == "12.00 EUR")
        check("%s /requests: incoming in incoming-list" % tag, tid(p, "incoming-list").get_by_test_id("request-item-rq_in").count() == 1 and tid(p, "outgoing-list").get_by_test_id("request-item-rq_out").count() == 1)
        check("%s /requests: current-user shown" % tag, tid(p, "current-user").is_visible())
        layout_checks(p, "%s /requests" % tag)
        shot(p, "requests-%d" % w)
        # split
        p.get_by_role("link", name="Split a bill").click()
        tid(p, "split-amount").fill("10")
        tid(p, "split-handles").fill("ada, bob, abcdefghijklmnopqrst")
        p.wait_for_selector("[data-testid=split-preview]")
        sh = [text(p, "split-share-" + x) for x in ("ada", "bob", "abcdefghijklmnopqrst")]
        check("%s /split: preview 3.34/3.33/3.33" % tag, sh == ["3.34 EUR", "3.33 EUR", "3.33 EUR"], sh)
        layout_checks(p, "%s /split" % tag)
        shot(p, "split-%d" % w)
        # authorizations
        p.get_by_role("link", name="Holds").click()
        p.wait_for_selector("[data-testid=authorization-item-a_out]")
        ids = p.evaluate("() => [...document.querySelector('[data-testid=authorization-list]').children].map(c => c.getAttribute('data-testid'))")
        check("%s /authorizations: list has all 5" % tag, sorted(ids) == sorted("authorization-item-" + x for x in ("a_out", "a_in", "a_cap", "a_void", "a_exp")), ids)
        st = {x: tid(p, "authorization-item-" + x).get_attribute("data-status") for x in ("a_out", "a_in", "a_cap", "a_void", "a_exp")}
        check("%s /authorizations: statuses" % tag, st == {"a_out": "open", "a_in": "open", "a_cap": "captured", "a_void": "voided", "a_exp": "expired"}, st)
        check("%s /authorizations: capture input prefilled 10.00 on incoming open" % tag, tid(p, "authorization-capture-amount-a_in").input_value() == "10.00" and tid(p, "authorization-capture-a_in").is_visible())
        check("%s /authorizations: void only on outgoing open" % tag, tid(p, "authorization-void-a_out").is_visible() and tid(p, "authorization-void-a_in").count() == 0 and tid(p, "authorization-capture-a_out").count() == 0)
        check("%s /authorizations: no buttons on closed" % tag, all(tid(p, "authorization-%s-%s" % (b, x)).count() == 0 for b in ("void", "capture") for x in ("a_cap", "a_void", "a_exp")))
        check("%s /authorizations: captured amount only on captured" % tag, text(p, "authorization-captured-a_cap") == "3.00 EUR" and tid(p, "authorization-captured-a_out").count() == 0)
        check("%s /authorizations: amount exact" % tag, text(p, "authorization-amount-a_out") == "20.00 EUR")
        ex = text(p, "authorization-expires-a_out")
        check("%s /authorizations: expires is RFC 3339" % tag, datetime.datetime.fromisoformat(ex).tzinfo is not None, ex)
        check("%s /authorizations: wallet shows available headline" % tag, text(p, "wallet-available") == "80.00 EUR")
        layout_checks(p, "%s /authorizations" % tag)
        shot(p, "authorizations-%d" % w)
        # direct URL load of every route while signed in
        for r in ("/", "/requests", "/split", "/authorizations"):
            p.goto(BASE + r)
            tid(p, "current-user").wait_for()
            check("%s direct URL %s keeps session and shows current-user" % (tag, r), tid(p, "current-user").is_visible())
        tid(p, "logout-button").click()
        p.wait_for_url("**/login")
        check("%s logout -> /login" % tag, tid(p, "login-email").is_visible() and tid(p, "current-user").count() == 0)
        layout_checks(p, "%s /login" % tag)
        shot(p, "login-%d" % w)
        p.goto(BASE + "/signup")
        tid(p, "signup-email").wait_for()
        layout_checks(p, "%s /signup" % tag)
        shot(p, "signup-%d" % w)
        p.context.close()


def u_auth_forms(br):
    reset(fixture())
    p = br.page()
    p.goto(BASE + "/login")
    tid(p, "login-email").fill("ada@example.com")
    tid(p, "login-password").fill("wrong password")
    tid(p, "login-submit").click()
    tid(p, "auth-error").wait_for()
    check("login wrong password -> auth-error", tid(p, "auth-error").is_visible())
    shot(p, "login-error")
    p.goto(BASE + "/signup")
    tid(p, "signup-email").fill("ada@other.com")
    tid(p, "signup-password").fill("12345678")
    tid(p, "signup-display-name").fill("Other Ada")
    tid(p, "signup-submit").click()
    tid(p, "auth-error").wait_for()
    check("signup handle taken -> auth-error", tid(p, "auth-error").is_visible())
    tid(p, "signup-email").fill("new.person@example.com")
    tid(p, "signup-submit").click()
    tid(p, "current-handle").wait_for()
    check("signup success signs in, derived handle", tid(p, "current-handle").inner_text() == "new_person" and tid(p, "auth-error").count() == 0)
    check("new user wallet 0.00 EUR, no held", text(p, "wallet-available") == "0.00 EUR" and tid(p, "wallet-held").count() == 0)
    p.context.close()


def u_focus(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    seen, bad = 0, []
    for _ in range(25):
        p.keyboard.press("Tab")
        f = p.evaluate(FOCUS_JS)
        if not f:
            continue
        seen += 1
        if f["outline"].startswith("none") and f["shadow"] in ("none", ""):
            bad.append(f)
    check("keyboard focus visible on %d tabbed elements" % seen, seen >= 15 and not bad, bad[:3])
    tid(p, "pay-amount").focus()
    shot(p, "focus-pay-amount")
    p.context.close()


def u_states(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    # loading: delay the next /activity read
    gate = threading.Event()

    def slow(route):
        time.sleep(1.5)
        route.continue_()

    p.route("**/activity*", slow)
    p.reload()
    time.sleep(0.6)
    sk = p.evaluate("() => document.querySelectorAll('.skeleton-row, .skeleton, .loading-page').length")
    check("loading state visible while data loads", sk > 0, sk)
    shot(p, "loading")
    p.unroute("**/activity*")
    p.wait_for_selector("[data-testid=activity-list]")
    # error state: refresh while the API is unreachable
    p.route("**/activity*", lambda r: r.abort())
    tid(p, "wallet-refresh").click()
    time.sleep(0.5)
    err = p.get_by_text("Couldn't refresh").count()
    check("refresh failure shows an error notice and keeps last state", err > 0 and tid(p, "activity-list").is_visible())
    shot(p, "refresh-error")
    p.unroute("**/activity*")
    p.context.close()
    # empty states
    reset({"currency": "JPY", "minor_units": 0, "users": [
        {"id": "u_ada", "email": "ada@example.com", "password": "correct horse", "display_name": "Ada", "handle": "ada", "balance": 1200}]})
    for w in (375, 1280):
        p = br.page(w, 800)
        br.login(p)
        tid(p, "empty-activity").wait_for()
        check("%dpx empty-activity shown, no activity-list" % w, tid(p, "activity-list").count() == 0)
        check("%dpx JPY formatting 1200 JPY" % w, text(p, "wallet-available") == "1200 JPY" and text(p, "wallet-balance") == "1200 JPY")
        shot(p, "empty-wallet-%d" % w)
        p.goto(BASE + "/requests")
        tid(p, "empty-requests").wait_for()
        check("%dpx empty-requests shown" % w, tid(p, "empty-requests").is_visible())
        shot(p, "empty-requests-%d" % w)
        p.goto(BASE + "/authorizations")
        tid(p, "empty-authorizations").wait_for()
        check("%dpx empty-authorizations shown" % w, tid(p, "empty-authorizations").is_visible())
        shot(p, "empty-authorizations-%d" % w)
        p.context.close()


def posts(p, path):
    rec = []
    p.on("request", lambda r: rec.append(r) if r.method == "POST" and urllib.parse.urlsplit(r.url).path == path else None)
    return rec


def u_pay_form(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    rec = posts(p, "/payments")
    tid(p, "pay-handle").fill("bob")
    for bad_amt in ("15.005", "abc", "-1", "1,5"):
        tid(p, "pay-amount").fill(bad_amt)
        tid(p, "pay-submit").click()
        tid(p, "pay-error").wait_for()
        check("amount %r -> pay-error, nothing sent" % bad_amt, tid(p, "pay-error").is_visible() and len(rec) == 0, len(rec))
    tid(p, "pay-amount").fill("15")
    tid(p, "pay-note").fill("dinner")
    tid(p, "pay-submit").click()
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-available]').dataset.amount === '6500'")
    check("15 submits 1500; available falls once", json.loads(rec[0].post_data)["amount"] == 1500 and tid(p, "pay-error").count() == 0, rec[0].post_data)
    check("form keeps values after success", tid(p, "pay-handle").input_value() == "bob" and tid(p, "pay-amount").input_value() == "15")
    new = [x for x in p.evaluate("() => [...document.querySelector('[data-testid=activity-list]').children].map(c => c.getAttribute('data-testid'))") if x not in ("activity-item-p_1", "activity-item-p_2", "activity-item-p_3")]
    check("feed shows new payment without reload", len(new) == 1, new)
    # double submit (twice quickly) without changing anything
    tid(p, "pay-submit").click()
    tid(p, "pay-submit").click()
    time.sleep(1.0)
    s, me = api("GET", "/me", token=token("ada@example.com"))
    check("resubmit unchanged form: no second payment (total 8500)", me["total"] == 8500 and tid(p, "pay-error").count() == 0, me)
    check("resubmits reused the same Idempotency-Key and body", len({(r.headers.get("idempotency-key"), r.post_data) for r in rec}) == 1, [(r.headers.get("idempotency-key"), r.post_data) for r in rec])
    n = len(p.evaluate("() => [...document.querySelector('[data-testid=activity-list]').children]"))
    check("feed contains one new payment", n == 4, n)
    tid(p, "pay-amount").fill("15.5")
    tid(p, "pay-submit").click()
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-balance]').dataset.amount === '6950'")
    check("changed field -> new payment 1550 with a new key", json.loads(rec[-1].post_data)["amount"] == 1550 and rec[-1].headers.get("idempotency-key") != rec[0].headers.get("idempotency-key"))
    # refused: insufficient available (held funds can't be spent)
    tid(p, "pay-amount").fill("50.00")
    tid(p, "pay-note").fill("too much")
    tid(p, "pay-visibility").select_option("private")
    tid(p, "pay-submit").click()
    tid(p, "pay-error").wait_for()
    check("insufficient available -> pay-error, inputs preserved", tid(p, "pay-amount").input_value() == "50.00" and tid(p, "pay-note").input_value() == "too much"
          and tid(p, "pay-visibility").input_value() == "private" and tid(p, "pay-handle").input_value() == "bob")
    shot(p, "pay-refused")
    # BHD three decimals
    p.context.close()
    reset(fixture("BHD", 3))
    p = br.page()
    br.login(p)
    check("BHD formatting 10.000 BHD", text(p, "wallet-balance") == "10.000 BHD" and text(p, "wallet-available") == "8.000 BHD")
    rec = posts(p, "/payments")
    tid(p, "pay-handle").fill("bob")
    tid(p, "pay-amount").fill("1.5")
    tid(p, "pay-submit").click()
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-balance]').dataset.amount === '8500'")
    check("BHD 1.5 submits 1500", json.loads(rec[0].post_data)["amount"] == 1500)
    tid(p, "pay-amount").fill("1.0005")
    tid(p, "pay-submit").click()
    tid(p, "pay-error").wait_for()
    check("BHD 4 decimals rejected, nothing sent", len(rec) == 1)
    p.context.close()


def u_lost_response(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    state = {"n": 0}
    rec = posts(p, "/payments")

    def lose_after_commit(route):
        state["n"] += 1
        if state["n"] == 1:
            route.fetch()      # the server commits the payment
            return route.abort()   # but the browser never sees the response
        route.continue_()

    p.route("**/payments", lose_after_commit)
    tid(p, "pay-handle").fill("bob")
    tid(p, "pay-amount").fill("12.34")
    tid(p, "pay-submit").click()
    tid(p, "pay-uncertain").wait_for()
    check("lost response after commit -> pay-uncertain (nonempty), not pay-error", tid(p, "pay-uncertain").inner_text().strip() != "" and tid(p, "pay-error").count() == 0)
    shot(p, "pay-uncertain")
    tid(p, "pay-submit").click()
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-balance]').dataset.amount === '8766'")
    check("retry: same key and body, uncertainty cleared, money moved once", len(rec) == 2 and rec[0].headers.get("idempotency-key") == rec[1].headers.get("idempotency-key")
          and rec[0].post_data == rec[1].post_data and tid(p, "pay-uncertain").count() == 0 and tid(p, "pay-error").count() == 0)
    s, me = api("GET", "/me", token=token("ada@example.com"))
    check("server total 8766 (one payment)", me["total"] == 8766, me)
    p.context.close()


def u_latest_refresh_wins(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    bob = token("bob@example.com")
    tid(p, "pay-handle").fill("cy")
    tid(p, "pay-amount").fill("1.23")
    tid(p, "pay-note").fill("keep me")
    # In-page delay (non-blocking): the next GET /me is read now but delivered 2 s later, out of order.
    p.evaluate("""() => { const orig = window.fetch; window.__delayNextMe = true;
      window.fetch = async (url, opts) => { const res = await orig(url, opts);
        if (String(url).endsWith('/me') && window.__delayNextMe) { window.__delayNextMe = false;
          await new Promise(r => setTimeout(r, 2000)); }
        return res; }; }""")
    tid(p, "wallet-refresh").click()      # refresh #1: reads the old state, delivered late
    time.sleep(0.4)
    api("POST", "/payments", {"to_handle": "ada", "amount": 111}, token=bob, key=key())   # another client
    tid(p, "wallet-refresh").click()      # refresh #2: fresh, delivered first
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-balance]').dataset.amount === '10111'")
    time.sleep(2.5)                       # the stale response arrives now
    check("latest refresh wins over a delayed earlier read", tid(p, "wallet-balance").get_attribute("data-amount") == "10111"
          and tid(p, "wallet-available").get_attribute("data-amount") == "8111", tid(p, "wallet-balance").get_attribute("data-amount"))
    check("refresh keeps pay form values", tid(p, "pay-handle").input_value() == "cy" and tid(p, "pay-amount").input_value() == "1.23"
          and tid(p, "pay-note").input_value() == "keep me")
    p.context.close()


def u_requests_flow(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    p.goto(BASE + "/requests")
    p.wait_for_selector("[data-testid=request-pay-rq_in]")
    bob = token("bob@example.com")
    api("POST", "/requests/rq_in/cancel", token=bob)          # cancelled elsewhere
    tid(p, "request-pay-rq_in").click()
    tid(p, "request-error").wait_for()
    p.wait_for_function("() => !document.querySelector('[data-testid=request-pay-rq_in]')")
    check("cancelled elsewhere: request-error and stale pay button disappears", tid(p, "request-error").is_visible()
          and tid(p, "request-item-rq_in").get_attribute("data-status") == "cancelled")
    shot(p, "request-refused")
    s, rq = api("POST", "/requests", {"payer_handle": "ada", "amount": 250, "note": "lunch"}, token=bob, key=key())
    p.reload()
    p.wait_for_selector("[data-testid=request-pay-%s]" % rq["request_id"])
    tid(p, "request-pay-" + rq["request_id"]).click()
    p.wait_for_function("(id) => document.querySelector('[data-testid=request-item-' + id + ']').dataset.status === 'paid'", arg=rq["request_id"])
    check("pay request: status paid, button gone, balance refreshed", tid(p, "request-pay-" + rq["request_id"]).count() == 0 and tid(p, "wallet-balance").get_attribute("data-amount") == "9750")
    tid(p, "request-cancel-rq_out").click()
    p.wait_for_function("() => document.querySelector('[data-testid=request-item-rq_out]').dataset.status === 'cancelled'")
    check("cancel outgoing -> cancelled, no button", tid(p, "request-cancel-rq_out").count() == 0)
    p.context.close()


def u_split_flow(br):
    reset(fixture("BHD", 3))
    p = br.page()
    br.login(p)
    p.goto(BASE + "/split")
    tid(p, "split-amount").fill("1")
    tid(p, "split-handles").fill("bob, ada, abcdefghijklmnopqrst")
    p.wait_for_selector("[data-testid=split-preview]")
    pre = [text(p, "split-share-" + h) for h in ("bob", "ada", "abcdefghijklmnopqrst")]
    check("BHD preview 0.334/0.333/0.333", pre == ["0.334 BHD", "0.333 BHD", "0.333 BHD"], pre)
    tid(p, "split-submit").click()
    p.wait_for_selector("[data-testid=split-success], [data-testid=split-error]")
    s, js = api("GET", "/requests?direction=outgoing", token=token("ada@example.com"))
    amts = {r["payer_handle"]: r["amount"] for r in js["requests"] if r["request_id"] not in ("rq_in", "rq_out", "rq_paid")}
    check("server shares equal preview (bob 334, cy 333)", amts.get("bob") == 334 and amts.get("abcdefghijklmnopqrst") == 333, amts)
    tid(p, "split-handles").fill("bob, nobody")
    tid(p, "split-submit").click()
    tid(p, "split-error").wait_for()
    check("unknown handle -> split-error", tid(p, "split-error").is_visible())
    p.context.close()


def u_holds_flow(br):
    reset(fixture())
    p = br.page()
    br.login(p)
    tid(p, "authorize-handle").fill("bob")
    tid(p, "authorize-amount").fill("30")
    tid(p, "authorize-submit").click()
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-held]') && document.querySelector('[data-testid=wallet-held]').dataset.amount === '5000'")
    check("authorize via form: held 50.00, available 50.00, total unchanged", text(p, "wallet-available") == "50.00 EUR" and text(p, "wallet-balance") == "100.00 EUR")
    tid(p, "authorize-amount").fill("60")
    tid(p, "authorize-submit").click()
    tid(p, "authorize-error").wait_for()
    check("authorize above available -> authorize-error", tid(p, "authorize-error").is_visible())
    tid(p, "authorize-amount").fill("1.234")
    tid(p, "authorize-submit").click()
    check("authorize 3 decimals -> authorize-error", tid(p, "authorize-error").is_visible())
    shot(p, "wallet-with-holds")
    # capture as the receiver (ada receives a_in from bob)
    p.goto(BASE + "/authorizations")
    p.wait_for_selector("[data-testid=authorization-capture-a_in]")
    tid(p, "authorization-capture-amount-a_in").fill("4.00")
    tid(p, "authorization-capture-a_in").click()
    p.wait_for_function("() => document.querySelector('[data-testid=authorization-item-a_in]').dataset.status === 'captured'")
    check("capture 4.00 of 10.00 (final): captured 4.00, buttons gone, balance up", text(p, "authorization-captured-a_in") == "4.00 EUR"
          and tid(p, "authorization-capture-a_in").count() == 0 and tid(p, "wallet-balance").get_attribute("data-amount") == "10400")
    # void outgoing
    tid(p, "authorization-void-a_out").click()
    p.wait_for_function("() => document.querySelector('[data-testid=authorization-item-a_out]').dataset.status === 'voided'")
    check("void releases hold: held now 30.00 (form hold only)", tid(p, "wallet-held").get_attribute("data-amount") == "3000")
    # stale capture refused: bob voids his hold elsewhere after page shows capture button
    bob = token("bob@example.com")
    s, a = api("POST", "/authorizations", {"to_handle": "ada", "amount": 100}, token=bob, key=key())
    p.reload()
    p.wait_for_selector("[data-testid=authorization-capture-%s]" % a["authorization_id"])
    api("POST", "/authorizations/%s/void" % a["authorization_id"], token=bob)
    tid(p, "authorization-capture-" + a["authorization_id"]).click()
    tid(p, "authorization-error").wait_for()
    p.wait_for_function("(id) => !document.querySelector('[data-testid=authorization-capture-' + id + ']')", arg=a["authorization_id"])
    check("capture of a hold voided elsewhere -> authorization-error, list refreshed", tid(p, "authorization-error").is_visible())
    shot(p, "authorizations-after-actions-1280")
    lay = p.evaluate(LAYOUT_JS)
    p.context.close()


def u_upgrade_pending_retry(br):
    """Lost payment before export; import into the same service; retry recovers the original payment."""
    reset(fixture())
    p = br.page()
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    rec = posts(p, "/payments")
    state = {"n": 0}

    def lose(route):
        state["n"] += 1
        if state["n"] == 1:
            route.fetch()
            return route.abort()
        route.continue_()

    p.route("**/payments", lose)
    tid(p, "pay-handle").fill("bob")
    tid(p, "pay-amount").fill("7")
    tid(p, "pay-submit").click()
    tid(p, "pay-uncertain").wait_for()
    s, exp = api("GET", "/_test/export")
    reset(fixture())
    s, _ = api("POST", "/_test/import", exp)
    tid(p, "pay-submit").click()
    p.wait_for_function("() => document.querySelector('[data-testid=wallet-balance]').dataset.amount === '9300'")
    check("after import: still signed in, retry recovers original payment, balance refreshed", tid(p, "current-user").is_visible() and tid(p, "pay-uncertain").count() == 0
          and len(rec) == 2 and rec[0].headers.get("idempotency-key") == rec[1].headers.get("idempotency-key"))
    s, me = api("GET", "/me", token=token("ada@example.com"))
    check("money moved once across upgrade", me["total"] == 9300, me)
    p.context.close()


PROBES = [u_routes_and_testids, u_auth_forms, u_focus, u_states, u_pay_form, u_lost_response, u_latest_refresh_wins,
          u_requests_flow, u_split_flow, u_holds_flow, u_upgrade_pending_retry]


def main():
    global BASE, SHOTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18281")
    ap.add_argument("--shots", default=None)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    BASE, SHOTS = a.base.rstrip("/"), a.shots
    with sync_playwright() as pw:
        br = Browser(pw)
        for fn in PROBES:
            if a.only and a.only not in fn.__name__:
                continue
            try:
                fn(br)
            except Exception as e:
                check(fn.__name__ + " crashed", False, repr(e)[:300])
    check("no external or failed-asset requests, no JS errors (%d)" % len(EXTERNAL), not EXTERNAL, EXTERNAL[:5])
    fails = [n for n, ok in RESULTS if not ok]
    print("TOTAL %d PASS %d FAIL %d" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
