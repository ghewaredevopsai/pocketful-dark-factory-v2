#!/usr/bin/env python3
"""Stage-4 fault injection (scratch copies only). One planted defect per copy of server.py (a plant may need several
replacements), judged by the modeler's driver, probes4.py and the stage-3 late-capture probe.

Usage: python3 faults4.py <stage-4 dir> <workdir> <model dir (contains stage-4/model)> <docker network> [stage-3 base]
"""
import os
import shutil
import subprocess
import sys

FAULTS = {
    "G1_refund_cap_ignores_corrections": [('    if refunded(p) + amount > p["_revs"][-1]["amount"]:', '    if refunded(p) + amount > p["_revs"][0]["amount"]:')],
    "G2_refund_from_total_not_available": [('    st.move(user, sender, amount)\n    r = st.add_payment(',
                                            '    if user["balance"] < amount:\n        raise ApiError(409, "insufficient_funds", "x")\n'
                                            '    user["balance"] -= amount\n    sender["balance"] += amount\n    r = st.add_payment(')],
    "G3_correction_below_refunded_allowed": [("    if amount < refunded(p):\n", "    if False:\n")],
    "G4_batch_skips_completeness": [('        if any(m["payment_id"] not in chosen for m in st.payments if m["settlement_id"] == sid):\n', "        if False:\n")],
    "G5_batch_skips_historical_check": [("        if not boundary_ok(st, st.users[uid], cands):\n", "        if False:\n")],
    "G6_batch_recorded_at_per_item": [("    for uid in delta:\n        if not boundary_ok(st, st.users[uid], cands):\n",
                                       "    for i_, c_ in enumerate(cands.values()):\n"
                                       "        c_[\"_rec\"] = rec + datetime.timedelta(microseconds=i_)\n        c_[\"recorded_at\"] = iso(c_[\"_rec\"])\n"
                                       "    for uid in delta:\n        if not boundary_ok(st, st.users[uid], cands):\n")],
    "G7_late_stamp_after_clock_check": [("        if self.op_ts is not None:\n            return self.op_ts\n", "")],
    "G8_refund_reopens_request": [('    r = st.add_payment(user, sender, amount, p["note"], p["visibility"], st.stamp(), refund_of=pid)\n',
                                   '    r = st.add_payment(user, sender, amount, p["note"], p["visibility"], st.stamp(), refund_of=pid)\n'
                                   '    if p.get("request_id") in st.requests:\n        st.requests[p["request_id"]]["status"] = "pending"\n')],
    "G9_batch_partially_applied_on_failure": [("    for uid in delta:\n        if not boundary_ok(st, st.users[uid], cands):\n",
                                               "    for uid, d in delta.items():\n        st.users[uid][\"balance\"] += d\n"
                                               "    for uid in delta:\n        if not boundary_ok(st, st.users[uid], cands):\n"),
                                              ('    bid = st.new_id("cb")\n    for uid, d in delta.items():\n        st.users[uid]["balance"] += d\n',
                                               '    bid = st.new_id("cb")\n')],
    "G10_snapshot_not_frozen_after_batch": [('        result = snap["result"]\n',
                                             '        result = dict(snap["result"], entries=[dict(e, payment=dict(st.payment_view(st.payment_ids[e["payment"]["payment_id"]]),'
                                             ' amount=st.payment_ids[e["payment"]["payment_id"]]["_revs"][-1]["amount"])) for e in snap["result"]["entries"]])\n')],
}


def sh(cmd, timeout=2400):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def main():
    src, work, model, net = sys.argv[1:5]
    s3 = sys.argv[5] if len(sys.argv) > 5 else None
    review = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(review))   # repo root: probes4 imports ../../stage-3/review/probes3.py
    os.makedirs(work, exist_ok=True)
    caught = []
    for name, reps in FAULTS.items():
        d = os.path.join(work, name)
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(src, d, ignore=shutil.ignore_patterns("__pycache__", "review", "model"))
        p = os.path.join(d, "server.py")
        code = open(p).read()
        for old, new in reps:
            assert code.count(old) == 1, (name, old[:60], code.count(old))
            code = code.replace(old, new)
        open(p, "w").write(code)
        tag = "rev4-fault-" + name.lower()
        r = sh("docker build -q -t %s %s" % (tag, d))
        assert r.returncode == 0, r.stderr
        sh("docker rm -f %s" % tag)
        sh("docker run -d --name %s --network %s --cpus 2 --memory 2g -e PORT=8080 %s" % (tag, net, tag))
        sh("docker exec %s python -c \"import time,urllib.request\nfor _ in range(150):\n  try: urllib.request.urlopen('http://127.0.0.1:8080/health'); break\n  except Exception: time.sleep(0.2)\"" % tag)
        base = "http://%s:8080" % tag
        drv = sh("docker run --rm --network %s -v %s:/m:ro python:3.12-slim python /m/stage-4/model/driver.py --base %s --seeds 1-8 --steps 150 --no-shrink" % (net, model, base))
        prb = sh("docker run --rm --network %s -v %s:/w:ro python:3.12-slim python /w/stage-4/review/probes4.py --base %s %s"
                 % (net, root, base, ("--stage3-base " + s3) if s3 else ""))
        late = sh("docker run --rm --network %s -v %s:/w:ro python:3.12-slim python /w/stage-3/review/probe_capture_after_deadline.py %s 30" % (net, root, base))
        sh("docker rm -f %s" % tag)
        dc, pc, lc = drv.returncode == 1, prb.returncode == 1, late.returncode == 1
        caught.append((name, dc, pc or lc))
        ds = [l for l in drv.stdout.splitlines() if l.startswith("SUMMARY")]
        ps = [l for l in prb.stdout.splitlines() if l.startswith("TOTAL")]
        print("== %s: driver %s %s | probes4 %s %s | late-capture %s" % (name, "CAUGHT" if dc else "missed", ds[0] if ds else (drv.stdout + drv.stderr)[-200:],
                                                                       "CAUGHT" if pc else "missed", ps[0] if ps else prb.stdout[-200:], "CAUGHT" if lc else "-"))
        for l in [l for l in prb.stdout.splitlines() if l.startswith("FAIL")][:3] + [l for l in late.stdout.splitlines() if "late capture" in l][:1]:
            print("     " + l[:220])
        sys.stdout.flush()
    print("FAULTS planted %d caught %d (driver %d, probes %d); missed: %s" % (
        len(caught), sum(1 for _, a, b in caught if a or b), sum(a for _, a, _ in caught), sum(b for _, _, b in caught),
        ", ".join(n for n, a, b in caught if not (a or b)) or "none"))


if __name__ == "__main__":
    main()
