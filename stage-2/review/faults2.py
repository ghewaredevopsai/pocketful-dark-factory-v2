#!/usr/bin/env python3
"""Stage-2 fault injection (scratch copies only). One planted defect per copy; server faults are judged by the
modeler's driver and probes2.py, UI faults by ui_review.py.

Usage: python3 faults2.py <stage-2 dir> <workdir> <model dir (contains stage-2/model)> <playwright python>
"""
import os
import shutil
import subprocess
import sys

SERVER = {
    "S1_held_funds_spendable": ("server.py", 'if self.available(frm) < amount:', 'if frm["balance"] < amount:'),
    "S2_capture_over_remaining": ("server.py", "    if amount > left:\n", '    if amount > a["amount"]:\n'),
    "S3_double_release_on_expiry": ("server.py", '            self.close_auth(a, "expired")',
                                    '            self.users[a["from_user_id"]]["held"] -= remaining(a)\n            self.close_auth(a, "expired")'),
    "S4_capture_race_lock_gap": ("server.py", "    left = remaining(a)\n",
                                 "    left = remaining(a)\n    LOCK.release(); __import__('time').sleep(0.003); LOCK.acquire()\n"),
    "S5_expiry_late_by_5s": ("server.py", 'if a["_exp"] <= now]', 'if a["_exp"] < now - datetime.timedelta(seconds=5)]'),
}
UI = {
    "U1_stale_refresh_overwrites": ("static/app.js", '    if (!fresh("wallet", gen)) return;      // a later refresh already painted\n', ""),
    "U2_repeat_pay_on_double_submit": ("static/app.js", "  if (cur && cur.body === body) return cur;\n", ""),
    "U3_lost_response_shown_as_refusal": ("static/app.js",
                                          '    if (res.lost) {\n      setMsg(msg, "warn", "pay-uncertain"',
                                          '    if (false) {\n      setMsg(msg, "warn", "pay-uncertain"'),
    "U4_split_preview_wrong_order": ("static/app.js", "base + (i < rem ? 1 : 0)", "base + (i >= n - rem ? 1 : 0)"),
}


def sh(cmd, timeout=1800):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def plant(src, work, name, f, old, new):
    d = os.path.join(work, name)
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(src, d, ignore=shutil.ignore_patterns("__pycache__", "review", "model"))
    p = os.path.join(d, f)
    code = open(p).read()
    assert code.count(old) == 1, (name, code.count(old))
    open(p, "w").write(code.replace(old, new))
    tag = "rev2-fault-" + name.lower()
    r = sh("docker build -q -t %s %s" % (tag, d))
    assert r.returncode == 0, r.stderr
    return tag


def wait_health(cname):
    sh("docker exec %s python -c \"import time,urllib.request\nfor _ in range(150):\n  try: urllib.request.urlopen('http://127.0.0.1:8080/health'); break\n  except Exception: time.sleep(0.2)\"" % cname)


def tail(out, prefix):
    return [l for l in out.splitlines() if l.startswith(prefix)]


def main():
    src, work, model, pwpy = sys.argv[1:5]
    review = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(work, exist_ok=True)
    summary = []
    for name, (f, old, new) in SERVER.items():
        tag = plant(src, work, name, f, old, new)
        sh("docker rm -f %s" % tag)
        sh("docker run -d --name %s --network revnet --cpus 2 --memory 2g -e PORT=8080 %s" % (tag, tag))
        wait_health(tag)
        base = "http://%s:8080" % tag
        drv = sh("docker run --rm --network revnet -v %s:/m:ro python:3.12-slim python /m/stage-2/model/driver.py --base %s --seeds 1-10 --steps 150 --no-shrink" % (model, base))
        prb = sh("docker run --rm --network revnet -v %s:/r:ro -w /r python:3.12-slim python probes2.py --base %s" % (review, base))
        sh("docker rm -f %s" % tag)
        d, p = drv.returncode == 1, prb.returncode == 1
        summary.append((name, d or p))
        print("== %s: driver %s %s | probes2 %s %s" % (name, "CAUGHT" if d else "missed", (tail(drv.stdout, "SUMMARY") or [drv.stdout[-200:] + drv.stderr[-200:]])[0],
                                                   "CAUGHT" if p else "missed", (tail(prb.stdout, "TOTAL") or [prb.stdout[-200:]])[0]))
        for l in tail(prb.stdout, "FAIL")[:3]:
            print("     " + l[:200])
        sys.stdout.flush()
    for i, (name, (f, old, new)) in enumerate(UI.items()):
        tag = plant(src, work, name, f, old, new)
        port = 18290 + i
        sh("docker rm -f %s" % tag)
        sh("docker run -d --name %s --cpus 2 --memory 2g -e PORT=8080 -p 127.0.0.1:%d:8080 %s" % (tag, port, tag))
        wait_health(tag)
        ui = sh("cd %s && %s ui_review.py --base http://127.0.0.1:%d" % (review, pwpy, port))
        sh("docker rm -f %s" % tag)
        u = ui.returncode == 1
        summary.append((name, u))
        print("== %s: ui_review %s %s" % (name, "CAUGHT" if u else "missed", (tail(ui.stdout, "TOTAL") or [ui.stdout[-300:] + ui.stderr[-300:]])[0]))
        for l in tail(ui.stdout, "FAIL")[:3]:
            print("     " + l[:200])
        sys.stdout.flush()
    print("FAULTS planted %d caught %d: %s" % (len(summary), sum(c for _, c in summary), ", ".join(n for n, c in summary if not c) or "none missed"))


if __name__ == "__main__":
    main()
