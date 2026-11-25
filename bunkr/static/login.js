"use strict";
const $ = (s) => document.querySelector(s);
const post = async (path, body) => {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Bunkr": "1" }, body: JSON.stringify(body || {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong");
  return data;
};
const show = (el, msg) => { el.textContent = msg; el.hidden = !msg; };
let ticket = "";

function home() { location.href = "/" + (/^#bin-[\w-]+$/.test(location.hash) ? location.hash : ""); }  // a scanned bin label survives the login

let suggest = null;  // the passkey offered in the username field's suggestions, waiting in the background

async function passkeyLogin(t, errEl, auto = false) {
  show(errEl, "");
  stopSuggesting();
  try {
    const o = await post("/api/login/passkey-options", t ? { ticket: t } : {});
    const cred = await Passkey.get(o.publicKey);
    await post("/api/login/passkey", { ...cred, state: o.state, ticket: t || "" });
    Passkey.remember(true);
    home();
    return true;
  } catch (e) {
    // asked by itself on an address passkeys don't work on (an IP, the home .lan name): just show the password form
    const quiet = Passkey.cancelled(e) || (auto && e.name === "SecurityError");
    if (!quiet) {
      show(errEl, e.message || "That didn't work");
      if (auto) Passkey.remember(false);  // BUNKR didn't accept it (the passkey was removed): stop asking for it first
    }
    if (!t) suggestPasskey();
    return false;
  }
}

// Without a remembered passkey, phones and browsers that can still offer one in the username field's suggestions.
async function suggestPasskey() {
  if (suggest || !Passkey.usable() || !(await Passkey.autofill())) return;
  suggest = new AbortController();
  const signal = suggest.signal;
  try {
    const o = await post("/api/login/passkey-options", {});
    const cred = await Passkey.get(o.publicKey, { mediation: "conditional", signal });
    await post("/api/login/passkey", { ...cred, state: o.state, ticket: "" });
    Passkey.remember(true);
    home();
  } catch (e) {
    if (!signal.aborted && !Passkey.cancelled(e)) show($("#login-error"), e.message || "That didn't work");
  } finally {
    if (suggest && suggest.signal === signal) suggest = null;
  }
}
function stopSuggesting() {
  if (suggest) { suggest.abort(); suggest = null; }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, err = $("#login-error"), btn = f.querySelector("button[type=submit]");
  btn.disabled = true; show(err, "");
  stopSuggesting();
  try {
    const r = await post("/api/login", { username: f.username.value, password: f.password.value });
    if (r.next === "two-step") return secondStep(r);
    if (r.next === "setup") return setup();
    home();
  } catch (x) {
    show(err, x.message === "Failed to fetch" ? "Can't reach BUNKR" : x.message);
  } finally { btn.disabled = false; }
});

if (Passkey.usable()) {
  $("#passkey-login").hidden = false;
  $("#passkey-login").addEventListener("click", () => passkeyLogin("", $("#login-error")));
  const q = new URLSearchParams(location.search);
  if (Passkey.remembered() && !q.get("setup") && !q.get("out")) {
    // this phone has a passkey for BUNKR: ask for it straight away (Face ID, fingerprint), no username or password.
    // Some browsers only allow that after a tap, so the passkey button is also made the big one.
    $("#passkey-login").classList.add("primary");
    $("#login-form button[type=submit]").classList.remove("primary");
    $("#login-form").prepend($("#passkey-login"));
    $("#login-form").username.removeAttribute("autofocus");
    passkeyLogin("", $("#login-error"), true);
  } else {
    suggestPasskey();
  }
}

function secondStep(r) {
  ticket = r.ticket;
  $("#login-form").hidden = true;
  $("#step-form").hidden = false;
  const pk = r.methods.includes("passkey") && Passkey.usable();
  if (pk) Passkey.remember(true);  // next time, start with the passkey
  $("#step-passkey").hidden = !pk;
  $("#step-code").hidden = !r.methods.includes("code") && pk;
  $("#step-recovery").hidden = !r.methods.includes("recovery");
  if (!r.methods.includes("code") && !pk) {
    $("#step-code-label").textContent = "Recovery code";
    show($("#step-error"), "Your passkey works on BUNKR's https address. Here, use a recovery code.");
  }
  if (pk) passkeyLogin(ticket, $("#step-error"));
  else $("#step-form").code.focus();
}
$("#step-passkey").addEventListener("click", () => passkeyLogin(ticket, $("#step-error")));
$("#step-recovery").addEventListener("click", () => {
  $("#step-code").hidden = false;
  $("#step-code-label").textContent = "Recovery code (like 4F7K-9QXM)";
  $("#step-form").code.focus();
});
$("#step-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await post("/api/login/code", { ticket, code: e.target.code.value }); home(); }
  catch (x) {
    show($("#step-error"), x.message);
    if (/too long|too many/.test(x.message)) setTimeout(() => location.reload(), 2500);
  }
});

// ---------- required setup (a setup-only session) ----------
function setup() {
  $("#login-form").hidden = true;
  $("#setup-box").hidden = false;
  $("#setup-passkey").hidden = !Passkey.usable();
}
$("#setup-passkey").addEventListener("click", async () => {
  show($("#setup-error"), "");
  try {
    const o = await post("/api/me/passkeys/options");
    const cred = await Passkey.create(o);
    const r = await post("/api/me/passkeys", { ...cred, name: guessDevice() });
    Passkey.remember(true);
    if (r.ready) home();
  } catch (e) { if (!Passkey.cancelled(e)) show($("#setup-error"), e.message); }
});
$("#setup-totp").addEventListener("click", async () => {
  try {
    const r = await post("/api/me/two-step/totp");
    $("#setup-key").textContent = r.key;
    $("#setup-link").href = r.uri;
    $("#setup-totp-box").hidden = false;
  } catch (e) { show($("#setup-error"), e.message); }
});
$("#setup-confirm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await post("/api/me/two-step/totp/confirm", { code: e.target.code.value });
    $("#setup-totp-box").hidden = true; $("#setup-totp").hidden = true; $("#setup-passkey").hidden = true;
    $("#setup-code-list").replaceChildren(...r.recovery.map((c) => Object.assign(document.createElement("li"), { textContent: c })));
    $("#setup-codes").hidden = false;
  } catch (x) { show($("#setup-error"), x.message); }
});
$("#setup-done").addEventListener("click", home);
$("#setup-logout").addEventListener("click", async () => { await post("/api/logout").catch(() => {}); location.href = "/login.html"; });
if (new URLSearchParams(location.search).get("setup")) setup();

function guessDevice() {
  const u = navigator.userAgent;
  return /iPhone/.test(u) ? "iPhone" : /iPad/.test(u) ? "iPad" : /Android/.test(u) ? "Android phone" : /Mac/.test(u) ? "Mac" : /Windows/.test(u) ? "Windows PC" : "Passkey";
}
