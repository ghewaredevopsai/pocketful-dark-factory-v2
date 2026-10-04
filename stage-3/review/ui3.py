#!/usr/bin/env python3
"""Stage-3 UI probes for the stage-2 review findings M3 and M4 (real Chromium; external requests aborted).
Reuses the stage-2 reviewer harness (../../stage-2/review/ui_review.py) for browser, fixtures, layout and contrast.

Usage: python ui3.py --base http://127.0.0.1:PORT [--shots DIR]
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "stage-2", "review"))
import ui_review as U  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def m3(br):
    U.reset(U.fixture())
    for w in (375, 1280):
        p = br.page(w, 900)
        br.login(p)
        p.goto(U.BASE + "/authorizations")
        p.wait_for_selector("[data-testid=authorization-item-a_cap]")
        txt = {x: U.tid(p, "authorization-item-" + x).inner_text() for x in ("a_out", "a_in", "a_cap", "a_void", "a_exp")}
        U.check("%dpx M3: collected hold does not say Expired" % w, "Expired" not in txt["a_cap"] and "Collected" in txt["a_cap"], txt["a_cap"])
        U.check("%dpx M3: released hold does not say Expired" % w, "Expired" not in txt["a_void"] and "Released" in txt["a_void"], txt["a_void"])
        U.check("%dpx M3: expired hold says Expired; open says Expires" % w, "Expired" in txt["a_exp"] and "Expires" in txt["a_out"], (txt["a_exp"], txt["a_out"]))
        U.check("%dpx M3: authorization-expires still the RFC 3339 deadline" % w,
                dt.datetime.fromisoformat(U.text(p, "authorization-expires-a_cap")).tzinfo is not None)
        U.layout_checks(p, "%dpx stage-3 /authorizations" % w)
        U.shot(p, "s3-authorizations-%d" % w)
        # act: void an open hold, capture partially; labels follow the new state
        U.tid(p, "authorization-void-a_out").click()
        p.wait_for_function("() => document.querySelector('[data-testid=authorization-item-a_out]').dataset.status === 'voided'")
        t = U.tid(p, "authorization-item-a_out").inner_text()
        U.check("%dpx M3: freshly voided hold says Released, not Expired" % w, "Released" in t and "Expired" not in t, t)
        p.context.close()
        U.reset(U.fixture())


def m4(br):
    U.reset(U.fixture())
    p = br.page(375, 812)
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    y = p.evaluate("""() => { const r = s => document.querySelector('[data-testid=' + s + ']').getBoundingClientRect().top + scrollY;
      return {pay: r('pay-submit'), act: r('activity-list'), req: r('request-submit'), hold: r('authorize-submit')}; }""")
    U.check("375px M4: feed directly after the pay form, before request and hold forms %s" % y, y["pay"] < y["act"] < y["req"] < y["hold"], y)
    U.check("375px M4: feed starts within 1100 px of the top (was ~1400 below forms)", y["act"] < 1100, y)
    U.layout_checks(p, "375px stage-3 /")
    U.shot(p, "s3-wallet-375")
    p.context.close()
    p = br.page(1280, 900)
    br.login(p)
    p.wait_for_selector("[data-testid=activity-list]")
    box = p.evaluate("""() => { const b = s => document.querySelector('[data-testid=' + s + ']').getBoundingClientRect();
      return {payL: b('pay-submit').left, actL: b('activity-list').left, actT: b('activity-list').top, payT: b('pay-handle').top}; }""")
    U.check("1280px M4: desktop keeps forms left, feed right at the top", box["actL"] > box["payL"] + 300 and abs(box["actT"] - box["payT"]) < 120, box)
    U.layout_checks(p, "1280px stage-3 /")
    U.shot(p, "s3-wallet-1280")
    p.context.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--shots", default=None)
    a = ap.parse_args()
    U.BASE, U.SHOTS = a.base.rstrip("/"), a.shots
    with sync_playwright() as pw:
        br = U.Browser(pw)
        for fn in (m3, m4):
            try:
                fn(br)
            except Exception as e:
                U.check(fn.__name__ + " crashed", False, repr(e)[:300])
    U.check("no external requests, no JS errors (%d)" % len(U.EXTERNAL), not U.EXTERNAL, U.EXTERNAL[:5])
    fails = [n for n, ok in U.RESULTS if not ok]
    print("TOTAL %d PASS %d FAIL %d" % (len(U.RESULTS), len(U.RESULTS) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
