#!/usr/bin/env python3
"""Stage-3 fault injection (scratch copies only). One planted defect per copy of server.py; judged by the modeler's
driver and probes3.py.

Usage: python3 faults3.py <stage-3 dir> <workdir> <model dir (contains stage-3/model)> <docker network> [stage-2 base]
With a stage-2 base, probe_s2_closed_hold_history.py also judges every plant (stage-2 holds closed before upgrade).
"""
import os
import shutil
import subprocess
import sys

FAULTS = {
    "F1_as_of_exclusive": ("    return user[\"opening\"] + sum(d for t, d, _, _ in payment_events(st, user[\"id\"], known) if t <= as_of)",
                           "    return user[\"opening\"] + sum(d for t, d, _, _ in payment_events(st, user[\"id\"], known) if t < as_of)"),
    "F2_balance_after_per_page": ('    out = {"opening_balance": result["opening_balance"], "entries": entries[offset:offset + limit],',
                                  '    pg = entries[offset:offset + limit]\n'
                                  '    pg = [dict(e, balance_after=result["opening_balance"] + sum(x["delta"] for x in pg[:i + 1])) for i, e in enumerate(pg)]\n'
                                  '    out = {"opening_balance": result["opening_balance"], "entries": pg,'),
    "F3_snapshot_not_frozen": ('        result = snap["result"]\n',
                               '        result = dict(snap["result"], entries=[dict(e, payment=dict(st.payment_view(st.payment_ids[e["payment"]["payment_id"]]),'
                               ' amount=st.payment_ids[e["payment"]["payment_id"]]["_revs"][-1]["amount"])) for e in snap["result"]["entries"]])\n'),
    "F4_known_at_ignored": ("    if known is None:\n        return revs[-1]\n", "    if True:\n        return revs[-1]\n"),
    "F5_linked_correction_allowed": ('    if p.get("settlement_id") or p.get("authorization_id"):\n', "    if False:\n"),
    "F6_same_revision_double_success": ('        raise ApiError(409, "stale_revision", "the payment is at revision %d" % current["revision"])\n',
                                        '        raise ApiError(409, "stale_revision", "the payment is at revision %d" % current["revision"])\n'
                                        "    LOCK.release(); __import__('time').sleep(0.003); LOCK.acquire()\n"),
    "F7_historical_overdraft_unchecked": ("        if not boundary_ok(st, u, (p, cand)):\n", "        if False:\n"),
    "F8_hold_released_twice_at_expiry": ('            self.close_auth(a, "expired")',
                                         '            self.users[a["from_user_id"]]["held"] -= remaining(a)\n            self.close_auth(a, "expired")'),
    "F10_closed_stage2_holds_lose_history": ('        a["_caps"] = later\n',
                                             '        a["_caps"] = later\n        if a["status"] != "open":\n            a["_ct"], a["_init"], a["_caps"] = None, 0, []\n'),
    "F9_historical_release_twice": ("            out.append((close, -(a[\"_init\"] - captured)))\n",
                                    "            out.append((close, -(a[\"_init\"] - captured)))\n            out.append((close, -(a[\"_init\"] - captured)))\n"),
}


def sh(cmd, timeout=2400):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def main():
    src, work, model, net = sys.argv[1:5]
    s2 = sys.argv[5] if len(sys.argv) > 5 else None
    review = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(work, exist_ok=True)
    caught = []
    for name, (old, new) in FAULTS.items():
        d = os.path.join(work, name)
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(src, d, ignore=shutil.ignore_patterns("__pycache__", "review", "model"))
        p = os.path.join(d, "server.py")
        code = open(p).read()
        assert code.count(old) == 1, (name, code.count(old))
        open(p, "w").write(code.replace(old, new))
        tag = "rev3-fault-" + name.lower()
        r = sh("docker build -q -t %s %s" % (tag, d))
        assert r.returncode == 0, r.stderr
        sh("docker rm -f %s" % tag)
        sh("docker run -d --name %s --network %s --cpus 2 --memory 2g -e PORT=8080 %s" % (tag, net, tag))
        sh("docker exec %s python -c \"import time,urllib.request\nfor _ in range(150):\n  try: urllib.request.urlopen('http://127.0.0.1:8080/health'); break\n  except Exception: time.sleep(0.2)\"" % tag)
        base = "http://%s:8080" % tag
        drv = sh("docker run --rm --network %s -v %s:/m:ro python:3.12-slim python /m/stage-3/model/driver.py --base %s --seeds 1-8 --steps 150 --no-shrink" % (net, model, base))
        prb = sh("docker run --rm --network %s -v %s:/r:ro -w /r python:3.12-slim python probes3.py --base %s" % (net, review, base))
        if s2:
            hist = sh("docker run --rm --network %s -v %s:/r:ro -w /r python:3.12-slim python probe_s2_closed_hold_history.py %s %s" % (net, review, s2, base))
            if "FAIL 0" not in hist.stdout:
                prb.returncode = 1
                prb.stdout += "\nFAIL probe_s2_closed_hold_history: " + " | ".join(l for l in hist.stdout.splitlines() if l.startswith("FAIL"))[:200]
        sh("docker rm -f %s" % tag)
        dc, pc = drv.returncode == 1, prb.returncode == 1
        caught.append((name, dc, pc))
        ds = [l for l in drv.stdout.splitlines() if l.startswith("SUMMARY")]
        ps = [l for l in prb.stdout.splitlines() if l.startswith("TOTAL")]
        print("== %s: driver %s %s | probes3 %s %s" % (name, "CAUGHT" if dc else "missed", ds[0] if ds else (drv.stdout + drv.stderr)[-200:],
                                                   "CAUGHT" if pc else "missed", ps[0] if ps else prb.stdout[-200:]))
        for l in [l for l in prb.stdout.splitlines() if l.startswith("FAIL")][:3]:
            print("     " + l[:220])
        sys.stdout.flush()
    print("FAULTS planted %d caught %d (driver %d, probes3 %d); missed: %s" % (
        len(caught), sum(1 for _, a, b in caught if a or b), sum(a for _, a, _ in caught), sum(b for _, _, b in caught),
        ", ".join(n for n, a, b in caught if not (a or b)) or "none"))


if __name__ == "__main__":
    main()
