"""Pocketful stage 4: wallet/payments HTTP service with holds, history, statements and corrections, plus its UI.

One process, one in-memory State object, one lock (LOCK). Every read and every
read-modify-write of state happens while LOCK is held, so LOCK is the single
serialization point: balances, idempotency records and request statuses change
together or not at all. Password hashing (slow by design) runs outside LOCK.
Amounts are Python ints (parsed from JSON as Decimal, never float).
Holds: each user carries "held" (sum of open authorization remainders); available = balance - held.
Expiry is evaluated lazily: every locked operation first closes holds whose expires_at <= now.
History: every payment keeps immutable revisions (amount, effective time, recorded time); every user keeps an
opening balance; every hold keeps its lifecycle (creation, captures, close). Historical reads (as_of, known_at,
statements) are recomputed from those records: total = opening + selected payment deltas, held = hold events.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_AMOUNT = 1_000_000_000
MAX_BALANCE = 2 ** 53
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
DIGITS_RE = re.compile(r"^[0-9]+$")
STATUSES = ("pending", "paid", "declined", "cancelled")
AUTH_STATUSES = ("open", "captured", "voided", "expired")
SCRYPT_N = 2 ** 12         # cost for hashes made by this version (stored per user as "n")
LEGACY_SCRYPT_N = 2 ** 14  # cost used by stage-1 exports, which carry no "n"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

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


RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def instant(value, name):
    """Strict RFC 3339 instant with an offset (query or body). Returns (datetime, echo string)."""
    if not isinstance(value, str):
        raise invalid("%s must be an RFC 3339 instant with an offset" % name)
    if re.match(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)? \d{2}:\d{2}$", value):
        value = value[:-6] + "+" + value[-5:]  # an unencoded '+' in a query string arrives as a space
    if not RFC3339_RE.match(value):
        raise invalid("%s must be an RFC 3339 instant with an offset" % name)
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00").replace("t", "T"))
    except ValueError:
        raise invalid("%s is not a valid instant" % name)
    return dt, value


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


def hash_password(password, salt=None, n=SCRYPT_N):
    salt = salt or secrets.token_bytes(16)
    with HASH_SLOTS:
        h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=8, p=1,
                           maxmem=64 * 1024 * 1024, dklen=32)
    return salt.hex(), h.hex()


def check_password(password, salt_hex, hash_hex, n):
    _, h = hash_password(password, bytes.fromhex(salt_hex), n)
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
    def __init__(self, currency="EUR", minor_units=2, ttl=600):
        self.currency = currency
        self.minor_units = minor_units
        self.ttl = ttl             # authorization_ttl_seconds
        self.users = {}            # id -> user dict (internal; "held" is the sum of open holds)
        self.by_email = {}         # lowercased email -> id
        self.by_handle = {}        # handle -> id
        self.tokens = {}           # token -> user id
        self.payments = []         # payment dicts in creation order (public fields + "_ts", "_seq")
        self.payment_ids = {}      # payment id -> payment dict
        self.requests = {}         # request id -> request dict (public fields + "_ts", "_seq")
        self.auths = {}            # authorization id -> authorization dict (+ "_ts", "_seq", "_exp")
        self.open_auths = {}       # authorization id -> dict, only those with status "open"
        self.idem = {}             # "uid\x00path\x00key" -> {"canon": str, "response": str}
        self.operators = set()
        self.used = set()          # every id ever issued or seeded, of any kind (M1)
        self.user_pays = {}        # user id -> payments sent or received (creation order)
        self.user_auths = {}       # user id -> authorizations where the user is the payer
        self.snapshots = {}        # statement snapshot token -> {"uid", "result"}
        self.seq = 0
        self.last_ts = None
        self.op_ts = None          # the one instant of the locked write in progress (taken by begin_op)

    # ids and clock
    def next_seq(self):
        self.seq += 1
        return self.seq

    def new_id(self, prefix):
        while True:
            i = "%s_%d" % (prefix, self.next_seq())
            if i not in self.used:
                self.used.add(i)
                return i

    def begin_op(self):
        """Take the single instant of a locked write: expiry, validation and the recorded time all use it."""
        self.op_ts = self.stamp()
        self.expire(self.op_ts)

    def end_op(self):
        self.op_ts = None

    def now(self):
        return self.op_ts or now_dt()

    def stamp(self):
        if self.op_ts is not None:
            return self.op_ts
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

    def auth_view(self, a):
        v = {k: x for k, x in a.items() if not k.startswith("_")}
        v["payment_ids"] = list(a["payment_ids"])
        v["remaining_amount"] = remaining(a)
        return v

    @staticmethod
    def revision_view(p, r):
        return {"payment_id": p["payment_id"], "revision": r["revision"], "amount": r["amount"],
                "effective_at": r["effective_at"], "recorded_at": r["recorded_at"], "reason": r["reason"],
                "correction_batch_id": r.get("correction_batch_id")}

    # holds
    def expire(self, now=None):
        """Close every open authorization whose expires_at is at or before now (lazy clock expiry)."""
        if not self.open_auths:
            return
        now = now or now_dt()
        for a in [a for a in self.open_auths.values() if a["_exp"] <= now]:
            self.close_auth(a, "expired")

    def close_auth(self, a, status, at=None):
        """Release the uncaptured remainder exactly once and record the final status and close time.

        Expiry closes at expires_at (whenever it is noticed); capture and void close at their event time `at`.
        """
        self.users[a["from_user_id"]]["held"] -= remaining(a)
        a["status"] = status
        if status == "expired":
            a["closed_at"] = a["expires_at"]
        else:
            a["closed_at"] = iso(at)
            a["_close"] = at
        self.open_auths.pop(a["authorization_id"], None)

    def available(self, u):
        return u["balance"] - u["held"]

    # mutations
    def add_payment(self, frm, to, amount, note, visibility, ts, request_id=None, settlement_id=None,
                    authorization_id=None, refund_of=None):
        p = {
            "payment_id": self.new_id("p"),
            "from_user_id": frm["id"], "from_handle": frm["handle"],
            "to_user_id": to["id"], "to_handle": to["handle"],
            "amount": amount, "currency": self.currency, "note": note,
            "visibility": visibility, "request_id": request_id,
            "authorization_id": authorization_id, "refund_of": refund_of,
            "settlement_id": settlement_id, "created_at": iso(ts),
            "_ts": ts, "_seq": self.next_seq(), "_revs": [first_revision(amount, ts, iso(ts))],
        }
        self.index_payment(p)
        return p

    def index_payment(self, p):
        """Index a payment; a refund (refund_of set) is linked to its already indexed target."""
        p.setdefault("refund_of", None)
        p["_refunds"] = []
        if p["refund_of"] is not None:
            self.payment_ids[p["refund_of"]]["_refunds"].append(p)
        self.payments.append(p)
        self.payment_ids[p["payment_id"]] = p
        self.used.add(p["payment_id"])
        self.user_pays.setdefault(p["from_user_id"], []).append(p)
        if p["to_user_id"] != p["from_user_id"]:
            self.user_pays.setdefault(p["to_user_id"], []).append(p)

    def add_request(self, requester, payer, amount, note, ts):
        r = {
            "request_id": self.new_id("rq"),
            "requester_id": requester["id"], "requester_handle": requester["handle"],
            "payer_id": payer["id"], "payer_handle": payer["handle"],
            "amount": amount, "currency": self.currency, "note": note,
            "status": "pending", "payment_id": None, "created_at": iso(ts),
            "_ts": ts, "_seq": self.next_seq(),
        }
        self.requests[r["request_id"]] = r
        return r

    def move(self, frm, to, amount):
        """Transfer between wallets; only the payer's available (unheld) funds may be spent."""
        if self.available(frm) < amount:
            raise ApiError(409, "insufficient_funds", "available balance is below amount")
        if to["balance"] + amount > MAX_BALANCE:
            raise invalid("balance would exceed 2^53")
        frm["balance"] -= amount
        to["balance"] += amount

    def index_auth(self, a):
        self.auths[a["authorization_id"]] = a
        self.used.add(a["authorization_id"])
        self.user_auths.setdefault(a["from_user_id"], []).append(a)
        if a["status"] == "open":
            self.open_auths[a["authorization_id"]] = a
            self.users[a["from_user_id"]]["held"] += remaining(a)

    # export / import
    def to_dict(self):
        return {
            "currency": self.currency, "minor_units": self.minor_units,
            "authorization_ttl_seconds": self.ttl,
            "users": [{k: v for k, v in u.items() if k != "held"} for u in self.users.values()],
            "tokens": dict(self.tokens),
            "payments": [dict(self.payment_view(p), _seq=p["_seq"],
                              revisions=[self.revision_view(p, r) for r in p["_revs"]]) for p in self.payments],
            "requests": [dict(self.request_view(r), _seq=r["_seq"]) for r in self.requests.values()],
            "authorizations": [dict(self.auth_view(a), _seq=a["_seq"], lifecycle=lifecycle_out(a))
                               for a in self.auths.values()],
            "snapshots": [dict(s, result=dict(s["result"])) for s in self.snapshots.values()],
            "idempotency": [dict(k=k, **v) for k, v in self.idem.items()],
            "operators": sorted(self.operators),
            "seq": self.seq,
            "last_ts": iso(self.last_ts) if self.last_ts else None,
        }

    @classmethod
    def from_dict(cls, d):
        """Rebuild an exported state (this service's stage-1 or stage-2 format).

        Any inconsistency raises ApiError 422. Fields absent from a stage-1 export take their
        stage-2 defaults: no authorizations, ttl 600, legacy scrypt cost, authorization_id null.
        """
        def need(cond, what):
            if not cond:
                raise invalid("invalid state: %s" % what)

        def is_int(v):
            return integral(v) is not None

        need(isinstance(d, dict), "state must be an object")
        need(isinstance(d.get("currency"), str) and d["currency"], "currency")
        need(integral(d.get("minor_units")) in (0, 2, 3), "minor_units")
        ttl = d.get("authorization_ttl_seconds", 600)
        need(is_int(ttl) and integral(ttl) > 0, "authorization_ttl_seconds")
        st = cls(d["currency"], integral(d["minor_units"]), integral(ttl))
        need(isinstance(d.get("users"), list), "users")
        for u in d["users"]:
            need(isinstance(u, dict), "user")
            for f in ("id", "email", "display_name", "handle", "salt", "hash"):
                need(isinstance(u.get(f), str), "user." + f)
            need(HANDLE_RE.match(u["handle"]), "user.handle")
            need(is_int(u.get("balance")) and 0 <= integral(u["balance"]) <= MAX_BALANCE, "user.balance")
            need(u["id"] not in st.users and u["handle"] not in st.by_handle
                 and u["email"].lower() not in st.by_email, "duplicate user")
            n = u.get("n", LEGACY_SCRYPT_N)
            need(integral(n) in (2 ** 12, 2 ** 13, 2 ** 14), "user.n")
            try:
                bytes.fromhex(u["salt"]), bytes.fromhex(u["hash"])
            except ValueError:
                need(False, "user password hash")
            nu = {"id": u["id"], "email": u["email"], "display_name": u["display_name"],
                  "handle": u["handle"], "balance": integral(u["balance"]),
                  "salt": u["salt"], "hash": u["hash"], "n": integral(n), "held": 0,
                  "opening": integral(u["opening"]) if is_int(u.get("opening")) else None}
            st.users[nu["id"]] = nu
            st.by_email[nu["email"].lower()] = nu["id"]
            st.by_handle[nu["handle"]] = nu["id"]
            st.used.add(nu["id"])
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
            for f in ("request_id", "settlement_id", "authorization_id"):
                need(p.get(f) is None or isinstance(p[f], str), "payment " + f)
            need(p.get("refund_of") is None or (p["refund_of"] in st.payment_ids
                                                and st.payment_ids[p["refund_of"]]["refund_of"] is None), "refund_of")
            ts = parse_ts(p.get("created_at"))
            np_ = {k: p.get(k) for k in ("payment_id", "from_user_id", "from_handle", "to_user_id", "to_handle",
                                         "amount", "currency", "note", "visibility", "request_id",
                                         "authorization_id", "refund_of", "settlement_id", "created_at")}
            np_.update(amount=integral(p["amount"]), _ts=ts, _seq=integral(p["_seq"]))
            revs = p.get("revisions")
            if revs is None:  # stage-1/stage-2 export: no corrections existed
                np_["_revs"] = [first_revision(np_["amount"], ts, p["created_at"])]
            else:
                need(isinstance(revs, list) and revs, "payment revisions")
                np_["_revs"] = []
                for i, r in enumerate(revs, 1):
                    need(isinstance(r, dict) and integral(r.get("revision")) == i and is_int(r.get("amount"))
                         and isinstance(r.get("reason"), str), "payment revision")
                    need(r.get("correction_batch_id") is None or isinstance(r["correction_batch_id"], str),
                         "correction_batch_id")
                    if r.get("correction_batch_id"):
                        st.used.add(r["correction_batch_id"])
                    np_["_revs"].append({"revision": i, "amount": integral(r["amount"]), "reason": r["reason"],
                                         "correction_batch_id": r.get("correction_batch_id"),
                                         "effective_at": r.get("effective_at"), "recorded_at": r.get("recorded_at"),
                                         "_eff": parse_ts(r.get("effective_at")), "_rec": parse_ts(r.get("recorded_at"))})
            st.index_payment(np_)
            if np_["settlement_id"]:
                st.used.add(np_["settlement_id"])
        need(isinstance(d.get("requests"), list), "requests")
        for r in d["requests"]:
            need(isinstance(r, dict), "request")
            need(isinstance(r.get("request_id"), str) and r["request_id"] not in st.requests, "request id")
            need(r.get("requester_id") in st.users and r.get("payer_id") in st.users, "request users")
            need(is_int(r.get("amount")) and is_int(r.get("_seq")), "request amount")
            need(r.get("status") in STATUSES and isinstance(r.get("note"), str), "request status")
            need(r.get("payment_id") is None or isinstance(r["payment_id"], str), "request payment_id")
            ts = parse_ts(r.get("created_at"))
            nr = {k: r.get(k) for k in ("request_id", "requester_id", "requester_handle", "payer_id",
                                        "payer_handle", "amount", "currency", "note", "status", "payment_id",
                                        "created_at")}
            nr.update(amount=integral(r["amount"]), _ts=ts, _seq=integral(r["_seq"]))
            st.requests[nr["request_id"]] = nr
            st.used.add(nr["request_id"])
        auths = d.get("authorizations", [])
        need(isinstance(auths, list), "authorizations")
        for a in auths:
            need(isinstance(a, dict), "authorization")
            aid = a.get("authorization_id")
            need(isinstance(aid, str) and aid not in st.auths, "authorization id")
            need(a.get("from_user_id") in st.users and a.get("to_user_id") in st.users, "authorization users")
            need(is_int(a.get("amount")) and is_int(a.get("captured_amount")) and is_int(a.get("_seq")),
                 "authorization amounts")
            need(0 <= integral(a["captured_amount"]) <= integral(a["amount"]), "captured_amount")
            need(a.get("status") in AUTH_STATUSES and isinstance(a.get("note"), str)
                 and a.get("visibility") in ("public", "private"), "authorization status")
            need(isinstance(a.get("payment_ids"), list) and all(isinstance(x, str) for x in a["payment_ids"]),
                 "payment_ids")
            na = {k: a.get(k) for k in AUTH_FIELDS}
            na.update(amount=integral(a["amount"]), captured_amount=integral(a["captured_amount"]),
                      payment_ids=list(a["payment_ids"]), _ts=parse_ts(a.get("created_at")),
                      _exp=parse_ts(a.get("expires_at")), _seq=integral(a["_seq"]))
            lifecycle_in(st, na, a.get("lifecycle"), need)
            st.index_auth(na)
        for u in st.users.values():
            need(u["held"] <= u["balance"], "holds exceed balance")
        # opening balances: carried by stage-3 exports; derived for stage-1/2 exports (no corrections existed)
        for u in st.users.values():
            if u["opening"] is None:
                u["opening"] = u["balance"] - sum(signed(p, u["id"], p["_revs"][-1]["amount"])
                                                  for p in st.user_pays.get(u["id"], ()))
        snaps = d.get("snapshots", [])
        need(isinstance(snaps, list), "snapshots")
        for sn in snaps:
            need(isinstance(sn, dict) and isinstance(sn.get("token"), str) and sn.get("uid") in st.users
                 and isinstance(sn.get("result"), dict), "snapshot")
            st.snapshots[sn["token"]] = json_ints(sn)
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
        # the clock never goes back behind any recorded instant (new revisions are strictly later)
        recs = [r["_rec"] for p in st.payments for r in p["_revs"]] + [p["_ts"] for p in st.payments]
        if recs and (st.last_ts is None or max(recs) > st.last_ts):
            st.last_ts = max(recs)
        return st


AUTH_FIELDS = ("authorization_id", "from_user_id", "from_handle", "to_user_id", "to_handle", "amount",
               "captured_amount", "currency", "note", "visibility", "status", "expires_at", "payment_id",
               "payment_ids", "created_at", "closed_at")


def json_ints(v):
    """Turn parsed JSON numbers (Decimal) back into ints throughout an imported structure."""
    if isinstance(v, dict):
        return {k: json_ints(x) for k, x in v.items()}
    if isinstance(v, list):
        return [json_ints(x) for x in v]
    if isinstance(v, Decimal):
        i = integral(v)
        if i is None:
            raise invalid("invalid state: non-integer number")
        return i
    return v


def lifecycle_out(a):
    return {"created": iso(a["_ct"]) if a.get("_ct") is not None else None, "initial": a.get("_init"),
            "captures": [[iso(t), amt] for t, amt in a.get("_caps", [])],
            "close": iso(a["_close"]) if a.get("_close") is not None else None}


def lifecycle_in(st, a, lc, need):
    """Restore a hold's lifecycle; stage-2 exports carry none, so derive it from the capture payments."""
    a["_caps"], a["_close"] = [], None
    if lc is not None:
        need(isinstance(lc, dict), "lifecycle")
        a["_ct"] = parse_ts(lc["created"]) if lc.get("created") is not None else None
        a["_init"] = integral(lc.get("initial")) if lc.get("initial") is not None else a["amount"]
        for t, amt in lc.get("captures") or []:
            a["_caps"].append((parse_ts(t), integral(amt)))
        a["_close"] = parse_ts(lc["close"]) if lc.get("close") is not None else None
    else:
        # Stage-1/2 export: no lifecycle recorded. Rebuild it from created_at, the capture payments and the close:
        #   captured -> the last capture; voided -> the latest known event (void time is unknown); expired ->
        #   expires_at; open -> still open. A hold seeded closed never held anything: its close (seeded capture
        #   payments, or created_at itself) is not after its creation, so hold_events ignores it. A hold marked
        #   expired whose deadline is still in the future cannot have expired by the clock, so it was seeded
        #   expired and holds nothing either. Captured amounts not explained by later capture payments were
        #   captured before creation (seeded) and reduce the initial hold.
        found = []
        for pid in a["payment_ids"]:
            p = st.payment_ids.get(pid)
            need(p is not None or a["status"] != "open", "capture payment")
            if p is not None:
                found.append((p["_ts"], p["amount"]))
        later = [(t, amt) for t, amt in found if t > a["_ts"]]
        a["_ct"] = a["_ts"]
        a["_init"] = a["amount"] - (a["captured_amount"] - sum(amt for _, amt in later))
        a["_caps"] = later
        if a["status"] in ("captured", "voided"):
            a["_close"] = max([a["_ts"]] + [t for t, _ in found])
        elif a["status"] == "expired" and a["_exp"] > now_dt():
            a["_ct"], a["_init"], a["_caps"] = None, 0, []
    if a.get("closed_at") is None and a["status"] != "open":
        a["closed_at"] = a["expires_at"] if a["status"] == "expired" else iso(a["_close"] or a["_ts"])
    if a["status"] == "open":
        a["closed_at"] = None


def remaining(a):
    return a["amount"] - a["captured_amount"] if a["status"] == "open" else 0


def hash_all(passwords):
    """Hash distinct passwords in parallel (scrypt releases the GIL). Returns {password: (salt, hash)}."""
    distinct = list(dict.fromkeys(passwords))
    if not distinct:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(distinct))) as ex:
        return dict(zip(distinct, ex.map(hash_password, distinct)))


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

    def fresh_id(v, what):
        if not v or len(v) > 64 or v in st.used:
            raise invalid("bad or duplicate %s id" % what)
        st.used.add(v)

    currency = typed(fx, "currency", str)
    if not currency:
        raise invalid("currency is empty")
    mu = typed(fx, "minor_units", "int")
    if mu not in (0, 2, 3):
        raise invalid("minor_units must be 0, 2 or 3")
    ttl = typed(fx, "authorization_ttl_seconds", "int", False)
    if ttl is not None and ttl < 1:
        raise invalid("authorization_ttl_seconds must be a positive integer")
    st = State(currency, mu, 600 if ttl is None else ttl)
    users = typed(fx, "users", list)
    pending_pw = []
    for u in users:
        if not isinstance(u, dict):
            raise bad("user must be an object")
        uid = typed(u, "id", str)
        email = typed(u, "email", str)
        pw = typed(u, "password", str)
        dn = typed(u, "display_name", str)
        handle = typed(u, "handle", str)
        bal = typed(u, "balance", "int")
        if not EMAIL_RE.match(email):
            raise invalid("bad email")
        if not HANDLE_RE.match(handle):
            raise invalid("bad handle")
        if bal < 0 or bal > MAX_BALANCE:
            raise invalid("balance out of range")
        if handle in st.by_handle or email.lower() in st.by_email:
            raise invalid("duplicate email or handle")
        fresh_id(uid, "user")
        st.users[uid] = {"id": uid, "email": email, "display_name": dn, "handle": handle,
                         "balance": bal, "held": 0, "opening": bal}
        st.by_email[email.lower()] = uid
        st.by_handle[handle] = uid
        pending_pw.append((uid, pw))

    now = now_dt()  # reset time
    seeded = []  # (dict, explicit ts or None)
    for p in typed(fx, "payments", list, False) or []:
        if not isinstance(p, dict):
            raise bad("payment must be an object")
        pid = typed(p, "id", str)
        frm, to = typed(p, "from_user_id", str), typed(p, "to_user_id", str)
        amt = typed(p, "amount", "int")
        note = typed(p, "note", str, False)
        vis = typed(p, "visibility", str, False) or "public"
        links = {f: p.get(f) for f in ("request_id", "settlement_id", "authorization_id", "refund_of")}
        fresh_id(pid, "payment")
        if frm not in st.users or to not in st.users:
            raise invalid("payment references an unknown user")
        if amt < 0 or amt > MAX_AMOUNT:
            raise invalid("payment amount out of range")
        if vis not in ("public", "private"):
            raise invalid("bad visibility")
        if any(v is not None and not isinstance(v, str) for v in links.values()):
            raise bad("request_id/settlement_id/authorization_id/refund_of must be strings")
        if links["refund_of"] is not None:
            tgt = next((r for r, _ in seeded if "settlement_id" in r and r["payment_id"] == links["refund_of"]), None)
            if tgt is None or tgt["refund_of"] is not None:
                raise invalid("refund_of must name an earlier seeded payment that is not a refund")
        given = None
        if "created_at" in p:
            ts, given = instant(p["created_at"], "created_at")
            if ts > now:
                raise invalid("a seeded payment's created_at is in the future")
        else:
            ts = None
        fu, tu = st.users[frm], st.users[to]
        rec = {"payment_id": pid, "from_user_id": frm, "from_handle": fu["handle"],
               "to_user_id": to, "to_handle": tu["handle"], "amount": amt,
               "currency": currency, "note": note or "", "visibility": vis,
               "request_id": links["request_id"], "authorization_id": links["authorization_id"],
               "refund_of": links["refund_of"], "settlement_id": links["settlement_id"], "_given": given}
        if links["settlement_id"]:
            st.used.add(links["settlement_id"])
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
        fresh_id(rid, "request")
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
    seeded_auths = []
    for a in typed(fx, "authorizations", list, False) or []:
        if not isinstance(a, dict):
            raise bad("authorization must be an object")
        aid = typed(a, "id", str)
        frm, to = typed(a, "from_user_id", str), typed(a, "to_user_id", str)
        amt = typed(a, "amount", "int")
        note = typed(a, "note", str, False)
        vis = typed(a, "visibility", str, False) or "public"
        status = typed(a, "status", str, False) or "open"
        exp_text = typed(a, "expires_at", str)
        exp = parse_ts(exp_text)
        cap = typed(a, "captured_amount", "int", False)
        pay_id = a.get("payment_id")
        pay_ids = a.get("payment_ids")
        fresh_id(aid, "authorization")
        if frm not in st.users or to not in st.users or frm == to:
            raise invalid("authorization references an unknown user or pays itself")
        if amt < 0 or amt > MAX_AMOUNT:  # seeded data, like seeded payments/requests, may carry 0
            raise invalid("authorization amount out of range")
        if vis not in ("public", "private"):
            raise invalid("bad visibility")
        if status not in AUTH_STATUSES:
            raise invalid("bad authorization status")
        if cap is None:
            cap = amt if status == "captured" else 0
        if cap < 0 or cap > amt:
            raise invalid("captured_amount out of range")
        if pay_id is not None and not isinstance(pay_id, str):
            raise bad("payment_id must be a string")
        if pay_ids is None:
            pay_ids = [pay_id] if pay_id else []
        if not isinstance(pay_ids, list) or not all(isinstance(x, str) for x in pay_ids):
            raise bad("payment_ids must be a list of strings")
        if status == "open" and exp <= now:
            status = "expired"
        ts = parse_ts(a["created_at"]) if "created_at" in a else None
        rec = {"authorization_id": aid, "from_user_id": frm, "from_handle": st.users[frm]["handle"],
               "to_user_id": to, "to_handle": st.users[to]["handle"], "amount": amt,
               "captured_amount": cap, "currency": currency, "note": note or "", "visibility": vis,
               "status": status, "expires_at": exp_text, "payment_id": pay_id,
               "payment_ids": list(pay_ids), "_exp": exp}
        seeded.append((rec, ts))
        seeded_auths.append(rec)
    # Seeded records without a timestamp get increasing instants in fixture order.
    for i, (rec, ts) in enumerate(seeded):
        ts = ts or now + datetime.timedelta(microseconds=i)
        rec["_ts"], rec["_seq"] = ts, st.next_seq()
        rec["created_at"] = rec.pop("_given", None) or iso(ts)
        if "settlement_id" in rec:  # only payments carry settlement_id: revision 1 at its created_at; not replayed on balances
            rec["_revs"] = [first_revision(rec["amount"], ts, rec["created_at"])]
            st.index_payment(rec)
            st.users[rec["from_user_id"]]["opening"] += rec["amount"]
            st.users[rec["to_user_id"]]["opening"] -= rec["amount"]
    for rec in seeded_auths:
        # open holds start at their created_at (default: reset time); closed seeded holds carry no lifecycle
        rec["_caps"], rec["_close"] = [], None
        if rec["status"] == "open":
            rec["_ct"], rec["_init"], rec["closed_at"] = rec["_ts"], rec["amount"] - rec["captured_amount"], None
        else:
            rec["_ct"], rec["_init"] = None, 0
            rec["closed_at"] = rec["expires_at"] if rec["status"] == "expired" else iso(now)
        st.index_auth(rec)
    for u in st.users.values():
        if u["held"] > u["balance"]:
            raise invalid("seeded open holds exceed the user's balance")
    st.last_ts = max([now + datetime.timedelta(microseconds=len(seeded))]
                     + [rec["_ts"] for rec, _ in seeded])
    ops = typed(fx, "settlement_operator_ids", list, False) or []
    for o in ops:
        if not isinstance(o, str):
            raise bad("operator ids must be strings")
        if o not in st.users:
            raise invalid("operator id is not a user")
    st.operators = set(ops)
    # Hash last, after every cheap validation passed; distinct passwords in parallel (M2).
    hashed = hash_all(pw for _, pw in pending_pw)
    for uid, pw in pending_pw:
        st.users[uid]["salt"], st.users[uid]["hash"] = hashed[pw]
        st.users[uid]["n"] = SCRYPT_N
    return st


STATE = State()


def newest_first(items):
    return sorted(items, key=lambda x: (x["_ts"], x["_seq"]), reverse=True)


# ---------------------------------------------------------------- handlers
# Each handler runs with LOCK held unless noted, and returns (status, body).

# ---------------------------------------------------------------- history (stage 3)

def first_revision(amount, ts, ts_text):
    return {"revision": 1, "amount": amount, "effective_at": ts_text, "recorded_at": ts_text, "reason": "",
            "_eff": ts, "_rec": ts}


def selected(p, known):
    """The latest revision recorded at or before `known` (None = everything recorded so far)."""
    revs = p["_revs"]
    if known is None:
        return revs[-1]
    for r in reversed(revs):
        if r["_rec"] <= known:
            return r
    return None


def signed(p, uid, amount):
    return -amount if p["from_user_id"] == uid else amount


def payment_events(st, uid, known, override=None):
    """(effective time, delta, payment, revision) for the user's payments under `known`.

    `override` = {payment_id: revision} substitutes candidate latest revisions (correction pre-check).
    """
    out = []
    for p in st.user_pays.get(uid, ()):
        r = override.get(p["payment_id"]) if override else None
        r = r or selected(p, known)
        if r is not None:
            out.append((r["_eff"], signed(p, uid, r["amount"]), p, r))
    return out


def hold_events(st, uid, known):
    """(time, change in held) for the user's holds as known at `known` (None = all known).

    Held is never negative: a hold whose close (or deadline) is not after its creation contributes nothing.
    """
    out = []
    for a in st.user_auths.get(uid, ()):
        ct = a.get("_ct")
        if ct is None or (known is not None and ct > known):
            continue  # no lifecycle (seeded/imported closed hold), or creation not yet known
        close = a.get("_close")
        if close is None or (known is not None and close > known):
            close = a["_exp"]  # as far as this view knows, the hold runs to its deadline
        if close <= ct:
            continue
        out.append((ct, a["_init"]))
        captured = 0
        for t, amt in a["_caps"]:
            if (known is None or t <= known) and t <= close:
                out.append((t, -amt))
                captured += amt
        if a["_init"] - captured > 0:
            out.append((close, -(a["_init"] - captured)))
    return out


def total_at(st, user, as_of, known):
    return user["opening"] + sum(d for t, d, _, _ in payment_events(st, user["id"], known) if t <= as_of)


def held_at(st, user, as_of, known):
    return sum(d for t, d in hold_events(st, user["id"], known) if t <= as_of)


def boundary_ok(st, user, override):
    """True when total and available stay >= 0 after every effective/event boundary (latest revisions)."""
    events = [(t, d, 0) for t, d, _, _ in payment_events(st, user["id"], None, override)]
    events += [(t, 0, d) for t, d in hold_events(st, user["id"], None)]
    events.sort(key=lambda e: e[0])
    total, held, i = user["opening"], 0, 0
    while i < len(events):
        t = events[i][0]
        while i < len(events) and events[i][0] == t:  # combine every movement at one instant
            total += events[i][1]
            held += events[i][2]
            i += 1
        if total < 0 or total - held < 0:
            return False
    return True


def read_now(st, q):
    """Request start, never before the last recorded instant (the clock is forced monotonic)."""
    now = q["_now"]
    if st.last_ts is not None and st.last_ts >= now:
        now = st.last_ts + datetime.timedelta(microseconds=1)
    return now


def temporal(q, name):
    return instant(q[name], name) if name in q else (None, None)


def h_me(st, user, q, body):
    out = {"user_id": user["id"], "display_name": user["display_name"], "handle": user["handle"],
           "balance": user["balance"], "total": user["balance"], "available": st.available(user),
           "held": user["held"], "currency": st.currency, "minor_units": st.minor_units}
    as_of, as_of_text = temporal(q, "as_of")
    known, known_text = temporal(q, "known_at")
    if as_of is None and known is None:
        return 200, out
    at = as_of if as_of is not None else read_now(st, q)
    total = total_at(st, user, at, known)
    held = held_at(st, user, at, known)
    out.update(balance=total, total=total, held=held, available=total - held)
    if as_of_text is not None:
        out["as_of"] = as_of_text
    if known_text is not None:
        out["known_at"] = known_text
    return 200, out


def h_statement(st, user, q, body):
    limit, offset = page(q)
    if "snapshot" in q:
        for f in ("from", "to", "known_at"):
            if f in q:
                raise invalid("snapshot cannot be combined with %s" % f)
        snap = st.snapshots.get(q["snapshot"])
        if snap is None or snap["uid"] != user["id"]:
            raise ApiError(404, "not_found", "no such statement snapshot")
        result = snap["result"]
    else:
        frm, frm_text = temporal(q, "from")
        to, to_text = temporal(q, "to")
        known, known_text = temporal(q, "known_at")
        if to is None:
            to = read_now(st, q)
        if frm is not None and frm > to:
            raise invalid("from must not be after to")
        events = payment_events(st, user["id"], known)
        events.sort(key=lambda e: (e[0], e[2]["payment_id"]))
        opening = user["opening"] + sum(d for t, d, _, _ in events if frm is not None and t < frm)
        running, entries = opening, []
        for t, d, p, r in events:
            if (frm is None or t >= frm) and t < to:
                running += d
                entries.append({"payment": dict(st.payment_view(p), amount=r["amount"]), "delta": d,
                                "balance_after": running, "revision": r["revision"],
                                "effective_at": r["effective_at"], "recorded_at": r["recorded_at"]})
        result = {"opening_balance": opening, "closing_balance": running, "entries": entries,
                  "from": frm_text, "to": to_text if to_text is not None else iso(to), "known_at": known_text}
        token = secrets.token_urlsafe(18)
        st.snapshots[token] = {"uid": user["id"], "result": result, "token": token}
        result["snapshot"] = token
    entries = result["entries"]
    out = {"opening_balance": result["opening_balance"], "entries": entries[offset:offset + limit],
           "closing_balance": result["closing_balance"], "has_more": len(entries) > offset + limit,
           "snapshot": result["snapshot"], "from": result["from"], "to": result["to"]}
    if result["known_at"] is not None:
        out["known_at"] = result["known_at"]
    return 200, out


def find_own_payment(st, user, pid):
    p = st.payment_ids.get(pid)
    if p is None or user["id"] not in (p["from_user_id"], p["to_user_id"]):
        return None, p
    return p, p


def h_revisions(st, user, q, body, pid):
    p, _ = find_own_payment(st, user, pid)
    if p is None:
        raise ApiError(404, "not_found", "no such payment")
    return 200, {"revisions": [st.revision_view(p, r) for r in p["_revs"]]}


def refunded(p):
    return sum(r["amount"] for r in p["_refunds"])


def correction_item(st, p, body, members_ok):
    """Validate one correction of payment p (single or batch item). Returns (current revision, amount, reason,
    effective datetime, effective text). Order: immutable -> fields -> stale_revision -> refund floor."""
    if p.get("authorization_id") or p.get("refund_of") or (p.get("settlement_id") and not members_ok):
        raise ApiError(422, "linked_payment_immutable", "captures, refunds and (singly) settlement members "
                                                        "cannot be corrected")
    for f in ("expected_revision", "amount", "effective_at", "reason"):
        if f not in body:
            raise invalid("%s is required" % f)
    expected = integral(body["expected_revision"])
    if expected is None or expected < 1:
        raise invalid("expected_revision must be a positive integer")
    amount = integral(body["amount"])
    if amount is None or amount < 0 or amount > MAX_AMOUNT:
        raise invalid("amount must be an integer from 0 to %d" % MAX_AMOUNT)
    reason = body["reason"]
    if not isinstance(reason, str) or not 1 <= len(reason) <= 200:
        raise invalid("reason must be a string of 1 to 200 characters")
    eff, eff_text = instant(body["effective_at"], "effective_at")
    if eff > st.now():
        raise invalid("effective_at must not be later than now")
    current = p["_revs"][-1]
    if expected != current["revision"]:
        raise ApiError(409, "stale_revision", "the payment is at revision %d" % current["revision"])
    if amount < refunded(p):
        raise ApiError(422, "refund_exceeds_payment", "the payment has already been refunded beyond that amount")
    return current, amount, reason, eff, eff_text


def h_correction(st, user, q, body, pid):
    p = st.payment_ids.get(pid)
    if p is None:
        raise ApiError(404, "not_found", "no such payment")
    if p["from_user_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the original sender may correct a payment")
    current, amount, reason, eff, eff_text = correction_item(st, p, body, members_ok=False)
    diff = amount - current["amount"]
    sender, receiver = st.users[p["from_user_id"]], st.users[p["to_user_id"]]
    debtor, creditor = (sender, receiver) if diff > 0 else (receiver, sender)
    if st.available(debtor) < abs(diff):
        raise ApiError(409, "insufficient_funds", "available balance cannot cover the correction")
    if creditor["balance"] + abs(diff) > MAX_BALANCE:
        raise invalid("balance would exceed 2^53")
    rec = st.stamp()
    cand = {"revision": current["revision"] + 1, "amount": amount, "effective_at": eff_text,
            "recorded_at": iso(rec), "reason": reason, "_eff": eff, "_rec": rec, "correction_batch_id": None}
    for u in (sender, receiver):
        if not boundary_ok(st, u, {p["payment_id"]: cand}):
            raise ApiError(409, "historical_overdraft", "the correction would overdraw a wallet in the past")
    debtor["balance"] -= abs(diff)
    creditor["balance"] += abs(diff)
    p["_revs"].append(cand)
    return 201, st.revision_view(p, cand)


def h_correction_batch(st, user, q, body):
    items = body.get("corrections")
    if not isinstance(items, list) or not 1 <= len(items) <= 32 or not all(isinstance(i, dict) for i in items):
        raise invalid("corrections must be a list of 1 to 32 objects")
    pids = [i.get("payment_id") for i in items]
    if not all(isinstance(x, str) for x in pids) or len(set(pids)) != len(pids):
        raise invalid("every correction needs a distinct payment_id")
    plan = []  # (payment, current revision, amount, reason, eff, eff_text) in input order
    for it in items:  # item errors in input order
        p = st.payment_ids.get(it["payment_id"])
        if p is None:
            raise ApiError(404, "not_found", "no such payment: %s" % it["payment_id"])
        plan.append((p,) + correction_item(st, p, it, members_ok=True))
    chosen = set(pids)
    instants = {}
    for p, _, _, _, eff, _ in plan:
        if p["settlement_id"]:
            instants.setdefault(p["settlement_id"], set()).add(eff)
    for sid in instants:
        if any(m["payment_id"] not in chosen for m in st.payments if m["settlement_id"] == sid):
            raise ApiError(422, "incomplete_settlement", "every member of settlement %s must be corrected" % sid)
    for sid, effs in instants.items():
        if len(effs) != 1:
            raise invalid("members of settlement %s need identical effective instants" % sid)
    delta = {}
    for p, current, amount, _, _, _ in plan:
        diff = amount - current["amount"]
        delta[p["from_user_id"]] = delta.get(p["from_user_id"], 0) - diff
        delta[p["to_user_id"]] = delta.get(p["to_user_id"], 0) + diff
    for uid, d in delta.items():
        u = st.users[uid]
        if d < 0 and st.available(u) + d < 0:
            raise ApiError(409, "insufficient_funds", "available balance cannot cover the corrections")
        if u["balance"] + d > MAX_BALANCE:
            raise invalid("balance would exceed 2^53")
    rec = st.stamp()
    cands = {p["payment_id"]: {"revision": current["revision"] + 1, "amount": amount, "effective_at": eff_text,
                               "recorded_at": iso(rec), "reason": reason, "_eff": eff, "_rec": rec}
             for p, current, amount, reason, eff, eff_text in plan}
    for uid in delta:
        if not boundary_ok(st, st.users[uid], cands):
            raise ApiError(409, "historical_overdraft", "the corrections would overdraw a wallet in the past")
    bid = st.new_id("cb")
    for uid, d in delta.items():
        st.users[uid]["balance"] += d
    for p, *_ in plan:
        cands[p["payment_id"]]["correction_batch_id"] = bid
        p["_revs"].append(cands[p["payment_id"]])
    return 201, {"correction_batch_id": bid, "recorded_at": iso(rec),
                 "revisions": [st.revision_view(p, cands[p["payment_id"]]) for p, *_ in plan]}


def h_refund(st, user, q, body, pid):
    p = st.payment_ids.get(pid)
    if p is None:
        raise ApiError(404, "not_found", "no such payment")
    if p["to_user_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the original receiver may refund a payment")
    if p["refund_of"] is not None:
        raise ApiError(422, "invalid_refund_target", "a refund cannot be refunded")
    amount = amount_field(body)
    if refunded(p) + amount > p["_revs"][-1]["amount"]:
        raise ApiError(422, "refund_exceeds_payment", "refunds would exceed the payment's current amount")
    sender = st.users[p["from_user_id"]]
    st.move(user, sender, amount)
    r = st.add_payment(user, sender, amount, p["note"], p["visibility"], st.stamp(), refund_of=pid)
    return 201, st.payment_view(r)


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
    return 201, {"split_id": st.new_id("sp"), "amount": amount, "currency": st.currency,
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
        if b - st.users[uid]["held"] < 0:  # net debits may only use available funds
            raise ApiError(409, "insufficient_funds", "settlement is not affordable")
        if b > MAX_BALANCE:
            raise invalid("balance would exceed 2^53")
    for uid, d in delta.items():
        st.users[uid]["balance"] += d
    ts = st.stamp()
    sid = st.new_id("st")
    pays = [st.add_payment(f, t, a, n, v, ts, settlement_id=sid) for f, t, a, n, v in plan]
    return 201, {"settlement_id": sid, "committed_at": iso(ts),
                 "payments": [st.payment_view(p) for p in pays]}


def find_auth(st, aid):
    a = st.auths.get(aid)
    if a is None:
        raise ApiError(404, "not_found", "no such authorization")
    return a


def h_authorize(st, user, q, body):
    if "to_handle" in body and not isinstance(body["to_handle"], str):
        raise bad("to_handle must be a string")
    amount = amount_field(body)
    note = note_field(body)
    vis = visibility_field(body)
    to_handle = str_field(body, "to_handle")
    if to_handle == user["handle"]:
        raise ApiError(422, "self_payment", "cannot authorize a payment to yourself")
    to_id = st.by_handle.get(to_handle)
    if to_id is None:
        raise ApiError(404, "not_found", "no user has that handle")
    if st.available(user) < amount:
        raise ApiError(409, "insufficient_funds", "available balance is below amount")
    to = st.users[to_id]
    ts = st.stamp()
    exp = ts + datetime.timedelta(seconds=st.ttl)
    a = {"authorization_id": st.new_id("a"), "from_user_id": user["id"], "from_handle": user["handle"],
         "to_user_id": to["id"], "to_handle": to["handle"], "amount": amount, "captured_amount": 0,
         "currency": st.currency, "note": note, "visibility": vis, "status": "open",
         "expires_at": iso(exp), "payment_id": None, "payment_ids": [], "created_at": iso(ts),
         "closed_at": None, "_ts": ts, "_exp": exp, "_seq": st.next_seq(),
         "_ct": ts, "_init": amount, "_caps": [], "_close": None}
    st.index_auth(a)
    return 201, st.auth_view(a)


def h_capture(st, user, q, body, aid):
    amount = None
    if "amount" in body:
        amount = integral(body["amount"])
        if amount is None or amount < 1 or amount > MAX_AMOUNT:
            raise invalid("amount must be an integer from 1 to %d" % MAX_AMOUNT)
    final = body.get("final", True)
    if not isinstance(final, bool):
        raise bad("final must be a boolean")
    a = find_auth(st, aid)
    if a["to_user_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the receiver may capture")
    if a["status"] == "expired":
        raise ApiError(409, "authorization_expired", "the authorization has expired")
    if a["status"] != "open":
        raise ApiError(409, "authorization_not_open", "authorization is %s" % a["status"])
    left = remaining(a)
    if amount is None:
        amount = left
    if amount > left:
        raise ApiError(422, "capture_exceeds_authorization", "amount exceeds the uncaptured remainder")
    payer = st.users[a["from_user_id"]]
    if user["balance"] + amount > MAX_BALANCE:
        raise invalid("balance would exceed 2^53")
    # the reservation funds the capture: total and held fall together, available is unchanged
    payer["balance"] -= amount
    payer["held"] -= amount
    user["balance"] += amount
    ts = st.stamp()
    p = st.add_payment(payer, user, amount, a["note"], a["visibility"], ts, authorization_id=aid)
    a["captured_amount"] += amount
    a["payment_id"] = p["payment_id"]
    a["payment_ids"].append(p["payment_id"])
    if a.get("_ct") is not None:
        a["_caps"].append((ts, amount))
    if final or remaining(a) == 0:
        st.close_auth(a, "captured", ts)
    return 201, st.payment_view(p)


def h_void(st, user, q, body, aid):
    a = find_auth(st, aid)
    if a["from_user_id"] != user["id"]:
        raise ApiError(403, "forbidden", "only the payer may void")
    if a["status"] == "open":
        st.close_auth(a, "voided", st.stamp())
    elif a["status"] != "voided":
        raise ApiError(409, "authorization_not_open", "authorization is %s" % a["status"])
    return 200, st.auth_view(a)


def h_list_auths(st, user, q, body):
    limit, offset = page(q)
    direction = q.get("direction")
    if direction is not None and direction not in ("incoming", "outgoing"):
        raise invalid("direction must be incoming or outgoing")
    status = q.get("status")
    if status is not None and status not in AUTH_STATUSES:
        raise invalid("unknown status")
    uid = user["id"]
    items = [a for a in st.auths.values()
             if ((direction in (None, "outgoing") and a["from_user_id"] == uid)
                 or (direction in (None, "incoming") and a["to_user_id"] == uid))
             and (status is None or a["status"] == status)]
    items = newest_first(items)
    return 200, {"authorizations": [st.auth_view(a) for a in items[offset:offset + limit]],
                 "has_more": len(items) > offset + limit}


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
    ("POST", re.compile(r"^/authorizations$"), h_authorize, True),
    ("GET", re.compile(r"^/authorizations$"), h_list_auths, False),
    ("POST", re.compile(r"^/authorizations/([^/]+)/capture$"), h_capture, True),
    ("POST", re.compile(r"^/authorizations/([^/]+)/void$"), h_void, False),
    ("GET", re.compile(r"^/statement$"), h_statement, False),
    ("POST", re.compile(r"^/payments/([^/]+)/corrections$"), h_correction, True),
    ("GET", re.compile(r"^/payments/([^/]+)/revisions$"), h_revisions, False),
    ("POST", re.compile(r"^/payments/([^/]+)/refunds$"), h_refund, True),
    ("POST", re.compile(r"^/correction-batches$"), h_correction_batch, True),
]
UI_ROUTES = {"/", "/requests", "/split", "/signup", "/login", "/authorizations"}
UI_ALWAYS = {"/", "/split", "/signup", "/login"}  # no API shares these paths
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

    def send_static(self, name):
        data, ctype = STATIC[name]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
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
        except Exception as e:  # never leak a traceback to the client; log it and keep the connection sane
            traceback.print_exc()
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
        q["_now"] = now_dt()  # the instant the request began (default `to` and as_of)
        raw = self.read_body()
        if raw is None:  # cut-off body: never act on a partial request
            self.close_connection = True
            return
        if path in PUBLIC:
            return self.public(method, path, raw)
        if method == "GET" and path in UI_ROUTES and (
                path in UI_ALWAYS or "text/html" in (self.headers.get("Accept") or "").lower()):
            return self.send_static("index.html")
        if method == "GET" and path.startswith("/static/") and path[8:] in STATIC:
            return self.send_static(path[8:])
        allowed = [m for m, r, _, _ in ROUTES if r.match(path)]
        if not allowed:
            raise ApiError(404, "not_found", "no such endpoint")
        if method not in allowed:
            raise ApiError(405, "method_not_allowed", "method not allowed")
        m, rx, fn, idem = next(r for r in ROUTES if r[0] == method and r[1].match(path))
        args = rx.match(path).groups()
        token = self.bearer()
        if fn in (h_settlement, h_correction_batch):
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
        body = parse_json(raw, empty_ok=fn in (h_pay, h_capture)) if idem else {}
        if not isinstance(body, dict):
            raise bad("body must be a JSON object")
        with LOCK:
            st = STATE
            uid = st.tokens.get(token)
            if uid is None:
                raise ApiError(401, "unauthenticated", "missing or unknown bearer token")
            user = st.users[uid]
            if method == "GET":
                st.expire()
                status, out = fn(st, user, q, body, *args)
                return self.send(status, out)
            st.begin_op()
            try:
                return self.write_op(st, user, uid, path, key, idem, fn, q, body, args)
            finally:
                st.end_op()

    def write_op(self, st, user, uid, path, key, idem, fn, q, body, args):
        """A state-changing request, under LOCK, with st.op_ts as its one instant."""
        if not idem:
            status, out = fn(st, user, q, body, *args)
            return self.send(status, out)
        if fn in (h_settlement, h_correction_batch) and uid not in st.operators:
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
                STATE.expire()
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
            uid = st.new_id("u")
            st.users[uid] = {"id": uid, "email": email, "display_name": display_name, "handle": handle,
                             "balance": 0, "held": 0, "opening": 0, "salt": salt, "hash": h, "n": SCRYPT_N}
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
        if u is None or not check_password(password, u["salt"], u["hash"], u["n"]):
            raise ApiError(401, "unauthenticated", "wrong email or password")
        with LOCK:
            if STATE is not st or uid not in st.users:
                raise ApiError(401, "unauthenticated", "wrong email or password")
            token = secrets.token_urlsafe(32)
            st.tokens[token] = uid
        return self.send(200, {"user_id": uid, "display_name": u["display_name"], "token": token})


def load_static():
    types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
    out = {}
    if os.path.isdir(STATIC_DIR):
        for name in os.listdir(STATIC_DIR):
            ext = os.path.splitext(name)[1]
            if ext in types:
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    out[name] = (f.read(), types[ext])
    return out


STATIC = load_static()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True


def main():
    port = int(os.environ.get("PORT") or 8080)
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
