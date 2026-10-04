/* Pocketful browser client. Vanilla JS, no dependencies, no network beyond this origin.
 *
 * Money is handled as integer minor units (BigInt while parsing, Number only for values the
 * API returned). Writes reuse one Idempotency-Key while the submitted body is unchanged, so a
 * double submit or a retry after a lost response is a replay, never a second payment.
 */
"use strict";

const S = {
  token: localStorage.getItem("pf_token"),
  me: null,             // last applied GET /me
  forms: {},            // form drafts by form name: survive re-renders and client-side navigation
  retry: {},            // write identity by form/action name: {body, key}
  gens: {},             // latest-wins generation counters per data source
  applied: {},
  captureDraft: {},
};

/* ------------------------------------------------------------------ helpers */

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "testid") el.setAttribute("data-testid", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** replaceChildren that skips null/false placeholders (replaceChildren would print "null"). */
function put(el, ...kids) { el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false)); }

function newKey() {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

function mu() { return S.me ? S.me.minor_units : 2; }
function ccy() { return S.me ? S.me.currency : ""; }

/** Minor units -> "100.00" (no currency). */
function plain(minor, units = mu()) {
  const neg = minor < 0;
  let s = String(Math.abs(minor));
  if (units === 0) return (neg ? "-" : "") + s;
  s = s.padStart(units + 1, "0");
  return (neg ? "-" : "") + s.slice(0, -units) + "." + s.slice(-units);
}

/** Minor units -> "100.00 EUR"; minor_units 0 has no decimal point. */
function money(minor) { return plain(minor) + " " + ccy(); }

/** "15", "15.5", "15.00" -> minor units (Number), or null when not a valid amount for this currency. */
function parseAmount(text, units = mu()) {
  const t = String(text).trim();
  const m = /^(\d+)(?:\.(\d+))?$/.exec(t);
  if (!m) return null;
  const frac = m[2] || "";
  if (frac.length > units) return null;
  const minor = BigInt(m[1]) * 10n ** BigInt(units) + BigInt((frac + "0".repeat(units)).slice(0, units) || "0");
  if (minor > 1000000000000n) return null;
  return Number(minor);
}

function cleanHandle(s) { return String(s).trim().replace(/^@/, ""); }

function when(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(d);
}

function initial(handle) { return (handle || "?").slice(0, 1); }

const MESSAGES = {
  insufficient_funds: "Not enough available funds. Money on hold can't be spent.",
  self_payment: "You can't send money to yourself.",
  self_request: "You can't request money from yourself.",
  not_found: "We couldn't find that person or item. Check the handle and try again.",
  request_not_pending: "This request is no longer pending — it was already paid, declined or cancelled.",
  forbidden: "You're not allowed to do that.",
  authorization_not_open: "This hold is no longer open.",
  authorization_expired: "This hold has expired; its funds were released.",
  capture_exceeds_authorization: "That's more than the amount still held.",
  idempotency_key_reuse: "This submission conflicts with an earlier one. Change a field and try again.",
  email_taken: "That email is already registered. Try logging in.",
  handle_taken: "The handle made from that email is taken. Try another email.",
  unauthenticated: "Email or password is incorrect.",
};

function explain(res) {
  const err = res && res.data && res.data.error;
  if (!err) return "Something went wrong. Please try again.";
  if (err.code === "validation_failed") return "Please check the details: " + err.message + ".";
  return MESSAGES[err.code] || err.message || "The request was refused.";
}

/** One API call. Returns {ok, status, data} or {lost: true} when the outcome is unknown. */
async function api(method, path, body, key) {
  const headers = { Accept: "application/json" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  if (key) headers["Idempotency-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body, cache: "no-store" });
  } catch (e) {
    return { lost: true };
  }
  let data = null;
  try { data = await res.json(); } catch (e) { return { lost: true, status: res.status }; }
  if (res.status >= 500) return { lost: true, status: res.status, data };
  if (res.status === 401 && S.token && path !== "/auth/login") { signOut(); return { status: 401, data }; }
  return { ok: res.status >= 200 && res.status < 300, status: res.status, data };
}

/** Body + key for a write: the same body keeps the same key (a replay); any change mints a new key. */
function identity(name, bodyObj) {
  const body = JSON.stringify(bodyObj);
  const cur = S.retry[name];
  if (cur && cur.body === body) return cur;
  return (S.retry[name] = { body, key: newKey() });
}

/** Latest-wins: start() returns a token; fresh(token) is false once a newer read has been applied. */
function start(name) { return (S.gens[name] = (S.gens[name] || 0) + 1); }
function fresh(name, gen) {
  if (gen < (S.applied[name] || 0)) return false;
  S.applied[name] = gen;
  return true;
}

function setMsg(slot, kind, testid, text) {
  if (!slot) return;
  slot.replaceChildren();
  if (text) slot.append(h("div", { class: "notice notice-" + kind, testid, role: kind === "error" ? "alert" : "status" }, text));
}

/* ------------------------------------------------------------------ routing */

const ROUTES = {
  "/": pageWallet, "/requests": pageRequests, "/split": pageSplit,
  "/authorizations": pageAuthorizations, "/login": pageLogin, "/signup": pageSignup,
};
const GATED = new Set(["/", "/requests", "/split", "/authorizations"]);

function go(path, replace) {
  if (replace) history.replaceState(null, "", path); else history.pushState(null, "", path);
  render();
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault();
  go(a.getAttribute("href"));
});
window.addEventListener("popstate", render);

function signOut() {
  S.token = null; S.me = null; S.retry = {}; S.forms = {};
  localStorage.removeItem("pf_token");
  go("/login", true);
}

async function loadMe() {
  const gen = start("me");
  const res = await api("GET", "/me");
  if (res.ok && fresh("me", gen)) S.me = res.data;
  return res;
}

async function render() {
  const path = location.pathname;
  const page = ROUTES[path] || pageNotFound;
  const main = document.getElementById("main");
  if (GATED.has(path) && !S.token) return go("/login", true);
  if (S.token && !S.me) {
    main.replaceChildren(h("p", { class: "loading-page", role: "status" }, "Loading your wallet…"));
    const res = await loadMe();
    if (!res.ok && !S.token) return;  // signed out by a 401
    if (!res.ok) {
      main.replaceChildren(h("div", { class: "card narrow" },
        h("div", { class: "notice notice-error", role: "alert" }, "We couldn't reach Pocketful. Check your connection."),
        h("p", {}, " "), h("button", { class: "btn btn-primary", onclick: render }, "Try again")));
      return;
    }
  }
  renderTopbar(path);
  main.replaceChildren();
  page(main);
}

function renderTopbar(path) {
  const bar = document.getElementById("topbar");
  const link = (href, label) => h("a", { href, "data-link": true, "aria-current": path === href ? "page" : null }, label);
  const inner = h("div", { class: "topbar-inner" },
    h("a", { class: "brand", href: S.token ? "/" : "/login", "data-link": true },
      h("span", { class: "brand-mark", "aria-hidden": "true" }, "P"), "Pocketful"));
  if (S.token && S.me) {
    inner.append(
      h("nav", { class: "nav", "aria-label": "Main" },
        link("/", "Wallet"), link("/requests", "Requests"), link("/split", "Split a bill"), link("/authorizations", "Holds")),
      h("div", { class: "user" },
        h("div", { class: "user-chip" },
          h("span", { class: "user-name", testid: "current-user" }, S.me.display_name),
          h("span", { class: "handle", testid: "current-handle" }, S.me.handle)),
        h("button", { class: "btn btn-ghost", type: "button", testid: "logout-button", onclick: signOut }, "Log out")));
  } else {
    inner.append(h("nav", { class: "nav", "aria-label": "Account" }, link("/login", "Log in"), link("/signup", "Sign up")));
  }
  bar.replaceChildren(inner);
}

function pageNotFound(main) {
  main.append(h("div", { class: "card narrow" }, h("h1", {}, "Page not found"),
    h("p", {}, h("a", { href: "/", "data-link": true }, "Back to your wallet"))));
}

/* ------------------------------------------------------------------ shared pieces */

function field(label, input, hint) {
  const target = input.matches("input,select") ? input : input.querySelector("input,select");
  const id = target.id || (target.id = "f-" + Math.random().toString(36).slice(2, 9));
  return h("div", { class: "field" }, h("label", { for: id }, label), input, hint ? h("span", { class: "hint" }, hint) : null);
}

function draftInput(form, name, attrs) {
  const drafts = (S.forms[form] = S.forms[form] || {});
  const el = h(attrs.tag || "input", Object.assign({ name, autocomplete: "off" }, attrs, { tag: null }));
  if (drafts[name] !== undefined) el.value = drafts[name];
  el.addEventListener("input", () => { drafts[name] = el.value; });
  return el;
}

function amountInput(form, name, testid) {
  const input = draftInput(form, name, { testid, inputmode: "decimal", placeholder: mu() ? "0." + "0".repeat(mu()) : "0" });
  return h("div", { class: "amount-input" }, input, h("span", { class: "ccy", "aria-hidden": "true" }, ccy()));
}

function visibilitySelect(form, testid) {
  const sel = h("select", { name: "visibility", testid },
    h("option", { value: "public" }, "Public — shown in everyone's feed"),
    h("option", { value: "private" }, "Private — only you and them"));
  const drafts = (S.forms[form] = S.forms[form] || {});
  sel.value = drafts.visibility || "public";
  sel.addEventListener("change", () => { drafts.visibility = sel.value; });
  return sel;
}

/** Wallet summary with latest-wins refresh. Returns {el, refresh}. */
function walletCard(withRefresh) {
  const body = h("div", {}, h("p", { class: "headline" }, h("span", { class: "skeleton" }), h("span", { class: "sr-only" }, "Loading balance")));
  const meta = h("p", { class: "meta", "aria-live": "polite" });
  const btn = withRefresh ? h("button", { class: "btn btn-ghost", type: "button", testid: "wallet-refresh" }, "Refresh") : null;
  const el = h("section", { class: "card wallet", "aria-label": "Wallet" },
    h("div", { class: "card-head" }, h("h2", {}, "Available to spend"), btn), body, meta);
  const paint = () => {
    const m = S.me;
    if (!m) return;
    body.replaceChildren(
      h("p", { class: "headline", testid: "wallet-available", "data-amount": String(m.available) }, money(m.available)),
      h("dl", { class: "wallet-sub" },
        h("div", {}, h("dt", {}, "Total balance"), h("dd", { testid: "wallet-balance", "data-amount": String(m.balance) }, money(m.balance))),
        m.held > 0 ? h("div", {}, h("dt", {}, "On hold"), h("dd", { class: "held-value", testid: "wallet-held", "data-amount": String(m.held) }, money(m.held))) : null));
  };
  paint();
  return { el, paint, meta, btn };
}

/* ------------------------------------------------------------------ wallet page: / */

function pageWallet(main) {
  const wallet = walletCard(true);
  const feed = h("div", {}, h("div", { class: "skeleton-row" }), h("div", { class: "skeleton-row" }), h("span", { class: "sr-only" }, "Loading activity"));
  const feedMsg = h("div", { "aria-live": "polite" });

  async function refresh() {
    const gen = start("wallet");
    wallet.meta.textContent = "Updating…";
    const [me, act] = await Promise.all([api("GET", "/me"), api("GET", "/activity?limit=50")]);
    if (!fresh("wallet", gen)) return;      // a later refresh already painted
    if (me.ok && act.ok) {
      S.me = me.data;
      wallet.paint();
      paintFeed(feed, act.data.payments);
      wallet.meta.textContent = "Updated " + new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(new Date());
      setMsg(feedMsg, "", null, "");
    } else {
      wallet.meta.textContent = "";
      setMsg(feedMsg, "error", null, "Couldn't refresh your wallet. Showing the last known state.");
    }
  }
  wallet.btn.addEventListener("click", refresh);

  const pay = payForm(refresh);
  const req = requestForm();
  const auth = authorizeForm(refresh);
  main.append(
    h("div", { class: "stack" },
      wallet.el,
      h("div", { class: "grid-2" },
        h("div", { class: "stack" }, pay, req, auth),
        h("section", { class: "card", "aria-labelledby": "act-h" },
          h("div", { class: "card-head" }, h("h2", { id: "act-h" }, "Activity"), h("p", {}, "Newest first")),
          feedMsg, feed))));
  refresh();
}

function paintFeed(feed, payments) {
  if (!payments.length) {
    feed.replaceChildren(h("div", { class: "empty", testid: "empty-activity" },
      h("strong", {}, "No activity yet"), "Payments you send or receive, and public payments, will show up here."));
    return;
  }
  const me = S.me.handle;
  feed.replaceChildren(h("ul", { class: "list", testid: "activity-list" }, payments.map((p) => {
    const out = p.from_handle === me, inn = p.to_handle === me;
    const dir = out ? "Sent" : inn ? "Received" : "Between others";
    return h("li", { class: "item", testid: "activity-item-" + p.payment_id, "data-visibility": p.visibility },
      h("div", { class: "avatar" + (out ? " out" : ""), "aria-hidden": "true" }, initial(out ? p.to_handle : p.from_handle)),
      h("div", { class: "item-main" },
        h("div", { class: "item-title", testid: "activity-parties-" + p.payment_id }, "@" + p.from_handle + " → @" + p.to_handle),
        h("div", { class: "item-note", testid: "activity-note-" + p.payment_id }, p.note),
        h("div", { class: "item-meta" }, h("span", {}, dir), h("time", { datetime: p.created_at }, when(p.created_at)),
          p.authorization_id ? h("span", {}, "From a hold") : null,
          p.request_id ? h("span", {}, "Paid request") : null)),
      h("div", { class: "item-side" },
        h("span", { class: "item-amount" + (inn && !out ? " in" : out ? " out" : "") },
          h("span", { testid: "activity-amount-" + p.payment_id }, money(p.amount))),
        h("span", { class: "badge b-" + p.visibility }, p.visibility === "private" ? "Private" : "Public")));
  })));
}

/** Pay form: same body => same Idempotency-Key; lost response => pay-uncertain, retry safe. */
function payForm(refresh) {
  const F = "pay";
  const msg = h("div", { "aria-live": "polite" });
  const handle = draftInput(F, "to_handle", { testid: "pay-handle", placeholder: "their handle", autocapitalize: "none", spellcheck: "false" });
  const amount = amountInput(F, "amount", "pay-amount");
  const note = draftInput(F, "note", { testid: "pay-note", placeholder: "What's it for?" });
  const vis = visibilitySelect(F, "pay-visibility");
  const submit = h("button", { class: "btn btn-primary", type: "submit", testid: "pay-submit" }, "Send money");
  const form = h("form", { novalidate: true, "aria-labelledby": "pay-h" },
    h("div", { class: "card-head" }, h("h2", { id: "pay-h" }, "Send money")),
    field("To", handle), field("Amount", amount), field("Note (optional)", note), field("Who can see it", vis), msg, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.getAttribute("aria-busy") === "true") return;
    const minor = parseAmount(amount.querySelector("input").value);
    if (minor === null) return setMsg(msg, "error", "pay-error", "Enter an amount like " + plain(1500) + " (at most " + mu() + " decimal places).");
    if (!cleanHandle(handle.value)) return setMsg(msg, "error", "pay-error", "Enter the recipient's handle.");
    const id = identity(F, { to_handle: cleanHandle(handle.value), amount: minor, note: note.value, visibility: vis.value });
    submit.setAttribute("aria-busy", "true");
    submit.textContent = "Sending…";
    const res = await api("POST", "/payments", id.body, id.key);
    submit.removeAttribute("aria-busy");
    submit.textContent = "Send money";
    if (res.lost) {
      setMsg(msg, "warn", "pay-uncertain", "We couldn't confirm this payment — it may or may not have gone through. Press Send again to check; you won't be charged twice.");
    } else if (res.ok) {
      setMsg(msg, "ok", "pay-success", "Sent " + money(res.data.amount) + " to @" + res.data.to_handle + ".");
      await refresh();
    } else if (res.status !== 401) {
      setMsg(msg, "error", "pay-error", explain(res));
      await refresh();
    }
  });
  return h("section", { class: "card" }, form);
}

function requestForm() {
  const F = "request";
  const msg = h("div", { "aria-live": "polite" });
  const handle = draftInput(F, "payer_handle", { testid: "request-handle", placeholder: "who should pay", autocapitalize: "none", spellcheck: "false" });
  const amount = amountInput(F, "amount", "request-amount");
  const note = draftInput(F, "note", { testid: "request-note", placeholder: "What's it for?" });
  const submit = h("button", { class: "btn btn-secondary", type: "submit", testid: "request-submit" }, "Request money");
  const form = h("form", { novalidate: true, "aria-labelledby": "req-h" },
    h("div", { class: "card-head" }, h("h2", { id: "req-h" }, "Request money")),
    field("From", handle), field("Amount", amount), field("Note (optional)", note), msg, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.getAttribute("aria-busy") === "true") return;
    const minor = parseAmount(amount.querySelector("input").value);
    if (minor === null) return setMsg(msg, "error", "request-error", "Enter an amount like " + plain(1500) + " (at most " + mu() + " decimal places).");
    if (!cleanHandle(handle.value)) return setMsg(msg, "error", "request-error", "Enter the handle of the person who should pay.");
    const id = identity(F, { payer_handle: cleanHandle(handle.value), amount: minor, note: note.value });
    submit.setAttribute("aria-busy", "true");
    const res = await api("POST", "/requests", id.body, id.key);
    submit.removeAttribute("aria-busy");
    if (res.lost) setMsg(msg, "warn", "request-uncertain", "We couldn't confirm the request was sent. Press Request again to check — it won't be duplicated.");
    else if (res.ok) setMsg(msg, "ok", "request-success", "Asked @" + res.data.payer_handle + " for " + money(res.data.amount) + ". Track it under Requests.");
    else if (res.status !== 401) setMsg(msg, "error", "request-error", explain(res));
  });
  return h("section", { class: "card" }, form);
}

function authorizeForm(refresh) {
  const F = "authorize";
  const msg = h("div", { "aria-live": "polite" });
  const handle = draftInput(F, "to_handle", { testid: "authorize-handle", placeholder: "who can collect", autocapitalize: "none", spellcheck: "false" });
  const amount = amountInput(F, "amount", "authorize-amount");
  const note = draftInput(F, "note", { testid: "authorize-note", placeholder: "e.g. deposit" });
  const vis = visibilitySelect(F, "authorize-visibility");
  const submit = h("button", { class: "btn btn-secondary", type: "submit", testid: "authorize-submit" }, "Place hold");
  const form = h("form", { novalidate: true, "aria-labelledby": "auth-h" },
    h("div", { class: "card-head" }, h("h2", { id: "auth-h" }, "Reserve money"),
      h("p", {}, "Hold funds for someone to collect later")),
    field("For", handle), field("Amount", amount), field("Note (optional)", note), field("Who can see the payment", vis), msg, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.getAttribute("aria-busy") === "true") return;
    const minor = parseAmount(amount.querySelector("input").value);
    if (minor === null) return setMsg(msg, "error", "authorize-error", "Enter an amount like " + plain(1500) + " (at most " + mu() + " decimal places).");
    if (!cleanHandle(handle.value)) return setMsg(msg, "error", "authorize-error", "Enter the handle of the person who will collect.");
    const id = identity(F, { to_handle: cleanHandle(handle.value), amount: minor, note: note.value, visibility: vis.value });
    submit.setAttribute("aria-busy", "true");
    const res = await api("POST", "/authorizations", id.body, id.key);
    submit.removeAttribute("aria-busy");
    if (res.lost) {
      setMsg(msg, "warn", "authorize-uncertain", "We couldn't confirm the hold. Press Place hold again to check — it won't be duplicated.");
    } else if (res.ok) {
      setMsg(msg, "ok", "authorize-success", "Holding " + money(res.data.amount) + " for @" + res.data.to_handle + " until " + when(res.data.expires_at) + ".");
      await refresh();
    } else if (res.status !== 401) {
      setMsg(msg, "error", "authorize-error", explain(res));
      await refresh();
    }
  });
  return h("section", { class: "card" }, form);
}

/* ------------------------------------------------------------------ requests page */

const STATUS_LABEL = { pending: "Pending", paid: "Paid", declined: "Declined", cancelled: "Cancelled",
  open: "On hold", captured: "Collected", voided: "Released", expired: "Expired" };

function pageRequests(main) {
  const wallet = walletCard(false);
  const msg = h("div", { "aria-live": "polite" });
  const lists = h("div", { class: "stack" }, h("div", { class: "skeleton-row" }), h("div", { class: "skeleton-row" }), h("span", { class: "sr-only" }, "Loading requests"));
  S.retry.reqPay = S.retry.reqPay || {};

  async function refresh() {
    const gen = start("requests");
    const [me, inc, out] = await Promise.all([api("GET", "/me"),
      api("GET", "/requests?direction=incoming&limit=200"), api("GET", "/requests?direction=outgoing&limit=200")]);
    if (!fresh("requests", gen)) return;
    if (me.ok) { S.me = me.data; wallet.paint(); }
    if (inc.ok && out.ok) paint(inc.data.requests, out.data.requests);
    else setMsg(msg, "error", null, "Couldn't load your requests. Try again in a moment.");
  }

  async function act(r, kind) {
    let res;
    if (kind === "pay") {
      const k = (S.retry.reqPay[r.request_id] = S.retry.reqPay[r.request_id] || newKey());
      res = await api("POST", "/requests/" + encodeURIComponent(r.request_id) + "/pay", "{}", k);
    } else {
      res = await api("POST", "/requests/" + encodeURIComponent(r.request_id) + "/" + kind);
    }
    if (res.lost) setMsg(msg, "warn", "request-uncertain", "We couldn't confirm that action. Try again — a payment will not be made twice.");
    else if (res.ok) setMsg(msg, "ok", "request-success",
      kind === "pay" ? "Paid " + money(r.amount) + " to @" + r.requester_handle + "." :
      kind === "decline" ? "Declined the request from @" + r.requester_handle + "." : "Cancelled your request to @" + r.payer_handle + ".");
    else if (res.status !== 401) setMsg(msg, "error", "request-error", explain(res));
    await refresh();
  }

  function item(r, incoming) {
    const other = incoming ? r.requester_handle : r.payer_handle;
    const buttons = [];
    if (r.status === "pending" && incoming) {
      buttons.push(h("button", { class: "btn btn-primary btn-sm", type: "button", testid: "request-pay-" + r.request_id, onclick: (e) => busy(e, () => act(r, "pay")) }, "Pay " + money(r.amount)));
      buttons.push(h("button", { class: "btn btn-danger btn-sm", type: "button", testid: "request-decline-" + r.request_id, onclick: (e) => busy(e, () => act(r, "decline")) }, "Decline"));
    }
    if (r.status === "pending" && !incoming) {
      buttons.push(h("button", { class: "btn btn-danger btn-sm", type: "button", testid: "request-cancel-" + r.request_id, onclick: (e) => busy(e, () => act(r, "cancel")) }, "Cancel request"));
    }
    return h("li", { class: "item", testid: "request-item-" + r.request_id, "data-status": r.status },
      h("div", { class: "avatar" + (incoming ? " out" : ""), "aria-hidden": "true" }, initial(other)),
      h("div", { class: "item-main" },
        h("div", { class: "item-title" }, incoming ? "@" + other + " asks you" : "You asked @" + other),
        h("div", { class: "item-note" }, r.note),
        h("div", { class: "item-meta" }, h("time", { datetime: r.created_at }, when(r.created_at)))),
      h("div", { class: "item-side" },
        h("span", { class: "item-amount" }, h("span", { testid: "request-amount-" + r.request_id }, money(r.amount))),
        h("span", { class: "badge b-" + r.status }, STATUS_LABEL[r.status])),
      buttons.length ? h("div", { class: "actions item-wide" }, buttons) : null);
  }

  function paint(incoming, outgoing) {
    const section = (title, testid, rows, inc, emptyText) => h("section", { class: "card", "aria-label": title },
      h("div", { class: "card-head" }, h("h2", {}, title), h("p", {}, rows.length + (rows.length === 1 ? " request" : " requests"))),
      h("ul", { class: "list", testid }, rows.map((r) => item(r, inc))),
      rows.length ? null : h("p", { class: "hint" }, emptyText));
    put(lists,
      !incoming.length && !outgoing.length ? h("div", { class: "empty", testid: "empty-requests" },
        h("strong", {}, "No requests yet"), "Ask someone for money from your wallet, or split a bill.") : null,
      section("Waiting for you", "incoming-list", incoming, true, "Nobody is asking you for money."),
      section("You requested", "outgoing-list", outgoing, false, "You haven't requested money."));
  }

  main.append(h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("h1", {}, "Requests"), h("p", {}, "Pay, decline or cancel money requests")),
    wallet.el, msg, lists));
  refresh();
}

async function busy(e, fn) {
  const b = e.currentTarget;
  if (b.getAttribute("aria-busy") === "true") return;
  b.setAttribute("aria-busy", "true");
  try { await fn(); } finally { b.removeAttribute("aria-busy"); }
}

/* ------------------------------------------------------------------ split page */

function shares(amount, n) {
  const base = Math.floor(amount / n), rem = amount - base * n;
  return Array.from({ length: n }, (_, i) => base + (i < rem ? 1 : 0));
}

function pageSplit(main) {
  const F = "split";
  const msg = h("div", { "aria-live": "polite" });
  const amount = amountInput(F, "amount", "split-amount");
  const handles = draftInput(F, "handles", { testid: "split-handles", placeholder: "ada, bob, cy", autocapitalize: "none", spellcheck: "false" });
  const note = draftInput(F, "note", { testid: "split-note", placeholder: "e.g. dinner" });
  const submit = h("button", { class: "btn btn-primary", type: "submit", testid: "split-submit" }, "Send requests");
  const preview = h("div", { "aria-live": "polite" });
  const parse = () => handles.value.split(",").map(cleanHandle).filter(Boolean);

  function paintPreview() {
    const minor = parseAmount(amount.querySelector("input").value);
    const list = parse();
    if (minor === null || !list.length) {
      preview.replaceChildren(h("p", { class: "hint" }, "Enter an amount and the people sharing it to see each share."));
      return;
    }
    if (new Set(list).size !== list.length) {
      preview.replaceChildren(h("p", { class: "hint" }, "Each person can appear only once."));
      return;
    }
    const s = shares(minor, list.length);
    const me = S.me.handle;
    preview.replaceChildren(h("div", { class: "preview", testid: "split-preview" },
      list.map((hd, i) => h("div", { class: "share" },
        h("span", {}, "@" + hd + (hd === me ? " (you — already paid)" : "")),
        h("span", { class: "amount", testid: "split-share-" + hd }, money(s[i]))))));
  }
  amount.querySelector("input").addEventListener("input", paintPreview);
  handles.addEventListener("input", paintPreview);

  const form = h("form", { novalidate: true },
    field("Total amount", amount), field("People sharing (in order)", handles, "Separate handles with commas. Include yourself to take a share."),
    field("Note (optional)", note), h("div", {}, h("h3", { class: "section-title" }, "Shares"), preview), msg, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.getAttribute("aria-busy") === "true") return;
    const minor = parseAmount(amount.querySelector("input").value);
    const list = parse();
    if (minor === null) return setMsg(msg, "error", "split-error", "Enter an amount like " + plain(3000) + " (at most " + mu() + " decimal places).");
    if (!list.length) return setMsg(msg, "error", "split-error", "Add at least one handle.");
    const id = identity(F, { amount: minor, participant_handles: list, note: note.value });
    submit.setAttribute("aria-busy", "true");
    const res = await api("POST", "/splits", id.body, id.key);
    submit.removeAttribute("aria-busy");
    if (res.lost) setMsg(msg, "warn", "split-uncertain", "We couldn't confirm the split. Press Send requests again to check — it won't be duplicated.");
    else if (res.ok) {
      const asked = res.data.requests.map((r) => "@" + r.payer_handle);
      setMsg(msg, "ok", "split-success", asked.length ? "Requests sent to " + asked.join(", ") + "." : "Split recorded — nobody else to ask.");
    } else if (res.status !== 401) setMsg(msg, "error", "split-error", explain(res));
  });

  main.append(h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("h1", {}, "Split a bill"), h("p", {}, "You paid — ask everyone for their share")),
    h("section", { class: "card", style: "max-width: 560px" }, form)));
  paintPreview();
}

/* ------------------------------------------------------------------ authorizations page */

function pageAuthorizations(main) {
  const wallet = walletCard(false);
  const msg = h("div", { "aria-live": "polite" });
  const list = h("div", {}, h("div", { class: "skeleton-row" }), h("div", { class: "skeleton-row" }), h("span", { class: "sr-only" }, "Loading holds"));
  S.retry.capture = S.retry.capture || {};

  async function refresh() {
    const gen = start("auths");
    const [me, res] = await Promise.all([api("GET", "/me"), api("GET", "/authorizations?limit=200")]);
    if (!fresh("auths", gen)) return;
    if (me.ok) { S.me = me.data; wallet.paint(); }
    if (res.ok) paint(res.data.authorizations);
    else setMsg(msg, "error", null, "Couldn't load your holds. Try again in a moment.");
  }

  async function capture(a, input) {
    const minor = parseAmount(input.value);
    if (minor === null) return setMsg(msg, "error", "authorization-error", "Enter an amount like " + plain(a.remaining_amount) + " (at most " + mu() + " decimal places).");
    const body = JSON.stringify({ amount: minor });
    const cur = S.retry.capture[a.authorization_id];
    const key = cur && cur.body === body ? cur.key : newKey();
    S.retry.capture[a.authorization_id] = { body, key };
    const res = await api("POST", "/authorizations/" + encodeURIComponent(a.authorization_id) + "/capture", body, key);
    if (res.lost) setMsg(msg, "warn", "authorization-uncertain", "We couldn't confirm the collection. Press Collect again to check — it won't be collected twice.");
    else if (res.ok) { delete S.captureDraft[a.authorization_id]; setMsg(msg, "ok", "authorization-success", "Collected " + money(res.data.amount) + " from @" + a.from_handle + "."); }
    else if (res.status !== 401) setMsg(msg, "error", "authorization-error", explain(res));
    await refresh();
  }

  async function voidIt(a) {
    const res = await api("POST", "/authorizations/" + encodeURIComponent(a.authorization_id) + "/void");
    if (res.lost) setMsg(msg, "warn", "authorization-uncertain", "We couldn't confirm the release. Try again.");
    else if (res.ok) setMsg(msg, "ok", "authorization-success", "Released the hold for @" + a.to_handle + ".");
    else if (res.status !== 401) setMsg(msg, "error", "authorization-error", explain(res));
    await refresh();
  }

  function item(a) {
    const me = S.me.handle;
    const outgoing = a.from_handle === me;
    const id = a.authorization_id;
    const tail = [];
    if (a.status === "open" && !outgoing) {
      const input = h("input", { testid: "authorization-capture-amount-" + id, inputmode: "decimal", id: "cap-" + id,
        value: S.captureDraft[id] !== undefined ? S.captureDraft[id] : plain(a.remaining_amount) });
      input.addEventListener("input", () => { S.captureDraft[id] = input.value; });
      tail.push(h("div", { class: "capture item-wide" },
        h("div", { class: "field" }, h("label", { for: "cap-" + id }, "Amount to collect"), input),
        h("button", { class: "btn btn-primary btn-sm", type: "button", testid: "authorization-capture-" + id, onclick: (e) => busy(e, () => capture(a, input)) }, "Collect")));
    }
    if (a.status === "open" && outgoing) {
      tail.push(h("div", { class: "actions item-wide" },
        h("button", { class: "btn btn-danger btn-sm", type: "button", testid: "authorization-void-" + id, onclick: (e) => busy(e, () => voidIt(a)) }, "Release hold")));
    }
    return h("li", { class: "item", testid: "authorization-item-" + id, "data-status": a.status },
      h("div", { class: "avatar" + (outgoing ? " out" : ""), "aria-hidden": "true" }, initial(outgoing ? a.to_handle : a.from_handle)),
      h("div", { class: "item-main" },
        h("div", { class: "item-title" }, outgoing ? "You reserved for @" + a.to_handle : "@" + a.from_handle + " reserved for you"),
        h("div", { class: "item-note" }, a.note),
        h("div", { class: "item-meta" },
          a.status === "open" ? h("span", {}, "Still held " + money(a.remaining_amount)) : null,
          a.captured_amount > 0 && a.status !== "captured" ? h("span", {}, "Collected " + money(a.captured_amount)) : null,
          a.status === "captured" ? h("span", {}, "Collected ", h("span", { testid: "authorization-captured-" + id }, money(a.captured_amount))) : null,
          h("span", {}, (a.status === "open" ? "Expires " : "Expired ") + when(a.expires_at)),
          h("time", { class: "tech", testid: "authorization-expires-" + id, datetime: a.expires_at, title: "Expiry (RFC 3339)" }, a.expires_at))),
      h("div", { class: "item-side" },
        h("span", { class: "item-amount" }, h("span", { testid: "authorization-amount-" + id }, money(a.amount))),
        h("span", { class: "badge b-" + a.status }, STATUS_LABEL[a.status])),
      tail);
  }

  function paint(auths) {
    put(list,
      auths.length ? null : h("div", { class: "empty", testid: "empty-authorizations" },
        h("strong", {}, "No holds"), "Reserve money for someone and they can collect it later."),
      h("ul", { class: "list", testid: "authorization-list" }, auths.map(item)));
  }

  main.append(h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("h1", {}, "Holds"), h("p", {}, "Money reserved now, collected later")),
    wallet.el,
    h("div", { class: "grid-2" },
      h("div", { class: "stack" }, authorizeForm(refresh)),
      h("section", { class: "card", "aria-label": "Your holds" },
        h("div", { class: "card-head" }, h("h2", {}, "Your holds"), h("p", {}, "Newest first")), msg, list))));
  refresh();
}

/* ------------------------------------------------------------------ auth pages */

function authPage(main, mode) {
  const signup = mode === "signup";
  const msg = h("div", { "aria-live": "assertive" });
  const email = h("input", { type: "email", testid: signup ? "signup-email" : "login-email", autocomplete: "email", required: true });
  const password = h("input", { type: "password", testid: signup ? "signup-password" : "login-password",
    autocomplete: signup ? "new-password" : "current-password", required: true });
  const name = signup ? h("input", { testid: "signup-display-name", autocomplete: "name", required: true }) : null;
  const submit = h("button", { class: "btn btn-primary", type: "submit", testid: signup ? "signup-submit" : "login-submit" },
    signup ? "Create account" : "Log in");
  const form = h("form", { novalidate: true },
    signup ? field("Your name", name) : null, field("Email", email),
    field("Password", password, signup ? "At least 8 characters." : null), msg, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.getAttribute("aria-busy") === "true") return;
    msg.replaceChildren();
    const body = signup ? { email: email.value.trim(), password: password.value, display_name: name.value.trim() }
      : { email: email.value.trim(), password: password.value };
    submit.setAttribute("aria-busy", "true");
    const res = await api("POST", signup ? "/auth/signup" : "/auth/login", JSON.stringify(body));
    submit.removeAttribute("aria-busy");
    if (res.ok) {
      S.token = res.data.token;
      localStorage.setItem("pf_token", S.token);
      S.me = null; S.retry = {}; S.forms = {}; S.captureDraft = {};
      go("/");
    } else {
      setMsg(msg, "error", "auth-error", res.lost ? "We couldn't reach Pocketful. Check your connection and try again." : explain(res));
    }
  });
  main.append(h("section", { class: "card narrow" },
    h("div", { class: "card-head" }, h("h1", {}, signup ? "Create your Pocketful account" : "Welcome back")),
    form,
    h("p", { class: "hint", style: "margin-top:12px" }, signup ? "Already have an account? " : "New to Pocketful? ",
      h("a", { href: signup ? "/login" : "/signup", "data-link": true }, signup ? "Log in" : "Create an account"))));
}

function pageLogin(main) { authPage(main, "login"); }
function pageSignup(main) { authPage(main, "signup"); }

render();
