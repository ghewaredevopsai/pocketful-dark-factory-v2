"""Pocketful stage 1: a wallet/payments HTTP service.

One process, one in-memory State object, one lock (LOCK). Every read and every
read-modify-write of state happens while LOCK is held, so LOCK is the single
serialization point: balances, idempotency records and request statuses change
together or not at all. Password hashing (slow by design) runs outside LOCK.
Amounts are Python ints (parsed from JSON as Decimal, never float).
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_AMOUNT = 1_000_000_000
MAX_BALANCE = 2 ** 53
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
DIGITS_RE = re.compile(r"^[0-9]+$")
STATUSES = ("pending", "paid", "declined", "cancelled")

LOCK = threading.Lock()
HASH_SLOTS = threading.BoundedSemaphore(4)


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def bad(msg="malformed request"):
    return ApiError(400, "malformed_request", msg)


def invalid(msg="validation failed"):
    return ApiError(422, "validation_failed", msg)


# ---------------------------------------------------------------- helpers

def now_dt():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="microseconds")


def parse_ts(s):
    if not isinstance(s, str):
        raise invalid("bad timestamp")
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise invalid("bad timestamp")
    if dt.tzinfo is None:
        raise invalid("timestamp needs an offset")
    return dt


def _reject_constant(name):
    raise ValueError(name)


def parse_json(raw, empty_ok=False):
    """Parse a request body. Numbers become Decimal (exact); NaN/Infinity are rejected.

    An empty body is unparseable (400) unless the endpoint's body is optional (empty_ok).
    """
    if raw.strip() == b"":
        if empty_ok:
            return {}
        raise bad("body is empty")
    try:
        text = raw.decode("utf-8")
        return json.loads(text, parse_float=Decimal, parse_int=Decimal,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise bad("body is not valid JSON")


def integral(v):
    """Return the int value of a JSON number with an integral value, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal) and v.is_finite():
        if v.is_zero():
            return 0
        if v.adjusted() > 30:  # far beyond any valid amount; avoid huge-exponent arithmetic
            return None
        if v.adjusted() < 0:  # 0 < |v| < 1
            return None
        if v == v.to_integral_value():
            return int(v)
    return None


def canon(v):
    """A canonical, comparable form of a parsed JSON value (§7 'same body')."""
    if isinstance(v, dict):
        return {k: canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [canon(x) for x in v]
    if isinstance(v, Decimal):
        # exact numeric identity without arithmetic: sign, significant digits, exponent
        sign, digits, exp = v.as_tuple()
        digits = list(digits)
        while digits and digits[0] == 0:
            digits.pop(0)
        while digits and digits[-1] == 0:
            digits.pop()
            exp += 1
        return {"\u0000n": "0" if not digits else "%d:%s:%d" % (sign, "".join(map(str, digits)), exp)}
    return v


def canon_str(v):
    return json.dumps(canon(v), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def amount_field(body, lo=1):
    if "amount" not in body:
        raise invalid("amount is required")
    a = integral(body["amount"])
    if a is None or a < lo or a > MAX_AMOUNT:
        raise invalid("amount must be an integer from %d to %d" % (lo, MAX_AMOUNT))
    return a


def note_field(body):
    if "note" not in body:
        return ""
    n = body["note"]
    if not isinstance(n, str):
        raise invalid("note must be a string")
    if len(n) > 200:
        raise invalid("note is longer than 200 characters")
    return n


def visibility_field(body):
    if "visibility" not in body:
        return "public"
    v = body["visibility"]
    if v not in ("public", "private") or not isinstance(v, str):
        raise invalid("visibility must be public or private")
    return v


def str_field(body, name):
    if name not in body:
        raise invalid("%s is required" % name)
    v = body[name]
    if not isinstance(v, str):
        raise bad("%s must be a string" % name)
    return v


def int_query(q, name, default, lo, hi=None):
    if name not in q:
        return default
    s = q[name]
    if not DIGITS_RE.match(s):
        raise invalid("%s must be plain decimal digits" % name)
    n = int(s)
    if n < lo or (hi is not None and n > hi):
        raise invalid("%s out of range" % name)
    return n


def page(q):
    return int_query(q, "limit", 50, 1, 200), int_query(q, "offset", 0, 0)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    with HASH_SLOTS:
        h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1,
                           maxmem=64 * 1024 * 1024, dklen=32)
    return salt.hex(), h.hex()


def check_password(password, salt_hex, hash_hex):
    _, h = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(h, hash_hex)


def derive_handle(email):
    local = email.rsplit("@", 1)[0]
    out = []
    for ch in local:
        if "A" <= ch <= "Z":
            ch = ch.lower()
        out.append(ch if ("a" <= ch <= "z" or "0" <= ch <= "9" or ch == "_") else "_")
    return "".join(out)[:20]


# ---------------------------------------------------------------- state

class State:
    def __init__(self, currency="EUR", minor_units=2):
        self.currency = currency
        self.minor_units = minor_units
        self.users = {}        # id -> user dict (internal)
        self.by_email = {}     # lowercased email -> id
        self.by_handle = {}    # handle -> id
        self.tokens = {}       # token -> user id
        self.payments = []     # payment dicts in creation order (public fields + "_ts", "_seq")
        self.payment_ids = {}  # payment id -> payment dict
        self.requests = {}     # request id -> request dict (public fields + "_ts", "_seq")
        self.idem = {}         # "uid\x00path\x00key" -> {"canon": str, "response": str}
        self.operators = set()
        self.seq = 0
        self.last_ts = None

    # ids and clock
    def next_seq(self):
        self.seq += 1
        return self.seq

    def new_id(self, prefix, taken):
        while True:
            i = "%s_%d" % (prefix, self.next_seq())
            if i not in taken:
                return i

    def stamp(self):
        t = now_dt()
        if self.last_ts is not None and t <= self.last_ts:
            t = self.last_ts + datetime.timedelta(microseconds=1)
        self.last_ts = t
        return t

    # views
    def payment_view(self, p):
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def request_view(self, r):
        return {k: v for k, v in r.items() if not k.startswith("_")}

    # mutations
    def add_payment(self, frm, to, amount, note, visibility, ts, request_id=None, settlement_id=None):
        p = {
            "payment_id": self.new_id("p", self.payment_ids),
            "from_user_id": frm["id"], "from_handle": frm["handle"],
            "to_user_id": to["id"], "to_handle": to["handle"],
            "amount": amount, "currency": self.currency, "note": note,
            "visibility": visibility, "request_id": request_id,
            "settlement_id": settlement_id, "created_at": iso(ts),
            "_ts": ts, "_seq": self.next_seq(),
        }
        self.payments.append(p)
        self.payment_ids[p["payment_id"]] = p
        return p

    def add_request(self, requester, payer, amount, note, ts):
        r = {
            "request_id": self.new_id("rq", self.requests),
            "requester_id": requester["id"], "requester_handle": requester["handle"],
            "payer_id": payer["id"], "payer_handle": payer["handle"],
            "amount": amount, "currency": self.currency, "note": note,
            "status": "pending", "payment_id": None, "created_at": iso(ts),
            "_ts": ts, "_seq": self.next_seq(),
        }
        self.requests[r["request_id"]] = r
        return r

    def move(self, frm, to, amount):
        if frm["balance"] < amount:
            raise ApiError(409, "insufficient_funds", "balance is below amount")
        if to["balance"] + amount > MAX_BALANCE:
            raise invalid("balance would exceed 2^53")
        frm["balance"] -= amount
        to["balance"] += amount

    # export / import
    def to_dict(self):
        return {
            "currency": self.currency, "minor_units": self.minor_units,
            "users": [dict(u) for u in self.users.values()],
            "tokens": dict(self.tokens),
            "payments": [dict(self.payment_view(p), _seq=p["_seq"]) for p in self.payments],
            "requests": [dict(self.request_view(r), _seq=r["_seq"]) for r in self.requests.values()],
            "idempotency": [dict(k=k, **v) for k, v in self.idem.items()],
            "operators": sorted(self.operators),
            "seq": self.seq,
            "last_ts": iso(self.last_ts) if self.last_ts else None,
        }

    @classmethod
    def from_dict(cls, d):
        """Rebuild an exported state; any inconsistency raises ApiError 422."""
        def need(cond, what):
            if not cond:
                raise invalid("invalid state: %s" % what)

        def is_int(v):
            return integral(v) is not None

        need(isinstance(d, dict), "state must be an object")
        need(isinstance(d.get("currency"), str) and d["currency"], "currency")
        need(integral(d.get("minor_units")) in (0, 2, 3), "minor_units")
        st = cls(d["currency"], integral(d["minor_units"]))
        need(isinstance(d.get("users"), list), "users")
        for u in d["users"]:
            need(isinstance(u, dict), "user")
            for f in ("id", "email", "display_name", "handle", "salt", "hash"):
                need(isinstance(u.get(f), str), "user." + f)
            need(HANDLE_RE.match(u["handle"]), "user.handle")
            need(is_int(u.get("balance")) and 0 <= integral(u["balance"]) <= MAX_BALANCE, "user.balance")
            need(u["id"] not in st.users and u["handle"] not in st.by_handle
                 and u["email"].lower() not in st.by_email, "duplicate user")
            try:
                bytes.fromhex(u["salt"]), bytes.fromhex(u["hash"])
            except ValueError:
                need(False, "user password hash")
            nu = {"id": u["id"], "email": u["email"], "display_name": u["display_name"],
                  "handle": u["handle"], "balance": integral(u["balance"]),
                  "salt": u["salt"], "hash": u["hash"]}
            st.users[nu["id"]] = nu
            st.by_email[nu["email"].lower()] = nu["id"]
            st.by_handle[nu["handle"]] = nu["id"]
        need(isinstance(d.get("tokens"), dict), "tokens")
        for t, uid in d["tokens"].items():
            need(uid in st.users, "token user")
            st.tokens[t] = uid
        need(isinstance(d.get("payments"), list), "payments")
        for p in d["payments"]:
            need(isinstance(p, dict), "payment")
            need(isinstance(p.get("payment_id"), str) and p["payment_id"] not in st.payment_ids, "payment id")
            need(p.get("from_user_id") in st.users and p.get("to_user_id") in st.users, "payment users")
            need(is_int(p.get("amount")) and is_int(p.get("_seq")), "payment amount")
            need(isinstance(p.get("note"), str) and p.get("visibility") in ("public", "private"), "payment note")
            need(p.get("request_id") is None or isinstance(p["request_id"], str), "payment request_id")
            need(p.get("settlement_id") is None or isinstance(p["settlement_id"], str), "payment settlement_id")
            ts = parse_ts(p.get("created_at"))
            np_ = {k: p[k] for k in ("payment_id", "from_user_id", "from_handle", "to_user_id", "to_handle",
                                     "currency", "note", "visibility", "request_id", "settlement_id",
                                     "created_at") if k in p}
            np_.update(amount=integral(p["amount"]), _ts=ts, _seq=integral(p["_seq"]))
            st.payments.append(np_)
            st.payment_ids[np_["payment_id"]] = np_
        need(isinstance(d.get("requests"), list), "requests")
        for r in d["requests"]:
            need(isinstance(r, dict), "request")
            need(isinstance(r.get("request_id"), str) and r["request_id"] not in st.requests, "request id")
            need(r.get("requester_id") in st.users and r.get("payer_id") in st.users, "request users")
            need(is_int(r.get("amount")) and is_int(r.get("_seq")), "request amount")
            need(r.get("status") in STATUSES and isinstance(r.get("note"), str), "request status")
            need(r.get("payment_id") is None or isinstance(r["payment_id"], str), "request payment_id")
            ts = parse_ts(r.get("created_at"))
            nr = {k: r[k] for k in ("request_id", "requester_id", "requester_handle", "payer_id",
                                    "payer_handle", "currency", "note", "status", "payment_id",
                                    "created_at") if k in r}
            nr.update(amount=integral(r["amount"]), _ts=ts, _seq=integral(r["_seq"]))
            st.requests[nr["request_id"]] = nr
        need(isinstance(d.get("idempotency"), list), "idempotency")
        for rec in d["idempotency"]:
            need(isinstance(rec, dict) and isinstance(rec.get("k"), str)
                 and isinstance(rec.get("canon"), str) and isinstance(rec.get("response"), str), "idempotency")
            st.idem[rec["k"]] = {"canon": rec["canon"], "response": rec["response"]}
        need(isinstance(d.get("operators"), list) and all(o in st.users for o in d["operators"]), "operators")
        st.operators = set(d["operators"])
        need(is_int(d.get("seq")), "seq")
        st.seq = integral(d["seq"])
        if d.get("last_ts") is not None:
            st.last_ts = parse_ts(d["last_ts"])
        return st


def state_from_fixture(fx):
    """Validate a reset fixture and build a State. Raises ApiError (4xx) on any problem."""
    if not isinstance(fx, dict):
        raise bad("fixture must be an object")

    def typed(obj, name, kind, required=True):
        if name not in obj:
            if required:
                raise invalid("%s is required" % name)
            return None
        v = obj[name]
        if kind == "int":
            if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
                raise bad("%s must be a number" % name)
            iv = integral(v)
            if iv is None:
                raise invalid("%s must be an integer" % name)
            return iv
        if not isinstance(v, kind):
            raise bad("%s has the wrong type" % name)
        return v

    currency = typed(fx, "currency", str)
    if not currency:
        raise invalid("currency is empty")
    mu = typed(fx, "minor_units", "int")
    if mu not in (0, 2, 3):
        raise invalid("minor_units must be 0, 2 or 3")
    st = State(currency, mu)
    users = typed(fx, "users", list)
    pw_cache = {}
    for u in users:
        if not isinstance(u, dict):
            raise bad("user must be an object")
        uid = typed(u, "id", str)
        email = typed(u, "email", str)
        pw = typed(u, "password", str)
        dn = typed(u, "display_name", str)
        handle = typed(u, "handle", str)
        bal = typed(u, "balance", "int")
        if not uid or len(uid) > 64:
            raise invalid("user id must be 1 to 64 characters")
        if not EMAIL_RE.match(email):
            raise invalid("bad email")
        if not HANDLE_RE.match(handle):
            raise invalid("bad handle")
        if bal < 0 or bal > MAX_BALANCE:
            raise invalid("balance out of range")
        if uid in st.users or handle in st.by_handle or email.lower() in st.by_email:
            raise invalid("duplicate user id, email or handle")
        if pw not in pw_cache:
            pw_cache[pw] = hash_password(pw)
        salt, h = pw_cache[pw]
        st.users[uid] = {"id": uid, "email": email, "display_name": dn, "handle": handle,
                         "balance": bal, "salt": salt, "hash": h}
        st.by_email[email.lower()] = uid
        st.by_handle[handle] = uid
    pw_cache.clear()

    base = now_dt()
    seeded = []  # (dict, explicit ts or None)
    for p in typed(fx, "payments", list, False) or []:
        if not isinstance(p, dict):
            raise bad("payment must be an object")
        pid = typed(p, "id", str)
        frm, to = typed(p, "from_user_id", str), typed(p, "to_user_id", str)
        amt = typed(p, "amount", "int")
        note = typed(p, "note", str, False)
        vis = typed(p, "visibility", str, False) or "public"
        rid = p.get("request_id")
        sid = p.get("settlement_id")
        if not pid or len(pid) > 64 or pid in st.payment_ids:
            raise invalid("bad or duplicate payment id")
        if frm not in st.users or to not in st.users:
            raise invalid("payment references an unknown user")
        if amt < 0 or amt > MAX_AMOUNT:
            raise invalid("payment amount out of range")
        if vis not in ("public", "private"):
            raise invalid("bad visibility")
        if rid is not None and not isinstance(rid, str) or sid is not None and not isinstance(sid, str):
            raise bad("request_id/settlement_id must be strings")
        ts = parse_ts(p["created_at"]) if "created_at" in p else None
        fu, tu = st.users[frm], st.users[to]
        rec = {"payment_id": pid, "from_user_id": frm, "from_handle": fu["handle"],
               "to_user_id": to, "to_handle": tu["handle"], "amount": amt,
               "currency": currency, "note": note or "", "visibility": vis,
               "request_id": rid, "settlement_id": sid}
        st.payment_ids[pid] = rec
        st.payments.append(rec)
        seeded.append((rec, ts))
    for r in typed(fx, "requests", list, False) or []:
        if not isinstance(r, dict):
            raise bad("request must be an object")
        rid = typed(r, "id", str)
        rq, py = typed(r, "requester_id", str), typed(r, "payer_id", str)
        amt = typed(r, "amount", "int")
        note = typed(r, "note", str, False)
        status = typed(r, "status", str, False) or "pending"
        pay_id = r.get("payment_id")
        if not rid or len(rid) > 64 or rid in st.requests:
            raise invalid("bad or duplicate request id")
        if rq not in st.users or py not in st.users:
            raise invalid("request references an unknown user")
        if amt < 0 or amt > MAX_AMOUNT:
            raise invalid("request amount out of range")
        if status not in STATUSES:
            raise invalid("bad status")
        if pay_id is not None and not isinstance(pay_id, str):
            raise bad("payment_id must be a string")
        ts = parse_ts(r["created_at"]) if "created_at" in r else None
        rec = {"request_id": rid, "requester_id": rq, "requester_handle": st.users[rq]["handle"],
               "payer_id": py, "payer_handle": st.users[py]["handle"], "amount": amt,
               "currency": currency, "note": note or "", "status": status,
               "payment_id": pay_id}
        st.requests[rid] = rec
        seeded.append((rec, ts))
    # Seeded records without a timestamp get increasing instants in fixture order.
    for i, (rec, ts) in enumerate(seeded):
        ts = ts or base + datetime.timedelta(microseconds=i)
        rec["_ts"], rec["_seq"] = ts, st.next_seq()
        rec["created_at"] = iso(ts)
    st.last_ts = max([base + datetime.timedelta(microseconds=len(seeded))]
                     + [rec["_ts"] for rec, _ in seeded])
    ops = typed(fx, "settlement_operator_ids", list, False) or []
    for o in ops:
        if not isinstance(o, str):
            raise bad("operator ids must be strings")
        if o not in st.users:
            raise invalid("operator id is not a user")
    st.operators = set(ops)
    return st


STATE = State()


def newest_first(items):
    return sorted(items, key=lambda x: (x["_ts"], x["_seq"]), reverse=True)


# ---------------------------------------------------------------- handlers
# Each handler runs with LOCK held unless noted, and returns (status, body).

def h_me(st, user, q, body):
    return 200, {"user_id": user["id"], "display_name": user["display_name"], "handle": user["handle"],
                 "balance": user["balance"], "currency": st.currency, "minor_units": st.minor_units}


def h_payment(st, user, q, body):
    for f in ("to_handle",):
        if f in body and not isinstance(body[f], str):
            raise bad("%s must be a string" % f)
    amount = amount_field(body)
    note = note_field(body)
    vis = visibility_field(body)
    to_handle = str_field(body, "to_handle")
    if to_handle == user["handle"]:
        raise ApiError(422, "self_payment", "cannot pay yourself")
    to_id = st.by_handle.get(to_handle)
    if to_id is None:
        raise ApiError(404, "not_found", "no user has that handle")
    to = st.users[to_id]
    st.move(user, to, amount)
    p = st.add_payment(user, to, amount, note, vis, st.stamp())
    return 201, st.payment_view(p)


def h_create_request(st, user, q, body):
    if "payer_handle" in body and not isinstance(body["payer_handle"], str):
        raise bad("payer_handle must be a string")
    amount = amount_field(body)
    note = note_field(body)
    payer_handle = str_field(body, "payer_handle")
    if payer_handle == user["handle"]:
        raise ApiError(422, "self_request", "cannot request from yourself")
    pid = st.by_handle.get(payer_handle)
    if pid is None:
        raise ApiError(404, "not_found", "no user has that handle")
    r = st.add_request(user, st.users[pid], amount, note, st.stamp())
    return 201, st.request_view(r)


def find_request(st, rid):
    r = st.requests.get(rid)
    if r is None:
        raise ApiError(404, "not_found", "no such request")
    return r


def h_pay(st, user, q, body, rid):
    vis = visibility_field(body)
    r = find_request(st, rid)
    if r["payer_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the payer may pay")
    if r["status"] != "pending":
        raise ApiError(409, "request_not_pending", "request is %s" % r["status"])
    requester = st.users[r["requester_id"]]
    st.move(user, requester, r["amount"])
    p = st.add_payment(user, requester, r["amount"], r["note"], vis, st.stamp(), request_id=r["request_id"])
    r["status"], r["payment_id"] = "paid", p["payment_id"]
    return 201, st.payment_view(p)


def h_decline(st, user, q, body, rid):
    r = find_request(st, rid)
    if r["payer_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the payer may decline")
    if r["status"] not in ("pending", "declined"):
        raise ApiError(409, "request_not_pending", "request is %s" % r["status"])
    r["status"] = "declined"
    return 200, st.request_view(r)


def h_cancel(st, user, q, body, rid):
    r = find_request(st, rid)
    if r["requester_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the requester may cancel")
    if r["status"] not in ("pending", "cancelled"):
        raise ApiError(409, "request_not_pending", "request is %s" % r["status"])
    r["status"] = "cancelled"
    return 200, st.request_view(r)


def h_list_requests(st, user, q, body):
    limit, offset = page(q)
    direction = q.get("direction")
    if direction is not None and direction not in ("incoming", "outgoing"):
        raise invalid("direction must be incoming or outgoing")
    status = q.get("status")
    if status is not None and status not in STATUSES:
        raise invalid("unknown status")
    uid = user["id"]
    items = []
    for r in st.requests.values():
        mine = (direction in (None, "incoming") and r["payer_id"] == uid) or \
               (direction in (None, "outgoing") and r["requester_id"] == uid)
        if mine and (status is None or r["status"] == status):
            items.append(r)
    items = newest_first(items)
    return 200, {"requests": [st.request_view(r) for r in items[offset:offset + limit]],
                 "has_more": len(items) > offset + limit}


def h_activity(st, user, q, body):
    limit, offset = page(q)
    uid = user["id"]
    items = newest_first([p for p in st.payments if p["visibility"] == "public"
                          or p["from_user_id"] == uid or p["to_user_id"] == uid])
    return 200, {"payments": [st.payment_view(p) for p in items[offset:offset + limit]],
                 "has_more": len(items) > offset + limit}


def split_shares(amount, n):
    base, rem = divmod(amount, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def h_split(st, user, q, body):
    handles = body.get("participant_handles")
    if "participant_handles" in body and (not isinstance(handles, list)
                                          or not all(isinstance(h, str) for h in handles)):
        raise bad("participant_handles must be a list of strings")
    amount = amount_field(body)
    note = note_field(body)
    if handles is None:
        raise invalid("participant_handles is required")
    if not handles or len(set(handles)) != len(handles):
        raise invalid("participant_handles must be non-empty and without duplicates")
    users = []
    for h in handles:
        uid = st.by_handle.get(h)
        if uid is None:
            raise ApiError(404, "not_found", "no user has handle %s" % h)
        users.append(st.users[uid])
    shares = split_shares(amount, len(handles))
    ts = st.stamp()
    reqs = [st.add_request(user, u, s, note, ts) for u, s in zip(users, shares) if u["id"] != user["id"]]
    return 201, {"split_id": st.new_id("sp", ()), "amount": amount, "currency": st.currency,
                 "note": note, "shares": [{"handle": h, "amount": s} for h, s in zip(handles, shares)],
                 "requests": [st.request_view(r) for r in reqs], "created_at": iso(ts)}


def h_settlement(st, user, q, body):
    transfers = body.get("transfers")
    if not isinstance(transfers, list) or not 1 <= len(transfers) <= 32:
        raise invalid("transfers must be a list of 1 to 32 objects")
    plan = []
    for t in transfers:
        if not isinstance(t, dict):
            raise invalid("each transfer must be an object")
        for f in ("from_handle", "to_handle"):
            if f in t and not isinstance(t[f], str):
                raise invalid("%s must be a string" % f)
        amount = amount_field(t)
        note = note_field(t)
        vis = visibility_field(t)
        if "from_handle" not in t or "to_handle" not in t:
            raise invalid("from_handle and to_handle are required")
        if t["from_handle"] == t["to_handle"]:
            raise ApiError(422, "self_payment", "a transfer cannot go to its sender")
        ids = [st.by_handle.get(t["from_handle"]), st.by_handle.get(t["to_handle"])]
        if None in ids:
            raise ApiError(404, "not_found", "no user has that handle")
        plan.append((st.users[ids[0]], st.users[ids[1]], amount, note, vis))
    delta = {}
    for frm, to, amount, _, _ in plan:
        delta[frm["id"]] = delta.get(frm["id"], 0) - amount
        delta[to["id"]] = delta.get(to["id"], 0) + amount
    for uid, d in delta.items():
        b = st.users[uid]["balance"] + d
        if b < 0:
            raise ApiError(409, "insufficient_funds", "settlement is not affordable")
        if b > MAX_BALANCE:
            raise invalid("balance would exceed 2^53")
    for uid, d in delta.items():
        st.users[uid]["balance"] += d
    ts = st.stamp()
    sid = st.new_id("st", ())
    pays = [st.add_payment(f, t, a, n, v, ts, settlement_id=sid) for f, t, a, n, v in plan]
    return 201, {"settlement_id": sid, "committed_at": iso(ts),
                 "payments": [st.payment_view(p) for p in pays]}


# ---------------------------------------------------------------- HTTP layer

ROUTES = [
    # (method, regex, handler, needs idempotency key)
    ("GET", re.compile(r"^/me$"), h_me, False),
    ("POST", re.compile(r"^/payments$"), h_payment, True),
    ("POST", re.compile(r"^/requests$"), h_create_request, True),
    ("GET", re.compile(r"^/requests$"), h_list_requests, False),
    ("POST", re.compile(r"^/requests/([^/]+)/pay$"), h_pay, True),
    ("POST", re.compile(r"^/requests/([^/]+)/decline$"), h_decline, False),
    ("POST", re.compile(r"^/requests/([^/]+)/cancel$"), h_cancel, False),
    ("POST", re.compile(r"^/splits$"), h_split, True),
    ("GET", re.compile(r"^/activity$"), h_activity, False),
    ("POST", re.compile(r"^/settlements$"), h_settlement, True),
]
PUBLIC = {"/health", "/_test/reset", "/_test/export", "/_test/import", "/auth/signup", "/auth/login"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pocketful"
    sys_version = ""
    timeout = 60

    def log_message(self, *args):
        pass

    # -- I/O
    def read_body(self):
        """Return the raw body, or None when the client sent less than it promised."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    return None
                try:
                    size = int(line.split(b";")[0].strip(), 16)
                except ValueError:
                    raise bad("bad chunked encoding")
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                data = self.rfile.read(size)
                if len(data) < size:
                    return None
                chunks.append(data)
                self.rfile.readline(65537)
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        if not DIGITS_RE.match(cl.strip()):
            self.close_connection = True
            raise bad("bad Content-Length")
        n = int(cl.strip())
        if n > 16 * 1024 * 1024:
            self.close_connection = True
            raise ApiError(413, "payload_too_large", "body too large")
        data = self.rfile.read(n) if n else b""
        if len(data) < n:
            return None
        return data

    def send(self, status, payload=None, raw=None):
        if status == 204:
            self.send_response(204)
            self.end_headers()
            return
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def send_error_body(self, e):
        self.send(e.status, {"error": {"code": e.code, "message": e.message}})

    def dispatch(self, method):
        try:
            self.route(method)
        except ApiError as e:
            self.send_error_body(e)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # never leak a traceback; keep the connection sane
            self.close_connection = True
            try:
                self.send_error_body(ApiError(500, "internal_error", type(e).__name__))
            except OSError:
                pass

    do_GET = lambda self: self.dispatch("GET")
    do_POST = lambda self: self.dispatch("POST")
    do_PUT = lambda self: self.dispatch("PUT")
    do_DELETE = lambda self: self.dispatch("DELETE")
    do_PATCH = lambda self: self.dispatch("PATCH")

    # -- routing
    def route(self, method):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        q = {}
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            q.setdefault(k, v)
        raw = self.read_body()
        if raw is None:  # cut-off body: never act on a partial request
            self.close_connection = True
            return
        if path in PUBLIC:
            return self.public(method, path, raw)
        allowed = [m for m, r, _, _ in ROUTES if r.match(path)]
        if not allowed:
            raise ApiError(404, "not_found", "no such endpoint")
        if method not in allowed:
            raise ApiError(405, "method_not_allowed", "method not allowed")
        m, rx, fn, idem = next(r for r in ROUTES if r[0] == method and r[1].match(path))
        args = rx.match(path).groups()
        token = self.bearer()
        if fn is h_settlement:
            with LOCK:
                uid = STATE.tokens.get(token)
                if uid is not None and uid not in STATE.operators:
                    raise ApiError(403, "forbidden", "not a settlement operator")
        key = None
        if idem:
            key = self.headers.get("Idempotency-Key")
            # auth first (R129), then the key header
            with LOCK:
                if STATE.tokens.get(token) is None:
                    raise ApiError(401, "unauthenticated", "missing or unknown bearer token")
            if not key:
                raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header is required")
            if len(key) > 255:
                raise invalid("Idempotency-Key is longer than 255 characters")
        body = parse_json(raw, empty_ok=fn is h_pay) if idem else {}
        if not isinstance(body, dict):
            raise bad("body must be a JSON object")
        with LOCK:
            st = STATE
            uid = st.tokens.get(token)
            if uid is None:
                raise ApiError(401, "unauthenticated", "missing or unknown bearer token")
            user = st.users[uid]
            if not idem:
                status, out = fn(st, user, q, body, *args)
                return self.send(status, out)
            if fn is h_settlement and uid not in st.operators:
                raise ApiError(403, "forbidden", "not a settlement operator")
            k = "%s\x00%s\x00%s" % (uid, path, key)
            c = canon_str(body)
            rec = st.idem.get(k)
            if rec is not None:
                if rec["canon"] != c:
                    raise ApiError(409, "idempotency_key_reuse", "key already used with a different body")
                return self.send(200, raw=rec["response"].encode("utf-8"))
            status, out = fn(st, user, q, body, *args)
            resp = json.dumps(out, ensure_ascii=False)
            st.idem[k] = {"canon": c, "response": resp}
            return self.send(status, raw=resp.encode("utf-8"))

    def bearer(self):
        h = self.headers.get("Authorization") or ""
        parts = h.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise ApiError(401, "unauthenticated", "missing or malformed bearer token")
        return parts[1].strip()

    def public(self, method, path, raw):
        want = "GET" if path in ("/health", "/_test/export") else "POST"
        if method != want:
            raise ApiError(405, "method_not_allowed", "method not allowed")
        if path == "/health":
            return self.send(200, {"status": "ok"})
        if path == "/_test/export":
            with LOCK:
                snap = STATE.to_dict()
            return self.send(200, {"track": "pocketful", "format_version": 1, "state": snap})
        body = parse_json(raw)
        if path == "/_test/reset":
            new = state_from_fixture(body)
            return self.swap(new)
        if path == "/_test/import":
            if not isinstance(body, dict):
                raise bad("body must be a JSON object")
            if body.get("track") != "pocketful" or integral(body.get("format_version")) != 1 \
                    or "state" not in body:
                raise invalid("expected track pocketful, format_version 1 and state")
            try:
                new = State.from_dict(body["state"])
            except ApiError:
                raise
            except Exception:
                raise invalid("invalid state")
            return self.swap(new)
        if not isinstance(body, dict):
            raise bad("body must be a JSON object")
        if path == "/auth/signup":
            return self.signup(body)
        return self.login(body)

    def swap(self, new):
        global STATE
        with LOCK:
            STATE = new
        return self.send(204)

    def signup(self, body):
        for f in ("email", "password", "display_name"):
            if f in body and not isinstance(body[f], str):
                raise bad("%s must be a string" % f)
        email = str_field(body, "email")
        password = str_field(body, "password")
        display_name = str_field(body, "display_name")
        if not EMAIL_RE.match(email):
            raise invalid("email must look like local@domain")
        if len(password) < 8:
            raise invalid("password must be at least 8 characters")
        handle = derive_handle(email)

        def conflicts(st):
            if email.lower() in st.by_email:
                raise ApiError(409, "email_taken", "email already registered")
            if not HANDLE_RE.match(handle) or handle in st.by_handle:
                raise ApiError(409, "handle_taken", "derived handle is taken")

        with LOCK:
            conflicts(STATE)
        salt, h = hash_password(password)
        with LOCK:
            st = STATE
            conflicts(st)
            uid = st.new_id("u", st.users)
            st.users[uid] = {"id": uid, "email": email, "display_name": display_name, "handle": handle,
                             "balance": 0, "salt": salt, "hash": h}
            st.by_email[email.lower()] = uid
            st.by_handle[handle] = uid
            token = secrets.token_urlsafe(32)
            st.tokens[token] = uid
        return self.send(201, {"user_id": uid, "display_name": display_name, "token": token})

    def login(self, body):
        for f in ("email", "password"):
            if f in body and not isinstance(body[f], str):
                raise bad("%s must be a string" % f)
        email = str_field(body, "email")
        password = str_field(body, "password")
        with LOCK:
            st = STATE
            uid = st.by_email.get(email.lower())
            u = dict(st.users[uid]) if uid else None
        if u is None or not check_password(password, u["salt"], u["hash"]):
            raise ApiError(401, "unauthenticated", "wrong email or password")
        with LOCK:
            if STATE is not st or uid not in st.users:
                raise ApiError(401, "unauthenticated", "wrong email or password")
            token = secrets.token_urlsafe(32)
            st.tokens[token] = uid
        return self.send(200, {"user_id": uid, "display_name": u["display_name"], "token": token})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True


def main():
    port = int(os.environ.get("PORT") or 8080)
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
