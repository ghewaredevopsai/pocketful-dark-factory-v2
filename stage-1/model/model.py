"""Pocketful stage-1 executable reference model.

Written from the stage-1 requirements text only (see RULINGS.md for every reading chosen).
Plain and slow on purpose: one dict of state, one function per endpoint, no concurrency.

Model.handle(method, path, query, headers, raw_body) -> Result
  Result.status  : int for success (200/201/204)
  Result.body    : JSON-able success body
  Result.alts    : for errors, the frozenset of acceptable (status, code) pairs.
                   More than one pair means the requirements leave the precedence open
                   (every alternative changes nothing, so the ambiguity never affects state).
  Result.meta    : extra facts the differential driver needs (full filtered lists).
"""
import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs

ANY = "<any-string>"  # a field whose value the requirements do not fix (see RULINGS.md)

MAL = (400, "malformed_request")
NOKEY = (400, "missing_idempotency_key")
UNAUTH = (401, "unauthenticated")
FORB = (403, "forbidden")
NF = (404, "not_found")
REUSE = (409, "idempotency_key_reuse")
VAL = (422, "validation_failed")
INSUFF = (409, "insufficient_funds")
RNP = (409, "request_not_pending")
EMAIL_TAKEN = (409, "email_taken")
HANDLE_TAKEN = (409, "handle_taken")
SELF_PAY = (422, "self_payment")
SELF_REQ = (422, "self_request")

HANDLE_RE = re.compile(r"[a-z0-9_]{1,20}")
EMAIL_RE = re.compile(r"[^@]+@[^@]+")
INT_PARAM_RE = re.compile(r"[0-9]+")
STATUSES = ("pending", "paid", "declined", "cancelled")
BAD = object()  # unparseable body marker


class Result:
    def __init__(self, status=None, body=None, alts=None, meta=None):
        self.status, self.body, self.alts, self.meta = status, body, alts, meta or {}

    @property
    def ok(self):
        return self.alts is None

    def __repr__(self):
        return f"Result({self.status}, {self.body!r})" if self.ok else f"Error({sorted(self.alts)})"


def fail(errs):
    """errs: list of sets of (status, code). Any one of them is an acceptable answer."""
    alts = set()
    for e in errs:
        alts |= set(e)
    return Result(alts=frozenset(alts))


def now_rfc3339():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reject_constant(name):
    raise ValueError(name)


def parse_json(raw):
    if raw is None or raw == b"":
        return BAD
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return BAD


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------- field rules (§5, §8) ----------

def f_amount(d, lo=1):
    if "amount" not in d:
        return None, {VAL}
    v = d["amount"]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None, {VAL}
    if isinstance(v, float):
        if not math.isfinite(v) or not v.is_integer():
            return None, {VAL}
        v = int(v)
    if not lo <= v <= 1_000_000_000:
        return None, {VAL}
    return v, None


def f_note(d):
    if "note" not in d:
        return "", None
    v = d["note"]
    if not isinstance(v, str) or len(v) > 200:
        return None, {VAL}
    return v, None


def f_vis(d):
    if "visibility" not in d:
        return "public", None
    v = d["visibility"]
    if not isinstance(v, str) or v not in ("public", "private"):
        return None, {VAL}
    return v, None


def f_str(d, name, missing=frozenset({VAL}), wrong=frozenset({MAL})):
    if name not in d:
        return None, set(missing)
    v = d[name]
    if v is None:
        return None, set(wrong) | {VAL}
    if not isinstance(v, str):
        return None, set(wrong)
    return v, None


def derive_handle(email):
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "_", local)[:20]


def split_shares(amount, n):
    q, r = divmod(amount, n)
    return [q + 1 if i < r else q for i in range(n)]


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), 1000).hex()


class Model:
    def __init__(self, lenient=True, clock=now_rfc3339, bug=None):
        # lenient=True: emit ANY for fields the requirements leave open (driver mode).
        self.lenient, self.clock, self.bug = lenient, clock, bug
        self.s = self._empty()

    @staticmethod
    def _empty():
        return {"currency": None, "minor_units": None, "total": 0, "operators": [],
                "users": {}, "tokens": {}, "payments": {}, "requests": {}, "splits": {},
                "settlements": {}, "idem": {}, "seq": 0, "ctr": 0}

    # ---------- helpers ----------
    def _id(self, prefix):
        self.s["ctr"] += 1
        return f"{prefix}{self.s['ctr']}"

    def _seq(self):
        self.s["seq"] += 1
        return self.s["seq"]

    def _by_handle(self, handle):
        for u in self.s["users"].values():
            if u["handle"] == handle:
                return u
        return None

    def _handle_err(self, handle):
        # Unknown handle is 404; a string that cannot be a handle may also be read as 422.
        return {NF} if HANDLE_RE.fullmatch(handle) else {NF, VAL}

    def _auth(self, h):
        v = h.get("authorization")
        if not v or not v.startswith("Bearer "):
            return None
        uid = self.s["tokens"].get(v[len("Bearer "):])
        return uid if uid in self.s["users"] else None

    def pview(self, p):
        v = {k: p[k] for k in ("payment_id", "from_user_id", "from_handle", "to_user_id", "to_handle",
                               "amount", "currency", "note", "visibility", "request_id",
                               "settlement_id", "created_at")}
        if self.lenient and p.get("note_free"):
            v["note"] = ANY
        return v

    def rview(self, r):
        v = {k: r[k] for k in ("request_id", "requester_id", "requester_handle", "payer_id",
                               "payer_handle", "amount", "currency", "note", "status",
                               "payment_id", "created_at")}
        if self.lenient and r.get("note_free"):
            v["note"] = ANY
        return v

    def _move(self, frm, to, amount, note, vis, request_id=None, settlement_id=None,
              created_at=None, note_free=False):
        frm["balance"] -= amount
        to["balance"] += amount
        pid = self._id("mp")
        p = {"payment_id": pid, "from_user_id": frm["id"], "from_handle": frm["handle"],
             "to_user_id": to["id"], "to_handle": to["handle"], "amount": amount,
             "currency": self.s["currency"], "note": note, "visibility": vis,
             "request_id": request_id, "settlement_id": settlement_id,
             "created_at": created_at or self.clock(), "seq": self._seq(), "note_free": note_free}
        self.s["payments"][pid] = p
        return p

    def _new_request(self, requester, payer, amount, note, note_free=False):
        rid = self._id("mrq")
        r = {"request_id": rid, "requester_id": requester["id"], "requester_handle": requester["handle"],
             "payer_id": payer["id"], "payer_handle": payer["handle"], "amount": amount,
             "currency": self.s["currency"], "note": note, "status": "pending", "payment_id": None,
             "created_at": self.clock(), "seq": self._seq(), "note_free": note_free}
        self.s["requests"][rid] = r
        return r

    # ---------- dispatch ----------
    def handle(self, method, path, query="", headers=None, raw=None):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        q = parse_qs(query or "", keep_blank_values=True)
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/health":
            return Result(200, {"status": "ok"})
        if method == "POST" and path == "/_test/reset":
            return self.reset(raw)
        if method == "GET" and path == "/_test/export":
            return Result(200, {"track": "pocketful", "format_version": 1, "state": copy.deepcopy(self.s)})
        if method == "POST" and path == "/_test/import":
            return self.import_(raw)
        if method == "POST" and path == "/auth/signup":
            return self.signup(raw)
        if method == "POST" and path == "/auth/login":
            return self.login(raw)
        if method == "GET" and path == "/me":
            return self.me(h)
        if method == "POST" and path == "/payments":
            return self._idem(h, "POST", path, raw, self.payment)
        if method == "POST" and path == "/requests":
            return self._idem(h, "POST", path, raw, self.create_request)
        if method == "GET" and path == "/requests":
            return self.list_requests(h, q)
        if method == "POST" and len(parts) == 3 and parts[0] == "requests":
            rid, action = parts[1], parts[2]
            if action == "pay":
                return self._idem(h, "POST", path, raw, lambda uid, b: self.pay(uid, b, rid))
            if action == "decline":
                return self.transition(h, rid, "payer_id", "declined")
            if action == "cancel":
                return self.transition(h, rid, "requester_id", "cancelled")
        if method == "POST" and path == "/splits":
            return self._idem(h, "POST", path, raw, self.split)
        if method == "GET" and path == "/activity":
            return self.activity(h, q)
        if method == "POST" and path == "/settlements":
            return self._idem(h, "POST", path, raw, self.settlement, operator=True)
        return fail([{NF}])

    # ---------- §3.3 reset ----------
    def reset(self, raw):
        fx = parse_json(raw)
        if fx is BAD:
            return fail([{MAL}])
        bad = {MAL, VAL}
        try:
            assert isinstance(fx, dict) and isinstance(fx["currency"], str)
            mu = fx["minor_units"]
            assert type(mu) is int and mu in (0, 2, 3)
            users = fx["users"]
            assert isinstance(users, list)
            new = self._empty()
            new.update(currency=fx["currency"], minor_units=mu)
            for u in users:
                bal = u["balance"]
                assert type(bal) is int
                if bal < 0:
                    return fail([{VAL}])
                for k in ("id", "email", "password", "display_name", "handle"):
                    assert isinstance(u[k], str)
                new["users"][u["id"]] = {"id": u["id"], "email": u["email"], "salt": u["id"],
                                         "pw": hash_pw(u["password"], u["id"]),
                                         "display_name": u["display_name"], "handle": u["handle"],
                                         "balance": bal}
                new["total"] += bal
            seq = 0
            ts = self.clock()
            for p in fx.get("payments", []):
                fu, tu = new["users"][p["from_user_id"]], new["users"][p["to_user_id"]]
                seq += 1
                new["payments"][p["id"]] = {
                    "payment_id": p["id"], "from_user_id": fu["id"], "from_handle": fu["handle"],
                    "to_user_id": tu["id"], "to_handle": tu["handle"], "amount": p["amount"],
                    "currency": fx["currency"], "note": p.get("note", ""),
                    "visibility": p.get("visibility", "public"), "request_id": None,
                    "settlement_id": None, "created_at": ts, "seq": seq}
            for r in fx.get("requests", []):
                ru, pu = new["users"][r["requester_id"]], new["users"][r["payer_id"]]
                seq += 1
                new["requests"][r["id"]] = {
                    "request_id": r["id"], "requester_id": ru["id"], "requester_handle": ru["handle"],
                    "payer_id": pu["id"], "payer_handle": pu["handle"], "amount": r["amount"],
                    "currency": fx["currency"], "note": r.get("note", ""),
                    "status": r.get("status", "pending"), "payment_id": None,
                    "created_at": ts, "seq": seq}
            ops = fx.get("settlement_operator_ids", [])
            assert isinstance(ops, list)
            new["operators"] = list(ops)
            new["seq"] = seq
        except (AssertionError, KeyError, TypeError):
            return fail([bad])
        self.s = new
        return Result(204)

    # ---------- §10 import ----------
    def import_(self, raw):
        d = parse_json(raw)
        if d is BAD:
            return fail([{MAL}])
        if not isinstance(d, dict):
            return fail([{MAL, VAL}])
        st = d.get("state")
        fv = d.get("format_version")
        if (d.get("track") != "pocketful" or type(fv) is not int or fv != 1
                or not isinstance(st, dict) or set(st) != set(self._empty())):
            return fail([{VAL}])
        self.s = copy.deepcopy(st)
        return Result(204)

    # ---------- §6 auth ----------
    def signup(self, raw):
        b = parse_json(raw)
        if b is BAD or not isinstance(b, dict):
            return fail([{MAL}])
        errs = []
        email, e1 = f_str(b, "email")
        pw, e2 = f_str(b, "password")
        dn, e3 = f_str(b, "display_name")
        errs += [e for e in (e1, e2, e3) if e]
        if pw is not None and len(pw) < 8:
            errs.append({VAL})
        if email is not None:
            if not EMAIL_RE.fullmatch(email):
                errs.append({VAL})
            else:
                if any(u["email"] == email for u in self.s["users"].values()):
                    errs.append({EMAIL_TAKEN})
                if self._by_handle(derive_handle(email)):
                    errs.append({HANDLE_TAKEN})
        if errs:
            return fail(errs)
        uid = self._id("mu")
        self.s["users"][uid] = {"id": uid, "email": email, "salt": uid, "pw": hash_pw(pw, uid),
                                "display_name": dn, "handle": derive_handle(email), "balance": 0}
        tok = self._id("mtok")
        self.s["tokens"][tok] = uid
        return Result(201, {"user_id": uid, "display_name": dn, "token": tok})

    def login(self, raw):
        b = parse_json(raw)
        if b is BAD or not isinstance(b, dict):
            return fail([{MAL}])
        email, e1 = f_str(b, "email", missing={VAL, UNAUTH})
        pw, e2 = f_str(b, "password", missing={VAL, UNAUTH})
        if e1 or e2:
            return fail([e for e in (e1, e2) if e])
        for u in self.s["users"].values():
            if u["email"] == email and u["pw"] == hash_pw(pw, u["salt"]):
                tok = self._id("mtok")
                self.s["tokens"][tok] = u["id"]
                return Result(200, {"user_id": u["id"], "display_name": u["display_name"], "token": tok})
        return fail([{UNAUTH}])

    # ---------- §7 idempotency wrapper ----------
    def _idem(self, h, method, path, raw, fn, operator=False):
        errs = []
        uid = self._auth(h)
        if uid is None:
            errs.append({UNAUTH})
        body = parse_json(raw)
        if body is BAD or not isinstance(body, dict):
            errs.append({MAL})
        key = h.get("idempotency-key")
        if not key:
            errs.append({NOKEY})
        elif len(key) > 255:
            errs.append({VAL})
        if operator and uid is not None and uid not in self.s["operators"]:
            errs.append({FORB})
        if errs:
            return fail(errs)
        ik = "\x1f".join((uid, method, path, key))
        rec = self.s["idem"].get(ik)
        if rec is not None:
            if rec["body"] == canonical(body):
                if self.bug == "replay_reexecutes":
                    fn(uid, body)
                return Result(200, copy.deepcopy(rec["response"]))
            return fail([{REUSE}])
        res = fn(uid, body)
        if res.ok and res.status == 201:
            self.s["idem"][ik] = {"body": canonical(body), "response": copy.deepcopy(res.body)}
        return res

    # ---------- §8 endpoints ----------
    def me(self, h):
        uid = self._auth(h)
        if uid is None:
            return fail([{UNAUTH}])
        u = self.s["users"][uid]
        return Result(200, {"user_id": uid, "display_name": u["display_name"], "handle": u["handle"],
                            "balance": u["balance"], "currency": self.s["currency"],
                            "minor_units": self.s["minor_units"]})

    def payment(self, uid, b):
        errs = []
        to, e = f_str(b, "to_handle")
        e and errs.append(e)
        amt, e = f_amount(b)
        e and errs.append(e)
        note, e = f_note(b)
        e and errs.append(e)
        vis, e = f_vis(b)
        e and errs.append(e)
        me = self.s["users"][uid]
        tu = None
        if to is not None:
            tu = self._by_handle(to)
            if tu is None:
                errs.append(self._handle_err(to))
            elif tu["id"] == uid:
                errs.append({SELF_PAY})
        if errs:
            return fail(errs)
        if me["balance"] < amt and self.bug != "overdraft":
            return fail([{INSUFF}])
        return Result(201, self.pview(self._move(me, tu, amt, note, vis)))

    def create_request(self, uid, b):
        errs = []
        ph, e = f_str(b, "payer_handle")
        e and errs.append(e)
        amt, e = f_amount(b)
        e and errs.append(e)
        note, e = f_note(b)
        e and errs.append(e)
        pu = None
        if ph is not None:
            pu = self._by_handle(ph)
            if pu is None:
                errs.append(self._handle_err(ph))
            elif pu["id"] == uid:
                errs.append({SELF_REQ})
        if errs:
            return fail(errs)
        return Result(201, self.rview(self._new_request(self.s["users"][uid], pu, amt, note)))

    def pay(self, uid, b, rid):
        errs = []
        vis, e = f_vis(b)
        r = self.s["requests"].get(rid)
        if r is None:
            errs.append({NF})
        else:
            if r["payer_id"] != uid:
                errs.append({FORB} if r["requester_id"] == uid else {FORB, NF})
            if r["status"] != "pending":
                errs.append({RNP})
        e and errs.append(e)
        if errs:
            return fail(errs)
        payer, requester = self.s["users"][r["payer_id"]], self.s["users"][r["requester_id"]]
        if payer["balance"] < r["amount"]:
            return fail([{INSUFF}])
        p = self._move(payer, requester, r["amount"], r["note"], vis, request_id=rid, note_free=True)
        r["status"], r["payment_id"] = "paid", p["payment_id"]
        return Result(201, self.pview(p))

    def transition(self, h, rid, role, target):
        uid = self._auth(h)
        if uid is None:
            return fail([{UNAUTH}])
        r = self.s["requests"].get(rid)
        if r is None:
            return fail([{NF}])
        errs = []
        if r[role] != uid:
            errs.append({FORB} if uid in (r["payer_id"], r["requester_id"]) else {FORB, NF})
        if r["status"] not in ("pending", target):
            errs.append({RNP})
        if errs:
            return fail(errs)
        r["status"] = target
        return Result(200, self.rview(r))

    def _page(self, q, errs):
        def intp(name, default, lo, hi):
            vals = q.get(name)
            if not vals:
                return default
            v = vals[-1]
            if not INT_PARAM_RE.fullmatch(v) or not lo <= int(v) <= hi:
                errs.append({VAL})
                return default
            return int(v)
        return intp("limit", 50, 1, 200), intp("offset", 0, 0, float("inf"))

    def list_requests(self, h, q):
        errs = []
        uid = self._auth(h)
        if uid is None:
            errs.append({UNAUTH})
        limit, offset = self._page(q, errs)
        d = (q.get("direction") or [None])[-1]
        st = (q.get("status") or [None])[-1]
        if d is not None and d not in ("incoming", "outgoing"):
            errs.append({VAL})
        if st is not None and st not in STATUSES:
            errs.append({VAL})
        if errs:
            return fail(errs)
        items = [r for r in self.s["requests"].values()
                 if (d in (None, "incoming") and r["payer_id"] == uid)
                 or (d in (None, "outgoing") and r["requester_id"] == uid)]
        items = [r for r in items if st is None or r["status"] == st]
        items.sort(key=lambda r: -r["seq"])
        page = items[offset:offset + limit]
        return Result(200, {"requests": [self.rview(r) for r in page],
                            "has_more": len(items) > offset + len(page)},
                      meta={"list_key": "requests", "all": {r["request_id"]: self.rview(r) for r in items}})

    def activity(self, h, q):
        errs = []
        uid = self._auth(h)
        if uid is None:
            errs.append({UNAUTH})
        limit, offset = self._page(q, errs)
        if errs:
            return fail(errs)
        items = [p for p in self.s["payments"].values()
                 if p["visibility"] == "public" or uid in (p["from_user_id"], p["to_user_id"])
                 or self.bug == "private_leak"]
        items.sort(key=lambda p: -p["seq"])
        page = items[offset:offset + limit]
        return Result(200, {"payments": [self.pview(p) for p in page],
                            "has_more": len(items) > offset + len(page)},
                      meta={"list_key": "payments", "all": {p["payment_id"]: self.pview(p) for p in items}})

    def split(self, uid, b):
        errs = []
        amt, e = f_amount(b)
        e and errs.append(e)
        note, e = f_note(b)
        e and errs.append(e)
        users = []
        if "participant_handles" not in b:
            errs.append({VAL})
        elif not isinstance(b["participant_handles"], list):
            errs.append({MAL} if b["participant_handles"] is not None else {MAL, VAL})
        else:
            hs = b["participant_handles"]
            if not hs:
                errs.append({VAL})
            seen = set()
            for x in hs:
                if not isinstance(x, str):
                    errs.append({MAL, VAL})
                    continue
                if x in seen:
                    errs.append({VAL})
                seen.add(x)
                u = self._by_handle(x)
                if u is None:
                    errs.append(self._handle_err(x))
                users.append(u)
        if errs:
            return fail(errs)
        me = self.s["users"][uid]
        shares = split_shares(amt, len(users))
        reqs = [self._new_request(me, u, sh, note, note_free=True)
                for u, sh in zip(users, shares) if u["id"] != uid]
        sid = self._id("msp")
        body = {"split_id": sid, "amount": amt, "currency": self.s["currency"], "note": note,
                "shares": [{"handle": u["handle"], "amount": sh} for u, sh in zip(users, shares)],
                "requests": [self.rview(r) for r in reqs], "created_at": self.clock()}
        self.s["splits"][sid] = body
        return Result(201, copy.deepcopy(body))

    # ---------- §11 settlements ----------
    def settlement(self, uid, b):
        tr = b.get("transfers")
        if "transfers" not in b:
            return fail([{VAL}])
        if not isinstance(tr, list):
            return fail([{VAL, MAL}])
        if not 1 <= len(tr) <= 32:
            return fail([{VAL}])
        moves = []
        for t in tr:  # entry errors in input order; the first bad entry decides
            if not isinstance(t, dict):
                return fail([{VAL, MAL}])
            errs = []
            wrong = {MAL, VAL}
            fh, e = f_str(t, "from_handle", wrong=wrong)
            e and errs.append(e)
            th, e = f_str(t, "to_handle", wrong=wrong)
            e and errs.append(e)
            amt, e = f_amount(t)
            e and errs.append(e)
            note, e = f_note(t)
            e and errs.append(e)
            vis, e = f_vis(t)
            e and errs.append(e)
            fu = tu = None
            if fh is not None:
                fu = self._by_handle(fh)
                if fu is None:
                    errs.append(self._handle_err(fh))
            if th is not None:
                tu = self._by_handle(th)
                if tu is None:
                    errs.append(self._handle_err(th))
            if fh is not None and fh == th:
                errs.append({SELF_PAY})
            if errs:
                return fail(errs)
            moves.append((fu, tu, amt, note, vis))
        net = {}
        for fu, tu, amt, _, _ in moves:
            net[fu["id"]] = net.get(fu["id"], 0) - amt
            net[tu["id"]] = net.get(tu["id"], 0) + amt
        if any(self.s["users"][k]["balance"] + v < 0 for k, v in net.items()):
            return fail([{INSUFF}])
        sid = self._id("mst")
        ts = self.clock()
        ps = [self._move(fu, tu, amt, note, vis, settlement_id=sid, created_at=ts)
              for fu, tu, amt, note, vis in moves]
        body = {"settlement_id": sid, "committed_at": ts, "payments": [self.pview(p) for p in ps]}
        self.s["settlements"][sid] = [p["payment_id"] for p in ps]
        return Result(201, body)

    # ---------- invariants (§1) ----------
    def check_invariants(self):
        bal = [u["balance"] for u in self.s["users"].values()]
        problems = []
        if sum(bal) != self.s["total"]:
            problems.append(f"sum {sum(bal)} != seeded total {self.s['total']}")
        if any(b < 0 for b in bal):
            problems.append("negative balance")
        paid = [p["request_id"] for p in self.s["payments"].values() if p["request_id"]]
        if len(paid) != len(set(paid)):
            problems.append("a request moved money twice")
        return problems
