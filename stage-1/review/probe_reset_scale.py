"""Reset timing vs fixture size (10 s test-control timeout). Usage: python3 probe_reset_scale.py <base>"""
import json, sys, time, urllib.request
base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
for n, distinct in ((1000, False), (150, True)):
    users = [{"id": "u%d" % i, "email": "u%d@example.com" % i, "password": ("pw-%08d" % i) if distinct else "correct horse",
              "display_name": "U", "handle": "u%d" % i, "balance": 1} for i in range(n)]
    req = urllib.request.Request(base + "/_test/reset", json.dumps({"currency": "EUR", "minor_units": 2, "users": users}).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    st = urllib.request.urlopen(req, timeout=60).status
    print("reset %d users, %s passwords: %d in %.2fs" % (n, "distinct" if distinct else "shared", st, time.time() - t0))
