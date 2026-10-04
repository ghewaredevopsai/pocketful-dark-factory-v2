"""Stage-2 differential driver: random operation sequences against the reference model and a running service.

  python3 driver.py --base http://127.0.0.1:8080 --seeds 1-20 --steps 150 --out /tmp/diff
  python3 driver.py --base http://127.0.0.1:8080 --replay /tmp/diff/seed-7.json
  python3 driver.py --base http://127.0.0.1:8080 --stage1-base http://127.0.0.1:8081 --seeds 1-10   # upgrade runs

Expiry: the model expires holds only when the driver says so. For every call the driver takes the
call's send/receive window widened by GUARD seconds; holds whose deadline is before the window are
expired, holds whose deadline falls inside it may or may not be, and every consistent prefix is tried.

Each seed: random fixture -> reset both -> N steps (single calls, retries/replays, concurrent bursts,
export/import, bad resets). After every step the invariants (§1) are checked on the model and, through
GET /me for every live user, on the product. The first divergence stops the seed; the failing sequence is
then shrunk (product is reset before each attempt) and written to --out as JSON for --replay.

Comparison rules (see RULINGS.md): IDs are opaque and mapped one-to-one model<->product per entity kind;
timestamps are format-checked only; error responses must match one of the model's acceptable
(status, code) pairs; lists are compared by count, has_more, membership and per-item content (ties in
created_at make order unspecified, so the product's order is only checked to be newest-first).
Bursts are checked for linearizability: some sequential order of the burst must explain every product
response, and the model then continues from that order.
"""
import argparse
import collections
import copy
import http.client
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from urllib.parse import quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import ANY, ANY_INT, Model, fmt_ts  # noqa: E402

TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)")
NS = {"user_id": "u", "from_user_id": "u", "to_user_id": "u", "requester_id": "u", "payer_id": "u",
      "payment_id": "p", "request_id": "r", "split_id": "s", "settlement_id": "t", "authorization_id": "a"}
IDEM_PATHS = ("/payments", "/requests", "/splits", "/settlements", "/authorizations")
PASSWORD = "correct horse"
NO_EMPTY_BODY = False  # --no-empty-body: skip the empty-body probe (to look past a known divergence)


class Mismatch(Exception):
    pass


# ---------------------------------------------------------------- id binding
class Binder:
    def __init__(self):
        self.m2p, self.p2m = {}, {}

    def clone(self):
        b = Binder()
        b.m2p, b.p2m = dict(self.m2p), dict(self.p2m)
        return b

    def bind(self, ns, m, p, where):
        if (ns, m) in self.m2p:
            if self.m2p[(ns, m)] != p:
                raise Mismatch(f"{where}: expected id {self.m2p[(ns, m)]!r} (model {m}), got {p!r}")
            return
        if (ns, p) in self.p2m:
            raise Mismatch(f"{where}: product id {p!r} already names another entity ({self.p2m[(ns, p)]})")
        self.m2p[(ns, m)], self.p2m[(ns, p)] = p, m


def cmp_val(mv, pv, key, b, where):
    if key in NS:
        if mv is None:
            if pv is not None:
                raise Mismatch(f"{where}: expected null, got {pv!r}")
            return
        if not isinstance(pv, str) or not pv or len(pv) > 64:
            raise Mismatch(f"{where}: id must be a string of 1..64 chars, got {pv!r}")
        b.bind(NS[key], mv, pv, where)
        return
    if key == "payment_ids":
        if not isinstance(pv, list) or len(pv) != len(mv):
            raise Mismatch(f"{where}: expected {len(mv)} payment ids, got {pv!r}")
        for i, (x, y) in enumerate(zip(mv, pv)):
            cmp_val(x, y, "payment_id", b, f"{where}[{i}]")
        return
    if key in ("created_at", "committed_at", "expires_at"):
        if not isinstance(pv, str) or not TS_RE.fullmatch(pv):
            raise Mismatch(f"{where}: not RFC 3339 with offset: {pv!r}")
        if key == "expires_at" and isinstance(mv, str) and ts(mv) != ts(pv):
            raise Mismatch(f"{where}: expected instant {mv}, got {pv}")
        return
    if mv == ANY_INT:
        if type(pv) is not int:
            raise Mismatch(f"{where}: expected an integer, got {pv!r}")
        return
    if key == "token":
        if not isinstance(pv, str) or not pv:
            raise Mismatch(f"{where}: token must be a non-empty string")
        return
    if mv == ANY:
        if not isinstance(pv, str):
            raise Mismatch(f"{where}: expected a string, got {pv!r}")
        return
    if mv is None or isinstance(mv, bool):
        if pv is not mv:
            raise Mismatch(f"{where}: expected {json.dumps(mv)}, got {json.dumps(pv)}")
        return
    if isinstance(mv, int):
        if type(pv) is not int or pv != mv:
            raise Mismatch(f"{where}: expected integer {mv}, got {pv!r}")
        return
    if isinstance(mv, str):
        if pv != mv:
            raise Mismatch(f"{where}: expected {mv!r}, got {pv!r}")
        return
    if isinstance(mv, dict):
        if not isinstance(pv, dict):
            raise Mismatch(f"{where}: expected object, got {pv!r}")
        for k in mv:
            if k not in pv:
                raise Mismatch(f"{where}: missing field {k!r}")
            cmp_val(mv[k], pv[k], k, b, f"{where}.{k}")
        if isinstance(mv.get("expires_at"), dict):  # new authorization: expires_at = created_at + ttl
            d = (ts(pv["expires_at"]) - ts(pv["created_at"])).total_seconds()
            if d != mv["expires_at"]["$ttl"]:
                raise Mismatch(f"{where}: expires_at - created_at = {d}s, expected {mv['expires_at']['$ttl']}s")
        return
    if isinstance(mv, list):
        if not isinstance(pv, list) or len(pv) != len(mv):
            raise Mismatch(f"{where}: expected list of {len(mv)}, got {pv!r}"[:300])
        for i, (x, y) in enumerate(zip(mv, pv)):
            cmp_val(x, y, None, b, f"{where}[{i}]")
        return
    raise Mismatch(f"{where}: unhandled model value {mv!r}")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def compare(mres, pres, b):
    """Raise Mismatch unless the product response pres is allowed by the model result mres."""
    if pres.get("err"):
        raise Mismatch(f"product: {pres['err']} (status {pres.get('status')}, body {pres.get('raw', '')[:200]!r})")
    st, pb = pres["status"], pres.get("body")
    if not mres.ok:
        if st < 400:
            raise Mismatch(f"expected {' or '.join(f'{s} {c}' for s, c in sorted(mres.alts))}, got {st} "
                           f"{json.dumps(pb, ensure_ascii=False)[:250] if pb is not None else ''}")
        err = pb.get("error") if isinstance(pb, dict) else None
        code = err.get("code") if isinstance(err, dict) else None
        if not isinstance(err, dict) or not isinstance(code, str) or not isinstance(err.get("message"), str):
            raise Mismatch(f"error body must be {{error:{{code,message}}}}, got status {st} {pb!r}"[:300])
        if (st, code) not in mres.alts:
            raise Mismatch(f"expected {' or '.join(f'{s} {c}' for s, c in sorted(mres.alts))}, got {st} {code}")
        return
    if st != mres.status:
        code = pb.get("error", {}).get("code") if isinstance(pb, dict) and isinstance(pb.get("error"), dict) else ""
        raise Mismatch(f"expected {mres.status}, got {st} {code} {json.dumps(pb)[:200] if pb else ''}")
    if st == 204:
        return
    if "list_key" in mres.meta:
        lk, allm = mres.meta["list_key"], mres.meta["all"]
        idk, ns = {"requests": ("request_id", "r"), "payments": ("payment_id", "p"),
                   "authorizations": ("authorization_id", "a")}[lk]
        items = pb.get(lk) if isinstance(pb, dict) else None
        if not isinstance(items, list):
            raise Mismatch(f"missing list {lk!r}")
        if len(items) != len(mres.body[lk]):
            raise Mismatch(f"{lk}: expected {len(mres.body[lk])} items, got {len(items)}")
        if pb.get("has_more") is not mres.body["has_more"]:
            raise Mismatch(f"has_more: expected {mres.body['has_more']}, got {pb.get('has_more')!r}")
        seen = set()
        for i, it in enumerate(items):
            mid = b.p2m.get((ns, it.get(idk) if isinstance(it, dict) else None))
            if mid is None:
                raise Mismatch(f"{lk}[{i}]: id {it.get(idk) if isinstance(it, dict) else it!r} unknown to the model")
            if mid not in allm:
                raise Mismatch(f"{lk}[{i}]: {it.get(idk)!r} must not be visible to this caller/filter")
            if mid in seen:
                raise Mismatch(f"{lk}[{i}]: duplicate item")
            seen.add(mid)
            cmp_val(allm[mid], it, None, b, f"{lk}[{i}]")
        stamps = [ts(it["created_at"]) for it in items]
        if any(x < y for x, y in zip(stamps, stamps[1:])):
            raise Mismatch(f"{lk}: not newest-first by created_at")
        return
    cmp_val(mres.body, pb, None, b, "$")
    if isinstance(mres.body, dict) and "committed_at" in mres.body:
        for i, p in enumerate(pb["payments"]):
            if p.get("created_at") != pb["committed_at"]:
                raise Mismatch(f"$.payments[{i}].created_at {p.get('created_at')!r} != committed_at")
            if p.get("settlement_id") != pb["settlement_id"]:
                raise Mismatch(f"$.payments[{i}].settlement_id does not link the batch")


# ---------------------------------------------------------------- product transport
def http_call(base, method, path, query, headers, body):
    u = urlsplit(base)
    timeout = 10 if path.startswith("/_test/") else 5
    try:
        c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        c.request(method, path + ("?" + query if query else ""),
                  body=body.encode("utf-8") if isinstance(body, str) else body, headers=headers)
        r = c.getresponse()
        data = r.read()
        ct = r.getheader("Content-Type") or ""
        c.close()
    except Exception as e:  # transport failures are divergences, not crashes
        return {"status": None, "err": f"transport {type(e).__name__}: {e}"}
    out = {"status": r.status, "raw": data[:2000].decode("utf-8", "replace")}
    if r.status >= 500:
        out["err"] = f"5xx {r.status}"
    elif r.status == 204:
        if data.strip():
            out["err"] = "204 with a body"
    else:
        if not ct.lower().startswith("application/json"):
            out["err"] = f"content-type {ct!r}"
        try:
            out["body"] = json.loads(data)
        except ValueError:
            out["err"] = "non-JSON body"
    return out


# ---------------------------------------------------------------- one run
class Divergence(Exception):
    def __init__(self, info):
        super().__init__(info.get("reason"))
        self.info = info


COV = collections.Counter()  # (method, path template, product status) -> count, for --coverage
GUARD = 1.0  # seconds of clock uncertainty around each call for expiry decisions


def materialize(fx):
    """Seeded expires_at are stored relative ({"$rel": seconds}) so a replay hours later keeps its meaning."""
    fx = copy.deepcopy(fx)
    for a in fx.get("authorizations", []):
        if isinstance(a.get("expires_at"), dict):
            a["expires_at"] = fmt_ts(int(time.time()) + a["expires_at"]["$rel"])
    return fx


class Run:
    def __init__(self, bases):
        # bases = (stage-2 base, optional stage-1 base). With a stage-1 base the run starts there and an
        # "upgrade" op exports from it and imports into the stage-2 service.
        self.base2, self.base1 = bases
        self.base = self.base2
        self.model = Model(lenient=True)
        self.b = Binder()
        self.sessions = []  # {"user", "mtok", "ptok"}
        self.orig = {}      # (user, model path, key) -> product's original 201 body
        self.snap = None    # export snapshot
        self.fixture = None
        self.passwords = {}
        self.stale = []     # product tokens invalidated by an import

    def start_stage1(self):
        self.base, self.model.stage = self.base1, 1

    # ---- request building
    def session(self, auth):
        if not auth or "user" not in auth:
            return None
        lst = [s for s in self.sessions if s["user"] == auth["user"]]
        return lst[auth["ord"] % len(lst)] if lst else None

    def build(self, op):
        hm, hp = {}, {}
        a = op.get("auth")
        if a and "raw" in a:
            hm["Authorization"] = hp["Authorization"] = a["raw"]
        elif a:
            s = self.session(a)
            if s is None:
                return None
            hm["Authorization"], hp["Authorization"] = "Bearer " + s["mtok"], "Bearer " + s["ptok"]
        if op.get("key") is not None:
            hm["Idempotency-Key"] = hp["Idempotency-Key"] = op["key"]
        if op.get("body") is not None:
            hm["Content-Type"] = hp["Content-Type"] = "application/json"
        ref = op.get("ref")
        ns = op.get("refns", "r")
        mpath = op["path"].replace("{ref}", ref or "")
        ppath = op["path"].replace("{ref}", quote(self.b.m2p.get((ns, ref), ref or ""), safe=""))
        return {"mpath": mpath, "ppath": ppath, "hm": hm, "hp": hp}

    def mcall(self, m, op, rq):
        body = op.get("body")
        return m.handle(op["method"], rq["mpath"], op.get("query", ""), rq["hm"],
                        body.encode("utf-8") if body is not None else None)

    def pcall(self, op, rq):
        t0 = time.time()
        r = http_call(self.base, op["method"], rq["ppath"], op.get("query", ""), rq["hp"], op.get("body"))
        r["t0"], r["t1"] = t0, time.time()
        COV[(op["method"], op["path"], r.get("status"),
             (r.get("body") or {}).get("error", {}).get("code") if isinstance(r.get("body"), dict) else None)] += 1
        return r

    def idem_key(self, op, rq):
        if op["method"] != "POST" or op.get("key") is None:
            return None
        p = rq["mpath"]
        if p in IDEM_PATHS or p.endswith("/pay") or p.endswith("/capture"):
            s = self.session(op.get("auth"))
            return (s["user"], p, op["key"]) if s else None
        return None

    def after(self, op, rq, mres, pres):
        k = self.idem_key(op, rq)
        if k and mres.ok and mres.status == 201:
            self.orig[k] = pres["body"]
        elif k and mres.ok and mres.status == 200 and k in self.orig and pres["body"] != self.orig[k]:
            raise Mismatch("replay body is not identical to the original response")
        if op.get("after") == "session" and mres.ok:
            self.sessions.append({"user": mres.body["user_id"], "mtok": mres.body["token"],
                                  "ptok": pres["body"]["token"]})

    @staticmethod
    def patch(m, mres, pres):
        """Adopt the product's expires_at for a newly created authorization (relation already checked)."""
        body = mres.body if mres.ok else None
        if isinstance(body, dict) and "authorization_id" in body and isinstance(body.get("expires_at"), dict):
            a = m.s["auths"].get(body["authorization_id"])
            if a is not None and not a.get("exp_exact"):
                m.set_expiry(a["authorization_id"], pres["body"]["expires_at"])

    def explain(self, model, b, fn, pres):
        """Try every expiry prefix consistent with the call's time window; return (model, binder, mres)
        for the first that explains the product response, else (None, None, (mres, error))."""
        forced, maybe = model.due(pres.get("t0", time.time()) - GUARD, pres.get("t1", time.time()) + GUARD)
        last = None
        for k in range(len(maybe) + 1):
            m = copy.deepcopy(model) if maybe else model
            for aid in forced + maybe[:k]:
                m.expire(aid)
            b2 = b.clone()
            mres = fn(m)
            try:
                compare(mres, pres, b2)
                self.patch(m, mres, pres)
                if forced or maybe:
                    COV[("expiry", f"forced={len(forced)} window={len(maybe)}", f"k={k}", None)] += 1
                return m, b2, mres
            except Mismatch as e:
                last = (mres, e)
        return None, None, last

    # ---- steps
    def call_once(self, i, op):
        rq = self.build(op)
        if rq is None:
            return
        pres = self.pcall(op, rq)
        m, b, got = self.explain(self.model, self.b, lambda m: self.mcall(m, op, rq), pres)
        if m is None:
            mres, e = got
            raise Divergence({"step": i, "op": op, "reason": str(e), "model": repr(mres)[:600],
                              "product": {k: pres.get(k) for k in ("status", "raw", "err")}})
        self.model, self.b = m, b
        try:
            self.after(op, rq, got, pres)
        except Mismatch as e:
            raise Divergence({"step": i, "op": op, "reason": str(e),
                              "product": {k: pres.get(k) for k in ("status", "raw", "err")}})

    def step(self, i, op):
        t = op["t"]
        if t == "call":
            self.call_once(i, op)
        elif t == "burst":
            self.burst(i, op)
        elif t == "reset":
            self.reset(i, op)
        elif t == "export":
            self.export(i)
        elif t == "import":
            self.import_(i, op)
        elif t == "sleep":
            time.sleep(op["s"])
        elif t == "upgrade":
            self.upgrade(i, op)
        self.invariants(i, op)

    def fail(self, i, op, reason, **kw):
        raise Divergence({"step": i, "op": op, "reason": reason, **kw})

    def reset(self, i, op):
        fx = materialize(op["fixture"])
        body = json.dumps(fx)
        mres = self.model.handle("POST", "/_test/reset", "", {"Content-Type": "application/json"}, body.encode())
        pres = http_call(self.base, "POST", "/_test/reset", "", {"Content-Type": "application/json"}, body)
        try:
            compare(mres, pres, self.b)
        except Mismatch as e:
            self.fail(i, {"t": "reset", "fixture": op["fixture"]}, f"reset: {e}", product=pres)
        if not mres.ok:
            return
        self.b, self.sessions, self.orig, self.snap = Binder(), [], {}, None
        self.fixture = op["fixture"]
        for u in fx["users"]:
            self.b.bind("u", u["id"], u["id"], "fixture")
            self.passwords[u["email"]] = u["password"]
        for p in fx.get("payments", []):
            self.b.bind("p", p["id"], p["id"], "fixture")
        for r in fx.get("requests", []):
            self.b.bind("r", r["id"], r["id"], "fixture")
        if self.model.stage >= 2:
            for a in fx.get("authorizations", []):
                self.b.bind("a", a["id"], a["id"], "fixture")
        for u in fx["users"]:
            lop = {"t": "call", "method": "POST", "path": "/auth/login", "after": "session",
                   "body": json.dumps({"email": u["email"], "password": u["password"]})}
            self.call_once(i, lop)

    def export(self, i):
        mres = self.model.handle("GET", "/_test/export")
        pres = http_call(self.base, "GET", "/_test/export", "", {}, None)
        pb = pres.get("body")
        if pres.get("err") or pres["status"] != 200 or not isinstance(pb, dict) or pb.get("track") != "pocketful" \
                or pb.get("format_version") != 1 or not isinstance(pb.get("state"), dict):
            self.fail(i, {"t": "export"}, "export must be 200 {track:'pocketful', format_version:1, state:{...}}",
                      product={k: pres.get(k) for k in ("status", "raw", "err")})
        self.snap = {"m": json.dumps(mres.body), "p": json.dumps(pb, ensure_ascii=False),
                     "b": self.b.clone(), "orig": copy.deepcopy(self.orig)}
        return pb

    def upgrade(self, i, op):
        """Stage-1 product export -> stage-2 product import; sessions, receipts and retries must survive."""
        if self.model.stage >= 2:
            return
        pb = self.export(i)
        self.base, self.model.stage = self.base2, 2
        pres = http_call(self.base, "POST", "/_test/import", "", {"Content-Type": "application/json"},
                         json.dumps(pb, ensure_ascii=False))
        if pres.get("err") or pres["status"] != 204:
            self.fail(i, op, "stage-2 import of the stage-1 export must return 204",
                      product={k: pres.get(k) for k in ("status", "raw", "err")})
        self.snap = None

    def import_(self, i, op):
        v = op["variant"]
        if v == "good":
            if self.snap is None:
                return
            mb, pb = self.snap["m"], self.snap["p"]
        else:
            bad = {"wrong_track": {"track": "tablekeeper", "format_version": 1, "state": {}},
                   "bad_version": {"track": "pocketful", "format_version": 2, "state": {}},
                   "no_state": {"track": "pocketful", "format_version": 1},
                   "empty_state": {"track": "pocketful", "format_version": 1, "state": {}}}
            mb = pb = "{not json" if v == "not_json" else json.dumps(bad[v])
        h = {"Content-Type": "application/json"}
        mres = self.model.handle("POST", "/_test/import", "", h, mb.encode())
        pres = http_call(self.base, "POST", "/_test/import", "", h, pb)
        try:
            compare(mres, pres, self.b)
        except Mismatch as e:
            self.fail(i, op, f"import: {e}", product={k: pres.get(k) for k in ("status", "raw", "err")})
        if mres.ok:
            self.b, self.orig = self.snap["b"].clone(), copy.deepcopy(self.snap["orig"])
            # sessions issued after the export are gone; the model will reissue their ids and token
            # strings, so retire them and keep only their product tokens as must-be-401 probes
            tok = self.model.s["tokens"]
            self.stale += [s["ptok"] for s in self.sessions if tok.get(s["mtok"]) != s["user"]]
            self.sessions = [s for s in self.sessions if tok.get(s["mtok"]) == s["user"]]

    def burst(self, i, op):
        ops = op["ops"]
        rqs = [self.build(o) for o in ops]
        live = [(o, r) for o, r in zip(ops, rqs) if r is not None]
        if not live:
            return
        bar = Barrier(len(live))

        def go(pair):
            bar.wait()
            return self.pcall(*pair)
        with ThreadPoolExecutor(len(live)) as ex:
            presults = list(ex.map(go, live))
        for o, p in zip(live, presults):
            if p.get("err"):
                self.fail(i, op, f"burst member failed: {p['err']}", member=o[0])
        lo = min(p["t0"] for p in presults) - GUARD
        hi = max(p["t1"] for p in presults) + GUARD
        start = copy.deepcopy(self.model)
        forced, maybe = start.due(lo, hi)
        for aid in forced:
            start.expire(aid)
        if forced or maybe:
            COV[("expiry", f"burst forced={len(forced)} window={len(maybe)}", "", None)] += 1
        budget = [20000]

        # Linearization search. Uncertain expiries are extra events that may be placed anywhere, in
        # deadline order; unplaced ones stay pending for the next step.
        def dfs(model, b, remaining, nexp, chosen):
            if not remaining:
                return model, b, chosen
            for j in remaining:
                budget[0] -= 1
                if budget[0] < 0:
                    return None
                m2, b2 = copy.deepcopy(model), b.clone()
                o, r = live[j]
                mres = self.mcall(m2, o, r)
                try:
                    compare(mres, presults[j], b2)
                    self.patch(m2, mres, presults[j])
                except Mismatch:
                    continue
                got = dfs(m2, b2, [x for x in remaining if x != j], nexp, chosen + [(j, mres)])
                if got:
                    return got
            if nexp < len(maybe):
                m2 = copy.deepcopy(model)
                m2.expire(maybe[nexp])
                return dfs(m2, b, remaining, nexp + 1, chosen)
            return None
        got = dfs(start, self.b, list(range(len(live))), 0, [])
        if got is None:
            self.fail(i, op, "burst responses are not explained by any sequential order of the burst",
                      product=[{"op": o, "status": p["status"], "raw": p.get("raw", "")[:300]}
                               for (o, _), p in zip(live, presults)])
        self.model, self.b, chosen = got
        try:
            for j, mres in chosen:
                self.after(live[j][0], live[j][1], mres, presults[j])
        except Mismatch as e:
            self.fail(i, op, f"burst: {e}")

    def invariants(self, i, op):
        problems = self.model.check_invariants()
        if problems:
            self.fail(i, op, "MODEL invariant broken (model bug): " + "; ".join(problems))
        total = 0
        for uid in list(self.model.s["users"]):
            s = next((s for s in self.sessions if s["user"] == uid and self.model.s["tokens"].get(s["mtok"]) == uid),
                     None)
            if s is None:
                self.fail(i, op, f"driver lost every session for {uid}")
            mh = {"Authorization": "Bearer " + s["mtok"]}
            t0 = time.time()
            pres = http_call(self.base, "GET", "/me", "", {"Authorization": "Bearer " + s["ptok"]}, None)
            pres["t0"], pres["t1"] = t0, time.time()
            m, b, got = self.explain(self.model, self.b, lambda m: m.handle("GET", "/me", "", mh), pres)
            if m is None:
                self.fail(i, op, f"invariant check GET /me for {uid}: {got[1]}")
            self.model, self.b = m, b
            body = pres["body"]
            if body["balance"] < 0 or body.get("available", 0) < 0:
                self.fail(i, op, f"invariant: negative balance/available for {uid}")
            total += body["balance"]
        if total != self.model.s["total"]:
            self.fail(i, op, f"invariant: product balances sum to {total}, seeded total {self.model.s['total']}")


# ---------------------------------------------------------------- generator
HANDLES = ["ada", "bob", "cy", "dee", "eve_9", "f_long_handle_name20"]


def gen_fixture(rng, stage=2):
    cur, mu = rng.choice([("EUR", 2), ("JPY", 0), ("BHD", 3)])
    n = rng.randint(3, 6)
    users = []
    for h in HANDLES[:n]:
        users.append({"id": "u_" + h, "email": f"{h}@example.com", "password": PASSWORD,
                      "display_name": h.capitalize(), "handle": h, "balance": rng.choice([0, 1, 50, 500, 2500, 10000])})
    users[-1]["email"] = "zed.x@example.com"  # email local part differs from its handle
    if rng.random() < 0.2:
        users[0]["balance"] = 2 ** 53 - 10 ** 6  # total stays <= 2^53
    fx = {"currency": cur, "minor_units": mu, "users": users,
          "payments": [{"id": f"p_s{k}", "from_user_id": users[k]["id"], "to_user_id": users[k + 1]["id"],
                        "amount": 100 + k, "note": "seed", "visibility": rng.choice(["public", "private"])}
                       for k in range(rng.randint(0, 2))],
          "requests": [{"id": f"rq_s{k}", "requester_id": users[(k + 1) % n]["id"], "payer_id": users[k]["id"],
                        "amount": rng.choice([5, 1200, 99999]), "note": "seed",
                        "status": rng.choice(["pending", "pending", "paid", "declined", "cancelled"])}
                       for k in range(rng.randint(0, 3))]}
    ops = rng.sample([u["id"] for u in users], rng.choice([0, 1, 1, 2]))
    if ops or rng.random() < 0.5:
        fx["settlement_operator_ids"] = ops
    if stage < 2:
        return fx
    ttl = rng.choice([None, 600, 3, 5, 2, 600])
    if ttl is not None:
        fx["authorization_ttl_seconds"] = ttl
    auths = []
    for k in range(rng.randint(0, 3)):
        frm, to = users[k], users[(k + 1) % n]
        st = rng.choice(["open", "open", "open", "captured", "voided", "expired"])
        rel = rng.choice([1, 1, -1]) * rng.randint(3700, 7200)  # seeded expiry is >= 1 h from reset
        amt = max(1, min(frm["balance"], 10 ** 9) // 3)
        if st == "open" and rel > 0 and frm["balance"] < 1:
            continue
        auths.append({"id": f"a_s{k}", "from_user_id": frm["id"], "to_user_id": to["id"], "amount": amt,
                      "note": "seeded hold", "visibility": rng.choice(["public", "private"]), "status": st,
                      "expires_at": {"$rel": rel}})
    if auths or rng.random() < 0.5:
        fx["authorizations"] = auths
    return fx


class Gen:
    def __init__(self, rng, run):
        self.rng, self.run, self.n = rng, run, 0
        self.hist = []  # idempotent ops generated so far

    # helpers
    def key(self):
        r = self.rng.random()
        self.n += 1
        if r < 0.03:
            return None
        if r < 0.05:
            return ""
        if r < 0.07:
            return "k" * 256
        if r < 0.09:
            return f"{self.n:06d}".ljust(255, "x")
        return f"k{self.n}"

    def users(self):
        return self.run.model.s["users"]

    def live(self):
        tok = self.run.model.s["tokens"]
        return sorted({s["user"] for s in self.run.sessions if tok.get(s["mtok"]) == s["user"]})

    def auth(self, uid):
        return {"user": uid, "ord": self.rng.randrange(8)}

    def handle_of(self, uid):
        return self.users()[uid]["handle"]

    def pick_handle(self, uid, field_ok=0.85):
        r = self.rng.random()
        others = [u["handle"] for k, u in self.users().items() if k != uid]
        if r < field_ok and others:
            return json.dumps(self.rng.choice(others))
        return self.rng.choice([json.dumps(self.handle_of(uid)), '"nobody_here"', '"BOB"', "5", None, "null"])

    def amount(self, bal):
        r = self.rng.random()
        if r < 0.55:
            return str(self.rng.randint(1, max(1, min(bal, 10 ** 9) // 2 or 1)))
        return self.rng.choice([str(max(1, min(bal, 10 ** 9))), str(min(bal + 1, 10 ** 9 + 1)), "0", "-5",
                                "1000000000", "1000000001", "1e3", "100.0", "12.5", '"100"', "true", "null", None])

    def note(self):
        return self.rng.choice([None, None, None, '""', '"dinner"', json.dumps("🍕 café ñ \"q\" <b>"),
                                json.dumps("x" * 200), json.dumps("x" * 201), "null", "7", '"  spaced  "'])

    def vis(self):
        return self.rng.choice([None, None, '"public"', '"private"', '"private"', '"PUBLIC"', "null"])

    def obj(self, fields):
        parts = [f'{json.dumps(k)}:{v}' for k, v in fields if v is not None]
        if self.rng.random() < 0.1:
            parts.append('"unknown_field":{"x":[1,2]}')
        self.rng.shuffle(parts)
        return "{" + ",".join(parts) + "}"

    def maybe_garble(self, body):
        if self.rng.random() < 0.03:
            return self.rng.choice(["{not json", "[]", "null", '"str"'] + ([] if NO_EMPTY_BODY else [""]))
        return body

    def call(self, method, path, uid=None, key=False, body=None, ref=None, query="", **kw):
        op = {"t": "call", "method": method, "path": path, "auth": self.auth(uid) if uid else None, "body": body,
              "query": query}
        if ref is not None:
            op["ref"] = ref
        if key:
            op["key"] = self.key()
            self.hist.append(op)
        op.update(kw)
        return op

    # operations
    def payment(self, uid):
        bal = self.users()[uid]["balance"]
        body = self.obj([("to_handle", self.pick_handle(uid)), ("amount", self.amount(bal)),
                         ("note", self.note()), ("visibility", self.vis())])
        return self.call("POST", "/payments", uid, True, self.maybe_garble(body))

    def request(self, uid):
        body = self.obj([("payer_handle", self.pick_handle(uid)),
                         ("amount", self.amount(self.rng.choice([10, 5000, 10 ** 12]))), ("note", self.note())])
        return self.call("POST", "/requests", uid, True, self.maybe_garble(body))

    def pick_request(self):
        reqs = list(self.run.model.s["requests"].values())
        pend = [r for r in reqs if r["status"] == "pending"]
        if not reqs or self.rng.random() < 0.04:
            return None
        return self.rng.choice(pend if pend and self.rng.random() < 0.8 else reqs)

    def actor_for(self, r, role, p=0.75):
        x = self.rng.random()
        if x < p:
            return r[role]
        if x < 0.9:
            return r["requester_id" if role == "payer_id" else "payer_id"]
        return self.rng.choice(list(self.users()))

    def pay(self, uid=None):
        r = self.pick_request()
        rid = r["request_id"] if r else "rq_unknown"
        uid = self.actor_for(r, "payer_id") if r else (uid or self.rng.choice(list(self.users())))
        body = self.rng.choice(["{}", "{}", '{"visibility":"private"}', '{"visibility":"public"}',
                                '{"visibility":"bogus"}', '{"note":"ignored"}'])
        op = self.call("POST", "/requests/{ref}/pay", uid, True, body, ref=rid)
        same = [h for h in self.hist[:-1] if h["path"] == "/requests/{ref}/pay" and h.get("auth")
                and h["auth"]["user"] == uid and h.get("ref") != rid and h["body"] == body and h.get("key")]
        if same and self.rng.random() < 0.15:  # same key, same body, different path: not a replay
            op["key"] = self.rng.choice(same)["key"]
        return op

    def transition(self, action):
        r = self.pick_request()
        rid = r["request_id"] if r else "rq_unknown"
        role = "payer_id" if action == "decline" else "requester_id"
        uid = self.actor_for(r, role) if r else self.rng.choice(list(self.users()))
        return self.call("POST", "/requests/{ref}/" + action, uid, ref=rid)

    def authorize(self, uid):
        av = self.run.model.available(uid)
        if self.rng.random() < 0.6:  # mostly well-formed: holds must exist for captures/voids/expiry to matter
            to = self.rng.choice([u["handle"] for k, u in self.users().items() if k != uid])
            amt = self.rng.randint(1, max(1, min(av, 10 ** 9)))
            vis = self.rng.choice(["", ',"visibility":"private"'])
            return self.call("POST", "/authorizations", uid, True, f'{{"to_handle":"{to}","amount":{amt}{vis}}}')
        body = self.obj([("to_handle", self.pick_handle(uid)), ("amount", self.amount(av)),
                         ("note", self.note()), ("visibility", self.vis())])
        return self.call("POST", "/authorizations", uid, True, self.maybe_garble(body))

    def pick_auth(self):
        auths = list(self.run.model.s["auths"].values())
        opn = [a for a in auths if a["status"] == "open"]
        if not auths or self.rng.random() < 0.04:
            return None
        return self.rng.choice(opn if opn and self.rng.random() < 0.85 else auths)

    def capture(self, a=None):
        a = a or self.pick_auth()
        aid = a["authorization_id"] if a else "a_unknown"
        x = self.rng.random()
        uid = (a["to_user_id"] if x < 0.78 else a["from_user_id"] if x < 0.9
               else self.rng.choice(list(self.users()))) if a else self.rng.choice(list(self.users()))
        rem = a["remaining_amount"] if a else 10
        part = self.rng.randint(1, max(1, rem))
        body = self.rng.choice(["{}", "{}", f'{{"amount":{part}}}', f'{{"amount":{part},"final":false}}',
                                f'{{"amount":{part},"final":false}}', '{"final":false}', '{"final":true}',
                                f'{{"amount":{rem + 1}}}', '{"amount":0}', '{"final":"no"}',
                                f'{{"amount":{part}.0}}', '{"amount":"5"}'])
        return self.call("POST", "/authorizations/{ref}/capture", uid, True, body, ref=aid, refns="a")

    def void(self, a=None):
        a = a or self.pick_auth()
        aid = a["authorization_id"] if a else "a_unknown"
        x = self.rng.random()
        uid = (a["from_user_id"] if x < 0.8 else a["to_user_id"] if x < 0.92
               else self.rng.choice(list(self.users()))) if a else self.rng.choice(list(self.users()))
        return self.call("POST", "/authorizations/{ref}/void", uid, ref=aid, refns="a")

    def split(self, uid):
        hs = [u["handle"] for u in self.users().values()]
        k = self.rng.randint(1, min(5, len(hs)))
        parts = self.rng.sample(hs, k)
        r = self.rng.random()
        if r < 0.05:
            parts.append(parts[0])
        elif r < 0.08:
            parts.append("nobody_here")
        elif r < 0.1:
            parts = []
        ph = json.dumps(parts) if self.rng.random() > 0.03 else '"ada"'
        amt = self.rng.choice(["1", "10", "999", "1000", "3000", "5", str(self.rng.randint(1, 100000)), "0", "1e3"])
        body = self.obj([("amount", amt), ("participant_handles", ph), ("note", self.note())])
        return self.call("POST", "/splits", uid, True, self.maybe_garble(body))

    def settlement(self):
        ops = self.run.model.s["operators"]
        live = self.live()
        r = self.rng.random()
        if ops and r < 0.8:
            uid = self.rng.choice(ops)
        elif r < 0.95 or not ops:
            uid = self.rng.choice(live)
        else:
            uid = None
        hs = [u["handle"] for u in self.users().values()]
        n = self.rng.choice([1, 1, 2, 3, 4, 6])
        tr = []
        for _ in range(n):
            a, b = self.rng.sample(hs, 2)
            u = next(x for x in self.users().values() if x["handle"] == a)
            amt = str(self.rng.randint(1, max(1, u["balance"] // 2 or 1))) if self.rng.random() < 0.8 \
                else str(u["balance"] + self.rng.randint(1, 500))
            e = [("from_handle", json.dumps(a)), ("to_handle", json.dumps(b)), ("amount", amt)]
            if self.rng.random() < 0.3:
                e.append(("note", self.note()))
            if self.rng.random() < 0.3:
                e.append(("visibility", self.vis()))
            if self.rng.random() < 0.05:
                e[1] = ("to_handle", json.dumps(a))  # self-transfer
            if self.rng.random() < 0.04:
                e[0] = ("from_handle", '"nobody_here"')
            tr.append(self.obj(e))
        r = self.rng.random()
        if r < 0.03:
            tr = []
        elif r < 0.05:
            tr = tr * 33
            tr = tr[:33]
        body = "{" + ('"transfers":[' + ",".join(tr) + "]" if self.rng.random() > 0.02 else '"transfers":5') + "}"
        op = self.call("POST", "/settlements", uid, True, body)
        if uid is None:
            op["auth"] = None
        return op

    def reads(self, uid):
        r = self.rng.random()
        def pg():
            q = []
            if self.rng.random() < 0.4:
                q.append("limit=" + self.rng.choice(["1", "2", "3", "200", "50", "007", "0", "201", "abc", "1e2",
                                                     "4.0", "+4", "", "99999999999999999999"]))
            if self.rng.random() < 0.3:
                q.append("offset=" + self.rng.choice(["0", "1", "2", "5", "100", "-1", "x", "1.0"]))
            if self.rng.random() < 0.1:
                q.append("unknown=1")
            return q
        if r < 0.15:
            return self.call("GET", "/me", uid)
        if r < 0.45:
            return self.call("GET", "/activity", uid, query="&".join(pg()))
        q = pg()
        if self.rng.random() < 0.5:
            q.append("direction=" + self.rng.choice(["incoming", "outgoing", "sideways"]))
        if self.run.model.stage >= 2 and r < 0.75:
            if self.rng.random() < 0.5:
                q.append("status=" + self.rng.choice(["open", "captured", "voided", "expired", "pending", "OPEN"]))
            return self.call("GET", "/authorizations", uid, query="&".join(q))
        if self.rng.random() < 0.5:
            q.append("status=" + self.rng.choice(["pending", "paid", "declined", "cancelled", "PENDING"]))
        return self.call("GET", "/requests", uid, query="&".join(q))

    def replay(self):
        h = self.rng.choice(self.hist)
        if not h.get("auth") or "user" not in h["auth"]:
            return None
        op = copy.deepcopy(h)
        op["auth"] = self.auth(h["auth"]["user"])  # same user, possibly another session
        r = self.rng.random()
        if r < 0.25 and op["body"]:
            try:
                op["body"] = json.dumps(json.loads(op["body"]), indent=1, sort_keys=True)  # same JSON value
            except ValueError:
                pass
        elif r < 0.45 and op["body"]:
            try:
                d = json.loads(op["body"])
            except ValueError:
                d = None
            if isinstance(d, dict):  # same key, different JSON value
                d["note"] = "changed" if d.get("note") != "changed" else "changed again"
                op["body"] = json.dumps(d, ensure_ascii=False)
        return op

    def signup(self):
        r = self.rng.random()
        self.n += 1
        known = list(self.run.passwords) or ["ada@example.com"]
        if r < 0.45:
            email = f"New.User{self.n}+tag@Example.com"
        elif r < 0.55:
            email = self.rng.choice(known)
        elif r < 0.65:
            email = f"{self.rng.choice(HANDLES)}@other.org"
        elif r < 0.72:
            email = self.rng.choice(["no-at-sign", "@nolocal.com", "nodomain@"])
        elif r < 0.8:
            email = f"averyveryverylonglocalpart{self.n}@example.com"
        else:
            email = f"u{self.n}@example.com"
        pw = PASSWORD if self.rng.random() > 0.1 else "short7c"
        if pw == PASSWORD:
            self.run.passwords.setdefault(email, pw)
        body = self.obj([("email", json.dumps(email)), ("password", json.dumps(pw)),
                         ("display_name", json.dumps(f"User {self.n}") if self.rng.random() > 0.03 else None)])
        return self.call("POST", "/auth/signup", body=body, after="session")

    def login(self):
        email = self.rng.choice(list(self.run.passwords))
        pw = self.run.passwords[email] if self.rng.random() < 0.8 else "wrong password"
        if self.rng.random() < 0.05:
            email = "ghost@example.com"
        return self.call("POST", "/auth/login", body=json.dumps({"email": email, "password": pw}), after="session")

    def badauth(self):
        raw = self.rng.choice(["Bearer nope", "Token abc", "Bearer ", "", None]
                              + ["Bearer " + t for t in self.run.stale[-3:]] * 2)
        op = self.rng.choice([self.call("GET", "/me"), self.call("GET", "/activity"),
                              self.call("POST", "/payments", key=True, body='{"to_handle":"bob","amount":1}')])
        op["auth"] = {"raw": raw} if raw is not None else None
        return op

    def write(self, uid):
        fs = [self.payment, self.payment, self.request, self.split, lambda u: self.pay(u),
              lambda u: self.transition("decline"), lambda u: self.transition("cancel"),
              lambda u: self.settlement(), self.reads]
        if self.run.model.stage >= 2:
            fs += [self.authorize, self.authorize, lambda u: self.capture(), lambda u: self.capture(),
                   lambda u: self.void()]
        return self.rng.choice(fs)(uid)

    def burst(self):
        live = self.live()
        r = self.rng.random()
        k = self.rng.randint(2, 8)
        if r < 0.3:  # identical retries, possibly via different sessions of the same user
            base = self.write(self.rng.choice(live))
            ops = []
            for _ in range(k):
                o = copy.deepcopy(base)
                if o.get("auth") and "user" in o["auth"]:
                    o["auth"]["ord"] = self.rng.randrange(8)
                ops.append(o)
        elif r < 0.6:  # drain one wallet
            uid = self.rng.choice(live)
            bal = self.users()[uid]["balance"]
            ops = []
            for _ in range(k):
                to = self.rng.choice([u["handle"] for x, u in self.users().items() if x != uid])
                amt = max(1, min(bal // 2 + 1, 10 ** 9))
                ops.append(self.call("POST", "/payments", uid, True, json.dumps({"to_handle": to, "amount": amt})))
        elif r < 0.75 and self.run.model.stage >= 2 and any(
                a["status"] == "open" for a in self.run.model.s["auths"].values()):
            # one open hold: captures by the receiver, voids by the payer, the payer spending available
            a = self.rng.choice([a for a in self.run.model.s["auths"].values() if a["status"] == "open"])
            ops = []
            for _ in range(k):
                x = self.rng.random()
                ops.append(self.capture(a) if x < 0.45 else self.void(a) if x < 0.6
                           else self.authorize(a["from_user_id"]) if x < 0.8 else self.payment(a["from_user_id"]))
            if self.rng.random() < 0.4:
                ops += [copy.deepcopy(ops[0])]  # an identical retry inside the burst
        else:
            ops = [self.write(self.rng.choice(live)) for _ in range(k)]
        return {"t": "burst", "ops": ops}

    def next(self):
        live = self.live()
        uid = self.rng.choice(live)
        table = [(17, lambda: self.payment(uid)), (10, lambda: self.request(uid)), (10, self.pay),
                 (4, lambda: self.transition("decline")), (4, lambda: self.transition("cancel")),
                 (7, lambda: self.split(uid)), (6, self.settlement), (12, lambda: self.reads(uid)),
                 (6, lambda: self.replay() if self.hist else None), (4, self.signup), (3, self.login),
                 (2, self.badauth), (9, self.burst), (2, lambda: {"t": "export"}),
                 (1.5, lambda: {"t": "import", "variant": "good"}),
                 (0.8, lambda: {"t": "import", "variant": self.rng.choice(
                     ["wrong_track", "bad_version", "no_state", "empty_state", "not_json"])}),
                 (0.8, self.bad_reset)]
        if self.run.model.stage >= 2:
            short = self.run.model.s["ttl"] <= 10
            table += [(12, lambda: self.authorize(uid)), (10, self.capture), (4, self.void),
                      (6 if short else 0, lambda: {"t": "sleep", "s": round(self.rng.uniform(0.5, 3), 2)})]
        x = self.rng.uniform(0, sum(w for w, _ in table))
        for w, f in table:
            x -= w
            if x < 0:
                return f()
        return None

    def bad_reset(self):
        fx = copy.deepcopy(self.run.fixture)
        r = self.rng.random()
        if self.run.model.stage < 2 or r < 0.4:
            fx["users"][self.rng.randrange(len(fx["users"]))]["balance"] = -1
        elif r < 0.7:  # seeded unexpired open holds above the payer's balance
            u = min(fx["users"], key=lambda u: u["balance"])
            fx.setdefault("authorizations", []).append(
                {"id": "a_over", "from_user_id": u["id"], "to_user_id": fx["users"][0]["id"]
                 if fx["users"][0] is not u else fx["users"][1]["id"], "amount": u["balance"] + 1,
                 "note": "", "visibility": "public", "status": "open", "expires_at": {"$rel": 7200}})
        else:
            fx["authorization_ttl_seconds"] = self.rng.choice([0, -5, 1.5, "600"])
        return {"t": "reset", "fixture": fx}


# ---------------------------------------------------------------- driving, shrinking
def execute(bases, ops):
    run = Run(bases)
    if any(op["t"] == "upgrade" for op in ops):
        run.start_stage1()
    for i, op in enumerate(ops):
        try:
            run.step(i, op)
        except Divergence as d:
            return d.info
    return None


def shrink(bases, ops, budget=120):
    """Greedy delta-debugging: drop chunks (never the initial reset) while the run still diverges."""
    cur = ops
    chunk = max(1, (len(cur) - 1) // 2)
    while chunk >= 1 and budget > 0:
        i, changed = 1, False
        while i < len(cur) and budget > 0:
            cand = cur[:i] + cur[i + chunk:]
            budget -= 1
            if execute(bases, cand):
                cur, changed = cand, True
            else:
                i += chunk
        if not changed:
            chunk //= 2
    # also shrink bursts to their smallest failing membership
    for j, op in enumerate(cur):
        if op["t"] == "burst" and len(op["ops"]) > 1:
            k = 0
            while k < len(op["ops"]) and budget > 0 and len(op["ops"]) > 1:
                cand_op = dict(op, ops=op["ops"][:k] + op["ops"][k + 1:])
                cand = cur[:j] + [cand_op] + cur[j + 1:]
                budget -= 1
                if execute(bases, cand):
                    cur, op = cand, cand_op
                else:
                    k += 1
    return cur


def run_seed(bases, seed, steps):
    rng = random.Random(seed)
    run = Run(bases)
    upgrade_at = steps // 3 if bases[1] else None
    if upgrade_at:
        run.start_stage1()
    gen = Gen(rng, run)
    ops = [{"t": "reset", "fixture": gen_fixture(rng, stage=1 if upgrade_at else 2)}]
    counts = {"steps": 0, "calls": 0, "bursts": 0}
    try:
        run.step(0, ops[0])
        for i in range(1, steps + 1):
            op = {"t": "upgrade"} if i == upgrade_at else gen.next()
            if op is None:
                continue
            ops.append(op)
            counts["steps"] += 1
            counts["bursts"] += op["t"] == "burst"
            counts["calls"] += len(op["ops"]) if op["t"] == "burst" else 1
            run.step(len(ops) - 1, op)
    except Divergence as d:
        return d.info, ops, counts
    return None, ops, counts


def wait_healthy(base, secs=60):
    t0 = time.time()
    while time.time() - t0 < secs:
        r = http_call(base, "GET", "/health", "", {}, None)
        if r.get("status") == 200 and r.get("body") == {"status": "ok"}:
            return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--stage1-base", default=None, help="stage-1 service: start there, then upgrade via export/import")
    ap.add_argument("--seeds", default="1-10", help="e.g. 1-20 or 3,7,9")
    ap.add_argument("--steps", type=int, default=150)
    ap.add_argument("--out", default=None)
    ap.add_argument("--replay", default=None, help="JSON file written by a previous run")
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--no-empty-body", action="store_true")
    ap.add_argument("--coverage", action="store_true", help="print product status counts per endpoint")
    a = ap.parse_args()
    global NO_EMPTY_BODY
    NO_EMPTY_BODY = a.no_empty_body
    bases = (a.base, a.stage1_base)
    for base in filter(None, bases):
        if not wait_healthy(base):
            print(f"{base} not healthy within 60 s")
            return 2
    if a.replay:
        ops = json.load(open(a.replay))["minimal_ops"]
        d = execute(bases, ops)
        print(json.dumps(d, indent=1, ensure_ascii=False) if d else f"replay of {len(ops)} ops: no divergence")
        return 1 if d else 0
    seeds = []
    for part in a.seeds.split(","):
        lo, _, hi = part.partition("-")
        seeds += list(range(int(lo), int(hi or lo) + 1))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    tot = {"steps": 0, "calls": 0, "bursts": 0}
    bad = []
    for seed in seeds:
        d, ops, c = run_seed(bases, seed, a.steps)
        for k in tot:
            tot[k] += c[k]
        if d is None:
            print(f"seed {seed}: ok ({c['steps']} steps, {c['calls']} calls, {c['bursts']} bursts)")
            continue
        mini = ops[:d["step"] + 1] if a.no_shrink else shrink(bases, ops[:d["step"] + 1])
        dm = execute(bases, mini) or d
        bad.append(seed)
        print(f"seed {seed}: DIVERGENCE at step {d['step']}: {d['reason']}")
        print(f"  minimal sequence: {len(mini)} ops; final divergence: {dm['reason']}")
        if a.out:
            path = os.path.join(a.out, f"seed-{seed}.json")
            with open(path, "w") as f:
                json.dump({"seed": seed, "divergence": dm, "first_divergence": d, "minimal_ops": mini},
                          f, indent=1, ensure_ascii=False)
            print(f"  written {path}  (repeat: --replay {path})")
    if a.coverage:
        for k, v in sorted(COV.items(), key=lambda kv: (kv[0][1], kv[0][0], str(kv[0][2]), str(kv[0][3]))):
            print(f"  cov {v:6d}  {k[0]} {k[1]} -> {k[2]} {k[3] or ''}")
    print(f"SUMMARY seeds={len(seeds)} diverged={len(bad)} {bad} steps={tot['steps']} calls={tot['calls']} "
          f"bursts={tot['bursts']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
