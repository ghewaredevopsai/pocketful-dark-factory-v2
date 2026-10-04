#!/usr/bin/env python3
"""Fault injection (scratch copies only): plant one defect per copy of server.py, build, run the modeler's
driver and the reviewer probes against it, report which planted faults were caught.

Usage: python3 faults.py <stage-1 dir> <workdir>    (needs docker; product copies never touch the repo)
"""
import os
import shutil
import subprocess
import sys

FAULTS = {
    "F1_offbyone_overdraft": (
        'if frm["balance"] < amount:',
        'if frm["balance"] < amount - 1:'),
    "F2_missing_lock_idem": (  # idempotency lookup and claim in two separate critical sections
        '''            rec = st.idem.get(k)
            if rec is not None:
                if rec["canon"] != c:
                    raise ApiError(409, "idempotency_key_reuse", "key already used with a different body")
                return self.send(200, raw=rec["response"].encode("utf-8"))
            status, out = fn(st, user, q, body, *args)''',
        '''            rec = st.idem.get(k)
            if rec is not None:
                if rec["canon"] != c:
                    raise ApiError(409, "idempotency_key_reuse", "key already used with a different body")
                return self.send(200, raw=rec["response"].encode("utf-8"))
            LOCK.release()
            import time as _t; _t.sleep(0.002)
            LOCK.acquire()
            status, out = fn(st, user, q, body, *args)'''),
    "F3_skipped_note_validation": (
        '    if len(n) > 200:\n        raise invalid("note is longer than 200 characters")\n',
        ''),
    "F4_wrong_comparison_feed": (
        'p["visibility"] == "public"\n',
        'p["visibility"] != "private" or p["visibility"] == "private"\n'),
    "F5_lost_retry_record_on_import": (
        '            st.idem[rec["k"]] = {"canon": rec["canon"], "response": rec["response"]}',
        '            pass'),
    "F6_partial_settlement": (
        '''    for uid, d in delta.items():
        b = st.users[uid]["balance"] + d
        if b < 0:
            raise ApiError(409, "insufficient_funds", "settlement is not affordable")''',
        '''    for frm, to, amount, _, _ in plan:
        st.move(frm, to, amount)
    delta = {}
    for uid, d in delta.items():
        b = st.users[uid]["balance"] + d
        if b < 0:
            raise ApiError(409, "insufficient_funds", "settlement is not affordable")'''),
}


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def main():
    src, work = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    summary = []
    for name, (old, new) in FAULTS.items():
        d = os.path.join(work, name)
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(src, d, ignore=shutil.ignore_patterns("__pycache__", "review"))
        p = os.path.join(d, "server.py")
        code = open(p).read()
        assert code.count(old) == 1, name
        open(p, "w").write(code.replace(old, new))
        tag = "rev-fault-" + name.lower()
        r = sh("docker build -q -t %s %s" % (tag, d))
        assert r.returncode == 0, r.stderr
        cname = tag
        sh("docker rm -f %s" % cname)
        sh("docker run -d --name %s --network none --cpus 2 --memory 2g -e PORT=8080 %s" % (cname, tag))
        sh("docker exec %s python -c \"import time,urllib.request\nfor _ in range(100):\n  try: urllib.request.urlopen('http://127.0.0.1:8080/health'); break\n  except Exception: time.sleep(0.2)\"" % cname)
        run = ("docker run --rm --network container:%s -v %s:/src:ro -w /src/review python:3.12-slim python %s"
               % (cname, os.path.dirname(os.path.abspath(__file__)).rsplit("/review", 1)[0], "%s"))
        drv = sh(run % "/src/model/driver.py --base http://127.0.0.1:8080 --seeds 1-12 --steps 150 --no-shrink", timeout=900)
        prb = sh(run % "probes.py --base http://127.0.0.1:8080", timeout=900)
        dsum = [l for l in drv.stdout.splitlines() if l.startswith("SUMMARY")]
        psum = [l for l in prb.stdout.splitlines() if l.startswith("TOTAL")]
        pfails = [l for l in prb.stdout.splitlines() if l.startswith("FAIL")][:4]
        d_caught = drv.returncode == 1
        p_caught = prb.returncode == 1
        sh("docker rm -f %s" % cname)
        summary.append((name, d_caught, p_caught))
        print("== %s: driver %s (%s) | probes %s (%s)" % (name, "CAUGHT" if d_caught else "missed", dsum[0] if dsum else drv.stdout[-300:],
                                                          "CAUGHT" if p_caught else "missed", psum[0] if psum else prb.stdout[-300:]))
        for l in pfails:
            print("     " + l[:220])
        sys.stdout.flush()
    caught = sum(1 for _, a, b in summary if a or b)
    print("FAULTS planted %d caught %d (driver %d, probes %d)" % (len(summary), caught, sum(a for _, a, _ in summary),
                                                                  sum(b for _, _, b in summary)))


if __name__ == "__main__":
    main()
