"""Pocketful stage-3 executable reference model (stages 1-2 + payment timestamps, historical balances,
statements with snapshots, payment revisions and corrections, historical holds).

Written from the stage-1, stage-2 and stage-3 requirements text only (see RULINGS.md for every reading chosen).
Plain and slow on purpose: one dict of state, one function per endpoint, no concurrency. Every historical
read is recomputed from scratch from the revision and hold-event records.

Model.handle(method, path, query, headers, raw_body) -> Result
  Result.status  : int for success (200/201/204)
  Result.body    : JSON-able success body
  Result.alts    : for errors, the frozenset of acceptable (status, code) pairs.
                   More than one pair means the requirements leave the precedence open
                   (every alternative changes nothing, so the ambiguity never affects state).
  Result.meta    : extra facts the differential driver needs (full filtered lists).

Time: every instant is an integer number of microseconds since the epoch. Server-assigned instants
(payment created_at, correction recorded_at, authorization created_at/closed_at, reset time) are the
model's own clock in strict mode (model_server.py); in lenient mode (driver) they are shown as
"unknown" markers until the driver adopts the product's value through the adopt_* hooks (after checking
the value lies inside the call's time window). Expiry is a state transition the caller drives
(expire_due / expire). Model(stage=1|2) emits earlier stages' shapes for upgrade runs.
"""
import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

ANY = "<any-string>"  # a field whose value the requirements do not fix (see RULINGS.md)
ANY_INT = "<any-integer>"
ANY_TS_OR_NULL = "<any-timestamp-or-null>"
TS_NEW = {"$ts": "server-assigned"}  # an instant the product assigns; format-checked, then adopted

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
NOT_OPEN = (409, "authorization_not_open")
EXPIRED = (409, "authorization_expired")
EXCEEDS = (422, "capture_exceeds_authorization")
STALE = (409, "stale_revision")
HIST = (409, "historical_overdraft")
LINKED = (422, "linked_payment_immutable")
AUTH_STATUSES = ("open", "captured", "voided", "expired")

HANDLE_RE = re.compile(r"[a-z0-9_]{1,20}")
EMAIL_RE = re.compile(r"[^@]+@[^@]+")
INT_PARAM_RE = re.compile(r"[0-9]+")
RFC3339_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})")
STATUSES = ("pending", "paid", "declined", "cancelled")
BAD = object()  # unparseable body marker
US = 1_000_000


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


# ---------- instants ----------
def us_of(s):
    """Strict RFC 3339 with an explicit offset -> integer microseconds since the epoch, else None."""
    if not isinstance(s, str):
        return None
    m = RFC3339_RE.fullmatch(s)
    if not m:
        return None
    y, mo, d, h, mi, se, frac, off = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), tzinfo=timezone.utc)
    except ValueError:
        return None
    o = 0
    if off != "Z":
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        o = (oh * 60 + om) * 60 * (1 if off[0] == "+" else -1)
    return (int(dt.timestamp()) - o) * US + int((frac or "0").ljust(6, "0")[:6])


def fmt_us(u, off_min=0):
    d = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=u)
    return d.astimezone(timezone(timedelta(minutes=off_min))).isoformat(
        timespec="microseconds" if u % US else "seconds")


def parse_ts(s):  # seconds (float) — used for expiry against the wall clock
    return us_of(s) / US


def fmt_ts(epoch):
    return fmt_us(int(epoch) * US)


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


def _last(q, name):
    v = q.get(name)
    return v[-1] if v else None


class Model:
    def __init__(self, lenient=True, bug=None, stage=3, now=time.time):
        # lenient=True: emit ANY / markers for values the requirements leave open or the product assigns.
        self.lenient, self.bug, self.stage, self.now = lenient, bug, stage, now
        self.idkey = None  # payment id -> sort key for statement ties (driver maps to the product's ids)
        self.s = self._empty()
        self.snaps, self.snap_n = {}, 0

    STAGE1_KEYS = ("currency", "minor_units", "total", "operators", "users", "tokens", "payments",
                   "requests", "splits", "settlements", "idem", "seq", "ctr")

    @staticmethod
    def _empty():
        return {"currency": None, "minor_units": None, "total": 0, "operators": [],
                "users": {}, "tokens": {}, "payments": {}, "requests": {}, "splits": {},
                "settlements": {}, "idem": {}, "seq": 0, "ctr": 0, "auths": {}, "ttl": 600, "last_us": 0}

    # ---------- clock ----------
    def _stamp(self):
        u = max(int(self.now() * US), self.s["last_us"] + 1)
        self.s["last_us"] = u
        return u, fmt_us(u)

    def now_us(self):
        return int(self.now() * US)

    def tv(self, obj, key):
        """Time value for a view: the stored string, or the unknown-marker while the product's value is pending."""
        if self.lenient and key in obj.get("unk", ()):
            return TS_NEW
        return obj[key]

    # ---------- adoption hooks (driver: take the product's server-assigned instant) ----------
    def adopt_payment(self, pid, s):
        p = self.s["payments"][pid]
        u = us_of(s)
        p["created_at"], p["t"] = s, u
        r = p["revs"][0]
        r["effective_at"] = r["recorded_at"] = s
        r["eff"] = r["rec"] = u
        p["unk"] = []

    def adopt_rev(self, pid, n, s):
        r = self.s["payments"][pid]["revs"][n - 1]
        r["recorded_at"], r["rec"], r["unk"] = s, us_of(s), []

    def adopt_auth(self, aid, created=None, expires=None, closed=None):
        a = self.s["auths"][aid]
        if created is not None:
            a["created_at"], a["c"] = created, us_of(created)
        if expires is not None:
            self.set_expiry(aid, expires)
        if closed is not None:
            a["closed_at"], a["void_t"] = closed, us_of(closed)
        a["unk"] = [k for k in a.get("unk", []) if not ((k == "created_at" and created) or
                                                       (k == "closed_at" and closed))]

    # ---------- holds and expiry ----------
    def held(self, uid):
        return sum(a["remaining_amount"] for a in self.s["auths"].values()
                   if a["status"] == "open" and a["from_user_id"] == uid)

    def available(self, uid):
        return self.s["users"][uid]["balance"] - self.held(uid)

    def expire(self, aid):
        a = self.s["auths"][aid]
        if a["status"] == "open":
            a["status"], a["remaining_amount"] = "expired", 0

    def due(self, lo, hi):
        """Open holds certainly expired by time lo, and those whose expiry falls in (lo, hi], in order."""
        if self.bug == "no_expiry":
            return [], []
        opn = sorted((a["exp"], aid) for aid, a in self.s["auths"].items() if a["status"] == "open")
        return [aid for e, aid in opn if e <= lo], [aid for e, aid in opn if lo < e <= hi]

    def expire_due(self, now):
        for aid in self.due(now, now)[0]:
            self.expire(aid)

    def set_expiry(self, aid, expires_at):
        """Adopt the product's expires_at (driver: already checked = created_at + ttl)."""
        a = self.s["auths"][aid]
        a["expires_at"], a["exp"], a["exp_us"], a["exp_exact"] = \
            expires_at, parse_ts(expires_at), us_of(expires_at), True

    # ---------- history (stage 3) ----------
    def rev_at(self, p, K):
        """Latest revision recorded at or before K (K None: every revision known now)."""
        if self.bug == "known_at_ignored":
            return p["revs"][-1]
        sel = None
        for r in p["revs"]:
            if K is None or r["rec"] <= K:
                sel = r
        return sel

    @staticmethod
    def sign(p, uid):
        return -1 if p["from_user_id"] == uid else 1

    def total_at(self, uid, T, K):
        """Balance after every selected revision with effective_at <= T (T None: no limit)."""
        b = self.s["users"][uid]["opening"]
        for p in self.s["payments"].values():
            if uid not in (p["from_user_id"], p["to_user_id"]):
                continue
            r = self.rev_at(p, K)
            if r is None or (T is not None and (r["eff"] > T or (r["eff"] == T and self.bug == "asof_exclusive"))):
                continue
            b += self.sign(p, uid) * r["amount"]
        return b

    def hold_events(self, a):
        evs = [(self.s["payments"][pid]["t"], amt, closes) for pid, amt, closes in a["caps"]]
        if a["status"] == "voided" and a.get("void_t") is not None:
            evs.append((a["void_t"], 0, True))
        return sorted(evs, key=lambda e: e[0])

    def held_at(self, uid, T, K):
        """Funds held for uid in the view (as_of T, known_at K); T None = the instant of the read."""
        h = 0
        lims = [x for x in (T, K) if x is not None]
        lim = min(lims) if lims else None
        for a in self.s["auths"].values():
            if a["from_user_id"] != uid or a.get("seeded_closed"):
                continue
            if lim is not None and a["c"] > lim:
                continue
            rem, closed = a["amount"], False
            for t, amt, closes in self.hold_events(a):
                if lim is not None and t > lim:
                    break
                rem -= amt
                if closes or rem <= 0:
                    closed = True
                    break
            if closed:
                continue
            if T is None:
                expired = a["status"] == "expired" or (a["status"] != "open" and a["exp_us"] <= self.now_us())
            else:
                expired = a["exp_us"] <= T
            if not expired:
                h += rem
        return h

    def boundaries(self, uid):
        bs = set()
        for p in self.s["payments"].values():
            if uid in (p["from_user_id"], p["to_user_id"]):
                bs.add(p["revs"][-1]["eff"])
        for a in self.s["auths"].values():
            if a["from_user_id"] == uid and not a.get("seeded_closed"):
                bs.add(a["c"])
                bs.add(a["exp_us"])
                bs.update(t for t, _, _ in self.hold_events(a))
        return sorted(bs)

    def history_ok(self, uid):
        """Total and available nonnegative at every effective/event boundary, under the latest revisions."""
        for b in self.boundaries(uid):
            tot = self.total_at(uid, b, None)
            if tot < 0 or tot - self.held_at(uid, b, None) < 0:
                return False
        return True

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

    def pview(self, p, amount=None):
        v = {k: p[k] for k in ("payment_id", "from_user_id", "from_handle", "to_user_id", "to_handle",
                               "amount", "currency", "note", "visibility", "request_id",
                               "settlement_id")}
        v["created_at"] = self.tv(p, "created_at")
        if self.stage >= 2:
            v["authorization_id"] = p.get("authorization_id")
        if amount is not None:
            v["amount"] = amount
        if self.lenient and p.get("note_free"):
            v["note"] = ANY
        return v

    def rview(self, r):
        v = {k: r[k] for k in ("request_id", "requester_id", "requester_handle", "payer_id",
                               "payer_handle", "amount", "currency", "note", "status",
                               "payment_id", "created_at")}
        if self.lenient:
            v["created_at"] = TS_NEW  # request instants are format-checked only
            if r.get("note_free"):
                v["note"] = ANY
        return v

    def aview(self, a):
        v = {k: a[k] for k in ("authorization_id", "from_user_id", "from_handle", "to_user_id", "to_handle",
                               "amount", "captured_amount", "currency", "note", "visibility", "status",
                               "expires_at", "payment_id", "payment_ids", "remaining_amount")}
        v["created_at"] = self.tv(a, "created_at")
        if self.lenient and not a.get("exp_exact"):
            v["expires_at"] = {"$ttl": a["ttl"]}
        if self.lenient and a.get("seeded_closed"):
            v["captured_amount"] = ANY_INT
        if self.stage >= 3:
            v["closed_at"] = self.closed_at(a)
        return v

    def closed_at(self, a):
        st = a["status"]
        if st == "open":
            return None
        if a.get("seeded_closed"):
            return ANY_TS_OR_NULL if self.lenient else None
        if st == "expired":
            return a["expires_at"] if a.get("exp_exact") or not self.lenient else TS_NEW
        if st == "captured":
            return self.tv(self.s["payments"][a["payment_id"]], "created_at")
        if "closed_at" in a.get("unk", ()):  # voided: by this run (pending adoption) or before an upgrade
            return (ANY_TS_OR_NULL if a.get("legacy_void") else TS_NEW) if self.lenient else a["closed_at"]
        return a["closed_at"]

    def revview(self, p, r):
        v = {"payment_id": p["payment_id"], "revision": r["revision"], "amount": r["amount"],
             "effective_at": r["effective_at"], "recorded_at": r["recorded_at"], "reason": r["reason"]}
        if self.lenient and "recorded_at" in r.get("unk", ()):
            v["recorded_at"] = TS_NEW
        if self.lenient and r["revision"] == 1 and "created_at" in p.get("unk", ()):
            v["effective_at"] = v["recorded_at"] = TS_NEW
        return v

    def _new_payment(self, frm, to, amount, note, vis, t, request_id=None, settlement_id=None,
                     note_free=False, authorization_id=None):
        u, s = t
        pid = self._id("mp")
        p = {"payment_id": pid, "from_user_id": frm["id"], "from_handle": frm["handle"],
             "to_user_id": to["id"], "to_handle": to["handle"], "amount": amount,
             "currency": self.s["currency"], "note": note, "visibility": vis,
             "request_id": request_id, "settlement_id": settlement_id, "authorization_id": authorization_id,
             "created_at": s, "t": u, "seq": self._seq(), "note_free": note_free, "unk": ["created_at"],
             "revs": [{"revision": 1, "amount": amount, "eff": u, "rec": u, "effective_at": s,
                       "recorded_at": s, "reason": "", "unk": []}]}
        self.s["payments"][pid] = p
        return p

    def _move(self, frm, to, amount, note, vis, t=None, **kw):
        frm["balance"] -= amount
        to["balance"] += amount
        return self._new_payment(frm, to, amount, note, vis, t or self._stamp(), **kw)

    def _new_request(self, requester, payer, amount, note, note_free=False):
        rid = self._id("mrq")
        r = {"request_id": rid, "requester_id": requester["id"], "requester_handle": requester["handle"],
             "payer_id": payer["id"], "payer_handle": payer["handle"], "amount": amount,
             "currency": self.s["currency"], "note": note, "status": "pending", "payment_id": None,
             "created_at": self._stamp()[1], "seq": self._seq(), "note_free": note_free}
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
            return self.me(h, q)
        if method == "POST" and path == "/payments":
            return self._idem(h, "POST", path, raw, self.payment)
        if self.stage >= 3 and len(parts) == 3 and parts[0] == "payments":
            pid, action = parts[1], parts[2]
            if method == "POST" and action == "corrections":
                return self._idem(h, "POST", path, raw, lambda uid, b: self.correct(uid, b, pid))
            if method == "GET" and action == "revisions":
                return self.revisions(h, pid)
        if self.stage >= 3 and method == "GET" and path == "/statement":
            return self.statement(h, q)
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
        if self.stage >= 2 and method == "POST" and path == "/authorizations":
            return self._idem(h, "POST", path, raw, self.authorize)
        if self.stage >= 2 and method == "GET" and path == "/authorizations":
            return self.list_auths(h, q)
        if self.stage >= 2 and method == "POST" and len(parts) == 3 and parts[0] == "authorizations":
            aid, action = parts[1], parts[2]
            if action == "capture":
                return self._idem(h, "POST", path, raw, lambda uid, b: self.capture(uid, b, aid))
            if action == "void":
                return self.void(h, aid)
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
                                         "balance": bal, "opening": bal}
                new["total"] += bal
            seq = 0
            ru, rs = self._stamp()  # the reset time
            for p in fx.get("payments", []):
                fu, tu = new["users"][p["from_user_id"]], new["users"][p["to_user_id"]]
                seq += 1
                known = self.stage >= 3 and "created_at" in p
                if known:
                    t = us_of(p["created_at"])
                    if t is None:
                        return fail([{VAL} if isinstance(p["created_at"], str) else {VAL, MAL}])
                    if t > ru:
                        return fail([{VAL}])
                    ts = p["created_at"]
                else:
                    t, ts = ru, rs
                amt = p["amount"]
                new["payments"][p["id"]] = {
                    "payment_id": p["id"], "from_user_id": fu["id"], "from_handle": fu["handle"],
                    "to_user_id": tu["id"], "to_handle": tu["handle"], "amount": amt,
                    "currency": fx["currency"], "note": p.get("note", ""),
                    "visibility": p.get("visibility", "public"), "request_id": None,
                    "settlement_id": None, "authorization_id": None, "created_at": ts, "t": t, "seq": seq,
                    "unk": [] if known else ["created_at"],
                    "revs": [{"revision": 1, "amount": amt, "eff": t, "rec": t, "effective_at": ts,
                              "recorded_at": ts, "reason": "", "unk": []}]}
                fu["opening"] += amt  # opening = seeded ending balance minus net of seeded payments
                tu["opening"] -= amt
            for r in fx.get("requests", []):
                rq, pu = new["users"][r["requester_id"]], new["users"][r["payer_id"]]
                seq += 1
                new["requests"][r["id"]] = {
                    "request_id": r["id"], "requester_id": rq["id"], "requester_handle": rq["handle"],
                    "payer_id": pu["id"], "payer_handle": pu["handle"], "amount": r["amount"],
                    "currency": fx["currency"], "note": r.get("note", ""),
                    "status": r.get("status", "pending"), "payment_id": None,
                    "created_at": rs, "seq": seq}
            ops = fx.get("settlement_operator_ids", [])
            assert isinstance(ops, list)
            new["operators"] = list(ops)
            if self.stage >= 2:
                ttl = fx.get("authorization_ttl_seconds", 600)
                if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
                    return fail([{VAL}] if isinstance(ttl, (int, float)) and not isinstance(ttl, bool)
                                else [{VAL, MAL}])
                new["ttl"] = ttl
                for a in fx.get("authorizations", []):
                    fu, tu = new["users"][a["from_user_id"]], new["users"][a["to_user_id"]]
                    assert a["status"] in AUTH_STATUSES and type(a["amount"]) is int
                    assert 0 <= a["amount"] <= 1_000_000_000
                    exp = us_of(a["expires_at"])
                    assert exp is not None
                    st = a["status"] if not (a["status"] == "open" and exp <= ru) else "expired"
                    cknown = self.stage >= 3 and "created_at" in a
                    c, cs = (us_of(a["created_at"]), a["created_at"]) if cknown else (ru, rs)
                    assert c is not None
                    seq += 1
                    new["auths"][a["id"]] = {
                        "authorization_id": a["id"], "from_user_id": fu["id"], "from_handle": fu["handle"],
                        "to_user_id": tu["id"], "to_handle": tu["handle"], "amount": a["amount"],
                        "captured_amount": 0, "currency": fx["currency"], "note": a.get("note", ""),
                        "visibility": a.get("visibility", "public"), "status": st,
                        "expires_at": a["expires_at"], "exp": exp / US, "exp_us": exp, "exp_exact": True,
                        "ttl": None, "payment_id": None, "payment_ids": [], "caps": [],
                        "remaining_amount": a["amount"] if st == "open" else 0,
                        "seeded_closed": a["status"] in ("captured", "voided", "expired"),
                        "created_at": cs, "c": c, "unk": [] if cknown else ["created_at"],
                        "closed_at": None, "void_t": None, "seq": seq}
                for uid, u in new["users"].items():
                    if sum(a["remaining_amount"] for a in new["auths"].values()
                           if a["status"] == "open" and a["from_user_id"] == uid) > u["balance"]:
                        return fail([{VAL}])
            new["seq"] = seq
            new["last_us"] = ru
        except (AssertionError, KeyError, TypeError, ValueError, AttributeError):
            return fail([bad])
        self.s = new
        self.snaps = {}
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
                or not isinstance(st, dict) or not set(self.STAGE1_KEYS) <= set(st)
                or not set(st) <= set(self._empty())):
            return fail([{VAL}])
        last = self.s["last_us"]
        new = self._empty()
        new.update(copy.deepcopy(st))
        new["last_us"] = max(last, new["last_us"])
        self.s = new
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
                                "display_name": dn, "handle": derive_handle(email), "balance": 0, "opening": 0}
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
    @staticmethod
    def _instant_params(q, names, errs):
        out = {}
        for n in names:
            v = _last(q, n)
            if v is None:
                out[n] = None
                continue
            u = us_of(v)
            if u is None:
                errs.append({VAL})
            out[n] = u
        return out

    def me(self, h, q=None):
        q = q or {}
        errs = []
        uid = self._auth(h)
        if uid is None:
            errs.append({UNAUTH})
        temporal = self.stage >= 3 and ("as_of" in q or "known_at" in q)
        ip = self._instant_params(q, ("as_of", "known_at"), errs) if temporal else {}
        if errs:
            return fail(errs)
        u = self.s["users"][uid]
        body = {"user_id": uid, "display_name": u["display_name"], "handle": u["handle"],
                "balance": u["balance"], "currency": self.s["currency"], "minor_units": self.s["minor_units"]}
        if self.stage >= 2:
            body.update(total=u["balance"], available=self.available(uid), held=self.held(uid))
        if temporal:
            T, K = ip["as_of"], ip["known_at"]
            tot = self.total_at(uid, T, K)
            held = self.held_at(uid, T, K)
            body.update(balance=tot, total=tot, held=held, available=tot - held)
            for n in ("as_of", "known_at"):
                if n in q:
                    body[n] = q[n][-1]
        return Result(200, body)

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
        if self._spendable(uid) < amt and self.bug != "overdraft":
            return fail([{INSUFF}])
        return Result(201, self.pview(self._move(me, tu, amt, note, vis)))

    def _spendable(self, uid):
        return self.s["users"][uid]["balance"] if self.bug == "held_ignored" else self.available(uid)

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
        if self._spendable(payer["id"]) < r["amount"]:
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
        items.sort(key=lambda p: (-p["t"], -p["seq"]))
        page = items[offset:offset + limit]
        return Result(200, {"payments": [self.pview(p) for p in page],
                            "has_more": len(items) > offset + len(page)},
                      meta={"list_key": "payments", "all": {p["payment_id"]: self.pview(p) for p in items}})

    # ---------- stage 3: statements, revisions, corrections ----------
    def statement(self, h, q):
        errs, meta = [], {}
        uid = self._auth(h)
        if uid is None:
            errs.append({UNAUTH})
        limit, offset = self._page(q, errs)
        ip = self._instant_params(q, ("from", "to", "known_at"), errs)
        token = _last(q, "snapshot")
        if token is not None:
            if any(n in q for n in ("from", "to", "known_at")):
                errs.append({VAL})
            snap = self.snaps.get(token)
            if uid is not None and (snap is None or snap["uid"] != uid):
                errs.append({NF} if token else {NF, VAL})
            if errs:
                return fail(errs)
            if self.bug == "snapshot_live":
                snap = self._statement_full(uid, snap["F"], snap["T"], snap["K"])
        else:
            if errs:
                return fail(errs)
            F, K = ip["from"], ip["known_at"]
            T = ip["to"] if ip["to"] is not None else self.now_us()
            snap = self._statement_full(uid, F, T, K)
            self.snap_n += 1
            token = f"msnap{self.snap_n}"
            self.snaps[token] = snap
            if F is not None and F > T:  # from after to (explicit or the default now): 422 also acceptable
                meta = {"or_err": {VAL}, "snapshot": token}
        ents = snap["entries"]
        page = copy.deepcopy(ents[offset:offset + limit])
        if self.bug == "stmt_page_balance" and offset:
            run = snap["opening"]
            for e in page:
                run += e["delta"]
                e["balance_after"] = run
        body = {"opening_balance": snap["opening"], "entries": page,
                "closing_balance": snap["closing"], "has_more": len(ents) > offset + len(page)}
        if "snapshot" not in q:
            body["snapshot"] = token
        return Result(200, body, meta=meta)

    def _statement_full(self, uid, F, T, K):
        key = self.idkey or (lambda pid: pid)
        rows = []
        for p in self.s["payments"].values():
            if uid not in (p["from_user_id"], p["to_user_id"]):
                continue
            r = self.rev_at(p, K)
            if r is not None:
                rows.append((r["eff"], key(p["payment_id"]), p, r))
        rows.sort(key=lambda x: (x[0], x[1]))
        bal = self.s["users"][uid]["opening"]
        for eff, _, p, r in rows:
            if F is not None and eff < F:
                bal += self.sign(p, uid) * r["amount"]
        opening = bal
        entries = []
        for eff, _, p, r in rows:
            if (F is None or eff >= F) and eff < T:
                d = self.sign(p, uid) * r["amount"]
                bal += d
                rv = self.revview(p, r)
                entries.append({"payment": self.pview(p, amount=r["amount"]), "delta": d, "balance_after": bal,
                                "revision": r["revision"], "effective_at": rv["effective_at"],
                                "recorded_at": rv["recorded_at"]})
        return {"uid": uid, "F": F, "T": T, "K": K, "opening": opening, "closing": bal, "entries": entries}

    def revisions(self, h, pid):
        uid = self._auth(h)
        if uid is None:
            return fail([{UNAUTH}])
        p = self.s["payments"].get(pid)
        if p is None or uid not in (p["from_user_id"], p["to_user_id"]):
            return fail([{NF}])
        return Result(200, {"revisions": [self.revview(p, r) for r in p["revs"]]})

    def correct(self, uid, b, pid):
        errs = []
        p = self.s["payments"].get(pid)
        if p is None:
            errs.append({NF})
        elif p["from_user_id"] != uid:
            errs.append({FORB})
        er = b.get("expected_revision")
        if "expected_revision" not in b:
            errs.append({VAL})
        elif isinstance(er, bool) or type(er) is not int:
            errs.append({VAL} if isinstance(er, (int, float)) and not isinstance(er, bool) else {VAL, MAL})
            er = None
        elif er < 1:
            errs.append({VAL})
            er = None
        amt, e = f_amount(b, lo=0)
        e and errs.append(e)
        reason = b.get("reason")
        if "reason" not in b:
            errs.append({VAL})
        elif not isinstance(reason, str):
            errs.append({VAL, MAL})
        elif not 1 <= len(reason) <= 200:
            errs.append({VAL})
        eff_s = b.get("effective_at")
        eff = us_of(eff_s)
        if "effective_at" not in b:
            errs.append({VAL})
        elif not isinstance(eff_s, str):
            errs.append({VAL, MAL})
        elif eff is None or eff > self.now_us():
            errs.append({VAL})
        if p is not None:
            if (p["settlement_id"] or p.get("authorization_id")) and self.bug != "linked_mutable":
                errs.append({LINKED})
            if er is not None and er != len(p["revs"]) and self.bug != "stale_ignored":
                errs.append({STALE})
        if errs:
            return fail(errs)
        frm, to = self.s["users"][p["from_user_id"]], self.s["users"][p["to_user_id"]]
        diff = amt - p["revs"][-1]["amount"]
        debtor = frm if diff > 0 else to if diff < 0 else None
        if debtor is not None and self._spendable(debtor["id"]) < abs(diff):
            return fail([{INSUFF}])
        u, s = self._stamp()
        rev = {"revision": len(p["revs"]) + 1, "amount": amt, "eff": eff, "rec": u, "effective_at": eff_s,
               "recorded_at": s, "reason": reason, "unk": ["recorded_at"]}
        p["revs"].append(rev)
        frm["balance"] -= diff
        to["balance"] += diff
        if self.bug != "no_hist_check" and not (self.history_ok(frm["id"]) and self.history_ok(to["id"])):
            p["revs"].pop()
            frm["balance"] += diff
            to["balance"] -= diff
            return fail([{HIST}])
        return Result(201, self.revview(p, rev))

    # ---------- stage 2: authorizations ----------
    def authorize(self, uid, b):
        errs = []
        to, e = f_str(b, "to_handle")
        e and errs.append(e)
        amt, e = f_amount(b)
        e and errs.append(e)
        note, e = f_note(b)
        e and errs.append(e)
        vis, e = f_vis(b)
        e and errs.append(e)
        tu = None
        if to is not None:
            tu = self._by_handle(to)
            if tu is None:
                errs.append(self._handle_err(to))
            elif tu["id"] == uid:
                errs.append({SELF_PAY})
        if errs:
            return fail(errs)
        if self._spendable(uid) < amt:
            return fail([{INSUFF}])
        me = self.s["users"][uid]
        aid = self._id("ma")
        ttl = self.s["ttl"]
        c, cs = self._stamp()
        exp = c + ttl * US
        a = {"authorization_id": aid, "from_user_id": uid, "from_handle": me["handle"],
             "to_user_id": tu["id"], "to_handle": tu["handle"], "amount": amt, "captured_amount": 0,
             "currency": self.s["currency"], "note": note, "visibility": vis, "status": "open",
             "expires_at": fmt_us(exp), "exp": exp / US, "exp_us": exp, "exp_exact": False, "ttl": ttl,
             "payment_id": None, "payment_ids": [], "caps": [], "remaining_amount": amt,
             "created_at": cs, "c": c, "unk": ["created_at"], "closed_at": None, "void_t": None,
             "seq": self._seq()}
        self.s["auths"][aid] = a
        return Result(201, self.aview(a))

    def capture(self, uid, b, aid):
        errs = []
        amt = None
        if "amount" in b:
            v = b["amount"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (
                    isinstance(v, float) and (not math.isfinite(v) or not v.is_integer())) or v < 1:
                errs.append({VAL})
            else:
                amt = int(v)
        final = True
        if "final" in b:
            if not isinstance(b["final"], bool):
                errs.append({MAL, VAL})
            else:
                final = b["final"]
        a = self.s["auths"].get(aid)
        if a is None:
            errs.append({NF})
        else:
            if a["to_user_id"] != uid:
                errs.append({FORB})
            if a["status"] == "expired":
                errs.append({NOT_OPEN, EXPIRED})
            elif a["status"] != "open" and self.bug != "capture_closed":
                errs.append({NOT_OPEN})
            elif amt is not None and amt > a["remaining_amount"]:
                errs.append({EXCEEDS, VAL} if amt > 1_000_000_000 else {EXCEEDS})
        if errs:
            return fail(errs)
        cap = a["remaining_amount"] if amt is None else amt
        if self.bug == "capture_closed" and a["status"] != "open":
            cap = amt or a["amount"]
        frm, to = self.s["users"][a["from_user_id"]], self.s["users"][a["to_user_id"]]
        p = self._move(frm, to, cap, a["note"], a["visibility"], authorization_id=aid)
        a["captured_amount"] += cap
        a["payment_id"] = p["payment_id"]
        a["payment_ids"].append(p["payment_id"])
        a["remaining_amount"] = max(0, a["remaining_amount"] - cap)
        closes = final or a["remaining_amount"] == 0
        a["caps"].append((p["payment_id"], cap, closes))
        if closes:
            a["status"], a["remaining_amount"] = "captured", 0
        return Result(201, self.pview(p))

    def void(self, h, aid):
        uid = self._auth(h)
        if uid is None:
            return fail([{UNAUTH}])
        a = self.s["auths"].get(aid)
        if a is None:
            return fail([{NF}])
        errs = []
        if a["from_user_id"] != uid:
            errs.append({FORB})
        if a["status"] not in ("open", "voided"):
            errs.append({NOT_OPEN})
        if errs:
            return fail(errs)
        if a["status"] == "open":
            u, s = self._stamp()
            a["status"], a["remaining_amount"] = "voided", 0
            a["void_t"], a["closed_at"] = u, s
            a["unk"] = a.get("unk", []) + ["closed_at"]
            if self.stage < 3:
                a["legacy_void"] = True
        return Result(200, self.aview(a))

    def list_auths(self, h, q):
        errs = []
        uid = self._auth(h)
        if uid is None:
            errs.append({UNAUTH})
        limit, offset = self._page(q, errs)
        d = (q.get("direction") or [None])[-1]
        st = (q.get("status") or [None])[-1]
        if d is not None and d not in ("incoming", "outgoing"):
            errs.append({VAL})
        if st is not None and st not in AUTH_STATUSES:
            errs.append({VAL})
        if errs:
            return fail(errs)
        items = [a for a in self.s["auths"].values()
                 if (d in (None, "outgoing") and a["from_user_id"] == uid)
                 or (d in (None, "incoming") and a["to_user_id"] == uid)]
        items = [a for a in items if st is None or a["status"] == st]
        items.sort(key=lambda a: -a["seq"])
        page = items[offset:offset + limit]
        return Result(200, {"authorizations": [self.aview(a) for a in page],
                            "has_more": len(items) > offset + len(page)},
                      meta={"list_key": "authorizations",
                            "all": {a["authorization_id"]: self.aview(a) for a in items}})

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
                "requests": [self.rview(r) for r in reqs],
                "created_at": TS_NEW if self.lenient else self._stamp()[1]}
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
        if any(self._spendable(k) + v < 0 for k, v in net.items()):
            return fail([{INSUFF}])
        sid = self._id("mst")
        t = self._stamp()
        ps = [self._move(fu, tu, amt, note, vis, t=t, settlement_id=sid)
              for fu, tu, amt, note, vis in moves]
        body = {"settlement_id": sid, "committed_at": TS_NEW if self.lenient else t[1],
                "payments": [self.pview(p) for p in ps]}
        self.s["settlements"][sid] = [p["payment_id"] for p in ps]
        return Result(201, body)

    # ---------- invariants (§1, stage 2 §1-3, stage 3) ----------
    def check_invariants(self):
        bal = [u["balance"] for u in self.s["users"].values()]
        problems = []
        if sum(bal) != self.s["total"]:
            problems.append(f"sum {sum(bal)} != seeded total {self.s['total']}")
        if sum(u["opening"] for u in self.s["users"].values()) != self.s["total"]:
            problems.append("opening balances do not sum to the seeded total")
        if any(b < 0 for b in bal):
            problems.append("negative balance")
        paid = [p["request_id"] for p in self.s["payments"].values() if p["request_id"]]
        if len(paid) != len(set(paid)):
            problems.append("a request moved money twice")
        for uid, u in self.s["users"].items():
            if self.available(uid) < 0:
                problems.append(f"available < 0 for {uid}")
            if self.total_at(uid, None, None) != u["balance"]:
                problems.append(f"{uid}: balance {u['balance']} != opening + latest revisions")
        for aid, a in self.s["auths"].items():
            caps = [p["amount"] for p in self.s["payments"].values() if p.get("authorization_id") == aid]
            if a["captured_amount"] + a["remaining_amount"] > a["amount"] or sum(caps) != (
                    a["captured_amount"] if not a.get("seeded_closed") else sum(caps)):
                problems.append(f"capture accounting broken for {aid}")
            if a["status"] != "open" and a["remaining_amount"] != 0:
                problems.append(f"closed {aid} still holds funds")
        for p in self.s["payments"].values():
            recs = [r["rec"] for r in p["revs"]]
            if any(x >= y for x, y in zip(recs, recs[1:])):
                problems.append(f"{p['payment_id']}: recorded times not strictly increasing")
        return problems
