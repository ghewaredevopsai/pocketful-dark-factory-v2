"""Minor-defect probe: generated settlement_id vs a seeded payment's settlement_id."""
import sys
sys.argv = sys.argv[:1] + ["--base", sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"]
import probes as P
P.BASE = sys.argv[2]
fx = P.fixture()
fx["payments"].append({"id": "p_9", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 1, "note": "seeded member",
                       "visibility": "public", "settlement_id": "st_4"})
fx["users"][1]["balance"] += 0
P.reset(fx)
t = P.toks()
s, js, _ = P.call("POST", "/settlements", {"transfers": [{"from_handle": "ada", "to_handle": "bob", "amount": 1}]}, token=t["op"], key=P.key())
print("seeded settlement_id st_4; new settlement ->", s, js["settlement_id"])
members = [p["payment_id"] for p in P.call("GET", "/activity?limit=200", token=t["ada"])[1]["payments"] if p.get("settlement_id") == js["settlement_id"]]
print("payments now sharing that settlement_id:", members)
print("COLLISION" if len(members) > 1 else "no collision")
