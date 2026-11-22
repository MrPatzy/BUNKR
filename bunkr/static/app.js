"use strict";

let S = null;            // last /api/state
let selectedMount = "";
let libraries = null;
let editingItem = null;
let pollTimer = null;

// replaceChildren() would print "null" for an empty slot; skip null/false (and flatten arrays) the same way h() does.
const nativeReplaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...kids) {
  return nativeReplaceChildren.apply(this, kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Bunkr": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch("/api/" + path, opts); }
  catch (e) {  // no connection at all (a change: the service worker only answers reads)
    offlineBar(true);
    throw new Error("No connection to BUNKR: that didn't go through. If it's urgent, text the family or use the mesh.");
  }
  if (res.status === 401) { forgetSaved(); location.href = "/login.html"; throw new Error("Please log in"); }
  if (method === "GET") offlineBar(res.headers.get("X-Bunkr-Saved") || (res.headers.get("X-Bunkr-Offline") ? true : false));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------- offline: the service worker keeps the app and what you last saw ---------- */
let offlineSince = null;
function offlineBar(saved) {
  const bar = $("#offline-bar");
  if (!bar) return;
  if (!saved) { if (offlineSince) { offlineSince = null; bar.hidden = true; } return; }
  if (typeof saved === "string" && (!offlineSince || saved < offlineSince)) offlineSince = saved;
  bar.hidden = false;
  bar.textContent = "No connection. Showing what this phone saved" +
    (offlineSince ? ` ${new Date(offlineSince).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : "") +
    ". Check-ins and changes need a connection: text the family or use the mesh.";
}
function forgetSaved() {
  try { navigator.serviceWorker?.controller?.postMessage("forget"); } catch {}
  try { caches?.delete("bunkr-data-v1"); } catch {}
}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
addEventListener("online", () => { if (offlineSince) refresh().catch(() => {}); });

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.hidden = true), isError ? 6000 : 2500);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

const gb = (b) => b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(0) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";
const tone = (pct) => pct >= 80 ? "good" : pct >= 40 ? "warn" : "bad";
const fmtNum = (n) => Number.isInteger(n) ? n : n.toFixed(1);
const essentialById = (id) => S.checklist.find((e) => e.id === id);
const when = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
const LEVEL = { "Very High": 5, "Relatively High": 4, "Relatively Moderate": 3, "Relatively Low": 2, "Very Low": 1 };

/* ---------- tabs ---------- */
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  const btn = $("#tabs button.active");
  if (btn) { btn.closest("details").open = true; $("#here").textContent = name === "home" ? "" : btn.textContent; }
  closeMenu();
  $$(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
  try { localStorage.setItem("bunkr-tab", name); } catch {}
  if (name === "kit") { loadDrives(); if (!libraries) loadLibraries(); }
  if (name === "documents") loadDocSizes();
  if (name === "nodes") { loadNodes(); loadConflicts(); }
  if (name === "household") { loadUsers(); loadAudit(); loadTwoStep(); loadFlightSettings(); loadMail(); loadHA(); loadNaming(); loadFoodSources(); loadKroger(); loadAI(); loadVersion(); }
  if (name === "situation") loadSituation();
  if (name === "brief") loadNewsTab();
  if (name === "home") { refloat(); if (!PUSH) loadPush(); }
  if (name === "comms") { loadComms(); loadRadio(); }
  if (name === "family") { loadFamily(); loadMesh(); }
  if (name === "medical") loadMedical();
  if (name === "power") loadPower();
  if (name === "recovery") loadRecovery();
  if (name === "scenarios") renderScenarios();
  if (name === "sites" && isAdmin()) { loadMaps(); loadLocalWatch(); }
  if (name === "downloads") loadDepot();
}
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) { showTab(e.target.dataset.tab); scrollTo(0, 0); } });
// the logo goes back to Home
$("#brand").addEventListener("click", (e) => { e.preventDefault(); closeNotices(); showTab("home"); scrollTo(0, 0); });

/* ---------- the bell: everything you'd be notified about, kept until you mark it read ---------- */
let NOTICES = null;
function setUnread(n) {
  const b = $("#bell-n");
  b.hidden = !n;
  b.textContent = n > 99 ? "99+" : String(n || "");
  $("#bell").setAttribute("aria-label", n ? `Notifications, ${n} unread` : "Notifications");
  try { n ? navigator.setAppBadge?.(n) : navigator.clearAppBadge?.(); } catch {}  // the count on the Home Screen icon
}
function goTo(url) {
  const tab = (url || "").split("#")[1] || "home";
  if ($(`#tabs [data-tab="${tab}"]`)) { showTab(tab); scrollTo(0, 0); }
}
function renderNotices() {
  const list = (NOTICES && NOTICES.items) || [];
  setUnread(NOTICES ? NOTICES.unread : 0);
  $("#notice-all").hidden = !(NOTICES && NOTICES.unread);
  $("#notice-list").replaceChildren(...(list.length ? list.map((n) => h("li", { class: "notice" + (n.read_at ? "" : " unread") },
    h("button", { type: "button", class: "notice-open", onclick: () => {
      if (!n.read_at) api("POST", "notices/read", { ids: [n.id] }).then((r) => { n.read_at = "now"; NOTICES.unread = r.unread; renderNotices(); }).catch(() => {});
      closeNotices(); goTo(n.url);
    } }, h("strong", {}, n.title), n.body ? h("span", { class: "notice-body" }, n.body) : null,
      h("span", { class: "muted notice-at" }, sinceIso(n.at))),
    n.read_at ? null : h("button", { type: "button", class: "link notice-mark", "aria-label": `Mark "${n.title}" read`, onclick: () =>
      guarded(async () => { const r = await api("POST", "notices/read", { ids: [n.id] }); n.read_at = "now"; NOTICES.unread = r.unread; renderNotices(); }) }, "✓")))
    : [h("li", { class: "muted" }, "Nothing yet. Family calls, check-ins, alerts and reminders show up here.")]));
}
async function loadNotices() {
  const r = await api("GET", "notices").catch(() => null);
  if (r) { NOTICES = r; renderNotices(); }
}
function closeNotices() {
  $("#notice-panel").hidden = true;
  $("#bell").setAttribute("aria-expanded", "false");
}
$("#bell").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = $("#notice-panel").hidden;
  if (!open) return closeNotices();
  closeMenu();
  $("#notice-panel").hidden = false;
  $("#bell").setAttribute("aria-expanded", "true");
  renderNotices();
  loadNotices();
});
$("#notice-close").addEventListener("click", closeNotices);
$("#notice-all").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "notices/read", {});
  (NOTICES.items || []).forEach((n) => { n.read_at = n.read_at || "now"; });
  NOTICES.unread = r.unread; renderNotices();
}));
document.addEventListener("click", (e) => { if (!$("#notice-panel").hidden && !e.target.closest("#notice-panel, #bell")) closeNotices(); });
addEventListener("keydown", (e) => { if (e.key === "Escape") closeNotices(); });
// the count stays fresh while the app is open; a push arriving also refreshes it
setInterval(() => { if (S && S.require_login && !document.hidden) loadNotices(); }, 60000);
navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data === "pushed") loadNotices(); });

/* ---------- the menu: a drawer on phones, a side column on wide screens ---------- */
const wide = matchMedia("(min-width: 1000px)");
const topHeight = () => document.documentElement.style.setProperty("--top-h", $(".top").offsetHeight + "px");
addEventListener("resize", topHeight);
topHeight();
function openMenu() {
  if (wide.matches) return;
  document.body.classList.add("menu-open");
  $("#scrim").hidden = false;
  $("#menu-btn").setAttribute("aria-expanded", "true");
  ($("#tabs button.active") || $("#tabs summary")).focus();
}
function closeMenu() {
  if (!document.body.classList.contains("menu-open")) return;
  document.body.classList.remove("menu-open");
  $("#scrim").hidden = true;
  $("#menu-btn").setAttribute("aria-expanded", "false");
}
$("#menu-btn").addEventListener("click", () => document.body.classList.contains("menu-open") ? closeMenu() : openMenu());
$("#scrim").addEventListener("click", closeMenu);
addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
// which menu groups are open is remembered on this device
$$("#tabs details").forEach((d) => {
  try { const v = localStorage.getItem("bunkr-nav-" + d.dataset.group); if (v) d.open = v === "open"; } catch {}
  d.addEventListener("toggle", () => { try { localStorage.setItem("bunkr-nav-" + d.dataset.group, d.open ? "open" : "shut"); } catch {} });
});
addEventListener("hashchange", () => { const t = location.hash.slice(1); if (t && $(`#tabs [data-tab="${t}"]`) && !$(`#tabs [data-tab="${t}"]`).hidden) showTab(t); });

/* ---------- state ---------- */
async function refresh() {
  S = await api("GET", "state");
  if (!NOTICES || NOTICES.unread !== S.unread) { setUnread(S.unread); if (!$("#notice-panel").hidden) loadNotices(); }
  renderSession();
  renderHome();
  renderReadiness();
  renderInventory();
  renderLocations();
  renderShopping();
  renderSites();
  renderDocuments();
  renderBackups();
  renderHousehold();
}

/* ---------- home: family call, check-in, board ---------- */
// The simple view shows only what a family member needs day to day; "All screens" shows everything.
const SIMPLE_TABS = ["home", "brief", "situation", "inventory", "shopping", "scenarios", "family", "medical", "household"];
const sinceIso = (iso) => {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso)) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : when(iso);
};
const mapLink = (p) => p.lat != null && p.lon != null ? `https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}`
  : p.address ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(p.address)}` : null;

// The library has its own port (books' scripts stay away from BUNKR's logins). Its address: the one an admin set,
// this host on port 8080 when we're on the home network, or the hub's home address (works only at home).
function libraryBase() {
  const L = S.library || {}, host = location.hostname;
  if (L.address) { try { return { url: new URL(L.address).origin + "/kiwix/", homeOnly: false }; } catch (e) { /* fall through */ } }
  const local = !host.includes(".") || /\.(local|lan|home\.arpa)$/.test(host) || /^[\d.]+$/.test(host) || host.includes(":");
  if (local) return { url: `http://${host.includes(":") ? "[" + host + "]" : host}:${L.port || 8080}/kiwix/`, homeOnly: false };
  return { url: L.home || "", homeOnly: true };
}
$("#lookup-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#lookup-q").value.trim();
  if (!q) return;
  $("#lookup-out").replaceChildren(h("li", { class: "muted" }, "Searching…"));
  guarded(async () => {
    const r = await api("GET", "kiwix/search?q=" + encodeURIComponent(q));
    const lib = libraryBase().url, base = lib ? new URL(lib).origin : "";
    $("#lookup-out").replaceChildren(...(r.results.length ? r.results.map((x) => h("li", {},
      base ? h("a", { href: base + x.path, target: "_blank", rel: "noopener" }, x.title) : h("strong", {}, x.title), " ", h("span", { class: "tag" }, x.book),
      x.snippet ? h("div", { class: "muted" }, x.snippet) : null)) : [h("li", { class: "muted" }, "Nothing found. Try fewer or simpler words.")]));
  });
});

function renderHome() {
  const lib = libraryBase();
  const empty = new Set();  // tiles with nothing to show right now
  if (!(S.library && S.library.ready)) empty.add("lookup");
  $("#lookup-all").hidden = !lib.url;
  $("#lookup-all").href = lib.url;
  $("#lookup-home").hidden = !lib.homeOnly;
  const b = S.board, call = b.call;
  const card = $("#call-card");
  card.hidden = !call;
  if (call) {
    card.className = "card call-card " + call.kind + (call.drill ? " drill" : "");
    const link = mapLink(call);
    card.replaceChildren(
      h("p", { class: "call-title" }, call.drill ? h("span", { class: "tag drill-tag" }, "DRILL") : null, call.label, call.place ? " " + call.place : ""),
      call.drill ? h("p", { class: "muted" }, "Practice run. Check in as if it were real; the report shows how fast everyone answered.") : null,
      call.address ? h("p", {}, call.address, link ? [" · ", h("a", { href: link, target: "_blank", rel: "noopener" }, "Directions")] : null) : null,
      call.message ? h("p", {}, call.message) : null,
      h("p", { class: "muted" }, `Sent by ${call.created_by}, ${sinceIso(call.created_at)}. `,
        b.waiting ? h("strong", { class: "bad" }, `Waiting on ${b.waiting} ${b.waiting === 1 ? "person" : "people"}.`) : "Everyone has checked in."),
      S.can_call ? h("button", { onclick: () => confirm(call.drill ? "End the drill?" : "Send all clear to everyone?") &&
        guarded(async () => { await api("POST", "calls/end"); toast(call.drill ? "Drill ended" : "All clear sent"); refresh(); }) },
        call.drill ? "End drill" : "All clear") : null);
  }
  if (!renderLastCall(call)) empty.add("lastcall");
  if (!renderBackupStatus()) empty.add("backup");
  const md = S.med_due || [];
  if (!md.length) empty.add("med");
  $("#home-med").replaceChildren(...md.map((d) => h("p", {}, h("strong", { class: d.kind === "out" ? "bad" : "" }, d.text))),
    h("p", {}, h("button", { class: "link", onclick: () => showTab("medical") }, "Open Medical")));
  const me = S.me_person;
  $("#me-name").textContent = me ? `as ${me.name}` : "";
  $("#checkin-help").textContent = me ? (call ? "Answer the family call: tap how you are." : "Let the family know how you are. It shows on everyone's Home screen.")
    : S.can_call ? "Your login isn't linked to a person yet: link it on the Family tab. You can still check others in below." : "Your login isn't linked to a person yet. Ask whoever set up BUNKR to link it on the Family tab.";
  $("#checkin-btns").replaceChildren(...Object.entries(b.statuses).map(([k, v]) => h("button", { class: "st-" + k, disabled: !me,
    onclick: () => checkIn(me && me.uid, k) }, v)));
  $("#checkin-btns").hidden = $("#checkin-extra").hidden = !me;
  // whoever needs help first, then whoever hasn't answered the call, then everyone else
  const rank = (p) => p.last && p.last.status === "help" ? 0 : p.waiting ? 1 : 2;
  const flightOf = Object.fromEntries((b.flights || []).map((f) => [f.person_uid, f]));
  $("#board").replaceChildren(...[...b.people].sort((x, y) => rank(x) - rank(y)).map((p) => {
    const l = p.last;
    const bits = l ? [sinceIso(l.at), l.via !== "app" ? "via " + (b.via[l.via] || l.via) : "", l.by_name ? "entered by " + l.by_name : "",
      l.place, l.note, !l.battery_low && l.battery != null ? `battery ${l.battery}%` : ""].filter(Boolean) : [];
    return h("li", {},
      h("span", { class: "who" }, p.name, p.remote ? h("span", { class: "tag" }, p.site || "lives elsewhere") : null),
      h("span", { class: l ? "state-" + l.status : "state-none" }, l ? p.status_label : "no check-in yet"),
      l && l.battery_low ? h("span", { class: "tag warn lowbatt", title: "Their phone or radio said its battery was low when they checked in. They may go quiet: that doesn't mean something is wrong." },
        "🔋 low battery" + (l.battery != null ? ` ${l.battery}%` : "") + ", may go quiet") : null,
      h("span", { class: "meta" }, bits.join(" · "),
        l && l.lat != null ? [" · ", h("a", { href: mapLink(l), target: "_blank", rel: "noopener" }, "map")] : null,
        p.waiting ? h("span", { class: "waiting" }, (bits.length ? " · " : "") + "hasn't answered the call") : null,
        S.can_call && (!me || p.uid !== me.uid) ? [bits.length || p.waiting ? " · " : "", h("button", { class: "link", onclick: () => {
          const st = prompt(`Check ${p.name} in as: safe, moving, or help?`, "safe");
          const key = { safe: "safe", moving: "moving", "on my way": "moving", help: "help", "need help": "help" }[(st || "").trim().toLowerCase()];
          if (st && !key) return toast("Type safe, moving or help", true);
          if (key) checkIn(p.uid, key, true);
        } }, "check in for them")] : null),
      flightOf[p.uid] ? flightBlock(flightOf[p.uid]) : null);
  }));
  const a = S.analysis;
  $("#home-ready").replaceChildren(h("strong", { class: tone(a.overall) }, `${a.overall}% ready. `),
    `Water for ${a.water_days} of ${a.target_days} days, food for ${a.food_days} days.`,
    (a.gaps || []).length ? h("span", { class: "muted" }, ` Biggest gap: ${a.gaps[0].label || a.gaps[0].name || a.gaps[0]}.`) : null);
  const top = (S.situation_top || []).slice(0, 4);
  $("#home-alerts").replaceChildren(...(top.length ? top.map((x) => h("p", {}, h("strong", { class: x.tier === "flash" ? "bad" : "" }, x.title),
    x.site && x.km != null ? h("span", { class: "muted" }, x.km === 0 ? ` · at ${x.site}` : ` · ${x.km} km away`) : null,
    x.scenario ? [h("br"), h("button", { class: "link", onclick: () => openScenario(x.scenario) }, "Open the checklist")] : null))
    : [h("p", { class: "muted" }, "Nothing near you right now.")]),
    h("p", {}, h("button", { class: "link", onclick: () => showTab("situation") }, "See everything")));
  const meet = S.places.filter((p) => ["meet_near", "meet_far", "relative", "bugout", "home"].includes(p.kind));
  $("#home-places").replaceChildren(...(meet.length ? meet.map((p) => h("li", {}, h("strong", {}, p.name), h("span", { class: "tag" }, S.site_kinds[p.kind] || p.kind),
    p.address ? h("div", { class: "muted" }, p.address, mapLink(p) ? [" · ", h("a", { href: mapLink(p), target: "_blank", rel: "noopener" }, "Directions")] : null) : null))
    : [h("li", { class: "muted" }, "Add meeting places on Sites & alerts.")]));
  $("#home-contacts").replaceChildren(...(S.contacts.length ? S.contacts.map((c) => h("li", {}, h("strong", {}, c.name),
    c.out_of_area ? h("span", { class: "tag" }, "out of area: everyone checks in here") : null, c.relation ? h("span", { class: "muted" }, ` ${c.relation}`) : null,
    c.phone ? h("div", {}, h("a", { href: "tel:" + c.phone.replace(/[^\d+]/g, "") }, c.phone)) : null))
    : [h("li", { class: "muted" }, "Add contacts on the Family tab.")]));
  if (!S.can_call) empty.add("sendcall");
  if (!S.require_login || !S.me.id) empty.add("push");
  if (!renderHomeNews()) empty.add("news");
  if (S.can_call && !$("#call-kind").options.length) {
    $("#call-kind").replaceChildren(...Object.entries(b.call_kinds).map(([k, v]) => h("option", { value: k }, v)));
    syncCallForm();
  }
  if (S.can_call) {
    const keep = $("#call-site").value;
    $("#call-site").replaceChildren(...S.places.map((p) => h("option", { value: p.id }, `${p.name} (${S.site_kinds[p.kind] || p.kind})`)));
    if (keep) $("#call-site").value = keep;
  }
  applyHome(empty);
}
function callReport(r) {
  const fmt = (m) => m < 1 ? "under a minute" : `${Math.round(m)} min`;
  return h("ul", { class: "report" }, ...r.people.map((p) => h("li", {}, h("span", {}, p.name),
    p.minutes == null ? h("span", { class: "bad" }, "didn't check in") :
      h("span", {}, `${p.status_label}, ${fmt(p.minutes)}`, p.via !== "app" ? h("span", { class: "muted" }, ` via ${S.board.via[p.via] || p.via}`) : null,
        p.by_name ? h("span", { class: "muted" }, ` (entered by ${p.by_name})`) : null))));
}
function renderLastCall(active) {
  const card = $("#lastcall-card"), r = S.last_call;
  if (!r || active) return false;
  card.replaceChildren(h("p", { class: "muted" }, (r.drill ? "Drill, " : "") + sinceIso(r.created_at)),
    h("p", {}, h("strong", { class: r.missing.length ? "bad" : "good" }, `${r.answered} of ${r.total} checked in`),
      r.slowest != null ? ` · slowest ${r.slowest < 1 ? "under a minute" : Math.round(r.slowest) + " min"}` : "",
      r.lasted != null ? ` · ended after ${Math.round(r.lasted)} min` : ""),
    r.missing.length && r.drill ? h("p", { class: "muted" }, `Worth a talk: how would ${r.missing.join(", ")} have heard about it?`) : null,
    h("details", {}, h("summary", { class: "link" }, "Who and how fast"), callReport(r)));
  return true;
}
// Backups on Home: admins only, in the full view, and only when something is wrong (the Backups screen has the rest).
function renderBackupStatus() {
  const card = $("#backup-card"), b = S.backup_status;
  if (!b || b.ok || S.view === "simple") return false;
  const t = b.test;
  card.replaceChildren(
    b.problems.length ? h("p", { class: "bad" }, b.problems.join(" ")) : null,
    h("p", { class: "muted" }, b.backup.at ? `Nightly copy ${sinceIso(b.backup.at)}. ` : "",
      t.at ? (t.ok ? `Restore test passed ${sinceIso(t.at)} on ${t.backup} (${Object.values(t.rows || {}).reduce((a, n) => a + n, 0)} records).`
        : `Restore test failed ${sinceIso(t.at)}.`) : "The restore test hasn't run yet; it runs monthly after the 2 am copy.",
      " Copies stay on this machine: keep Proxmox backups of it too."),
    h("button", { onclick: (e) => { e.target.disabled = true; guarded(async () => {
      const r = await api("POST", "backups-check"); toast(r.ok ? "Backed up and restore-tested: passed" : "Restore test failed: " + r.problem, !r.ok); refresh(); }); } },
      "Back up and test now"));
  return true;
}
/* ---------- Home tiles: each person's order, hidden and folded tiles; they float into columns on wider screens ---------- */
let HOME_EMPTY = new Set();
const tileEl = (id) => $(`#home-tiles [data-tile="${id}"]`);
function tilesForMe() {  // the tiles this login can have at all
  return (S.home_tiles || []).filter((t) => t.who === "all" || (t.who === "admin" && isAdmin() && S.view !== "simple") || (t.who === "caller" && S.can_call));
}
function applyHome(empty) {
  if (empty) HOME_EMPTY = empty;
  const L = S.home_layout || { order: [], hidden: [], collapsed: [] };
  const mine = new Set(tilesForMe().map((t) => t.id));
  const box = $("#home-tiles");
  const tiles = $$("#home-tiles .tile-card");
  const order = [...L.order, ...tiles.map((t) => t.dataset.tile).filter((id) => !L.order.includes(id))];
  for (const t of tiles) {
    const id = t.dataset.tile, folded = L.collapsed.includes(id);
    t.hidden = !mine.has(id) || L.hidden.includes(id) || HOME_EMPTY.has(id);
    t.classList.toggle("folded", folded);
    t.querySelector(".tile-toggle").setAttribute("aria-expanded", String(!folded));
    t.style.order = order.indexOf(id);
  }
  floatTiles(box);
}
// Masonry: each tile, in order, goes into whichever column is shortest, so the top of every column holds the
// person's first choices. One column on phones. Tiles are moved, not re-made, so their buttons keep working.
function floatTiles(box) {
  const tiles = $$("#home-tiles .tile-card").sort((a, b) => a.style.order - b.style.order);
  const n = Math.max(1, Math.min(3, Math.floor((box.clientWidth + 16) / 356)));
  let cols = [...box.children].filter((c) => c.classList.contains("tile-col"));
  if (cols.length !== n) {
    cols.forEach((c) => c.remove());
    cols = Array.from({ length: n }, () => box.appendChild(h("div", { class: "tile-col" })));
  }
  // Tiles are the same width in every column, so heights can be measured before deciding where each goes.
  const height = cols.map(() => 0), want = cols.map(() => []);
  for (const t of tiles) {
    const i = height.indexOf(Math.min(...height));
    want[i].push(t);
    if (!t.hidden) height[i] += t.offsetHeight + 16;
  }
  // Only touch a column whose tiles changed: moving a tile would take the cursor out of a box someone is typing in.
  cols.forEach((c, i) => {
    if (c.children.length !== want[i].length || want[i].some((t, k) => c.children[k] !== t)) c.append(...want[i]);
  });
}
let floatTimer;
const refloat = () => { clearTimeout(floatTimer); floatTimer = setTimeout(() => !$("#tab-home").hidden && floatTiles($("#home-tiles")), 120); };
addEventListener("resize", refloat);
let saveTimer;
function saveHome() {
  clearTimeout(saveTimer);
  const { order, hidden, collapsed } = S.home_layout;
  saveTimer = setTimeout(() => api("POST", "me/home", { layout: { order, hidden, collapsed } })
    .then((r) => { S.home_layout = r; }).catch((e) => toast(e.message, true)), 400);
}
$("#home-tiles").addEventListener("click", (e) => {
  const btn = e.target.closest(".tile-toggle");
  if (!btn) return;
  const id = btn.closest(".tile-card").dataset.tile, L = S.home_layout;
  L.collapsed = L.collapsed.includes(id) ? L.collapsed.filter((x) => x !== id) : [...L.collapsed, id];
  applyHome();
  saveHome();
});

// Arrange: a list of my tiles; drag the handle (mouse or finger) or use the arrows; untick to hide.
function renderArrange() {
  const L = S.home_layout, mine = tilesForMe(), byId = Object.fromEntries(mine.map((t) => [t.id, t]));
  const ids = L.order.filter((id) => byId[id]);
  const move = (id, d) => {
    const o = L.order, i = o.indexOf(id);
    let j = i + d;
    while (j >= 0 && j < o.length && !byId[o[j]]) j += d;  // skip tiles this login doesn't have
    if (j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    changed();
  };
  const changed = () => { renderArrange(); applyHome(); saveHome(); };
  $("#arrange-list").replaceChildren(...ids.map((id, n) => h("li", { "data-id": id },
    h("span", { class: "drag", title: "Drag to move", "aria-hidden": "true" }, "⋮⋮"),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: !L.hidden.includes(id), onchange: (e) => {
      L.hidden = e.target.checked ? L.hidden.filter((x) => x !== id) : [...L.hidden, id]; changed(); } }),
      h("span", {}, byId[id].title, HOME_EMPTY.has(id) ? h("span", { class: "muted" }, "Shows when there's something") : null)),
    h("span", { class: "arrows" },
      h("button", { type: "button", class: "small", "aria-label": `Move ${byId[id].title} up`, disabled: n === 0, onclick: () => move(id, -1) }, "↑"),
      h("button", { type: "button", class: "small", "aria-label": `Move ${byId[id].title} down`, disabled: n === ids.length - 1, onclick: () => move(id, 1) }, "↓")))));
  $("#arrange-reset").hidden = !L.custom;
  $("#arrange-default").hidden = !isAdmin() || !S.require_login;
}
(() => {  // dragging in the arrange list, with pointer events so it works with a finger too
  const list = $("#arrange-list");
  let drag = null;
  list.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest(".drag");
    if (!handle) return;
    e.preventDefault();
    drag = handle.closest("li");
    drag.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
  });
  list.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const over = [...list.children].find((li) => li !== drag && e.clientY > li.getBoundingClientRect().top && e.clientY < li.getBoundingClientRect().bottom);
    if (over) list.insertBefore(drag, e.clientY < over.getBoundingClientRect().top + over.offsetHeight / 2 ? over : over.nextSibling);
  });
  const drop = () => {
    if (!drag) return;
    drag.classList.remove("dragging");
    drag = null;
    const shown = [...list.children].map((li) => li.dataset.id), L = S.home_layout;
    let k = 0;  // put my tiles in their new order into the slots they held, leaving tiles I don't have where they were
    L.order = L.order.map((id) => shown.includes(id) ? shown[k++] : id);
    renderArrange(); applyHome(); saveHome();
  };
  list.addEventListener("pointerup", drop);
  list.addEventListener("pointercancel", drop);
})();
$("#home-arrange").addEventListener("click", () => { renderArrange(); $("#arrange-dialog").showModal(); });
$("#arrange-close").addEventListener("click", () => $("#arrange-dialog").close());
$("#arrange-reset").addEventListener("click", () => confirm("Go back to the family's layout?") && guarded(async () => {
  clearTimeout(saveTimer);
  S.home_layout = await api("DELETE", "me/home"); renderArrange(); applyHome(); toast("Back to the family layout");
}));
$("#arrange-default").addEventListener("click", () => confirm("New family logins (and anyone who resets) will start with your layout. Use it?") &&
  guarded(async () => { const { order, hidden, collapsed } = S.home_layout;
    await api("POST", "home/default", { layout: { order, hidden, collapsed } }); toast("Saved as the family's starting layout"); }));

// Today's news on Home: the brief's one line and its first stories.
function renderHomeNews() {
  const n = S.news_home;
  if (!n) return false;
  const go = (v) => { try { localStorage.setItem("bunkr-news-view", v); } catch {} showTab("brief"); };
  $("#home-news").replaceChildren(
    n.lead ? h("p", { class: "home-news-lead" }, n.lead, " ", h("button", { class: "link", onclick: () => go("brief") }, "Morning brief")) : null,
    n.stories.length ? h("ul", { class: "plain home-news" }, ...n.stories.map((t) => h("li", {},
      h("button", { class: "link story-link", onclick: () => go("feed") }, t.headline), h("span", { class: "muted" }, " · " + (t.place || t.area)))))
      : h("p", { class: "muted" }, "You're caught up."),
    h("p", {}, h("button", { class: "link", onclick: () => go("feed") }, n.unread ? `${n.unread} unread for you` : "Open the news"),
      " · ", h("button", { class: "link", onclick: () => go("map") }, "Map")));
  return true;
}
function syncCallForm() {
  $("#call-site-label").hidden = !["evacuate", "meet"].includes($("#call-kind").value);
}
$("#call-kind").addEventListener("change", syncCallForm);
$("#call-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, kind = f.kind.value, needs = ["evacuate", "meet"].includes(kind);
  if (needs && !S.places.length) return toast("Add places on Sites & alerts first", true);
  const label = f.kind.selectedOptions[0].textContent + (needs ? " " + f.site_id.selectedOptions[0].textContent : "");
  const drill = f.drill.checked;
  if (!confirm(drill ? `Start a DRILL: "${label}"? Everyone sees it marked as a drill.` : `Send "${label}" to the whole family?`)) return;
  guarded(async () => { await api("POST", "calls", { kind, site_id: needs ? f.site_id.value : "", message: f.message.value, drill });
    f.message.value = ""; f.drill.checked = false; toast(drill ? "Drill started" : "Sent to everyone"); refresh(); });
});
async function checkIn(personUid, status, forOther = false) {
  const body = { person_uid: personUid, status };
  if (!forOther) {
    const batt = await batteryNow();
    if (batt) { body.battery = batt.level; }
    if ($("#checkin-lowbat").checked) body.battery_low = true;
    body.note = $("#checkin-note").value;
    if ($("#checkin-flight").value.trim()) body.flight = $("#checkin-flight").value.trim();
    if ($("#checkin-loc").checked && navigator.geolocation) {
      const pos = await new Promise((res) => navigator.geolocation.getCurrentPosition(res, () => res(null), { timeout: 8000, maximumAge: 120000 }));
      if (pos) { body.lat = pos.coords.latitude; body.lon = pos.coords.longitude; } else toast("Couldn't get your location; sending without it", true);
    }
  }
  await guarded(async () => {
    const r = await api("POST", "checkin", body);
    if (!forOther) { $("#checkin-note").value = ""; $("#checkin-flight").value = ""; $("#checkin-lowbat").checked = false; }
    toast(r.flight ? "Checked in. Following your flight." : "Checked in");
    await refresh();
    if (!forOther && r.airport) flightPrompt(r.airport);
  });
}
// Android (Chrome) phones can say their battery level; iPhones can't, so they get a "My battery is low" box.
async function batteryNow() {
  try {
    if (!navigator.getBattery) return null;
    const b = await navigator.getBattery();
    return { level: Math.round(b.level * 100), charging: b.charging };
  } catch { return null; }
}
batteryNow().then((b) => {
  if (!b) return;
  $("#checkin-batt").hidden = false;
  $("#checkin-batt").textContent = `Your battery level goes with your check-in (now ${b.level}%), so the family knows if you might go quiet.`;
});
/* ---------- flights on the family board ---------- */
const FLIGHT_STATUS = { added: "Looking for it", scheduled: "Scheduled", taxiing: "On the ground", airborne: "In the air",
  landed: "Landed", cancelled: "Cancelled", diverted: "Diverted" };
const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
function flightBlock(f) {
  const me = S.me_person, t = f.times;
  const route = [f.origin_city || f.origin, f.dest_city || f.dest].filter(Boolean).join(" → ");
  const tone = { cancelled: "bad", diverted: "bad", landed: "good", airborne: "", taxiing: "", scheduled: "", added: "" }[f.status];
  const late = f.late_min != null && f.late_min >= 15 ? h("strong", { class: f.late_min >= 30 ? "bad" : "warn" }, ` ${f.late_min} min late`) : null;
  const dep = t.actual_out ? `Left ${clock(t.actual_out)}` : t.estimated_out || t.scheduled_out
    ? `Departs ${clock(t.estimated_out || t.scheduled_out)}` + (t.estimated_out && t.scheduled_out && t.estimated_out !== t.scheduled_out ? ` (was ${clock(t.scheduled_out)})` : "") : "";
  const arrIso = t.actual_in || t.actual_on || t.estimated_in || t.estimated_on || t.scheduled_in || t.scheduled_on;
  const arr = arrIso ? (t.actual_in || t.actual_on ? `Arrived ${clock(arrIso)}` : `Arrives ${clock(arrIso)}`) : "";
  const p = f.pos;
  const live = p ? [p.ground ? "On the ground" : `${Number(p.alt_ft || 0).toLocaleString()} ft`,
    p.gs_kt && !p.ground ? `${Math.round(p.gs_kt * 1.151)} mph` : "", p.km_to_go != null ? `${Math.round(p.km_to_go * 0.621)} mi to go` : "",
    p.age_min != null ? (p.age_min < 2 ? "just now" : `seen ${p.age_min} min ago`) : ""].filter(Boolean).join(" · ") : "";
  const g = f.gates;
  const gates = [g.gate_origin ? `Gate ${g.terminal_origin ? g.terminal_origin + " " : ""}${g.gate_origin}` : "",
    g.gate_destination ? `arrives gate ${g.terminal_destination ? g.terminal_destination + " " : ""}${g.gate_destination}` : "",
    g.baggage_claim ? `bags at ${g.baggage_claim}` : ""].filter(Boolean).join(" · ");
  const faa = Object.entries(f.faa || {}).map(([code, x]) => h("div", { class: "warn" }, `${code}: ${x.text}`));
  return h("div", { class: "flight" },
    h("div", { class: "flight-head" }, h("span", { "aria-hidden": "true" }, "✈ "), h("strong", {}, f.ident),
      route ? ` · ${route}` : "", " ", h("span", { class: "chip " + (tone || "") }, FLIGHT_STATUS[f.status] || f.status), late),
    dep || arr ? h("div", {}, [dep, arr].filter(Boolean).join(" · ")) : null,
    live ? h("div", {}, live, " ", h("a", { href: `https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}`, target: "_blank", rel: "noopener" }, "where")) : null,
    gates ? h("div", { class: "muted" }, gates) : null,
    ...faa,
    f.status === "added" ? h("div", { class: "muted" }, "Looking for it. Live position shows once the plane is heard; FlightAware times show if it's set up.") : null,
    !f.dest ? h("div", { class: "muted" }, "Destination unknown, so BUNKR can't tell when it lands. Stop following it when you're down.") : null,
    h("div", { class: "flight-links" }, "Track: ", ...Object.entries(f.links).flatMap(([name, url], i) =>
      [i ? " · " : "", h("a", { href: url, target: "_blank", rel: "noopener" }, name)]),
      (me && me.uid === f.person_uid) || S.can_call ? [" · ", h("button", { class: "link", onclick: () => confirm(`Stop following ${f.ident}?`) &&
        guarded(async () => { await api("POST", `flights/${f.uid}/end`); refresh(); }) }, "Stop following")] : null));
}
function flightPrompt(a) {
  // after checking in at an airport: offer to follow the flight
  const box = $("#flight-prompt");
  const input = h("input", { maxlength: "12", placeholder: "Flight, e.g. UA 1234", "aria-label": "Flight number", autocapitalize: "characters" });
  box.replaceChildren(h("p", {}, `You're at ${a.iata}. Flying? The family can follow your flight.`),
    a.home ? h("p", { class: "muted" }, `Not going to ${a.home}? Add the airport: "UA 1234 to LAX".`) : null,
    h("form", { class: "row gap", onsubmit: (e) => { e.preventDefault(); guarded(async () => {
      await api("POST", "flights", { number: input.value, origin: a.iata }); box.hidden = true; toast("Following your flight"); refresh(); }); } },
      input, h("button", { type: "submit", class: "primary" }, "Follow my flight"),
      h("button", { type: "button", class: "link", onclick: () => (box.hidden = true) }, "Not flying")));
  box.hidden = false;
  input.focus();
}
async function loadHA() {
  const card = $("#ha-card");
  card.hidden = !isAdmin();
  if (card.hidden) return;
  const r = await api("GET", "homeassistant").catch(() => null);
  if (!r) return;
  const f = $("#ha-form");
  if (!f.contains(document.activeElement)) {
    f.url.value = r.url; f.events.checked = r.events; f.actions.value = r.actions; f.sensors.value = r.sensors; f.token.value = "";
  }
  $("#ha-forget").hidden = !r.token;
  $("#ha-state").textContent = !r.url || !r.token ? "Not connected yet." : r.status.error ? `Last problem: ${r.status.error}`
    : r.status.last ? `Connected. Last event sent: ${r.status.last}.` : "Saved. Use Test the connection to check it.";
}
$("#ha-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, body = { url: f.url.value, events: f.events.checked, actions: f.actions.value, sensors: f.sensors.value };
  if (f.token.value.trim()) body.token = f.token.value.trim();
  guarded(async () => { await api("POST", "homeassistant", body); toast("Saved"); loadHA(); });
});
$("#ha-test").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "homeassistant/test"); toast(`Home Assistant: ${r.message}. A bunkr_test event was sent.`); loadHA();
}));
$("#ha-forget").addEventListener("click", () => confirm("Forget the Home Assistant token?") &&
  guarded(async () => { await api("POST", "homeassistant", { token: "" }); loadHA(); }));
async function loadFlightSettings() {
  const card = $("#flights-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "flights/settings").catch(() => null);
  if (!r) return;
  $("#flights-key-state").textContent = r.key
    ? `FlightAware is set up. ${r.lookups} of ${r.cap} lookups used this month.`
    : "Not set up: flights show live position from adsb.lol and FAA airport delays, but not the flight's own times or delays.";
  $("#flights-key-remove").hidden = !r.key;
}
$("#flights-key-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => { await api("POST", "flights/settings", { key: f.key.value }); f.key.value = ""; toast("Saved"); loadFlightSettings(); });
});
$("#flights-key-remove").addEventListener("click", () => confirm("Remove the FlightAware key?") &&
  guarded(async () => { await api("POST", "flights/settings", { key: "" }); loadFlightSettings(); }));
/* ---------- optional AI (admin, Household tab) ---------- */
async function loadAI() {
  const card = $("#ai-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "ai").catch(() => null);
  if (!r) return;
  const f = $("#ai-form");
  if (f.contains(document.activeElement)) return;
  f.provider.replaceChildren(...Object.entries(r.providers).map(([v, t]) => h("option", { value: v }, t)));
  for (const k of ["summaries", "connector"]) f[k].checked = r[k];
  for (const k of ["provider", "ollama_url", "model"]) f[k].value = r[k];
  aiShow(r);
}
let aiLast = null;
function aiShow(r) {
  if (r) aiLast = r;
  r = aiLast;
  const f = $("#ai-form"), ollama = f.provider.value === "ollama";
  $$(".ai-ollama", f).forEach((x) => { x.hidden = !ollama; });
  const cli = !r ? "" : r.cli_found ? `The hub has the AI command (${r.cli})` + (r.cli_login ? " and a login file." : " but no login file (ai.env).")
    : `The AI command (${r.cli}) isn't installed on the hub.`;
  const st = r && r.status && r.status.error ? ` Last try: ${r.status.error}` : "";
  $("#ai-note").textContent = (ollama ? "Ollama runs a free model on your own computer; nothing leaves your network." : cli) + st;
}
$("#ai-form").provider.addEventListener("change", () => aiShow(null));
$("#ai-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => {
    const r = await api("POST", "ai", { summaries: f.summaries.checked, connector: f.connector.checked, provider: f.provider.value,
      ollama_url: f.ollama_url.value, model: f.model.value });
    aiShow(r); toast("Saved");
  });
});
$("#ai-test").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "ai/test");
  toast(`AI answered: ${r.reply}`);
}));
/* ---------- names for scanned items (admin, Household tab) ---------- */
const naming = { pieces: [], pick: -1, default: "", fields: [], help: {}, timer: 0 };
const PUNCT_NAMES = { ", ": "comma", " ": "space", " - ": "dash", " / ": "slash", " | ": "bar", ": ": "colon", "; ": "semicolon", ".": "dot" };
const namingSplit = (t) => (t || "").split(/(\{\w+\}|[\[\]()])/).filter(Boolean);
function namingDraw() {
  const box = $("#naming-pieces");
  box.replaceChildren(...naming.pieces.map((p, i) => {
    const f = /^\{(\w+)\}$/.exec(p);
    const b = h("button", { type: "button", class: "chip-btn" + (f ? " name-field" : " name-punct") + (i === naming.pick ? " picked" : ""),
      title: f ? naming.help[f[1]] || "" : PUNCT_NAMES[p] || "" },
      f ? f[1][0].toUpperCase() + f[1].slice(1) : (p.trim() || "space"));
    b.addEventListener("click", () => { naming.pick = naming.pick === i ? -1 : i; namingDraw(); });
    return b;
  }));
  if (!naming.pieces.length) box.append(h("span", { class: "muted" }, "Empty: the store's own name is kept."));
  $("#naming-move").hidden = naming.pick < 0;
  clearTimeout(naming.timer);
  naming.timer = setTimeout(namingPreview, 250);
}
function namingBody() {
  const f = $("#naming-form");
  return { template: naming.pieces.join(""), caps: f.caps.value, joiner: f.joiner.value };
}
async function namingPreview() {
  const prob = $("#naming-problem");
  try {
    const r = await api("POST", "naming/preview", namingBody());
    $("#naming-examples").replaceChildren(...r.examples.map((x) => h("li", {}, x)));
    prob.hidden = true;
  } catch (e) { prob.textContent = e.message; prob.hidden = false; }
}
function namingSet(template) { naming.pieces = namingSplit(template); naming.pick = -1; namingDraw(); }
function namingAdd(piece) {
  const at = naming.pick < 0 ? naming.pieces.length : naming.pick + 1;
  naming.pieces.splice(at, 0, piece);
  naming.pick = at;
  namingDraw();
}
async function loadNaming() {
  const card = $("#naming-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "naming").catch(() => null);
  if (!r) return;
  const f = $("#naming-form");
  if (f.contains(document.activeElement)) return;
  Object.assign(naming, { default: r.default, fields: r.fields, help: r.help });
  f.preset.replaceChildren(h("option", { value: "" }, "Pick a style…"), ...r.presets.map((p) => h("option", { value: p.template }, p.name)));
  f.add_field.replaceChildren(h("option", { value: "" }, "Add…"), ...r.fields.map((x) => h("option", { value: `{${x}}` }, `${x[0].toUpperCase() + x.slice(1)}: ${r.help[x]}`)));
  f.caps.replaceChildren(...Object.entries(r.caps_options).map(([v, t]) => h("option", { value: v }, t)));
  f.joiner.replaceChildren(...Object.entries(r.joiners).map(([v, t]) => h("option", { value: v }, t)));
  f.caps.value = r.caps;
  f.joiner.value = r.joiner;
  namingSet(r.template);
}
$("#naming-form").preset.addEventListener("change", (e) => { if (e.target.value) namingSet(e.target.value); e.target.value = ""; });
$("#naming-form").add_field.addEventListener("change", (e) => { if (e.target.value) namingAdd(e.target.value); e.target.value = ""; });
$("#naming-form").add_punct.addEventListener("change", (e) => { if (e.target.value) namingAdd(e.target.value); e.target.value = ""; });
$("#naming-form").caps.addEventListener("change", namingDraw);
$("#naming-form").joiner.addEventListener("change", namingDraw);
$("#naming-move").addEventListener("click", (e) => {
  const m = e.target.closest("[data-move]");
  if (!m || naming.pick < 0) return;
  const d = +m.dataset.move, i = naming.pick, p = naming.pieces;
  if (d === 0) { p.splice(i, 1); naming.pick = Math.min(i, p.length - 1); }
  else if (p[i + d] !== undefined) { [p[i], p[i + d]] = [p[i + d], p[i]]; naming.pick = i + d; }
  namingDraw();
});
$("#naming-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { await api("POST", "naming", namingBody()); toast("Saved"); });
});
$("#naming-default").addEventListener("click", () => {
  const f = $("#naming-form");
  f.caps.value = ""; f.joiner.value = ", ";
  namingSet(naming.default);
});
/* ---------- barcode lookup sources (admin, Household tab) ---------- */
async function loadFoodSources() {
  const card = $("#foodsources-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "foodsources").catch(() => null);
  if (!r) return;
  const f = $("#foodsources-form");
  for (const k of ["usda", "upcitemdb"]) f[k].checked = r.sources[k];
  $("#usda-forget").hidden = !r.usda_own_key;
  const st = Object.entries(r.status || {}).filter(([, v]) => !v.ok).map(([k, v]) => `${r.names[k]}: ${v.note}`);
  $("#foodsources-note").textContent = (r.usda_own_key ? "Using your own USDA key. " : "") +
    (r.label_reader ? "Label reading is on (Tesseract on the hub)." : "Label reading is off: install tesseract-ocr on the hub to read nutrition labels.") +
    (st.length ? " Last problems: " + st.join("; ") : "");
}
$("#foodsources-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, body = { usda: f.usda.checked, upcitemdb: f.upcitemdb.checked };
  if (f.usda_key.value.trim()) body.usda_key = f.usda_key.value.trim();
  guarded(async () => { await api("POST", "foodsources", body); f.usda_key.value = ""; toast("Saved"); loadFoodSources(); });
});
$("#usda-forget").addEventListener("click", () => guarded(async () => { await api("POST", "foodsources", { forget_usda_key: true }); loadFoodSources(); }));
/* ---------- read calories off a nutrition label photo (any family member) ---------- */
function labelShow() {
  const f = $("#item-form");
  $("#label-read").hidden = f.category.value !== "food" || $("#per-unit-label").hidden;
}
$("#item-form").category.addEventListener("change", labelShow);
$("#label-read-btn").addEventListener("click", () => $("#label-file").click());
async function shrinkPhoto(file) {
  // big phone photos: scale to at most 2000 px and grey, which reads as well and uploads fast
  const img = await createImageBitmap(file);
  const k = Math.min(1, 2000 / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
  const g = c.getContext("2d");
  g.filter = "grayscale(1) contrast(1.3)";
  g.drawImage(img, 0, 0, c.width, c.height);
  return await new Promise((ok) => c.toBlob(ok, "image/jpeg", 0.9));
}
$("#label-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const msg = $("#label-msg"), f = $("#item-form");
  msg.textContent = "Reading the label…";
  try {
    const blob = await shrinkPhoto(file).catch(() => file);
    const res = await fetch("/api/label", { method: "POST", headers: { "X-Bunkr": "1", "Content-Type": "image/jpeg" }, body: blob });
    const r = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(r.error || "couldn't read that photo");
    if (!r.kcal) { msg.textContent = "Couldn't find the calories. Try again closer, flat and in good light, or type them in."; return; }
    let servings = r.servings;
    if (!servings) {
      const n = prompt(`${r.kcal} calories per serving. How many servings in one?`, "1");
      servings = n && +n > 0 ? +n : 0;
      if (!servings) { msg.textContent = `${r.kcal} calories per serving: type the total for one.`; return; }
    }
    const total = Math.round(r.kcal * servings);
    f.size_value.value = total;
    updateItemPreview();
    msg.textContent = `Label: ${r.kcal} calories × ${servings} servings = ${total} each. Check it before saving.`;
  } catch (err) { msg.textContent = err.message; }
});
/* ---------- Kroger barcode lookup (admin, Household tab) ---------- */
async function loadKroger() {
  const card = $("#kroger-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "kroger").catch(() => null);
  if (!r) return;
  const f = $("#kroger-form");
  if (!f.contains(document.activeElement)) {
    f.client_id.value = r.client_id || ""; f.client_secret.value = "";
    f.client_secret.placeholder = r.has_secret ? "saved: leave empty to keep it" : "from developer.kroger.com";
  }
  $("#kroger-forget").hidden = !r.has_secret;
  const s = r.status || {};
  $("#kroger-status").textContent = !r.ready ? "Not set up." : s.ok === false ? `Last problem: ${s.error}`
    : s.at ? `Working. Last lookup ${sinceIso(s.at)}.` : "Set up. Scan something, or press Test it.";
}
$("#kroger-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, body = { client_id: f.client_id.value.trim() };
  if (f.client_secret.value.trim()) body.client_secret = f.client_secret.value.trim();
  guarded(async () => { await api("POST", "kroger", body); f.client_secret.value = ""; toast("Saved"); loadKroger(); });
});
$("#kroger-test").addEventListener("click", () => guarded(async () => {
  await api("POST", "kroger/test"); toast("Kroger answered. Barcode lookup is working."); loadKroger();
}).finally(loadKroger));
$("#kroger-forget").addEventListener("click", () => confirm("Forget the Kroger secret? Lookups there stop until a new one is saved.") &&
  guarded(async () => { await api("POST", "kroger", { forget_secret: true }); loadKroger(); }));
/* ---------- local watch (admin, Sites tab) ---------- */
async function loadLocalWatch() {
  const card = $("#lw-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "localwatch").catch(() => null);
  if (!r) return;
  $("#lw-places").replaceChildren(...(r.places.length ? r.places.map((p) => {
    const st = (r.news || {})[p.name];
    return h("li", {}, h("strong", {}, p.label), p.state ? `, ${p.state}` : "",
      st ? h("span", { class: st.ok ? "muted" : "warn" }, st.ok ? ` · ${st.count} headline${st.count === 1 ? "" : "s"} today` : ` · ${st.error}`) : null);
  }) : [h("li", { class: "muted" }, "No towns yet. Give sites a full address (\"…, Nederland, CO 80466\") and set where each person lives on the Family tab.")]));
  const f = $("#lw-form");
  if (!f.contains(document.activeElement)) {
    f.extra_places.value = r.extra_places.join("\n"); f.subreddits.value = r.subreddits.join("\n");
    f.bluesky.checked = !!r.bluesky; f.reddit_id.value = r.reddit_id || ""; f.reddit_secret.value = "";
    f.reddit_secret.placeholder = r.has_secret ? "saved: leave empty to keep it" : "from reddit.com/prefs/apps";
  }
  $("#lw-forget").hidden = !r.has_secret;
  const s = r.status || {}, line = (k, name) => s[k] ? `${name}: ${s[k].ok ? `${s[k].count} matching post${s[k].count === 1 ? "" : "s"}` : s[k].error}` : "";
  $("#lw-status").textContent = [line("bluesky", "Bluesky"), r.reddit_ready ? line("reddit", "Reddit") || "Reddit: set up, waiting for the next check"
    : "Reddit: not set up"].filter(Boolean).join(" · ");
}
$("#lw-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, body = { extra_places: f.extra_places.value, subreddits: f.subreddits.value, bluesky: f.bluesky.checked, reddit_id: f.reddit_id.value.trim() };
  if (f.reddit_secret.value.trim()) body.reddit_secret = f.reddit_secret.value.trim();
  guarded(async () => { await api("POST", "localwatch", body); f.reddit_secret.value = ""; toast("Saved"); loadLocalWatch(); });
});
$("#lw-run").addEventListener("click", () => guarded(async () => { await api("POST", "localwatch/run"); toast("Checking now. Give it a minute."); setTimeout(loadLocalWatch, 30000); }));
$("#lw-forget").addEventListener("click", () => confirm("Forget the Reddit secret? Reddit stops until a new one is saved.") &&
  guarded(async () => { await api("POST", "localwatch", { forget_secret: true }); loadLocalWatch(); }));

/* ---------- BUNKR version: which one this is, and whether a newer one is out (admins choose when to upgrade) ---------- */
async function loadVersion() {
  const r = await api("GET", "version").catch(() => null);
  if (!r) return;
  $("#version-now").textContent = `This BUNKR is version ${r.current}.`;
  const admin = isAdmin() && r.latest !== undefined;
  $("#version-admin").hidden = !admin;
  if (!admin) return;
  const field = ["pi", "laptop"].includes(S.node_role);
  const L = r.latest || {};
  $("#version-new").hidden = !r.available;
  if (r.available) {
    $("#version-avail").replaceChildren(h("strong", {}, `Version ${L.version} is available`),
      L.published_at ? ` (released ${new Date(L.published_at).toLocaleDateString()})` : "", r.skipped ? h("span", { class: "muted" }, " · you skipped it") : "",
      L.url ? [" · ", h("a", { href: L.url, target: "_blank", rel: "noopener" }, "release page")] : null);
    $("#version-notes").textContent = L.notes || "No notes with this release.";
    $("#version-skip").textContent = r.skipped ? "Stop skipping it" : "Skip this version";
    $("#version-skip").onclick = () => guarded(async () => { await api("POST", "version/settings", { skip: r.skipped ? "" : L.version }); loadVersion(); });
  }
  $("#version-auto").checked = !!r.enabled;
  $("#version-auto").disabled = field || !r.repo;
  $("#version-check").hidden = field || !r.repo;
  $("#version-state").textContent = field ? "The hub checks for new versions."
    : !r.repo ? "No project address is set, so this BUNKR doesn't check for new versions."
    : !r.enabled ? "Not checking for new versions."
    : r.error ? `Couldn't check: ${r.error}`
    : r.checked_at ? `Checked ${sinceIso(r.checked_at)}.${r.available ? "" : " You have the latest."}` : "Checks once a day.";
}
$("#version-check").addEventListener("click", () => guarded(async () => { await api("POST", "version/check"); loadVersion(); }));
$("#version-auto").addEventListener("change", (e) => guarded(async () => { await api("POST", "version/settings", { enabled: e.target.checked }); loadVersion(); }));

/* ---------- email for reminders (admin) ---------- */
async function loadMail() {
  const card = $("#mail-card");
  card.hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  if (card.hidden) return;
  const r = await api("GET", "mail").catch(() => null);
  if (!r) return;
  const f = $("#mail-form");
  if (!f.contains(document.activeElement)) {
    $("#mail-security").replaceChildren(...Object.entries(r.security_options).map(([k, v]) => h("option", { value: k }, v)));
    for (const k of ["host", "port", "security", "username", "from"]) f[k].value = r[k] ?? "";
    f.password.value = "";
    f.password.placeholder = r.has_password ? "saved: leave empty to keep it" : "the account's app password";
  }
  $("#mail-forget").hidden = !r.has_password;
  $("#mail-state").className = r.last_error ? "warn" : "muted";
  $("#mail-state").textContent = !r.ready ? "Not set up: reminders go as notifications only."
    : r.last_error ? `${r.waiting} waiting to send. Last problem: ${r.last_error}`
    : `Set up. ${r.waiting ? r.waiting + " waiting to send." : "Nothing waiting."}`;
}
$("#mail-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, body = Object.fromEntries(["host", "port", "security", "username", "from"].map((k) => [k, f[k].value.trim()]));
  if (f.password.value) body.password = f.password.value;
  guarded(async () => { await api("POST", "mail", body); f.password.value = ""; toast("Saved"); loadMail(); });
});
$("#mail-test").addEventListener("click", () => guarded(async () => {
  const to = (PUSH && PUSH.prefs && PUSH.prefs.email) || prompt("Send a test to which address?");
  if (!to) return;
  await api("POST", "mail/test", { to }); toast("Sent. Check your inbox (and spam)."); loadMail();
}));
$("#mail-forget").addEventListener("click", () => confirm("Forget the mail password? Emails stop until a new one is saved.") &&
  guarded(async () => { await api("POST", "mail", { forget_password: true }); loadMail(); }));
// keep the board fresh while Home is open
setInterval(() => { if (S && !document.hidden && !$("#tab-home").hidden)
  api("GET", "board").then((b) => { S.board = b; renderHome(); }).catch(() => {}); }, 30000);

/* ---------- notifications on this phone ---------- */
let PUSH = null;
const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function keyBytes(b64) {
  const s = atob((b64 + "=".repeat((4 - b64.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
async function pushSub() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.register("/sw.js");
  return reg.pushManager.getSubscription();
}
async function loadPush() {
  if (!S.require_login || !S.me.id) return;
  PUSH = await api("GET", "push/key").catch(() => null);
  const sub = await pushSub().catch(() => null);
  const ok = pushSupported() && PUSH && PUSH.key;
  $("#push-ios").hidden = !(isIOS() && !standalone());
  $("#push-toggle").hidden = !ok;
  $("#push-toggle").textContent = sub ? "Turn off" : "Turn on";
  $("#push-test").hidden = !sub;
  $("#push-status").textContent = !pushSupported() ? "This browser can't receive notifications from BUNKR."
    : !PUSH || !PUSH.key ? "Notifications aren't set up on this BUNKR yet."
    : Notification.permission === "denied" ? "Notifications are blocked for BUNKR in this browser's settings."
    : sub ? `On for this phone.${PUSH.phones > 1 ? ` You have ${PUSH.phones} devices set up.` : ""}` : "Off on this phone.";
  const f = $("#push-prefs");
  if (PUSH && !f.contains(document.activeElement)) {
    const p = PUSH.prefs;
    $("#push-checkins").replaceChildren(...Object.entries(PUSH.modes).map(([k, v]) => h("option", { value: k }, v)));
    f.priority.checked = !!p.priority; f.priority_cap.value = p.priority_cap; f.checkins.value = p.checkins;
    f.brief.checked = !!p.brief; f.quiet_start.value = p.quiet_start; f.quiet_end.value = p.quiet_end;
    $("#push-reminders").replaceChildren(...Object.entries(PUSH.reminder_ways || {}).map(([k, v]) => h("option", { value: k }, v)));
    f.reminders_by.value = p.reminders_by || "push"; f.email.value = p.email || "";
    const wantsEmail = p.reminders_by !== "push";
    $("#push-email-note").hidden = !(wantsEmail && (!PUSH.email_ready || !p.email));
    $("#push-email-note").textContent = !PUSH.email_ready ? "Email isn't set up on this BUNKR yet: an admin adds the mail account on the Household tab. Until then reminders come as notifications."
      : "Add your email address, or reminders keep coming as notifications.";
  }
}
$("#push-toggle").addEventListener("click", () => guarded(async () => {
  const sub = await pushSub();
  if (sub) {
    await api("POST", "push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {});
    await sub.unsubscribe();
    toast("Notifications off on this phone");
  } else {
    if (await Notification.requestPermission() !== "granted") throw new Error("Notifications weren't allowed");
    const reg = await navigator.serviceWorker.ready;
    const fresh = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(PUSH.key) });
    await api("POST", "push/subscribe", { subscription: fresh.toJSON(), tz: Intl.DateTimeFormat().resolvedOptions().timeZone });
    toast("Notifications on");
  }
  loadPush();
}));
$("#push-test").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "push/test");
  toast(r.sent ? "Test sent. It should arrive in a few seconds." : "The push service didn't accept it; try turning notifications off and on.", !r.sent);
}));
$("#push-prefs").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => { PUSH.prefs = await api("POST", "push/prefs", { priority: f.priority.checked, priority_cap: f.priority_cap.value,
    checkins: f.checkins.value, brief: f.brief.checked, quiet_start: f.quiet_start.value, quiet_end: f.quiet_end.value,
    reminders_by: f.reminders_by.value, email: f.email.value.trim(),
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone }); toast("Saved"); loadPush(); });
});

/* ---------- news: a feed for each person, a map of unread stories, and the morning brief ---------- */
const NEWS = { view: "feed", area: "", topic: "", unread: false, offset: 0, data: null, zoom: "world", mapSvg: null };
const STATE_OWNERS_N = ["state-run", "state-funded", "state-owned", "state-aligned", "close to the state", "government-linked"];
function showNewsView(v) {
  NEWS.view = v;
  $$(".news-views button").forEach((b) => { b.classList.toggle("on", b.dataset.nv === v); b.setAttribute("aria-pressed", String(b.dataset.nv === v)); });
  $("#news-feed-view").hidden = v !== "feed";
  $("#news-map-view").hidden = v !== "map";
  $("#news-brief-view").hidden = v !== "brief";
  $("#news-chatter-view").hidden = v !== "chatter";
  try { localStorage.setItem("bunkr-news-view", v); } catch {}
  if (v === "chatter") loadChatter();
  if (v === "feed") loadNews();
  if (v === "map") loadNewsMap();
  if (v === "brief") loadBrief();
}
$(".news-views").addEventListener("click", (e) => { const b = e.target.closest("button[data-nv]"); if (b) showNewsView(b.dataset.nv); });
function loadNewsTab() {
  let v = "feed";
  try { v = localStorage.getItem("bunkr-news-view") || v; } catch {}
  showNewsView(["feed", "map", "brief", "chatter"].includes(v) ? v : "feed");
}
/* ---------- unconfirmed: public posts about the family's towns ---------- */
async function loadChatter() {
  const r = await guarded(() => api("GET", "news/chatter"));
  if (!r) return;
  $("#chatter-places").textContent = r.places.length ? `Watching: ${r.places.join(" · ")}.`
    : "No family towns yet: add addresses on Sites & alerts (with who lives there), or towns in Local watch there.";
  const src = (p) => p.source === "reddit" ? p.channel : "Bluesky";
  $("#chatter-list").replaceChildren(...(r.posts.length ? r.posts.map((p) => h("article", { class: "card chatter" + (p.story ? " reported" : "") },
    h("div", { class: "story-meta" }, h("span", { class: "tag" }, p.place), h("span", { class: "tag warn" }, p.alarm),
      p.story ? h("span", { class: "tag ok" }, "Now reported" + (p.reported_by ? ` by ${p.reported_by}` : "")) : h("span", { class: "tag" }, "Unconfirmed")),
    h("p", { class: "chatter-text" }, p.text),
    h("div", { class: "muted" }, `${src(p)} · ${p.author || "someone"} · ${sinceIso(p.at)} · `,
      h("a", { href: p.url, target: "_blank", rel: "noopener" }, "open"),
      p.story ? [" · ", h("span", {}, p.story_headline)] : null)))
    : [h("div", { class: "card" }, h("p", { class: "muted" }, "Nothing right now. Posts show up here when someone names a family town and mentions trouble."))]));
}

const areaName = (a) => (NEWS.data ? Object.fromEntries(NEWS.data.areas) : {})[a] || a;
const topicName = (t) => (NEWS.data ? Object.fromEntries(NEWS.data.topics) : {})[t] || t;
function storyCard(s) {
  const O = (NEWS.data && NEWS.data.outlets) || {};
  const label = (k) => O[k] ? `${O[k].name} · ${O[k].country}, ${O[k].owner}` : k;
  const card = h("article", { class: "card story-card" + (s.read ? "" : " unread"), "data-id": s.id });
  const markRead = () => { if (s.read) return; s.read = true; card.classList.remove("unread");
    api("POST", "news/read", { ids: [s.id] }).catch(() => {}); };
  const rateBtn = (v, text, label) => h("button", { type: "button", class: "small rate" + (s.rating === v ? " on" : ""), "aria-pressed": String(s.rating === v),
    "aria-label": label, title: label,
    onclick: () => guarded(async () => {
      const r = await api("POST", "news/rate", { id: s.id, rating: s.rating === v ? 0 : v });
      s.rating = r.rating; s.read = true;
      if (r.rating < 0) { card.remove(); toast("Hidden. Your feed will show fewer like it."); }
      else card.replaceWith(storyCard(s));
    }) }, text);
  const where = s.place || areaName(s.area);
  card.append(
    h("div", { class: "story-meta" }, h("span", { class: "tag area-" + s.area }, where), h("span", { class: "tag" }, topicName(s.topic)),
      s.alarm ? h("span", { class: "tag warn", title: "Raised early: BUNKR flagged this from the first outlet that reported it. Details may change." }, "Developing") : null,
      h("span", { class: "story-acts" }, rateBtn(1, "\u{1F44D}", "Relevant to me"), rateBtn(-1, "\u{1F44E}", "Not for me"))),
    h("details", { ontoggle: (e) => e.target.open && markRead() },
      h("summary", { class: "story-title" }, s.headline,
        h("span", { class: "story-count" }, `${s.outlets} outlet${s.outlets === 1 ? "" : "s"}` + (s.regions.length > 1 ? `, ${s.regions.length} regions` : "")
          + " \u00b7 " + sinceIso(s.last_at))),
      h("ul", { class: "plain story-srcs" }, ...(s.items || []).map((i) => h("li", {},
        h("a", { href: i.url, target: "_blank", rel: "noopener", onclick: markRead }, i.title),
        h("div", { class: "muted" }, label(i.outlet), O[i.outlet] && STATE_OWNERS_N.includes(O[i.outlet].owner) ? h("span", { class: "tag state" }, "state media") : null,
          (s.items || []).length > 1 ? [" · ", flagButton({ kind: "feed", story: s.id, url: i.url }, i.title, s.headline, () => loadNews())] : null))))));
  return card;
}
// "This source doesn't belong to this story": it comes out at once; an admin reviews the flag
function flagButton(target, sourceTitle, storyTitle, after) {
  return h("button", { type: "button", class: "link flag-btn", title: "This source is about something else",
    onclick: () => {
      const note = prompt(`Take this out of "${storyTitle}"?\n\n"${sourceTitle}"\n\nWhat's wrong? (optional)`, "");
      if (note === null) return;
      guarded(async () => {
        const r = await api("POST", "news/flags", { ...target, note });
        const where = r.moved_to && r.moved_to !== sourceTitle ? ` Moved to "${r.moved_to.slice(0, 70)}".` : " It's now a story of its own.";
        toast((r.reviewed ? "Taken out. BUNKR will match more carefully." : "Taken out, and sent for review. Thanks.") + where);
        after && after();
      });
    } }, "Doesn't belong");
}
async function loadNewsFlags() {
  const box = $("#news-flags");
  if (!isAdmin()) { box.hidden = true; return; }
  const r = await api("GET", "news/flags").catch(() => null);
  const open = r ? r.flags.filter((f) => f.status === "open") : [];
  const done = r ? r.flags.filter((f) => f.status === "confirmed" && f.kind === "feed").slice(0, 5) : [];
  box.hidden = !open.length && !done.length;
  if (box.hidden) return;
  const decide = (f, decision) => guarded(async () => { await api("POST", "news/flags/" + f.id, { decision });
    toast(decision === "confirm" ? "Confirmed. BUNKR will keep these apart." : decision === "undo" ? "Undone: put back, and the lesson forgotten." : "Put back.");
    loadNewsFlags(); loadNews(); });
  const taught = (f) => f.apart ? h("div", { class: "muted" }, `Keeps apart: ${f.apart[0].join(", ")} | ${f.apart[1].join(", ")}`) : null;
  box.replaceChildren(h("h2", {}, open.length ? `Flagged sources to review (${open.length})` : "News flags"),
    h("p", { class: "muted" }, "Someone said a source isn't about its story. It's already out of the story; confirm, or put it back."),
    h("ul", { class: "plain flag-list" }, ...open.map((f) => h("li", {},
      h("div", {}, h("span", { class: "muted" }, f.kind === "brief" ? `Morning brief ${f.day}: ` : "Story: "), h("strong", {}, f.story_title)),
      h("div", {}, h("span", { class: "muted" }, "Source: "), h("a", { href: f.url, target: "_blank", rel: "noopener" }, f.source_title || f.url),
        f.outlet ? h("span", { class: "muted" }, ` · ${f.outlet}`) : null),
      h("div", { class: "muted" }, `${f.username || "someone"}, ${sinceIso(f.at)}`, f.note ? `: "${f.note}"` : "",
        f.words.length ? ` · matched on: ${f.words.join(", ")}` : ""),
      h("div", { class: "row gap" }, h("button", { class: "primary", onclick: () => decide(f, "confirm") }, "Right, it doesn't belong"),
        h("button", { onclick: () => decide(f, "dismiss") }, "It's fine, put it back"))))),
    done.length ? h("details", {}, h("summary", {}, `What BUNKR learned from flags (${done.length} recent)`),
      h("ul", { class: "plain flag-list" }, ...done.map((f) => h("li", {},
        h("div", {}, h("strong", {}, f.source_title), h("span", { class: "muted" }, " isn't about "), h("strong", {}, f.story_title)),
        taught(f), h("button", { class: "link", onclick: () => decide(f, "undo") }, "Undo"))))) : null);
}
async function loadNews(more = false) {
  if (!more) NEWS.offset = 0;
  const qs = new URLSearchParams({ limit: 30, offset: NEWS.offset });
  if (NEWS.area) qs.set("area", NEWS.area);
  if (NEWS.topic) qs.set("topic", NEWS.topic);
  if (NEWS.unread) qs.set("unread", "1");
  const r = await guarded(() => api("GET", "news?" + qs));
  if (!r) return;
  NEWS.data = r;
  if (!more) loadNewsFlags();
  const follow = new Set(r.prefs.areas), counts = r.unread_by_area;
  const chip = (key, text, n) => h("button", { type: "button", class: "chip-btn" + (NEWS.area === key ? " on" : ""), "aria-pressed": String(NEWS.area === key),
    onclick: () => { NEWS.area = key; loadNews(); } }, text, n ? h("span", { class: "count" }, n) : null);
  const total = Object.entries(counts).filter(([a]) => follow.has(a)).reduce((t, [, n]) => t + n, 0);
  $("#news-areas").replaceChildren(chip("", "All I follow", total),
    ...r.areas.filter(([a]) => follow.has(a) || NEWS.area === a).map(([a, name]) => chip(a, name, counts[a])));
  if (!$("#news-topic").options.length)
    $("#news-topic").replaceChildren(h("option", { value: "" }, "All topics"), ...r.topics.map(([t, name]) => h("option", { value: t }, name)));
  $("#news-topic").value = NEWS.topic;
  const cards = r.stories.map(storyCard);
  if (more) $("#news-list").append(...cards);
  else $("#news-list").replaceChildren(...(cards.length ? cards : [h("div", { class: "card" }, h("p", { class: "caught-up" },
    r.updated_at ? "You're caught up." : "No stories yet."), h("p", { class: "muted" },
    r.updated_at ? "Nothing new here. Try another place or topic, or untick Unread only." : "Headlines are gathered on the hub: breaking and specialist news every 10 minutes, the rest hourly."))]));
  NEWS.offset += r.stories.length;
  $("#news-more").hidden = NEWS.offset >= r.total;
  $("#news-foot").textContent = r.updated_at ? `Updated ${sinceIso(r.updated_at)}. Breaking news every 10 minutes, the rest hourly.` : "";
}
$("#news-topic").addEventListener("change", (e) => { NEWS.topic = e.target.value; loadNews(); });
$("#news-unread").addEventListener("change", (e) => { NEWS.unread = e.target.checked; loadNews(); });
$("#news-more").addEventListener("click", () => loadNews(true));
$("#news-allread").addEventListener("click", () => guarded(async () => {
  const ids = $$("#news-list .story-card.unread").map((c) => c.dataset.id);
  if (!ids.length) return toast("Nothing unread here");
  await api("POST", "news/read", { ids }); toast(`Marked ${ids.length} read`); loadNews();
}));
$("#news-prefs-btn").addEventListener("click", () => guarded(async () => {
  const p = await api("GET", "me/news"), d = NEWS.data;
  $("#news-prefs-areas").replaceChildren(...d.areas.map(([a, name]) => h("label", { class: "check" },
    h("input", { type: "checkbox", name: "area", value: a, checked: p.areas.includes(a) }), name)));
  $("#news-prefs-topics").replaceChildren(...d.topics.map(([t, name]) => h("label", { class: "check" },
    h("input", { type: "checkbox", name: "topic", value: t, checked: !p.muted_topics.includes(t) }), name)));
  $("#news-prefs-form").watch.value = (p.watch || []).join("\n");
  $("#news-prefs-dialog").showModal();
}));
$("#news-prefs-close").addEventListener("click", () => $("#news-prefs-dialog").close());
$("#news-prefs-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const areas = $$("input[name=area]", f).filter((x) => x.checked).map((x) => x.value);
  const muted = $$("input[name=topic]", f).filter((x) => !x.checked).map((x) => x.value);
  if (!areas.length) return toast("Follow at least one place", true);
  const watch = f.watch.value.split("\n").map((w) => w.trim()).filter(Boolean);
  guarded(async () => { await api("POST", "me/news", { areas, muted_topics: muted, watch }); $("#news-prefs-dialog").close();
    NEWS.area = ""; toast("Saved"); loadNews(); NEWS.mapSvg && loadNewsMap(); });
});

// The map: a small built-in world outline (equirectangular, x = (lon+180)*2, y = (84-lat)*2), bubbles for unread stories.
const VIEWS = { world: [0, 0, 720, 284], us: [100, 60, 140, 66], co: [138.5, 83, 21, 14] };
const proj = (lon, lat) => [(lon + 180) * 2, (84 - lat) * 2];
async function loadNewsMap() {
  const box = $("#newsmap");
  if (!NEWS.mapSvg) {
    const r = await fetch("/world.svg");
    box.innerHTML = await r.text();  // our own static file
    NEWS.mapSvg = box.querySelector("svg");
    const ns = "http://www.w3.org/2000/svg";
    const [x1, y1] = proj(-109.05, 41), [x2, y2] = proj(-102.04, 37);
    const co = document.createElementNS(ns, "rect");
    Object.entries({ x: x1, y: y1, width: x2 - x1, height: y2 - y1, class: "co" }).forEach(([k, v]) => co.setAttribute(k, v));
    NEWS.mapSvg.append(co, document.createElementNS(ns, "g"));
    NEWS.mapSvg.lastChild.id = "bubbles";
    NEWS.mapSvg.setAttribute("role", "img");
    NEWS.mapSvg.setAttribute("aria-label", "World map of unread stories");
    if (!NEWS.data) NEWS.data = await api("GET", "news?limit=1").catch(() => null);
  }
  const r = await guarded(() => api("GET", "news/map" + ($("#map-all").checked ? "?all=1" : "")));
  if (!r) return;
  NEWS.points = r.points;
  drawBubbles();
  $("#map-list").replaceChildren();
}
function drawBubbles() {
  const svg = NEWS.mapSvg, v = VIEWS[NEWS.zoom], ns = "http://www.w3.org/2000/svg";
  svg.setAttribute("viewBox", v.join(" "));
  const W = svg.clientWidth || 360, H = svg.clientHeight || W * 284 / 720;
  const px = 1 / Math.min(W / v[2], H / v[3]);  // map units per screen pixel (the view is fitted inside the box)
  const all = $("#map-all").checked;
  // merge places closer than a bubble's width at this zoom
  const groups = [];
  for (const p of NEWS.points) {
    const [x, y] = proj(p.lon, p.lat), n = all ? p.count : p.unread;
    if (!n || (NEWS.zoom === "co" && p.place === "United States")) continue;  // the nationwide pin sits in Kansas
    const g = groups.find((q) => Math.hypot(q.x - x, q.y - y) < 26 * px);
    if (g) { g.ids.push(...p.ids); g.n += n; g.places.push(p.place); if (n > g.top) { g.top = n; } }
    else groups.push({ x, y, n, ids: [...p.ids], places: [p.place], top: n });
  }
  const layer = svg.querySelector("#bubbles");
  layer.replaceChildren(...groups.sort((a, b) => b.n - a.n).map((g) => {
    const r = (10 + Math.min(10, Math.log2(g.n) * 3)) * px;
    const el = document.createElementNS(ns, "g");
    el.setAttribute("class", "bubble");
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    const names = [...new Set(g.places)];
    el.setAttribute("aria-label", `${g.n} ${all ? "" : "unread "}stor${g.n === 1 ? "y" : "ies"}: ${names.slice(0, 4).join(", ")}`);
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", g.x); c.setAttribute("cy", g.y); c.setAttribute("r", r);
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", g.x); t.setAttribute("y", g.y); t.setAttribute("font-size", 11 * px);
    t.textContent = g.n;
    const title = document.createElementNS(ns, "title");
    title.textContent = names.slice(0, 6).join(", ");
    el.append(title, c, t);
    const open = () => showMapStories(g, names);
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return el;
  }));
  $("#map-note").textContent = groups.length ? "Each bubble is how many " + (all ? "" : "unread ") + "stories are about that place. Tap one to read them."
    : all ? "No stories with a place yet." : "No unread stories with a place. You're caught up.";
}
async function showMapStories(g, names) {
  const r = await guarded(() => api("GET", "news/stories?ids=" + g.ids.slice(0, 100).join(",")));
  if (!r) return;
  $("#map-list").replaceChildren(h("h2", { class: "map-list-title" }, names.slice(0, 4).join(", ") + (names.length > 4 ? ` and ${names.length - 4} more` : "")),
    ...r.stories.map(storyCard));
  $("#map-list").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("#map-zoom").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-z]");
  if (!b) return;
  NEWS.zoom = b.dataset.z;
  $$("#map-zoom button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", String(x === b)); });
  if (NEWS.points) drawBubbles();
});
$("#map-all").addEventListener("change", loadNewsMap);
addEventListener("resize", () => { if (NEWS.points && !$("#news-map-view").hidden) drawBubbles(); });

/* ---------- morning brief ---------- */
const briefOpen = () => { try { return localStorage.getItem("bunkr-brief-open") === "1"; } catch { return false; } };
$("#brief-openall").addEventListener("click", () => {
  const open = !briefOpen();
  try { localStorage.setItem("bunkr-brief-open", open ? "1" : "0"); } catch {}
  $$("#brief-stories details.story-more").forEach((d) => { d.open = open; });
  $("#brief-openall").textContent = open ? "Close all" : "Open all";
});
const STATE_OWNERS = ["state-run", "state-funded", "state-owned", "state-aligned", "close to the state", "government-linked"];
async function loadBrief(day) {
  const r = await guarded(() => api("GET", "brief" + (day ? "?day=" + encodeURIComponent(day) : "")));
  if (!r) return;
  const b = r.brief;
  $("#brief-run").hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  $("#brief-run").textContent = r.running ? "Making it…" : b ? "Make a fresh one" : "Make one now";
  $("#brief-run").disabled = r.running;
  const sel = $("#brief-day");
  sel.replaceChildren(...r.days.map((d) => h("option", { value: d, selected: b && d === b.day }, new Date(d + "T12:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }))));
  sel.hidden = r.days.length < 2;
  if (!b) {
    $("#brief-title").textContent = "Morning brief";
    $("#brief-lead").textContent = `No brief yet. It's made every morning at ${r.settings.time}.`;
    for (const id of ["#brief-near-card", "#brief-also-card"]) $(id).hidden = true;
    $("#brief-stories").replaceChildren(); $("#brief-foot").textContent = "";
    return;
  }
  const O = b.outlets;
  const label = (k) => O[k] ? `${O[k].name} · ${O[k].country}, ${O[k].owner}` : k;
  const chip = (s) => h("a", { href: s.url, target: "_blank", rel: "noopener", class: O[s.outlet] && STATE_OWNERS.includes(O[s.outlet].owner) ? "state" : "",
    title: label(s.outlet) }, O[s.outlet] ? O[s.outlet].name : s.outlet);
  $("#brief-title").textContent = "Morning brief · " + new Date(b.day + "T12:00").toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  $("#brief-lead").textContent = b.ai ? b.ai.lead : (b.groups[0] ? "Top story across regions: " + b.groups[0].headline : "A quiet news day.");
  $("#brief-near-card").hidden = false;
  $("#brief-openall").hidden = !b.ai;
  $("#brief-openall").textContent = briefOpen() ? "Close all" : "Open all";
  $("#brief-near").replaceChildren(...(b.near.length ? b.near.map((i) => h("p", { class: "near-item" }, h("strong", { class: i.tier === "flash" ? "bad" : "" }, i.title),
    i.site && i.km != null ? h("span", { class: "muted" }, i.km === 0 ? ` · at ${i.site}` : ` · ${i.km} km from ${i.site}`) : null,
    i.scenario ? [" ", h("button", { class: "link", onclick: () => openScenario(i.scenario) }, "Open the checklist")] : null))
    : [h("p", { class: "muted" }, "Nothing near you.")]));
  if (b.ai) {
    const openAll = briefOpen();
    $("#brief-stories").replaceChildren(...b.ai.stories.map((s, si) => h("article", { class: "card story" },
      h("h2", {}, s.title),
      s.matters ? h("p", { class: "for-us" }, h("b", {}, "For us: "), s.matters) : null,
      s.agree ? h("p", { class: "agree", title: "What everyone agrees on" }, s.agree) : null,
      s.differ.length || s.unconfirmed || s.sources.length ? h("details", { class: "story-more", open: openAll },
        h("summary", {}, [s.differ.length ? `How coverage differs (${s.differ.length})` : "", s.unconfirmed ? "not confirmed" : "",
          s.sources.length ? `${s.sources.length} source${s.sources.length === 1 ? "" : "s"}` : ""].filter(Boolean).join(" \u00b7 ")),
        s.differ.length ? h("ul", { class: "differ" }, ...s.differ.map((d) => h("li", {}, h("b", {}, (d.who || "").trim()), " " + (d.framing || "").trim()))) : null,
        s.unconfirmed ? h("p", { class: "unconfirmed" }, h("b", {}, "Not confirmed: "), s.unconfirmed) : null,
        s.sources.length ? h("ul", { class: "plain brief-srcs" }, ...s.sources.map((src) => h("li", {}, chip(src),
          src.title ? h("span", { class: "muted" }, " " + src.title) : null,
          s.sources.length > 1 ? [" ", flagButton({ kind: "brief", day: b.day, index: si, url: src.url }, src.title || src.url, s.title, () => loadBrief(b.day))] : null))) : null) : null)));
  } else {
    $("#brief-stories").replaceChildren(h("div", { class: "card" }, h("h2", {}, "Stories covered across regions"),
      h("p", { class: "muted" }, "Headlines grouped by story, widest coverage first. (No AI summary today" + (b.ai_error && isAdmin() ? `: ${b.ai_error}` : "") + ".)"),
      ...b.groups.map((g) => h("details", { class: "sit-cat" }, h("summary", {}, g.headline, h("span", { class: "muted" }, ` · ${g.regions.length} regions`)),
        h("ul", { class: "plain" }, ...g.items.map((i) => h("li", {}, h("a", { href: i.url, target: "_blank", rel: "noopener" }, i.title),
          h("span", { class: "muted" }, " · " + label(i.outlet)))))))));
  }
  $("#brief-also-card").hidden = !b.world_routine.length;
  $("#brief-also").replaceChildren(...b.world_routine.map((i) => h("p", {}, i.url ? h("a", { href: i.url, target: "_blank", rel: "noopener" }, i.title) : i.title)));
  const ok = Object.values(b.sources).filter((s) => s.ok && s.count).length;
  const failed = Object.entries(b.sources).filter(([, s]) => !s.ok).map(([k]) => O[k] ? O[k].name : k);
  $("#brief-foot").textContent = `Made ${sinceIso(b.created_at)} from ${b.headline_count} headlines across ${ok} outlets` +
    (b.ai ? "; AI summaries." : ".") + (failed.length ? ` Couldn't reach: ${failed.join(", ")}.` : "") + ` Next brief tomorrow at ${r.settings.time}.`;
}
$("#brief-day").addEventListener("change", (e) => loadBrief(e.target.value));
$("#brief-run").addEventListener("click", () => guarded(async () => {
  await api("POST", "brief/run"); toast("Making the brief; it takes about a minute"); $("#brief-run").disabled = true;
  setTimeout(() => loadBrief(), 70000);
}));

/* ---------- situation ---------- */
let SIT = null;
function openScenario(id) {
  showTab("scenarios");
  setTimeout(() => { const c = $("#scn-" + id); if (!c) return; c.querySelector("details.scn-body")?.setAttribute("open", "");
    c.scrollIntoView({ behavior: "smooth", block: "start" }); }, 150);
}
const scenarioLabel = (id) => (S.scenarios.find((x) => x.id === id) || {}).label;
function sitItem(i, seen, big = false) {
  const where = [i.site && i.km != null ? (i.km === 0 ? `at ${i.site}` : `${i.km} km from ${i.site}`) : i.where_text,
    i.starts ? sinceIso(i.starts) : "", SIT.trust[i.trust] || i.trust].filter(Boolean).join(" · ");
  const lines = (i.summary || "").trim();
  return h("div", { class: "sit-item" + (seen && i.first_seen > seen ? " new" : "") },
    h("div", { class: "t" }, i.title),
    h("div", { class: "m" }, where),
    big && lines ? h("p", {}, lines) : lines ? h("details", {}, h("summary", { class: "m" }, "More"), h("p", {}, lines)) : null,
    h("div", { class: "m" },
      i.scenario && scenarioLabel(i.scenario) ? h("button", { class: big ? "primary small" : "link", onclick: () => openScenario(i.scenario) },
        `Open the ${scenarioLabel(i.scenario).toLowerCase()} checklist`) : null,
      i.url ? [" ", h("a", { href: i.url, target: "_blank", rel: "noopener" }, "Source")] : null));
}
async function loadSituation() {
  const r = await guarded(() => api("GET", "situation"));
  if (!r) return;
  SIT = r;
  let seen = "";
  try { seen = localStorage.getItem("bunkr-sit-seen") || ""; localStorage.setItem("bunkr-sit-seen", new Date().toISOString()); } catch {}
  const by = (t) => r.items.filter((i) => i.tier === t);
  const fresh = seen ? r.items.filter((i) => i.first_seen > seen).length : 0;
  $("#sit-new").textContent = !seen ? "" : fresh ? `${fresh} new since you last looked (marked "new").` : "Nothing new since you last looked.";
  const ok = r.sources.filter((s) => s.at).map((s) => s.at).sort();
  $("#sit-asof").textContent = ok.length ? `As of ${sinceIso(ok[ok.length - 1])}` : "Not checked yet";
  $("#sit-refresh").hidden = !isAdmin() || ["pi", "laptop"].includes(S.node_role);
  $("#sit-flash").replaceChildren(...by("flash").map((i) => h("div", { class: "card flash-card" }, h("h2", {}, "Act now"), sitItem(i, seen, true))));
  const pri = by("priority");
  $("#sit-priority").replaceChildren(...(pri.length ? pri.map((i) => sitItem(i, seen)) : [h("p", { class: "muted" }, "Nothing near you right now.")]));
  const routine = by("routine");
  const cats = Object.entries(r.categories).map(([k, label]) => [label, routine.filter((i) => i.category === k)]).filter(([, l]) => l.length);
  $("#sit-routine").replaceChildren(...(cats.length ? cats.map(([label, list]) => h("details", { class: "sit-cat" },
    h("summary", {}, `${label} (${list.length})`), ...list.map((i) => sitItem(i, seen)))) : [h("p", { class: "muted" }, "Quiet.")]));
  $("#sit-sources").replaceChildren(...r.sources.map((s) => h("li", {}, h("strong", {}, s.name), " ",
    s.needs_key && !s.at ? h("span", { class: "muted" }, "off: needs a free key") :
    s.ok === false ? h("span", { class: "bad" }, `couldn't refresh (${s.error}); showing data from ${s.at ? sinceIso(s.at) : "never"}`)
      : s.at ? h("span", { class: "muted" }, `${sinceIso(s.at)} · ${s.count} item${s.count === 1 ? "" : "s"}`) : h("span", { class: "muted" }, "not checked yet"),
    s.key_label && isAdmin() && !["pi", "laptop"].includes(S.node_role) ? keyForm(s) : null)));
  if (sitView === "map") drawSitMap();
  $("#sit-prefs-card").hidden = !isAdmin();
  const f = $("#sit-prefs");
  if (document.activeElement?.form !== f) {
    f.devices.value = (r.prefs.devices || []).join("\n"); f.local_orgs.value = (r.prefs.local_orgs || []).join("\n"); f.state.value = r.prefs.state || "";
  }
}
function keyForm(src) {
  const inp = h("input", { type: "password", autocomplete: "off", placeholder: src.needs_key ? src.key_label : "Replace the key (blank removes it)" });
  return h("details", {}, h("summary", { class: "link" }, src.needs_key ? "Add key" : "Change key"),
    h("form", { class: "key-form", onsubmit: (e) => { e.preventDefault(); guarded(async () => {
      const r = await api("POST", "situation/key", { source: src.key, value: inp.value });
      toast(r.saved ? "Saved. Checking it now…" : "Key removed; that source is off."); inp.value = "";
      setTimeout(loadSituation, r.saved ? 12000 : 300); }); } },
      inp, h("button", { type: "submit" }, "Save")),
    h("p", { class: "muted" }, "Free. Get one at ", h("a", { href: src.key_url, target: "_blank", rel: "noopener" }, new URL(src.key_url).host),
      ". It stays in a file on the hub, never in backups or sync."));
}

/* the map: the offline maps pack when there is one, otherwise a simple "around us" drawing that needs nothing */
let sitView = "list";
function setSitView(v) {
  sitView = v;
  $("#sit-show-list").classList.toggle("on", v === "list"); $("#sit-show-list").setAttribute("aria-pressed", v === "list");
  $("#sit-show-map").classList.toggle("on", v === "map"); $("#sit-show-map").setAttribute("aria-pressed", v === "map");
  $("#sit-list").hidden = v !== "list"; $("#sit-map-card").hidden = v !== "map";
  try { localStorage.setItem("bunkr-sit-view", v); } catch {}
  if (v === "map" && SIT) drawSitMap();
}
$("#sit-show-list").addEventListener("click", () => setSitView("list"));
$("#sit-show-map").addEventListener("click", () => setSitView("map"));
window.addEventListener("message", (e) => { if (e.origin === location.origin && e.data?.bunkr === "scenario") openScenario(e.data.id); });
const TIER_COL = { flash: "#c62828", priority: "#ef6c00", routine: "#6d7a86" };
function bearing(a, b) {
  const r = Math.PI / 180, y = Math.sin((b.lon - a.lon) * r) * Math.cos(b.lat * r);
  const x = Math.cos(a.lat * r) * Math.sin(b.lat * r) - Math.sin(a.lat * r) * Math.cos(b.lat * r) * Math.cos((b.lon - a.lon) * r);
  return Math.atan2(y, x);
}
function kmBetween(a, b) {
  const r = Math.PI / 180, d = Math.sin((b.lat - a.lat) * r / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin((b.lon - a.lon) * r / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(d));
}
let sitCentre = null;
function drawSitMap() {
  const box = $("#sit-map"), note = $("#sit-map-note"), pick = $("#sit-map-pick");
  if (SIT.map?.pack && SIT.map?.viewer) {
    if (!box.querySelector("iframe")) box.replaceChildren(h("iframe", { src: "/store/web/maps/?overlay=situation", title: "Situation map" }));
    pick.replaceChildren();
    note.textContent = "Works offline from the maps pack. Tap a dot or an area for details.";
    return;
  }
  const sites = S.sites.filter((x) => x.lat != null && x.lon != null);
  if (!sites.length) { box.replaceChildren(); pick.replaceChildren(); note.textContent = "Add a place with an address on Sites to see the map."; return; }
  const centre = sites.find((x) => x.id === sitCentre) || sites.find((x) => x.kind === "home") || sites[0];
  sitCentre = centre.id;
  const W = 400, C = W / 2, R = 180, MAX = 500;
  const rad = (d) => R * Math.log1p(d / 5) / Math.log1p(MAX / 5);
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, at = {}) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(at)) e.setAttribute(k, v); return e; };
  const svg = el("svg", { viewBox: `0 0 ${W} ${W}`, role: "img", "aria-label": `What's happening around ${centre.name}` });
  for (const d of [10, 50, 150, 500]) {
    svg.append(el("circle", { class: "ring", cx: C, cy: C, r: rad(d) }));
    const t = el("text", { x: C + 3, y: C - rad(d) + 12 }); t.textContent = `${d} km`; svg.append(t);
  }
  for (const [lbl, x, y] of [["N", C, 12], ["E", W - 10, C + 4], ["S", C, W - 4], ["W", 4, C + 4]]) {
    const t = el("text", { x, y, "text-anchor": "middle" }); t.textContent = lbl; svg.append(t);
  }
  const detail = h("div", {});
  const here = SIT.items.filter((i) => i.km === 0 && i.site === centre.name);
  for (const [n, i] of here.entries()) {
    svg.append(el("circle", { cx: C, cy: C, r: 14 + n * 5, fill: "none", stroke: TIER_COL[i.tier], "stroke-width": 3 }));
  }
  const far = [], off = [];
  const dots = [];
  for (const i of SIT.items) {
    if (i.lat == null || i.lon == null) { if (!(i.km === 0 && i.site === centre.name)) off.push(i); continue; }
    const d = kmBetween(centre, i);
    if (d > MAX) { far.push(i); continue; }
    const b = bearing(centre, i), r = rad(d);
    dots.push([i, C + r * Math.sin(b), C - r * Math.cos(b)]);
  }
  dots.sort((a, b) => (TIER_ORDER_JS[b[0].tier] ?? 9) - (TIER_ORDER_JS[a[0].tier] ?? 9));  // worst on top
  for (const [i, x, y] of dots) {
    const c = el("circle", { class: "dot", cx: x.toFixed(1), cy: y.toFixed(1), r: i.tier === "flash" ? 8 : i.tier === "priority" ? 6.5 : 5, fill: TIER_COL[i.tier], tabindex: 0 });
    const t = el("title"); t.textContent = i.title; c.append(t);
    const show = () => { svg.querySelectorAll(".sel").forEach((e) => e.classList.remove("sel")); c.classList.add("sel"); detail.replaceChildren(sitItem(i, "", true)); };
    c.addEventListener("click", show); c.addEventListener("keydown", (e) => e.key === "Enter" && show());
    svg.append(c);
  }
  for (const x of sites) {
    if (x.id === centre.id) continue;
    const d = kmBetween(centre, x); if (d > MAX) continue;
    const b = bearing(centre, x), r = rad(d);
    svg.append(el("rect", { x: (C + r * Math.sin(b) - 5).toFixed(1), y: (C - r * Math.cos(b) - 5).toFixed(1), width: 10, height: 10, fill: "currentColor" }));
    const t = el("text", { x: (C + r * Math.sin(b) + 8).toFixed(1), y: (C - r * Math.cos(b) + 4).toFixed(1) }); t.textContent = x.name; svg.append(t);
  }
  for (const n of SIT.neighbours || []) {  // neighbors: small green dots, tap for who and what they can do
    const d = kmBetween(centre, n); if (d > MAX) continue;
    const b = bearing(centre, n), r = rad(d);
    const c = el("circle", { class: "dot", cx: (C + r * Math.sin(b)).toFixed(1), cy: (C - r * Math.cos(b)).toFixed(1), r: 4.5, fill: "#2e7d32", tabindex: 0 });
    const t = el("title"); t.textContent = `Neighbor: ${n.name}`; c.append(t);
    const show = () => detail.replaceChildren(h("p", {}, h("strong", {}, n.name), ` · neighbor${n.place ? " · " + n.place : ""}`,
      n.skill_labels.length ? h("span", { class: "muted" }, ` · ${n.skill_labels.join(", ")}`) : null));
    c.addEventListener("click", show); c.addEventListener("keydown", (e) => e.key === "Enter" && show());
    svg.append(c);
  }
  svg.append(el("rect", { x: C - 6, y: C - 6, width: 12, height: 12, fill: "currentColor" }));
  box.replaceChildren(svg, detail, here.length ? h("div", {}, h("h3", {}, `At ${centre.name}`), ...here.map((i) => sitItem(i, ""))) : null);
  pick.replaceChildren(sites.length > 1 ? h("p", {}, "Centre on: ", ...sites.map((x) => h("button", { class: x.id === centre.id ? "primary small" : "small",
    onclick: () => { sitCentre = x.id; drawSitMap(); } }, x.name))) : null);
  note.textContent = [`Distances are to scale in steps (10, 50, 150, 500 km). Tap a dot for details.`,
    off.length ? `${off.length} item${off.length === 1 ? " has" : "s have"} no location (cyber, space weather): see the list.` : "",
    far.length ? `${far.length} more than 500 km away.` : "",
    isAdmin() ? "For a street map that works offline, build the maps pack on Sites." : ""].filter(Boolean).join(" ");
}
const TIER_ORDER_JS = { flash: 0, priority: 1, routine: 2 };
try { if (localStorage.getItem("bunkr-sit-view") === "map") setTimeout(() => setSitView("map"), 0); } catch {}
$("#sit-refresh").addEventListener("click", () => guarded(async () => {
  await api("POST", "situation/refresh"); toast("Checking the feeds…"); setTimeout(loadSituation, 8000);
}));
$("#sit-prefs").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, lines = (t) => t.split("\n").map((x) => x.trim()).filter(Boolean);
  guarded(async () => { await api("POST", "situation/prefs", { devices: lines(f.devices.value), local_orgs: lines(f.local_orgs.value), state: f.state.value });
    toast("Saved. It applies at the next check."); });
});

/* ---------- readiness ---------- */
function renderReadiness() {
  const a = S.analysis;
  const chip = $("#overall-chip");
  chip.textContent = a.overall + "% ready";
  chip.className = "chip " + tone(a.overall);
  $("#t-overall").replaceChildren(h("span", { class: tone(a.overall) }, a.overall + "%"));
  $("#t-water").replaceChildren(h("span", { class: tone(100 * a.water_days / a.target_days) }, a.water_days),
    h("small", {}, ` / ${a.target_days} days`));
  $("#t-food").replaceChildren(h("span", { class: tone(100 * a.food_days / a.target_days) }, a.food_days),
    h("small", {}, ` / ${a.target_days} days`));
  $("#t-expiry").replaceChildren(
    h("span", { class: a.expired.length ? "bad" : "good" }, a.expired.length),
    h("small", {}, " / "), h("span", { class: a.expiring.length ? "warn" : "good" }, a.expiring.length));

  $("#cat-bars").replaceChildren(...a.categories.map((c) =>
    h("div", { class: "bar-row" },
      h("span", {}, c.label),
      h("div", { class: "bar" }, h("div", { class: "bg-" + tone(c.score), style: `width:${Math.max(2, c.score)}%` })),
      h("span", { class: "num " + tone(c.score) }, c.score + "%"))));

  const gaps = a.gaps.slice(0, 12);
  $("#gap-list").replaceChildren(...(gaps.length ? gaps.map((g) => {
    const pct = Math.round(g.coverage * 100);
    const addable = !g.id.startsWith("doc_");
    return h("li", {},
      h("span", { class: tone(pct) }, `${g.label}`), g.hazard ? h("span", { class: "tag hz" }, g.hazard) : null, " ",
      h("span", { class: "meta" }, `${fmtNum(g.have)} of ${fmtNum(g.need)}${g.unit ? " " + g.unit : ""}` + (g.detail ? ` · ${g.detail}` : "")), " ",
      addable ? h("button", { class: "link", onclick: () => startItemFor(g) }, "Add")
              : h("button", { class: "link", onclick: () => showTab("documents") }, "Register"));
  }) : [h("li", {}, "No gaps against your targets. Check expiry dates regularly.")]));

  renderAlerts();
  renderRunBanner();
  $("#kit-bars").replaceChildren(...(S.kits.length ? S.kits.map((k) =>
    h("div", { class: "bar-row" },
      h("span", {}, k.name),
      h("div", { class: "bar" }, h("div", { class: "bg-" + tone(k.score), style: `width:${Math.max(2, k.score)}%` })),
      h("span", { class: "num " + tone(k.score) }, k.score + "%")))
    : [h("p", { class: "muted" }, "No vehicles or go-bags yet. Add them on the Locations & kits tab.")]));
  const prof = S.hazard_profile;
  if (prof && prof.ratings) {
    const home = S.sites.find((x) => x.kind === "home");
    $("#hazard-source").textContent = `FEMA National Risk Index for ${prof.county || home?.county_name || "your county"}` +
      `${prof.version ? " (" + prof.version + ")" : ""}. Rated hazards add items to the checklist and weigh more in the score.`;
    $("#hazard-chips").replaceChildren(...(a.hazards.length ? a.hazards.map((z) =>
      h("span", { class: "chip lvl-" + z.level }, `${z.label}: ${z.rating}`)) : [h("span", { class: "muted" }, "No hazards rated moderate or higher.")]));
  } else {
    $("#hazard-source").textContent = "Add your home on the Sites & alerts tab to tailor the checklist to local hazards.";
    $("#hazard-chips").replaceChildren();
  }

  const exp = [...a.expired.map((i) => [i, "expired"]), ...a.expiring.map((i) => [i, "soon"])];
  $("#expiry-list").replaceChildren(exp.length
    ? h("table", {}, h("tbody", {}, exp.map(([i, kind]) =>
        h("tr", { class: kind === "expired" ? "expired" : "" },
          h("td", {}, i.name), h("td", {}, i.location || "no location"), h("td", {}, i.expires),
          h("td", { class: kind === "expired" ? "bad" : "warn" }, kind === "expired" ? "Expired, not counted" : "Expires within 60 days")))))
    : h("p", { class: "muted" }, "Nothing expired or expiring within 60 days."));
}

/* ---------- inventory ---------- */
function locationOptions(withNone = true, withNew = false) {
  const opts = [];
  if (withNone) opts.push(h("option", { value: "" }, "No location"));
  for (const l of S.locations) opts.push(h("option", { value: l.id }, `${l.name} (${S.location_kinds[l.kind].split(" (")[0]})`));
  if (withNew) opts.push(h("option", { value: "__new" }, "New location…"));
  return opts;
}

// an item is "in" a location if it's there or in any sub-location below it (names are full paths)
function inLocation(item, locId) {
  const l = S.locations.find((x) => String(x.id) === locId);
  return String(item.location_id) === locId || (!!l && (item.location || "").startsWith(l.name + " > "));
}

function fillLocationSelects() {
  const sel = $("#item-location"), keep = sel.value;
  sel.replaceChildren(...locationOptions(true, true));
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  $("#new-loc-label").hidden = sel.value !== "__new";
  const f = $("#inv-loc-filter"), fk = f.value;
  f.replaceChildren(h("option", { value: "" }, "All locations"), h("option", { value: "none" }, "No location"), ...locationOptions(false));
  if ([...f.options].some((o) => o.value === fk)) f.value = fk;
  const m = $("#move-target");
  m.replaceChildren(h("option", { value: "" }, "to: no location"), ...S.locations.map((l) => h("option", { value: l.id }, "to: " + l.name)));
}
$("#item-location").addEventListener("change", () => { $("#new-loc-label").hidden = $("#item-location").value !== "__new"; });

function fillCategorySelects() {
  const cats = Object.entries(S.categories);
  $("#item-category").replaceChildren(...cats.map(([k, v]) => h("option", { value: k }, v)));
  const filter = $("#inv-filter");
  const keep = filter.value;
  filter.replaceChildren(h("option", { value: "" }, "All categories"), ...cats.map(([k, v]) => h("option", { value: k }, v)));
  filter.value = keep;
}

function updateItemFormForCategory() {
  const cat = $("#item-category").value;
  const ess = $("#item-essential");
  const keep = ess.value;
  const generic = cat === "water" ? "Drinking water (gallons)" : cat === "food" ? "General food (calories)" : "Nothing specific";
  ess.replaceChildren(h("option", { value: "" }, generic),
    ...S.checklist.filter((e) => e.category === cat).map((e) => h("option", { value: e.id }, e.label)));
  if ([...ess.options].some((o) => o.value === keep)) ess.value = keep;
  const perUnit = $("#per-unit-label");
  const cash = ess.value === "cash";
  const show = cash || (!ess.value && (cat === "water" || cat === "food"));
  perUnit.hidden = !show;
  perUnit.firstChild.textContent = cash ? "Value of each bill ($)" : cat === "water" ? "Size of each" : "Calories each";
  if ($("#label-read")) labelShow();
  perUnit.querySelector("select").hidden = cat !== "water" || cash;
  const f = $("#item-form");
  f.unit.placeholder = cash ? "bills" : "cases, jugs, cans";
  for (const el of document.querySelectorAll(".gun-field")) el.hidden = cat !== "firearms";
  const sub = cat === "firearms" ? f.subtype.value : "";
  for (const el of document.querySelectorAll(".gun-field.serial-field")) el.hidden = sub !== "firearm";
  for (const el of document.querySelectorAll(".gun-field.ammo-field")) el.hidden = sub !== "ammo";
  for (const el of document.querySelectorAll(".gun-field.mag-field")) el.hidden = sub !== "magazine";
  if (f.ammo_type.options.length === 1) f.ammo_type.append(...Object.entries(S.ammo_types || {}).map(([k, v]) => h("option", { value: k }, v)));
  if (f.mag_status.options.length === 1) f.mag_status.append(...Object.entries(S.mag_status || {}).map(([k, v]) => h("option", { value: k }, v)));
  const keepFits = f.fits.value;
  f.fits.replaceChildren(h("option", { value: "" }, "Not matched"),
    ...S.items.filter((i) => i.category === "firearms" && i.subtype === "firearm").map((g) => h("option", { value: g.uid }, `${g.name}${g.caliber ? " (" + g.caliber + ")" : ""}`)));
  f.fits.value = keepFits;
  $("#caliber-list").replaceChildren(...(S.ammo || []).map((a) => h("option", { value: a.caliber })));
  updateItemPreview();
}

const GAL_PER = { ml: 1 / 3785.411784, L: 1 / 3.785411784, "fl oz": 1 / 128, gal: 1 };
function itemPerUnit(f) {  // gallons (water) or kcal (food) in one of the things
  const v = parseFloat(f.size_value.value) || 0;
  return f.category.value === "water" && f.essential.value !== "cash" ? v * (GAL_PER[f.size_unit.value] || 1) : v;
}
function updateItemPreview() {
  const f = $("#item-form");
  const q = parseFloat(f.quantity.value) || 0, size = parseFloat(f.pack_size.value) || 1;
  const inner = f.pack_unit.value.trim(), per = $("#per-unit-label").hidden ? 0 : itemPerUnit(f);
  const n = q * size, parts = [];
  if (f.essential.value === "cash") {
    $("#item-preview").textContent = `= $${Math.round(per ? n * per : n).toLocaleString()}` +
      (per ? "" : " (no bill value given, so the quantity is the dollar amount)");
    return;
  }
  if (size !== 1) parts.push(`${fmtNum(n)} ${inner || "in all"}`);
  if (per && f.category.value === "water") {
    parts.push(`${fmtNum(+(n * per).toPrecision(3))} gal`);
    parts.push(`${Math.round(size * per * 8.34)} lb ${size !== 1 ? "per " + (f.unit.value.trim() || "pack") : "each"}`);
  } else if (per) parts.push(`${Math.round(n * per).toLocaleString()} kcal`);
  $("#item-preview").textContent = parts.length ? "= " + parts.join(" · ") : "";
}
for (const n of ["quantity", "unit", "pack_size", "pack_unit", "size_value", "size_unit"])
  $("#item-form").elements[n].addEventListener("input", updateItemPreview);
$("#item-category").addEventListener("change", updateItemFormForCategory);
$("#item-essential").addEventListener("change", updateItemFormForCategory);
$("#item-form").subtype.addEventListener("change", updateItemFormForCategory);

function resetItemForm() {
  editingItem = null;
  const f = $("#item-form");
  f.reset();
  $("#item-form-title").textContent = "Add item";
  $("#item-cancel").hidden = true;
  updateItemFormForCategory();
}
$("#item-cancel").addEventListener("click", () => { resetItemForm(); $("#add-card").classList.remove("open"); $("#add-open").setAttribute("aria-expanded", "false"); });
$("#add-open").addEventListener("click", () => {
  const open = $("#add-card").classList.toggle("open");
  $("#add-open").setAttribute("aria-expanded", String(open));
  if (open) $("#item-form").name.focus();
});

function editItem(item) {
  editingItem = item.id;
  $("#add-card").classList.add("open");
  const f = $("#item-form");
  f.category.value = item.category;
  updateItemFormForCategory();
  for (const k of ["name", "essential", "quantity", "unit", "pack_size", "pack_unit", "expires", "notes", "caliber", "serial"]) f[k].value = item[k] ?? "";
  f.subtype.value = item.subtype || "firearm";
  updateItemFormForCategory();
  f.ammo_type.value = item.ammo_type || ""; f.mag_status.value = item.mag_status || ""; f.fits.value = item.fits || "";
  f.capacity.value = item.capacity || "";
  f.size_unit.value = item.size_unit || "gal";
  f.size_value.value = item.category === "water" ? +(item.per_unit / (GAL_PER[f.size_unit.value] || 1)).toPrecision(6) : item.per_unit;
  f.location_id.value = item.location_id ?? "";
  $("#new-loc-label").hidden = true;
  updateItemFormForCategory();
  $("#item-form-title").textContent = "Edit item";
  $("#item-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth" });
}

function startItemFor(gap, locationId = null, qty = null) {
  showTab("inventory");
  resetItemForm();
  const f = $("#item-form");
  f.category.value = gap.category;
  if (locationId) f.location_id.value = locationId;
  if (qty && gap.unit !== "kcal") f.quantity.value = qty;
  updateItemFormForCategory();
  if (essentialById(gap.id)) { f.essential.value = gap.id; f.name.value = essentialById(gap.id).label; }
  updateItemFormForCategory();
  if (gap.id === "water") { f.unit.value = "jugs"; f.size_value.value = 1; f.size_unit.value = "gal"; }
  updateItemPreview();
  f.name.focus();
}

$("#item-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.per_unit = (data.essential && data.essential !== "cash") || $("#per-unit-label").hidden ? 0 : itemPerUnit(e.target);
  if (data.category !== "water") data.size_unit = "";
  delete data.size_value;
  if (data.location_id === "__new") data.location_id = "";
  else data.location = "";
  guarded(async () => {
    await api("POST", "items" + (editingItem ? "/" + editingItem : ""), data);
    toast(editingItem ? "Item updated" : "Item added");
    resetItemForm();
    await refresh();
  });
});

/* ---------- fast entry: barcodes and pasted lists ---------- */
let scanStream = null, scanTimer = null;
document.addEventListener("click", (e) => {
  const q = e.target.closest("[data-quick]")?.dataset.quick;
  if (q === "scan") openScan();
  else if (q === "paste") { $("#paste-rows").replaceChildren(); $("#paste-dialog").showModal(); $("#paste-text").focus(); }
  else if (q === "one") { showTab("inventory"); resetItemForm(); $("#item-form").scrollIntoView({ behavior: "smooth" }); $("#item-form").name.focus(); }
});
function stopScan() {
  clearInterval(scanTimer); scanTimer = null;
  scanStream?.getTracks().forEach((t) => t.stop()); scanStream = null;
  $("#scan-video").hidden = true;
}
async function openScan() {
  $("#scan-result").replaceChildren(); $("#scan-form").code.value = "";
  $("#scan-dialog").showModal();
  if (!("BarcodeDetector" in window) || !navigator.mediaDevices || !isSecureContext) {
    $("#scan-msg").textContent = !isSecureContext ? "The camera only works over https (bunkr.example.org). Type the number instead."
      : "This browser can't read barcodes with the camera (iPhones can't yet). Type the number under the barcode.";
    $("#scan-form").code.focus();
    return;
  }
  try {
    const det = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"] });
    scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    const v = $("#scan-video");
    v.srcObject = scanStream; v.hidden = false; await v.play();
    $("#scan-msg").textContent = "Point the camera at the barcode.";
    scanTimer = setInterval(async () => {
      const found = await det.detect(v).catch(() => []);
      if (found.length) { stopScan(); lookupCode(found[0].rawValue); }
    }, 300);
  } catch (err) {
    stopScan();
    $("#scan-msg").textContent = "Couldn't use the camera (" + err.message + "). Type the number instead.";
  }
}
$("#scan-close").addEventListener("click", () => { stopScan(); $("#scan-dialog").close(); });
$("#scan-dialog").addEventListener("close", stopScan);
$("#scan-form").addEventListener("submit", (e) => { e.preventDefault(); lookupCode(e.target.code.value); });
async function lookupCode(code) {
  $("#scan-msg").textContent = `Barcode ${code}: looking it up…`;
  const r = await guarded(() => api("POST", "barcode", { code }));
  if (!r) return;
  if (r.item) {
    const it = r.item;
    $("#scan-msg").textContent = `${it.name}: you have ${fmtNum(it.quantity)}${it.unit ? " " + it.unit : ""}.`;
    const bump = (n) => guarded(async () => { const x = await api("POST", `items/${it.id}/bump`, { amount: n });
      toast(`${it.name}: now ${fmtNum(x.quantity)}`); refresh(); $("#scan-dialog").close(); });
    $("#scan-result").replaceChildren(h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => bump(1) }, `Add 1${it.unit ? " " + it.unit.replace(/s$/, "") : ""}`),
      h("button", { onclick: () => { const n = prompt("How many to add?", "6"); if (n && +n > 0) bump(+n); } }, "Add more…"),
      h("button", { onclick: () => bump(-1) }, "Used 1"), h("button", { onclick: () => openScan() }, "Scan another")));
    return;
  }
  $("#scan-dialog").close();
  showTab("inventory"); resetItemForm();
  const f = $("#item-form"), d = r.draft || {};
  f.barcode.value = r.code;
  if (d.category) { f.category.value = d.category; updateItemFormForCategory(); }
  if (d.name) f.name.value = d.name;
  if (d.unit) f.unit.value = d.unit;
  if (d.pack_size > 1) { f.pack_size.value = d.pack_size; f.pack_unit.value = d.pack_unit || ""; }
  if (d.note) f.notes.value = d.note;
  if (d.size_unit) { f.size_unit.value = d.size_unit; f.size_value.value = +(d.per_unit / (GAL_PER[d.size_unit] || 1)).toPrecision(4); }
  else if (d.per_unit) f.size_value.value = d.per_unit;
  updateItemPreview();
  f.scrollIntoView({ behavior: "smooth" });
  labelShow();
  const noKcal = d.name && d.category === "food" && !d.per_unit ? " No calories found: tap Read the nutrition label, or type them." :
    d.kcal_from ? ` Calories from ${d.kcal_from}.` : "";
  toast(d.name ? `Found: ${d.name}. Check it, pick where it goes, and save.${noKcal}` : (r.error || "Not found. Type the name and save; BUNKR will remember this barcode."), !d.name);
  (d.name ? f.quantity : f.name).focus();
}
$("#paste-close").addEventListener("click", () => $("#paste-dialog").close());
$("#paste-preview").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "items/parse", { text: $("#paste-text").value });
  if (!r.items.length) return toast("Nothing recognised: one item per line", true);
  const cats = Object.entries(S.categories || {}).filter(([k]) => k !== "firearms");
  const rows = r.items.map((it) => {
    const tr = h("tr", {},
      h("td", {}, h("input", { type: "checkbox", checked: true, "aria-label": "Add" })),
      h("td", { class: "q" }, h("input", { type: "number", step: "any", min: "0", value: it.quantity, "aria-label": "Quantity" })),
      h("td", {}, h("input", { type: "text", value: it.name, "aria-label": "Name" }),
        it.size_unit ? h("div", { class: "muted" }, `${it.unit} of ${+(it.per_unit / GAL_PER[it.size_unit]).toPrecision(4)} ${it.size_unit}`) : it.unit ? h("div", { class: "muted" }, it.unit) : null),
      h("td", { class: "c" }, h("select", { "aria-label": "Category" }, ...cats.map(([k, v]) => h("option", { value: k, selected: k === it.category ? true : null }, v)))));
    tr._item = it;
    return tr;
  });
  const loc = h("select", {}, ...[...$("#item-location").options].filter((o) => o.value !== "__new").map((o) => h("option", { value: o.value }, o.textContent)));
  $("#paste-rows").replaceChildren(h("table", { class: "paste-table" }, ...rows),
    h("p", { class: "muted" }, "Food added this way doesn't count toward days of food until you add calories (edit the item)."),
    h("label", {}, "Put them in ", loc),
    h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => guarded(async () => {
      const items = rows.filter((tr) => tr.querySelector("input[type=checkbox]").checked).map((tr) => {
        const [, q, name] = tr.querySelectorAll("input");
        return { ...tr._item, quantity: +q.value || 0, name: name.value, category: tr.querySelector("select").value };
      });
      if (!items.length) return toast("Tick at least one", true);
      const res = await api("POST", "items/bulk", { items, location_id: loc.value });
      toast(`Added ${res.added} item${res.added === 1 ? "" : "s"}` + (res.problems.length ? `; ${res.problems.length} skipped` : ""), !!res.problems.length);
      if (res.problems.length) $("#paste-rows").prepend(h("p", { class: "bad" }, res.problems.join(" ")));
      else { $("#paste-dialog").close(); $("#paste-text").value = ""; }
      refresh();
    }) }, "Add the ticked items")));
}));

const selectedItems = new Set();
function renderInventory() {
  if (!$("#item-category").options.length) { fillCategorySelects(); updateItemFormForCategory(); }
  fillLocationSelects();
  const q = $("#inv-search").value.toLowerCase();
  const cat = $("#inv-filter").value;
  const loc = $("#inv-loc-filter").value;
  const expired = new Set(S.analysis.expired.map((i) => i.id));
  const rows = S.items.filter((i) => (!cat || i.category === cat) &&
    (!loc || (loc === "none" ? !i.location_id : inLocation(i, loc))) &&
    (!q || (i.name + " " + i.location + " " + i.notes).toLowerCase().includes(q)));
  for (const id of [...selectedItems]) if (!S.items.some((i) => i.id === id)) selectedItems.delete(id);
  updateMoveBar();
  $("#ammo-card").hidden = !(S.ammo || []).length;
  $("#ammo-table tbody").replaceChildren(...(S.ammo || []).map((a) => h("tr", {}, h("td", {}, a.caliber),
    h("td", {}, a.rounds ? fmtNum(a.rounds) : h("span", { class: "warn" }, "none")),
    h("td", {}, Object.entries(a.by_type).map(([k, v]) => `${k.split(" (")[0]}: ${fmtNum(v)}`).join(" · ")),
    h("td", {}, a.in_mags ? fmtNum(a.in_mags) : ""), h("td", {}, a.firearms.join(", ")))));
  $("#mag-card").hidden = !(S.magazines || []).some((m) => m.magazines);
  $("#mag-table tbody").replaceChildren(...(S.magazines || []).map((m) => h("tr", {}, h("td", {}, m.firearm, m.caliber ? h("div", { class: "muted" }, m.caliber) : null),
    h("td", {}, m.magazines ? fmtNum(m.magazines) : h("span", { class: "warn" }, "none")), h("td", {}, m.capacity.map(fmtNum).join(", ")),
    h("td", {}, fmtNum(m.loaded)), h("td", {}, m.service ? h("span", { class: "warn" }, fmtNum(m.service)) : "0"))));
  $("#inv-table tbody").replaceChildren(...(rows.length ? rows.map((i) => {
    const qty = i.qty_text || `${fmtNum(i.quantity)} ${i.unit}`;
    return h("tr", { class: expired.has(i.id) ? "expired" : "" },
      h("td", {}, h("input", { type: "checkbox", checked: selectedItems.has(i.id), "aria-label": "Select " + i.name,
        onchange: (e) => { e.target.checked ? selectedItems.add(i.id) : selectedItems.delete(i.id); updateMoveBar(); } })),
      h("td", {}, i.name, i.category === "firearms" && (i.caliber || i.serial || i.subtype) ? h("div", { class: "muted" },
          [S.subtypes[i.subtype], i.caliber, (S.ammo_types || {})[i.ammo_type], i.serial && "SN " + i.serial,
           i.fits && (S.items.find((g) => g.uid === i.fits) || {}).name && "fits " + S.items.find((g) => g.uid === i.fits).name]
            .filter(Boolean).join(" · ")) : null,
        i.notes ? h("div", { class: "muted" }, i.notes) : null),
      h("td", { class: "c-cat" }, S.categories[i.category] || i.category),
      h("td", { class: "c-qty" }, qty),
      h("td", { class: "c-ess" }, i.essential ? (essentialById(i.essential) || {}).label : ""),
      h("td", { class: "c-loc" }, i.location), h("td", { class: "c-exp" }, i.expires),
      h("td", { class: "c-meta" }, [S.categories[i.category] || i.category, i.location && "\u{1F4CD}" + i.location,
        i.expires && "expires " + i.expires].filter(Boolean).join(" · ")),
      h("td", { class: "c-act" }, h("button", { class: "link", onclick: () => editItem(i) }, "Edit"),
        h("button", { class: "danger", onclick: () => confirm(`Delete "${i.name}"?`) &&
          guarded(async () => { await api("DELETE", "items/" + i.id); await refresh(); }) }, "Delete")));
  }) : [h("tr", {}, h("td", { colspan: 8, class: "muted" }, S.items.length ? "No matches." : "No items yet. Start with water and food."))]));
}
function updateMoveBar() {
  $("#move-bar").hidden = !selectedItems.size;
  $("#move-count").textContent = `${selectedItems.size} selected`;
}
$("#inv-search").addEventListener("input", renderInventory);
$("#inv-filter").addEventListener("change", renderInventory);
$("#inv-loc-filter").addEventListener("change", renderInventory);
$("#inv-all").addEventListener("change", (e) => {
  $$("#inv-table tbody input[type=checkbox]").forEach((cb) => { if (cb.checked !== e.target.checked) cb.click(); });
});
$("#move-btn").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "items/move", { ids: [...selectedItems], location_id: $("#move-target").value });
  selectedItems.clear(); $("#inv-all").checked = false;
  toast(`Moved ${r.moved} item${r.moved === 1 ? "" : "s"}`);
  await refresh();
}));

/* ---------- alerts ---------- */
function renderAlerts() {
  const list = S.alerts || [];
  const stale = S.sites.filter((x) => x.alerts_error);
  $("#alerts-card").hidden = !list.length && !stale.length;
  const sevClass = { Extreme: "bad", Severe: "bad", Moderate: "warn" };
  $("#alerts-list").replaceChildren(
    ...list.map((a) => h("div", { class: "alert" },
      h("div", {}, h("span", { class: "sev " + (sevClass[a.severity] || "") }, a.event), h("span", { class: "tag" }, a.site),
        a.ends || a.expires ? h("span", { class: "muted" }, ` until ${when(a.ends || a.expires)}`) : null),
      h("div", {}, a.headline),
      h("details", {}, h("summary", {}, "Details"), h("p", {}, a.description), a.instruction ? h("p", {}, h("strong", {}, "What to do: "), a.instruction) : null,
        h("p", { class: "muted" }, `${a.senderName} · as of ${when(a.fetched)}`)))),
    ...stale.map((x) => h("p", { class: "muted" }, `${x.name}: couldn't refresh alerts (${x.alerts_error}); showing data as of ${when(x.alerts_at)}.`)));
}

/* ---------- locations & kits ---------- */
let editingLoc = null;
function renderLocations() {
  if (!$("#loc-kind").options.length)
    $("#loc-kind").replaceChildren(...Object.entries(S.location_kinds).map(([k, v]) => h("option", { value: k }, v)));
  if (!editingLoc) fillParentSelect();
  const kitBy = Object.fromEntries(S.kits.map((k) => [k.location_id, k]));
  $("#loc-table tbody").replaceChildren(...(S.locations.length ? S.locations.map((l) => {
    const k = kitBy[l.id];
    return h("tr", {},
      h("td", {}, l.name, l.person ? h("div", { class: "muted" }, l.person) : null),
      h("td", {}, S.location_kinds[l.kind].split(" (")[0] + (k ? ` · ${l.people} ${l.people === 1 ? "person" : "people"}` : "")),
      h("td", {}, l.item_count),
      h("td", {}, l.counts_home ? "Yes" : "No"),
      h("td", {}, k ? h("span", { class: tone(k.score) }, k.score + "%") : "—"),
      h("td", {},
        h("a", { class: "btn small", href: "/api/packing/" + l.id, target: "_blank", rel: "noopener" }, "Packing list"),
        h("button", { class: "link", onclick: () => editLocation(l) }, "Edit"),
        h("button", { class: "danger", onclick: () => confirm(`Delete location "${l.name}"? Its ${l.item_count} items are kept with no location.`) &&
          guarded(async () => { await api("DELETE", "locations/" + l.id); await refresh(); }) }, "Delete")));
  }) : [h("tr", {}, h("td", { colspan: 6, class: "muted" }, "No locations yet."))]));

  $("#kit-details").replaceChildren(...S.kits.map((k) => h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", {}, `${k.name}: ${k.score}% ready`), h("span", { class: "muted" }, `${k.template}, ${k.people} ${k.people === 1 ? "person" : "people"}`)),
    k.expired.length ? h("p", { class: "bad" }, `${k.expired.length} expired item(s) here aren't counted.`) : null,
    h("ol", { class: "gaps" }, ...(k.gaps.length ? k.gaps.map((g) => h("li", {},
      h("span", { class: tone(Math.round(g.coverage * 100)) }, g.label), g.hazard ? h("span", { class: "tag hz" }, g.hazard) : null, " ",
      h("span", { class: "meta" }, `${fmtNum(g.have)} of ${fmtNum(g.need)}${g.unit ? " " + g.unit : ""}${g.detail ? " · " + g.detail : ""}`), " ",
      h("button", { class: "link", onclick: () => startItemFor(g, k.location_id) }, "Add")))
      : [h("li", {}, "Complete. Check expiry dates when you rotate supplies.")])))));
}
function editLocation(l) {
  editingLoc = l.id;
  const f = $("#loc-form");
  for (const k of ["kind", "person", "people", "notes"]) f[k].value = l[k];
  f.name.value = l.label || l.name;
  fillParentSelect();
  f.parent_id.value = l.parent_id ?? ""; f.parent_id.disabled = true;  // where it sits is chosen when it's added
  syncParentFields();
  f.counts_home.checked = !!l.counts_home;
  $("#loc-form-title").textContent = "Edit location";
  $("#loc-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth" });
}
function fillParentSelect() {
  const sel = $("#loc-parent"), keep = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "Nothing (top level)"),
    ...S.locations.filter((l) => (l.depth || 0) < 2).map((l) => h("option", { value: l.id }, l.name)));
  sel.value = keep;
}
function resetLocForm() {
  editingLoc = null; $("#loc-form").reset(); fillParentSelect(); $("#loc-parent").disabled = false; syncParentFields();
  $("#loc-form-title").textContent = "Add location"; $("#loc-cancel").hidden = true;
}
$("#loc-cancel").addEventListener("click", resetLocForm);
$("#loc-kind").addEventListener("change", (e) => {
  if (!editingLoc) $("#loc-counts").checked = ["home", "gobag"].includes(e.target.value);
});
function syncParentFields() {
  const f = $("#loc-form"), inside = !!f.parent_id.value;
  for (const k of ["kind", "person", "people", "counts_home"]) { f[k].disabled = inside; f[k].closest("label").hidden = inside; }
  $("#loc-inherits").hidden = !inside;
}
$("#loc-parent").addEventListener("change", syncParentFields);
$("#loc-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const data = { name: f.name.value, parent_id: f.parent_id.value, kind: f.kind.value, person: f.person.value, people: f.people.value,
    notes: f.notes.value, counts_home: f.counts_home.checked ? 1 : 0 };
  guarded(async () => {
    await api("POST", "locations" + (editingLoc ? "/" + editingLoc : ""), data);
    toast(editingLoc ? "Location updated" : "Location added");
    resetLocForm(); await refresh();
  });
});

/* ---------- shopping ---------- */
function renderShopping() {
  const qty = (r) => r.unit === "kcal" ? `${r.qty.toLocaleString()} kcal` : `${fmtNum(r.qty)} ${r.unit}`.trim();
  $("#shop-table tbody").replaceChildren(...(S.shopping.length ? S.shopping.map((r) => h("tr", {},
    h("td", {}, r.label, r.hazard ? h("span", { class: "tag hz" }, r.hazard) : null),
    h("td", {}, qty(r)), h("td", {}, r.where),
    h("td", {}, h("button", { class: "link", onclick: () => startItemFor(
      { id: r.essential || (r.category === "water" ? "water" : "food"), category: r.category, unit: r.unit }, r.location_id,
      r.unit === "gal" || r.unit === "kcal" ? null : r.qty) }, "Bought it"))))
    : [h("tr", {}, h("td", { colspan: 4, class: "muted" }, "Nothing to buy: every target is met."))]));
}

/* ---------- sites ---------- */
let editingSite = null;
function renderSites() {
  if (!$("#site-kind").options.length)
    $("#site-kind").replaceChildren(...Object.entries(S.site_kinds).map(([k, v]) => h("option", { value: k }, v)));
  $("#site-cards").replaceChildren(...(S.sites.length ? S.sites.map((x) => {
    const ratings = Object.entries(x.hazards?.ratings || {}).filter(([, r]) => LEVEL[r])
      .sort((a, b) => LEVEL[b[1]] - LEVEL[a[1]]);
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("h2", {}, x.name, h("span", { class: "tag" }, S.site_kinds[x.kind])),
        h("div", { class: "row-right" },
          h("button", { class: "link", onclick: (e) => { e.target.disabled = true; guarded(async () => { await api("POST", `sites/${x.id}/refresh`); await refresh(); toast("Refreshed"); }); } }, "Refresh now"),
          h("button", { class: "link", onclick: () => editSite(x) }, "Edit"),
          h("button", { class: "danger", onclick: () => confirm(`Delete site "${x.name}"?`) &&
            guarded(async () => { await api("DELETE", "sites/" + x.id); await refresh(); }) }, "Delete"))),
      h("p", { class: "muted" }, [x.address, x.lat != null ? `${x.lat.toFixed(4)}, ${x.lon.toFixed(4)}` : "no coordinates",
        x.county_name].filter(Boolean).join(" · ")),
      h("p", {}, x.alerts.length ? h("span", { class: "bad" }, `${x.alerts.length} active alert(s): ${x.alerts.map((a) => a.event).join(", ")}`) : "No active weather alerts.",
        h("span", { class: "muted" }, ` As of ${when(x.alerts_at)}.`), x.alerts_error ? h("span", { class: "warn" }, ` Last refresh failed: ${x.alerts_error}`) : null),
      ratings.length ? h("details", {}, h("summary", {}, `Hazard ratings (FEMA NRI${x.hazards.overall ? ", overall " + x.hazards.overall : ""})`),
        h("table", { class: "hz-table" }, h("tbody", {}, ratings.map(([c, r]) =>
          h("tr", {}, h("td", {}, S.hazard_names[c]), h("td", { class: "lvl-" + LEVEL[r] }, r))))))
        : h("p", { class: "muted" }, x.county_fips ? "Hazard ratings not loaded yet. Try Refresh now." : "No county found, so no hazard ratings."));
  }) : [h("div", { class: "card" }, h("p", { class: "muted" }, "No sites yet. Start with your home: it sets the hazard profile."))]));
}
function editSite(x) {
  editingSite = x.id;
  const f = $("#site-form");
  for (const k of ["name", "kind", "address", "notes"]) f[k].value = x[k] || "";
  f.lat.value = x.lat ?? ""; f.lon.value = x.lon ?? "";
  $("#site-form-title").textContent = "Edit site"; $("#site-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth" });
}
function resetSiteForm() {
  editingSite = null; $("#site-form").reset();
  $("#site-form-title").textContent = "Add site"; $("#site-cancel").hidden = true;
}
$("#site-cancel").addEventListener("click", resetSiteForm);
$("#site-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  if (editingSite) {  // typed coordinates win only if the user changed them
    const old = S.sites.find((x) => x.id === editingSite);
    if (old && data.address !== old.address && String(old.lat) === data.lat && String(old.lon) === data.lon) { data.lat = ""; data.lon = ""; }
  }
  const btn = $("#site-save"); btn.disabled = true; btn.textContent = "Looking up…";
  guarded(async () => {
    const r = await api("POST", "sites" + (editingSite ? "/" + editingSite : ""), data);
    toast(`Saved ${r.name}${r.county_name ? " (" + r.county_name + ")" : ""}`);
    resetSiteForm(); await refresh();
  }).finally(() => { btn.disabled = false; btn.textContent = "Save site"; });
});

/* ---------- documents ---------- */
let docSizes = {};
function renderDocuments() {
  if (!$("#doc-type").options.length)
    $("#doc-type").replaceChildren(...Object.entries(S.doc_types).map(([k, v]) => h("option", { value: k }, v)));
  const have = new Set(S.documents.map((d) => d.doc_type));
  $("#doc-coverage").replaceChildren(...Object.entries(S.doc_types).filter(([k]) => k !== "other" && (k !== "pets" || S.settings.pets > 0))
    .map(([k, v]) => h("span", { class: "chip " + (have.has(k) ? "ok" : "missing") }, (have.has(k) ? "✓ " : "✗ ") + v)));
  const prio = { 1: "Critical", 2: "Important", 3: "Useful" };
  $("#doc-table tbody").replaceChildren(...(S.documents.length ? S.documents.map((d) => {
    const info = docSizes[d.id];
    const sel = h("select", { onchange: (e) => guarded(async () => { await api("POST", "documents/" + d.id, { ...d, priority: e.target.value }); await refresh(); }) },
      [1, 2, 3].map((p) => h("option", { value: p, selected: p === d.priority }, `${p} - ${prio[p]}`)));
    const rn = info && info.renewal;
    const ref = [d.insurer, d.coverage, d.claim_phone ? "claims " + d.claim_phone : ""].filter(Boolean).join(" · ");
    return h("tr", {},
      h("td", {}, sel), h("td", {}, d.label, ref ? h("div", { class: "muted doc-ref" }, ref) : null), h("td", {}, S.doc_types[d.doc_type]),
      h("td", {}, d.renew_on ? h("span", { class: rn && rn.state === "overdue" ? "bad" : rn && rn.state === "soon" ? "warn" : "" },
        d.renew_on + (rn ? { overdue: " · overdue", soon: ` · in ${rn.days} d`, done: " · done", later: "" }[rn.state] : "")) : h("span", { class: "muted" }, "—")),
      h("td", { class: info && !info.exists ? "bad" : "" }, info ? (info.exists ? gb(info.size) : "missing!") : "…"),
      h("td", { class: "path" }, d.path),
      h("td", { class: "doc-acts" }, h("button", { onclick: () => editDocument(d) }, "Edit"), " ",
        h("button", { class: "danger", onclick: () => confirm(`Remove "${d.label}" from the kit? The file itself is not touched.`) &&
        guarded(async () => { await api("DELETE", "documents/" + d.id); await refresh(); loadDocSizes(); }) }, "Remove")));
  }) : [h("tr", {}, h("td", { colspan: 7, class: "muted" }, "No documents registered yet."))]));
}

async function loadDocSizes() {
  const list = await guarded(() => api("GET", "documents"));
  if (!list) return;
  docSizes = Object.fromEntries(list.map((d) => [d.id, d]));
  renderDocuments();
}

let editingDoc = null;
function editDocument(d) {
  const f = $("#doc-form");
  editingDoc = d.id;
  for (const k of ["path", "label", "doc_type", "priority", "renew_on", "insurer", "coverage", "claim_phone"]) f[k].value = d[k] ?? "";
  f.path.readOnly = true; $("#doc-browse").hidden = true;
  $("#doc-submit").textContent = "Save changes"; $("#doc-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "start" }); f.label.focus();
}
function stopEditingDoc() {
  const f = $("#doc-form");
  editingDoc = null; f.reset(); f.path.readOnly = false; $("#doc-browse").hidden = false;
  $("#doc-submit").textContent = "Add document"; $("#doc-cancel").hidden = true;
}
$("#doc-cancel").addEventListener("click", stopEditingDoc);
$("#doc-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  guarded(async () => {
    await api("POST", "documents" + (editingDoc ? "/" + editingDoc : ""), data);
    toast(editingDoc ? "Saved" : "Document registered");
    stopEditingDoc();
    await refresh();
    loadDocSizes();
  });
});

/* folder browser */
let browseCurrent = null;
async function browseTo(path) {
  const r = await guarded(() => api("GET", "browse?path=" + encodeURIComponent(path || "")));
  if (!r) return;
  browseCurrent = r;
  $("#browse-path").textContent = r.path;
  $("#browse-list").replaceChildren(...r.entries.map((en) => h("li", { class: en.dir ? "dir" : "" },
    en.dir ? h("span", { onclick: () => browseTo(en.path) }, "📁 " + en.name) : h("span", {}, en.name),
    h("span", {}, en.dir ? "" : gb(en.size) + " ",
      h("button", { class: "link", onclick: () => pickPath(en.path) }, "Choose")))));
}
function pickPath(p) { $("#doc-path").value = p; $("#browse-dialog").close(); }
$("#doc-browse").addEventListener("click", () => { $("#browse-dialog").showModal(); browseTo($("#doc-path").value); });
$("#browse-up").addEventListener("click", () => browseCurrent && browseTo(browseCurrent.parent));
$("#browse-pick").addEventListener("click", () => browseCurrent && pickPath(browseCurrent.path));
$("#browse-close").addEventListener("click", () => $("#browse-dialog").close());

/* ---------- USB kit ---------- */
async function loadDrives() {
  const list = await guarded(() => api("GET", "drives"));
  if (!list) return;
  const box = $("#drive-list");
  if (!list.length) {
    box.replaceChildren(h("p", { class: "muted" }, "No external drives found. Plug in a USB drive (it should mount automatically) and press Refresh, or type a folder path below."));
    return;
  }
  box.replaceChildren(...list.map((d) => h("button", {
    class: "drive" + (d.mount === selectedMount ? " selected" : ""),
    onclick: () => { selectedMount = d.mount; $("#drive-custom").value = ""; loadDrives(); $("#plan-out").replaceChildren(); },
  },
    h("div", { class: "d-name" }, d.label || d.model || d.device),
    h("div", { class: "d-meta" }, `${d.mount} · ${d.fstype || "?"}${d.removable ? " · removable" : ""}`),
    h("div", { class: "d-meta" }, `${gb(d.free)} free of ${gb(d.total)}`),
    !d.writable ? h("div", { class: "bad d-meta" }, "not writable") : null,
    /fat|msdos/i.test(d.fstype) && d.fstype !== "exfat" ? h("div", { class: "warn d-meta" }, "FAT32: files over 4 GB can't be stored") : null)));
}
$("#drive-refresh").addEventListener("click", loadDrives);
$("#drive-custom").addEventListener("input", (e) => { selectedMount = e.target.value.trim(); $$(".drive").forEach((d) => d.classList.remove("selected")); });

async function loadLibraries() {
  $("#lib-list").replaceChildren(h("p", { class: "muted" }, "Looking up current sizes from the Kiwix catalog…"));
  const r = await guarded(() => api("GET", "library"));
  if (!r) return;
  libraries = r;
  renderLibraries();
}

function renderLibraries() {
  const disabled = new Set(S.settings.kiwix_disabled);
  const out = [];
  let tier = null;
  for (const l of libraries.libraries) {
    if (l.tier !== tier) {
      tier = l.tier;
      out.push(h("div", { class: "tier" }, { 1: "Tier 1: survival essentials", 2: "Tier 2: skills and reference", 3: "Tier 3: general knowledge (one version is picked)" }[tier]));
    }
    out.push(h("label", { class: "lib" },
      h("input", { type: "checkbox", checked: !disabled.has(l.id), onchange: (e) => toggleLib(l.id, e.target.checked) }),
      h("div", {}, h("div", {}, l.why),
        h("div", { class: "l-sub" }, l.online ? `${l.file} · ${l.date}` : `estimate only (${l.error || "offline"})`)),
      h("span", { class: "num" }, (l.online ? "" : "~") + gb(l.size))));
  }
  out.push(h("div", { class: "tier" }, "Readers (always included)"));
  for (const t of libraries.tools)
    out.push(h("div", { class: "lib" }, h("span", {}), h("div", {}, t.why), h("span", { class: "num" }, gb(t.size))));
  $("#lib-list").replaceChildren(...out);
}

async function toggleLib(id, on) {
  const set = new Set(S.settings.kiwix_disabled);
  on ? set.delete(id) : set.add(id);
  await guarded(async () => { S.settings = await api("POST", "settings", { kiwix_disabled: [...set] }); });
  $("#plan-out").replaceChildren();
}

$("#plan-btn").addEventListener("click", () => {
  if (!selectedMount) return toast("Choose a drive first", true);
  $("#plan-out").replaceChildren(h("p", { class: "muted" }, "Measuring documents and checking sizes…"));
  guarded(async () => {
    const r = await api("POST", "plan", { mount: selectedMount });
    renderPlan(r.drive, r.plan);
    return true;
  }).then((ok) => ok || $("#plan-out").replaceChildren());
});

function renderPlan(drive, plan) {
  const cap = plan.free + plan.reusable;
  const byKind = {};
  for (const i of plan.included) byKind[i.kind] = (byKind[i.kind] || 0) + i.size;
  const names = { kit: "Inventory & readme", document: "Encrypted documents", tool: "Readers", library: "Offline libraries" };
  const segs = Object.entries(byKind).map(([k, v]) => h("div", { class: "k-" + k, style: `width:${(100 * v / cap).toFixed(2)}%`, title: `${names[k]}: ${gb(v)}` }));
  const out = [
    h("p", {}, `${drive.mount} (${drive.fstype}): planning ${gb(plan.used)} of ${gb(plan.budget)} usable` +
      (plan.reusable ? `, ${gb(plan.reusable)} already on the drive is reused` : "") + "."),
    h("div", { class: "space" }, ...segs),
    h("div", { class: "legend" }, ...Object.entries(byKind).map(([k, v]) => h("span", {}, h("i", { class: "k-" + k }), `${names[k]} ${gb(v)}`))),
  ];
  if (!plan.documents_complete) out.push(h("p", { class: "bad" }, "Not all documents fit. Lower-priority ones are left out (see below). Use a bigger drive to include them all."));
  if (plan.split_documents) out.push(h("p", { class: "warn" }, "Documents will be split into 3.9 GB parts because the drive is FAT32."));
  out.push(h("h2", { style: "margin-top:14px" }, "Included"),
    h("ul", { class: "plan-list" }, plan.included.map((i) => h("li", {}, h("span", {}, i.label, i.note ? h("span", { class: "why" }, ` · ${i.note}`) : null), h("span", { class: "num" }, gb(i.size))))));
  if (plan.skipped.length)
    out.push(h("h2", { style: "margin-top:14px" }, "Left out"),
      h("ul", { class: "plan-list" }, plan.skipped.map((s) => h("li", {}, h("span", {}, s.label, h("span", { class: "why" }, ` · ${s.reason}`)), h("span", { class: "num" }, s.size ? gb(s.size) : "")))));
  $("#plan-out").replaceChildren(...out);
}

$("#build-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!selectedMount) return toast("Choose a drive first", true);
  const f = e.target;
  if (f.pass1.value !== f.pass2.value) return toast("Passphrases don't match", true);
  guarded(async () => {
    await api("POST", "build", { mount: selectedMount, passphrase: f.pass1.value });
    f.reset();
    pollJob();
  });
});
$("#cancel-btn").addEventListener("click", () => guarded(() => api("POST", "cancel")));

async function pollJob() {
  clearTimeout(pollTimer);
  const j = await api("GET", "job").catch(() => null);
  if (!j || j.state === "idle") return;
  $("#job-out").hidden = false;
  const running = j.state === "running";
  $("#build-btn").disabled = running;
  $("#cancel-btn").hidden = !running;
  const pct = j.bytes_total ? (100 * j.bytes_done / j.bytes_total) : (running ? 3 : 100);
  $("#job-bar").style.width = Math.min(100, pct).toFixed(1) + "%";
  $("#job-step").textContent = running
    ? `${j.step}${j.bytes_total ? ` · ${gb(j.bytes_done)} of ${gb(j.bytes_total)}` : ""}`
    : { done: "Finished.", failed: "Build failed, see log.", cancelled: "Cancelled." }[j.state];
  const log = $("#job-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 10;
  log.textContent = j.log.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (running) pollTimer = setTimeout(pollJob, 1000);
}

/* ---------- backups ---------- */
function renderBackups() {
  $("#backup-table tbody").replaceChildren(...(S.backups.length ? S.backups.map((b) => h("tr", {},
    h("td", {}, b.name), h("td", { class: "path" }, b.source), h("td", { class: "path" }, b.dest), h("td", {}, b.keep),
    h("td", { class: b.last_status.startsWith("FAILED") ? "bad" : "" }, b.last_run ? `${b.last_run.replace("T", " ")}: ${b.last_status}` : "never"),
    h("td", {},
      h("button", { class: "link", onclick: (e) => {
        e.target.disabled = true; e.target.textContent = "Running…";
        guarded(async () => { const r = await api("POST", `backups/${b.id}/run`); toast(r.status, r.status.startsWith("FAILED")); }).finally(refresh);
      } }, "Run now"),
      h("button", { class: "danger", onclick: () => confirm(`Delete job "${b.name}"? Existing snapshots are kept.`) &&
        guarded(async () => { await api("DELETE", "backups/" + b.id); await refresh(); }) }, "Delete"))))
    : [h("tr", {}, h("td", { colspan: 6, class: "muted" }, "No backup jobs yet."))]));
}
$("#backup-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "backups", Object.fromEntries(new FormData(e.target)));
    e.target.reset();
    toast("Backup job added");
    await refresh();
  });
});

/* ---------- household ---------- */
function renderHousehold() {
  const f = $("#settings-form");
  for (const k of ["adults", "children", "pets", "target_days", "water_gal_per_person", "water_gal_per_pet", "kcal_per_person", "cash_per_person"])
    if (document.activeElement !== f[k]) f[k].value = S.settings[k];
  const fromPeople = !!S.settings.household_from_people;
  f.adults.disabled = f.children.disabled = fromPeople;
  $("#household-from-people").hidden = !fromPeople;
  const na = new Set(S.settings.not_applicable);
  const opts = [...S.essentials.map((e) => [e.id, e.label]),
    ...Object.entries(S.doc_types).filter(([k]) => k !== "other").map(([k, v]) => ["doc_" + k, "Document: " + v])];
  $("#na-list").replaceChildren(...opts.map(([id, label]) => h("label", {},
    h("input", { type: "checkbox", checked: na.has(id), onchange: (e) => {
      e.target.checked ? na.add(id) : na.delete(id);
      guarded(async () => { await api("POST", "settings", { not_applicable: [...na] }); await refresh(); });
    } }), label)));
}
$("#settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  guarded(async () => { await api("POST", "settings", data); toast("Household saved"); await refresh(); });
});

/* ---------- drive check (USB kit tab) ---------- */
function runDriveCheck(verify) {
  if (!selectedMount) return toast("Choose a drive first", true);
  guarded(async () => {
    await api("POST", "depot/drive-check", { mount: selectedMount, verify });
    $("#drive-check-out").replaceChildren(h("p", { class: "muted" }, verify ? "Checking and verifying…" : "Checking…"));
    pollDriveCheck();
  });
}
async function pollDriveCheck() {
  const j = await api("GET", "depot/job").catch(() => null);
  if (!j) return;
  if (j.state === "running") { $("#drive-check-out").replaceChildren(h("p", { class: "muted" }, j.step || "Working…")); return setTimeout(pollDriveCheck, 1000); }
  const r = j.result;
  const out = [h("pre", {}, j.log.join("\n"))];
  if (r && r.out_of_date.length) out.unshift(h("p", { class: "warn" }, `${r.out_of_date.length} file(s) out of date. Rebuild the kit on this drive to refresh them.`));
  else if (r) out.unshift(h("p", { class: "good" }, "Everything on this drive is current."));
  $("#drive-check-out").replaceChildren(...out);
}
$("#drive-check").addEventListener("click", () => runDriveCheck(false));
$("#drive-verify").addEventListener("click", () => runDriveCheck(true));

/* ---------- nodes ---------- */
let N = null, editingNode = null;
const PROFILE_KEYS = ["documents", "readers", "maps", "radio", "firmware", "apps"];
$("#nodelock-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => { await api("POST", "nodes/lock-passphrase", { passphrase: f.passphrase.value, repeat: f.repeat.value });
    f.reset(); toast("Saved. Nodes pick it up at their next sync."); loadNodes(); });
});
async function loadNodes() {
  const r = await guarded(() => api("GET", "nodes"));
  if (!r) return;
  N = r;
  $("#nodelock-status").replaceChildren(r.lock_passphrase_set
    ? h("span", {}, `Passphrase set ${when(r.lock_passphrase_set)}. Saving a new one replaces it on every node at its next sync.`)
    : h("strong", { class: "warn" }, "No passphrase yet: locked nodes can only unlock at home."));
  if (!$("#node-role").options.length) {
    $("#node-role").replaceChildren(...Object.entries(N.roles).map(([k, v]) => h("option", { value: k }, v)));
    $("#node-role").value = "pi";
    setProfileForm(N.defaults[$("#node-role").value]);
  }
  renderNodes();
}
function setProfileForm(prof) {
  const f = $("#node-form");
  for (const k of PROFILE_KEYS) f[k].checked = !!prof[k];
  for (const t of [1, 2, 3]) f["tier" + t].checked = (prof.tiers || []).includes(t);
}
function profileFromForm() {
  const f = $("#node-form"), prof = {};
  for (const k of PROFILE_KEYS) prof[k] = f[k].checked;
  prof.tiers = [1, 2, 3].filter((t) => f["tier" + t].checked);
  return prof;
}
$("#node-role").addEventListener("change", (e) => { if (!editingNode && N) setProfileForm(N.defaults[e.target.value]); });
function ago(days) { return days === null || days === undefined ? "never" : days === 0 ? "today" : `${days} day${days === 1 ? "" : "s"} ago`; }
function renderNodes() {
  $("#node-cards").replaceChildren(...(N.nodes.length ? N.nodes.map((n) => {
    const st = n.status;
    const behind = st.behind === null ? h("span", { class: "muted" }, "not reported yet")
      : st.behind ? h("span", { class: "warn" }, `${st.behind} file(s), ${gb(st.behind_bytes)}`) : h("span", { class: "good" }, "up to date");
    const arch = st.archive ? h("span", { class: st.archive.stale ? "warn" : "" },
      `${st.archive.age_days ?? "?"} days old${st.archive.stale ? " · documents changed since, rebuild" : ""}`) : h("span", { class: "muted" }, "none");
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("h2", {}, n.name, h("span", { class: "tag" }, N.roles[n.role]), n.paired ? h("span", { class: "tag" }, "paired") : null,
          n.lost ? h("span", { class: "tag hz" }, "lost or stolen") : n.lock === "on" || n.lock === "locked" ? h("span", { class: "tag" }, "locked data")
            : n.lock === "waiting" ? h("span", { class: "tag hz" }, "lock waiting for key") : null),
        h("div", { class: "row-right" },
          ["pi", "laptop"].includes(n.role) ? h("button", { class: "link", onclick: () => pairNode(n) }, n.paired ? "Re-pair" : "Pair") : null,
          h("button", { class: "link", onclick: () => showNodePlan(n) }, "What it carries"),
          h("button", { class: "link", onclick: () => editNode(n) }, "Edit"),
          ["pi", "laptop"].includes(n.role) && n.paired ? h("button", { class: "link", onclick: () =>
            confirm(`Mark "${n.name}" lost or stolen? It is cut off from this hub and can't get the key to unlock. Pair it again if it turns up.`) &&
            guarded(async () => { await api("POST", `nodes/${n.id}/lost`); loadNodes(); }) }, "Lost or stolen") : null,
          h("button", { class: "danger", onclick: () => confirm(`Remove node "${n.name}"? Nothing on the device itself is deleted.`) &&
            guarded(async () => { await api("DELETE", "nodes/" + n.id); loadNodes(); }) }, "Remove"))),
      h("div", { class: "node-grid" },
        h("div", {}, h("div", { class: "k" }, "Last synced"), h("div", { class: "v" + (st.last_sync_days > 30 ? " warn" : "") }, ago(st.last_sync_days))),
        h("div", {}, h("div", { class: "k" }, "Updates behind"), h("div", { class: "v" }, behind)),
        h("div", {}, h("div", { class: "k" }, "Planned content"), h("div", { class: "v" }, `${st.files_planned} files, ${gb(n.plan_bytes)}` +
          (n.budget_bytes ? ` of ${gb(n.budget_bytes)}` : ""))),
        h("div", {}, h("div", { class: "k" }, "Major updates waiting"), h("div", { class: "v" + (st.majors_pending ? " warn" : "") }, st.majors_pending || "none")),
        h("div", {}, h("div", { class: "k" }, "Document archive"), h("div", { class: "v" }, arch))),
      st.reminders.length ? h("ul", { class: "plan-list" }, st.reminders.map((r) => h("li", {},
        h("span", { class: r.overdue ? "warn" : "" }, `${r.label}: ${r.overdue ? "due now" : "next " + r.due}`),
        h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", `nodes/${n.id}/reminder`, { kind: r.kind }); loadNodes(); toast("Marked done"); }) }, "Done today")))) : null,
      h("div", { id: "node-extra-" + n.id }));
  }) : [h("div", { class: "card" }, h("p", { class: "muted" }, "No nodes yet. Kit drives appear here after their first build."))]));
}
async function pairNode(n) {
  const r = await guarded(() => api("POST", `nodes/${n.id}/pair`));
  if (!r) return;
  const hub = location.origin;
  $("#node-extra-" + n.id).replaceChildren(h("div", { class: "card" },
    h("p", {}, "On the node, run this within 15 minutes. If this page is open through the internet address, replace the hub address with the hub's home-network address (for example http://192.168.1.40:8765):"),
    h("pre", {}, `sudo -u bunkr python3 -m bunkr pair --hub ${hub} --code ${r.code} --token-file /etc/bunkr/node.token`),
    h("p", {}, "Pairing code: ", h("span", { class: "pair-code" }, r.code)),
    h("p", { class: "muted" }, "The code works once. The node then syncs every hour over the home network.")));
}
async function showNodePlan(n) {
  const r = await guarded(() => api("GET", `nodes/${n.id}/plan`));
  if (!r) return;
  $("#node-extra-" + n.id).replaceChildren(h("ul", { class: "plan-list" },
    ...r.files.map((f) => h("li", {}, h("span", {}, f.path, h("span", { class: "why" }, ` · ${f.kind} ${f.version}`)), h("span", { class: "num" }, gb(f.size)))),
    ...r.skipped.map((f) => h("li", {}, h("span", { class: "warn" }, f.path, h("span", { class: "why" }, ` · ${f.reason}`)), h("span", { class: "num" }, gb(f.size)))),
    r.files.length || r.skipped.length ? null : h("li", { class: "muted" }, "Nothing in the downloads store yet. Fetch libraries on the Downloads tab.")));
}
function editNode(n) {
  editingNode = n.id;
  const f = $("#node-form");
  f.name.value = n.name; f.role.value = n.role; f.notes.value = n.notes;
  f.budget_gb.value = n.budget_bytes ? Math.round(n.budget_bytes / 1e9) : "";
  setProfileForm(n.profile);
  $("#node-form-title").textContent = "Edit node"; $("#node-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth" });
}
function resetNodeForm() {
  editingNode = null; $("#node-form").reset();
  if (N) setProfileForm(N.defaults[$("#node-role").value]);
  $("#node-form-title").textContent = "Add node"; $("#node-cancel").hidden = true;
}
$("#node-cancel").addEventListener("click", resetNodeForm);
$("#node-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => {
    await api("POST", "nodes" + (editingNode ? "/" + editingNode : ""), { name: f.name.value, role: f.role.value,
      budget_gb: f.budget_gb.value || 0, notes: f.notes.value, profile: profileFromForm() });
    toast(editingNode ? "Node updated" : "Node added"); resetNodeForm(); loadNodes();
  });
});

/* ---------- downloads store ---------- */
let D = null, depotTimer = null;
async function loadDepot() {
  $("#depot-table tbody").replaceChildren(h("tr", {}, h("td", { colspan: 6, class: "muted" }, "Checking the Kiwix catalog…")));
  loadChirp();
  const r = await guarded(() => api("GET", "depot"));
  if (!r) return;
  D = r;
  renderDepot();
  if (D.job.state === "running") pollDepot();
}
async function loadChirp() {
  const c = await guarded(() => api("GET", "chirp"));
  if (!c) return;
  $("#chirp-page").href = c.download_page;
  const size = (n) => (n / 1e6).toFixed(1) + " MB";
  $("#chirp-status").replaceChildren(
    h("li", {}, h("b", {}, "Linux (Pi, laptop): "), c.linux ? `${c.linux.version} · ${size(c.linux.size)}` : "not fetched yet"),
    h("li", {}, h("b", {}, "Windows installer: "), c.windows
      ? [`${c.windows.file} · ${size(c.windows.size)} · `,
         h("span", { class: c.windows.due ? "warn" : "" }, c.windows.due ? `${c.windows.age_days} days old, time for a fresh one` : `${c.windows.age_days} days old`)]
      : h("span", { class: "warn" }, "none yet")));
}
$("#chirp-fetch").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "chirp/fetch"); toast(r.changed ? "Fetched the newest CHIRP code" : "Already up to date"); loadChirp();
}));
$("#chirp-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  guarded(async () => {
    toast("Uploading " + f.name + "…");
    const r = await fetch("/api/chirp/upload?name=" + encodeURIComponent(f.name),
      { method: "POST", body: f, headers: { "X-Bunkr": "1", "Content-Type": "application/octet-stream" } });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    toast("CHIRP installer saved"); e.target.value = ""; loadChirp();
  });
});

function renderReader() {
  const r = D.reader || {}, lib = libraryBase();
  $("#reader-open").href = lib.url || "#";
  $("#reader-open").hidden = !lib.url;
  $("#library-url").value = r.address || "";
  $("#reader-status").textContent = r.running
    ? `Running${r.books ? ` with ${r.books} book${r.books === 1 ? "" : "s"}` : ""} at ${lib.url || "port 8080"}. Everyone can search it from Home.`
      + (r.skipped && r.skipped.length ? ` Skipped (still copying or damaged): ${r.skipped.join(", ")}.` : "")
    : r.error ? `Not running: ${r.error}` : "Not running yet: fetch a library below and it starts by itself within a minute.";
  const extra = D.extra_books || [];
  $("#books-added").replaceChildren(...(extra.length ? [h("h3", {}, "Books you added"), h("ul", { class: "plain" }, ...extra.map((e) =>
    h("li", {}, e.why, " ", h("button", { class: "link", onclick: () => guarded(async () => {
      await api("DELETE", "library/custom/" + e.id); toast("Removed from the list (a downloaded copy stays until you delete it in the Catalog)"); loadDepot();
    }) }, "Remove"))))] : []));
}
$("#library-url-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    const r = await api("POST", "library/address", { url: $("#library-url").value.trim() });
    S.library = { ...(S.library || {}), address: r.url }; toast(r.url ? "Saved" : "Back to the home address"); renderReader();
  });
});
$("#books-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#books-q").value.trim();
  if (q.length < 2) return;
  $("#books-out").replaceChildren(h("li", { class: "muted" }, "Searching the Kiwix catalog…"));
  guarded(async () => {
    const r = await api("GET", `library/catalog?q=${encodeURIComponent(q)}&lang=${$("#books-lang").value}`);
    $("#books-out").replaceChildren(...(r.results.length ? r.results.map((b) => h("li", { class: "book" },
      h("div", {}, h("strong", {}, b.title), b.flavour ? h("span", { class: "tag" }, b.flavour) : null, " ", h("span", { class: "muted" }, `${gb(b.size)} · ${b.date}`),
        h("div", { class: "muted" }, b.summary)),
      b.added ? h("span", { class: "muted" }, "on the list") : h("button", { class: "small", onclick: () => guarded(async () => {
        await api("POST", "library/custom", b); toast(`Added ${b.title}. Tick it in the Catalog and fetch it.`); loadDepot();
        $("#books-q").value = ""; $("#books-out").replaceChildren();
      }) }, "Add"))) : [h("li", { class: "muted" }, "Nothing found. Try one word, like gardening or medicine.")]));
  });
});

function renderDepot() {
  renderReader();
  const u = D.usage;
  $("#depot-usage").textContent = `${gb(u.held_bytes)} stored · ${gb(u.free)} free on the disk`;
  $("#auto-update").checked = D.auto_update;
  const tierName = { 0: "Readers", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3" };
  $("#depot-table tbody").replaceChildren(...D.catalog.map((c) => h("tr", {},
    h("td", {}, h("input", { type: "checkbox", "data-kind": c.kind, "data-id": c.id, "aria-label": "Select " + c.label,
      checked: !!c.update })),
    h("td", {}, c.label, h("span", { class: "tag" }, tierName[c.tier])),
    h("td", {}, c.held ? h("span", { class: c.held.status === "ok" ? "" : "bad" }, `${c.held.version || "yes"}${c.held.status !== "ok" ? " (" + c.held.status + ")" : ""}`) : h("span", { class: "muted" }, "no")),
    h("td", {}, c.online ? (c.latest || "current") : h("span", { class: "muted" }, "offline")),
    h("td", { class: "num" }, gb(c.held ? c.held.size : c.latest_size)),
    h("td", {}, c.update ? h("span", { class: "warn" }, "update") : "",
      c.held ? h("button", { class: "danger small", onclick: () => confirm(`Delete ${c.held.path} from the store?`) &&
        guarded(async () => { await api("DELETE", "depot/files/" + c.held.id); loadDepot(); }) }, "Delete") : null))));
  const ups = D.updates;
  $("#updates-card").hidden = !ups.length;
  $("#updates-table tbody").replaceChildren(...ups.map((u) => h("tr", {},
    h("td", {}, u.label || u.item_id, u.major ? h("span", { class: "tag hz" }, "major") : null),
    h("td", {}, u.current), h("td", {}, u.latest), h("td", {}, u.status + (u.applied_at ? " " + u.applied_at.slice(0, 10) : "")),
    h("td", {}, u.status === "held" ? h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", `depot/updates/${u.id}/approve`); pollDepot(); }) }, "Approve") : ""))));
  const docs = D.files.filter((f) => f.kind === "documents");
  const newest = docs.map((f) => f.version).sort().pop();
  $("#archive-status").textContent = newest
    ? `Current archive: ${newest}.` + (D.documents_changed_at > (docs.find((f) => f.version === newest)?.fetched_at || "") ? " Documents changed since; rebuild it." : "")
    : "No archive in the store yet.";
}
$("#auto-update").addEventListener("change", (e) => guarded(async () => {
  await api("POST", "depot/settings", { auto_update: e.target.checked }); toast("Saved");
}));
$("#depot-fetch").addEventListener("click", () => {
  const items = $$("#depot-table input[type=checkbox]:checked").map((cb) => ({ kind: cb.dataset.kind, id: cb.dataset.id }));
  if (!items.length) return toast("Tick what to fetch first", true);
  const bytes = D.catalog.filter((c) => items.some((i) => i.id === c.id)).reduce((a, c) => a + c.latest_size, 0);
  if (!confirm(`Download ${items.length} item(s), about ${gb(bytes)}?`)) return;
  guarded(async () => { await api("POST", "depot/fetch", { items }); pollDepot(); });
});
$("#depot-verify").addEventListener("click", () => guarded(async () => { await api("POST", "depot/verify"); pollDepot(); }));
$("#depot-cancel").addEventListener("click", () => guarded(() => api("POST", "depot/cancel")));
$("#archive-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.pass1.value !== f.pass2.value) return toast("Passphrases don't match", true);
  guarded(async () => { await api("POST", "depot/archive", { passphrase: f.pass1.value }); f.reset(); pollDepot(); });
});
async function pollDepot() {
  clearTimeout(depotTimer);
  const j = await api("GET", "depot/job").catch(() => null);
  if (!j || j.state === "idle") return;
  $("#depot-job").hidden = false;
  const running = j.state === "running";
  $("#depot-cancel").hidden = !running;
  $("#depot-bar").style.width = (j.bytes_total ? Math.min(100, 100 * j.bytes_done / j.bytes_total) : running ? 3 : 100).toFixed(1) + "%";
  $("#depot-step").textContent = running ? `${j.step}${j.bytes_total ? ` · ${gb(j.bytes_done)} of ${gb(j.bytes_total)}` : ""}`
    : { done: "Finished.", failed: "Failed, see log.", cancelled: "Cancelled." }[j.state];
  $("#depot-log").textContent = j.log.join("\n");
  if (running) depotTimer = setTimeout(pollDepot, 1000);
  else if ($("#tab-downloads").hidden === false) { const r = await api("GET", "depot").catch(() => null); if (r) { D = r; renderDepot(); } }
}

/* ---------- session, logins, field node ---------- */
const isAdmin = () => S.me.role === "admin";
function renderSession() {
  $("#userbox").hidden = !S.require_login;
  $("#whoami").textContent = `${S.me.username} (${S.me.role})`;
  $$("#tabs [data-admin]").forEach((b) => (b.hidden = !isAdmin()));
  if (S.view === "simple") $$("#tabs button").forEach((b) => { if (!SIMPLE_TABS.includes(b.dataset.tab)) b.hidden = true; });
  $("#view-toggle").textContent = S.view === "simple" ? "All screens" : "Simple view";
  $("#view-toggle").hidden = !S.me_person && !isAdmin() && S.view === "full";
  $$("[data-admin-field]").forEach((el) => (el.hidden = !isAdmin()));
  $("#mypw-card").hidden = !S.require_login;
  $("#users-card").hidden = !(S.require_login && isAdmin());
  const field = ["pi", "laptop"].includes(S.node_role);
  if (field) { $("#tabs [data-tab=nodes]").hidden = true; $("#tabs [data-tab=downloads]").hidden = true; }
  $$("#tabs details").forEach((d) => (d.hidden = !d.querySelector("button:not([hidden])")));
  $("#field-card").hidden = !field;
  if (field) {
    $("#field-sync").textContent = `Synced from the hub ${S.last_node_sync ? when(S.last_node_sync) : "not yet"}. Everything here works with no internet.`;
    $("#field-lock").hidden = !S.node_lock;
    if (S.node_lock) $("#field-lock").replaceChildren(S.node_lock === "on"
      ? h("span", {}, "Node lock is on: this kit's data is encrypted when it's off or locked. ",
          isAdmin() ? h("button", { class: "link", onclick: () => confirm("Lock this kit now? It unlocks with the family passphrase (or when BUNKR restarts at home).") &&
            guarded(async () => { await api("POST", "lock"); setTimeout(() => (location.href = "/"), 900); }) }, "Lock now") : null)
      : "Node lock is waiting for the key from the hub; until it arrives the data here isn't encrypted.");
    const host = location.hostname;
    $("#field-links").replaceChildren(
      h("a", { class: "btn", href: libraryBase().url, target: "_blank", rel: "noopener" }, "Offline library"),
      h("a", { class: "btn", href: "/store/web/maps/", target: "_blank", rel: "noopener" }, "Maps"),
      h("a", { class: "btn", href: "/store/plan/family-plan.html", target: "_blank", rel: "noopener" }, "Family plan"),
      h("a", { class: "btn", href: "/api/report.html", target: "_blank", rel: "noopener" }, "Printable inventory"),
      h("a", { class: "btn", href: "/api/shopping.html", target: "_blank", rel: "noopener" }, "Shopping list"));
  }
}
$("#view-toggle").addEventListener("click", () => guarded(async () => {
  await api("POST", "me/view", { view: S.view === "simple" ? "full" : "simple" }); await refresh();
  if ($(`#tabs [data-tab="${localStorage.getItem("bunkr-tab")}"]`)?.hidden) showTab("home");
}));
$("#logout").addEventListener("click", () => guarded(async () => { await api("POST", "logout"); forgetSaved(); location.href = "/login.html?out=1"; }));
$("#mypw-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => { await api("POST", "me/password", { current: f.current.value, new: f.new.value }); location.href = "/login.html"; });
});
const AUDIT_WORDS = { login: "Logged in", logout: "Logged out", login_failed: "Login failed", change: "Changed", view: "Looked at",
  connector: "AI connector", security: "Security", denied: "Refused" };
const shortWhen = (iso) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
async function loadAudit() {
  $("#audit-card").hidden = !(S.require_login && isAdmin());
  if ($("#audit-card").hidden) return;
  const u = $("#audit-user").value, w = $("#audit-what").value;
  const r = await guarded(() => api("GET", `audit?user=${u}&what=${encodeURIComponent(w)}`));
  if (!r) return;
  const keep = (sel, opts, cur) => sel.replaceChildren(sel.options[0], ...opts.map(([v, t]) => h("option", { value: v, selected: String(v) === cur }, t)));
  keep($("#audit-user"), r.users.map((x) => [x.id, x.username]), u);
  keep($("#audit-what"), r.whats.filter(Boolean).map((x) => [x, x]), w);
  $("#audit-check").replaceChildren(r.check.ok
    ? h("span", { class: "muted" }, `${r.check.rows} entries, none changed since they were written.`)
    : h("strong", { class: "bad" }, `Entry ${r.check.broken_at} was changed or an entry before it was removed. Someone with access to the database edited the log.`));
  const risky = (x) => ["login_failed", "security", "denied"].includes(x.action) || x.what === "firearms";
  $("#audit-table tbody").replaceChildren(...(r.rows.length ? r.rows.map((x) => h("tr", { class: risky(x) ? "audit-flag" : "" },
    h("td", {}, shortWhen(x.at)), h("td", {}, x.username || "(not logged in)"),
    h("td", {}, h("strong", {}, AUDIT_WORDS[x.action] || x.action), x.what === "login" ? "" : " " + x.what, x.detail ? h("div", { class: "muted" }, x.detail) : null),
    h("td", { class: "muted" }, x.ip))) : [h("tr", {}, h("td", { colspan: 4, class: "muted" }, "Nothing yet."))]));
}
$("#audit-user").addEventListener("change", loadAudit);
$("#audit-what").addEventListener("change", loadAudit);

// ---------- two-step login (each person, Household tab) ----------
async function loadTwoStep() {
  $("#twostep-card").hidden = !S.require_login;
  if (!S.require_login) return;
  const t = await guarded(() => api("GET", "me/two-step"));
  if (!t) return;
  $("#ts-policy").value = t.policy;
  const here = t.passkeys.filter((k) => k.rp_id === t.rp_id);
  $("#ts-status").textContent = t.on
    ? "On: logging in takes your password and a second step" + (t.required ? " (required here)." : ".")
    : t.required ? "Required here: set it up now." : "Off. Turn it on so a stolen password isn't enough to log in as you.";
  $("#ts-list").replaceChildren(
    ...t.passkeys.map((k) => h("li", {}, h("strong", {}, k.name), " ", h("span", { class: "muted" },
      `passkey for ${k.rp_id}, added ${when(k.created_at)}${k.last_used ? ", last used " + when(k.last_used) : ""}`), " ",
      h("button", { class: "link", onclick: () => confirm(`Remove the passkey "${k.name}"?`) &&
        guarded(async () => { await api("DELETE", "me/passkeys/" + k.id); loadTwoStep(); }) }, "Remove"))),
    t.totp ? h("li", {}, h("strong", {}, "Authenticator app"), " ", h("span", { class: "muted" }, `${t.recovery_left} recovery codes left`), " ",
      h("button", { class: "link", onclick: () => { const pw = prompt("Your password, to turn off authenticator codes:");
        if (pw) guarded(async () => { await api("DELETE", "me/two-step/totp", { password: pw }); loadTwoStep(); }); } }, "Turn off")) : null);
  $("#ts-add-passkey").hidden = !Passkey.usable();
  $("#ts-passkey-note").hidden = Passkey.usable() || here.length > 0;
  $("#ts-add-totp").hidden = t.totp;
  $("#ts-new-codes").hidden = !t.totp;
}
function showCodes(codes) {
  $("#ts-code-list").replaceChildren(...codes.map((c) => h("li", {}, c)));
  $("#ts-codes").hidden = false; $("#ts-totp-box").hidden = true;
}
$("#ts-add-passkey").addEventListener("click", () => guarded(async () => {
  const name = prompt("Name this passkey (which phone or computer?)", /iPhone/.test(navigator.userAgent) ? "iPhone" : /Android/.test(navigator.userAgent) ? "Android phone" : "This computer");
  if (name === null) return;
  try {
    const cred = await Passkey.create(await api("POST", "me/passkeys/options"));
    await api("POST", "me/passkeys", { ...cred, name });
    Passkey.remember(true);  // the login page asks for it first from now on
    toast("Passkey added"); loadTwoStep();
  } catch (e) { if (!Passkey.cancelled(e)) throw e; }
}));
$("#ts-add-totp").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "me/two-step/totp");
  $("#ts-key").textContent = r.key; $("#ts-link").href = r.uri; $("#ts-totp-box").hidden = false;
}));
$("#ts-confirm").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { const r = await api("POST", "me/two-step/totp/confirm", { code: e.target.code.value }); e.target.reset(); showCodes(r.recovery); loadTwoStep(); });
});
$("#ts-new-codes").addEventListener("click", () => {
  const pw = prompt("Your password, to make new recovery codes (the old ones stop working):");
  if (pw) guarded(async () => showCodes((await api("POST", "me/two-step/recovery", { password: pw })).recovery));
});
$("#ts-codes-done").addEventListener("click", () => { $("#ts-codes").hidden = true; });
$("#ts-policy").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v !== "none" && !confirm("Anyone covered who hasn't set up two-step login will be asked to at their next login. Continue?")) return loadTwoStep();
  guarded(async () => { await api("POST", "users/policy", { policy: v }); toast("Saved"); loadTwoStep(); });
});

async function loadUsers() {
  if (!S.require_login || !isAdmin()) return;
  const list = await guarded(() => api("GET", "users"));
  if (!list) return;
  $("#users-table tbody").replaceChildren(...list.map((u) => h("tr", {},
    h("td", {}, u.username), h("td", {}, u.role),
    h("td", {}, h("select", { onchange: (e) => guarded(async () => { await api("POST", "users/" + u.id, { view: e.target.value }); toast("Saved"); }) },
      ...[["", u.role === "admin" ? "All (admin default)" : "Simple (family default)"], ["simple", "Simple"], ["full", "All screens"]].map(([v, t]) =>
        h("option", { value: v, selected: (u.view || "") === v }, t)))),
    h("td", {}, u.two_step ? "On" : h("span", { class: "muted" }, "Off"),
      u.two_step && u.username !== S.me.username ? h("button", { class: "link", onclick: () =>
        confirm(`Turn off two-step login for ${u.username}? Do this when they've lost their phone. They'll be logged out everywhere.`) &&
        guarded(async () => { await api("POST", `users/${u.id}/two-step-off`); toast("Turned off"); loadUsers(); }) }, "Turn off") : null),
    h("td", {}, u.last_login ? when(u.last_login) : "never"),
    h("td", {},
      h("button", { class: "link", onclick: () => { const pw = prompt(`New password for ${u.username} (10+ characters):`);
        if (pw) guarded(async () => { await api("POST", "users/" + u.id, { password: pw }); toast("Password set"); }); } }, "Reset password"),
      h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", "users/" + u.id, { role: u.role === "admin" ? "family" : "admin" }); loadUsers(); }) },
        u.role === "admin" ? "Make family" : "Make admin"),
      u.username === S.me.username ? null : h("button", { class: "danger", onclick: () => confirm(`Delete login ${u.username}?`) &&
        guarded(async () => { await api("DELETE", "users/" + u.id); loadUsers(); }) }, "Delete")))));
}
$("#user-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => { await api("POST", "users", { username: f.username.value, password: f.password.value, role: f.role.value });
    f.reset(); toast("Login added"); loadUsers(); });
});
async function loadConflicts() {
  const list = await api("GET", "conflicts").catch(() => []);
  $("#conflicts-card").hidden = !list.length;
  const fmt = (o) => Object.entries(o).filter(([k]) => !["uid", "updated_at"].includes(k)).map(([k, v]) => `${k} ${v}`).join(", ");
  $("#conflicts-list").replaceChildren(...list.map((c) => {
    const kept = JSON.parse(c.kept), lost = JSON.parse(c.lost);
    return h("div", { class: "alert" },
      h("div", {}, h("strong", {}, kept.name || lost.name || c.uid), h("span", { class: "tag" }, c.kind), h("span", { class: "muted" }, ` from ${c.node}, ${when(c.at)}`)),
      h("div", { class: "muted" }, "Kept: " + fmt(kept)),
      h("div", { class: "muted" }, "Replaced: " + fmt(lost)),
      h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", "conflicts/" + c.id); loadConflicts(); }) }, "Dismiss"));
  }));
}

/* ---------- maps pack (Sites tab, admins) ---------- */
let mapsTimer = null;
async function loadMaps() {
  const r = await api("GET", "maps").catch(() => null);
  if (!r) return;
  $("#maps-card").hidden = false;
  const f = $("#maps-form");
  for (const k of ["detail_km", "detail_zoom", "region_km", "region_zoom"]) f[k].value = r.settings[k];
  const map = r.files[0];
  $("#maps-status").textContent = map
    ? `Current pack: ${map.path.split("/").pop()} (${gb(map.size)}), built ${when(r.built?.at)}${r.stale ? ". Sites changed or it's a month old; it will be rebuilt tonight." : "."}`
    : "No maps pack yet. Estimate the size, then build it.";
  $("#maps-open").hidden = !(map && r.viewer);
}
$("#maps-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { await api("POST", "maps/settings", Object.fromEntries(new FormData(e.target))); toast("Map settings saved"); loadMaps(); });
});
$("#maps-estimate").addEventListener("click", () => {
  $("#maps-status").textContent = "Reading the world map index…";
  guarded(async () => {
    const r = await api("GET", "maps/estimate");
    $("#maps-status").textContent = `About ${gb(r.bytes)} (${r.tiles.toLocaleString()} tiles) from the ${r.build} world build.`;
  });
});
$("#maps-build").addEventListener("click", () => guarded(async () => { await api("POST", "maps/build"); pollMaps(); }));
async function pollMaps() {
  clearTimeout(mapsTimer);
  const j = await api("GET", "depot/job").catch(() => null);
  if (!j) return;
  $("#maps-job").hidden = false;
  $("#maps-bar").style.width = (j.bytes_total ? Math.min(100, 100 * j.bytes_done / j.bytes_total) : 3).toFixed(1) + "%";
  $("#maps-step").textContent = j.state === "running" ? `${j.step}${j.bytes_total ? ` · ${gb(j.bytes_done)} of ${gb(j.bytes_total)}` : ""}`
    : (j.log.slice(-2).join(" · ") || j.state);
  if (j.state === "running") mapsTimer = setTimeout(pollMaps, 1000); else loadMaps();
}

/* ---------- comms: radios, channels, firmware ---------- */
let R = null, editingRadio = null, editingCh = null;
async function loadRadio() {
  const r = await guarded(() => api("GET", "radio"));
  if (!r) return;
  R = r;
  if (!$("#radio-kind").options.length) {
    $("#radio-kind").replaceChildren(...Object.entries(R.kinds).map(([k, v]) => h("option", { value: k }, v)));
    $("#ch-service").replaceChildren(...Object.entries(R.services).map(([k, v]) => h("option", { value: k }, v)));
    $("#radio-profile").append(...Object.entries(R.profiles).map(([k, v]) => h("option", { value: k }, v.label)));
  }
  renderRadios(); renderChannels(); renderFirmware();
}
const txText = (c) => c.duplex === "off" ? "RX only" : c.duplex === "+" ? (c.rx_mhz + c.offset_mhz).toFixed(4)
  : c.duplex === "-" ? (c.rx_mhz - c.offset_mhz).toFixed(4) : c.duplex === "split" ? c.offset_mhz.toFixed(4) : "";
const toneText = (c) => c.tone_mode === "DTCS" ? `DCS ${String(c.dtcs).padStart(3, "0")}` : c.tone_mode ? `${c.tone_hz} Hz${c.tone_mode === "TSQL" ? " both" : ""}` : "";
const toneOne = (t) => !t ? "" : t.startsWith("D") ? `DCS ${t.slice(1)}` : `${t} Hz`;
const tonePair = (c) => c.tx_tone || c.rx_tone ? (c.tx_tone === c.rx_tone ? `${toneOne(c.tx_tone)} both` :
  [c.tx_tone && "TX " + toneOne(c.tx_tone), c.rx_tone && "RX " + toneOne(c.rx_tone)].filter(Boolean).join(" · ")) : toneText(c);
const STATUS = { verified: ["Verified", "good"], confirm: ["To confirm", "warn"], hold: ["On hold", "warn"], down: ["Down", "bad"] };
function renderRadios() {
  $("#radio-cards").replaceChildren(...(R.radios.length ? R.radios.map((r) => {
    const assigned = new Set(r.channel_ids);
    const editor = h("details", {}, h("summary", {}, `Channels (${r.channel_ids.length})`),
      h("div", { class: "na-list" }, ...R.channels.map((c) => h("label", {},
        h("input", { type: "checkbox", value: c.id, checked: assigned.has(c.id) }), `${c.name} ${c.rx_mhz.toFixed(4)}`))),
      h("button", { class: "small", onclick: (e) => guarded(async () => {
        const ids = [...e.target.closest("details").querySelectorAll("input:checked")].map((i) => Number(i.value));
        await api("POST", `radios/${r.id}/channels`, { channel_ids: ids }); toast("Channels saved"); loadRadio();
      }) }, "Save channel list"));
    const upload = h("input", { type: "file", onchange: (e) => {
      const file = e.target.files[0]; if (!file) return;
      guarded(async () => {
        const res = await fetch(`/api/radios/${r.id}/image`, { method: "POST", body: file,
          headers: { "X-Bunkr": "1", "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" } });
        const d = await res.json().catch(() => ({})); if (!res.ok) throw new Error(d.error || res.statusText);
        toast("Programming image saved"); loadRadio();
      });
    } });
    const prof = r.profile ? R.profiles[r.profile] : null;
    const planBox = prof ? h("div", {},
      h("p", {}, `${r.plan.count} memories from the master plan · `, prof.format === "adms"
        ? "load with ADMS-12: import this CSV, write to the SD card, then restore on the radio"
        : `in CHIRP pick ${prof.chirp}, download from the radio, File > Import this file, then upload`),
      r.plan.issues.filter((i) => i.level !== "info").length ? h("details", {},
        h("summary", {}, `${r.plan.issues.filter((i) => i.level === "error").length} problem(s), ${r.plan.issues.filter((i) => i.level === "warn").length} note(s)`),
        h("ul", {}, ...r.plan.issues.filter((i) => i.level !== "info").map((i) => h("li", { class: i.level === "error" ? "bad" : "" }, i.text)))) : null) : null;
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("h2", {}, r.name, h("span", { class: "tag" }, prof ? prof.label : R.services[r.service]), h("span", { class: "tag" }, R.kinds[r.kind])),
        h("div", { class: "row-right" },
          prof ? h("a", { class: "btn small", href: `/api/radios/${r.id}/file` }, prof.format === "adms" ? "ADMS-12 file" : "CHIRP file")
            : h("a", { class: "btn small", href: `/api/radios/${r.id}/chirp.csv`, download: `${r.name}-chirp.csv` }, "CHIRP file"),
          h("a", { class: "btn small", href: `/api/radios/${r.id}/card.html`, target: "_blank", rel: "noopener" }, "Channel card"),
          h("button", { class: "link", onclick: () => editRadio(r) }, "Edit"),
          h("button", { class: "danger", onclick: () => confirm(`Remove radio ${r.name}? Saved images stay in the store.`) &&
            guarded(async () => { await api("DELETE", "radios/" + r.id); loadRadio(); }) }, "Remove"))),
      h("p", { class: "muted" }, [r.owner && "Carried by " + r.owner, r.model, r.chirp_driver && "CHIRP: " + r.chirp_driver, r.notes].filter(Boolean).join(" · ")),
      r.kind === "hf" ? h("p", { class: "muted" }, "HF radio: no memory channels here. Digital-mode settings live with JS8Call, fldigi and Pat on the Pi and laptop.") : (planBox || editor),
      isAdmin() ? h("details", {}, h("summary", {}, `Programming images (${r.images.length})`),
        h("p", { class: "muted" }, "In CHIRP, download from the radio and save the .img file, then add it here. Every version is kept."),
        upload,
        h("ul", { class: "plan-list" }, ...r.images.map((im) => h("li", {},
          h("a", { href: "/store/" + im.path }, im.path.split("/").pop()), h("span", { class: "num" }, gb(im.size)))))) : null);
  }) : [h("p", { class: "muted" }, "No radios yet. Add one above; it starts with the standard channels for its service.")]));
}
function editRadio(r) {
  editingRadio = r.id;
  const f = $("#radio-form");
  for (const k of ["name", "model", "kind", "service", "chirp_driver", "notes", "profile", "owner"]) f[k].value = r[k] || "";
  $("#radio-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
$("#radio-cancel").addEventListener("click", () => { editingRadio = null; $("#radio-form").reset(); $("#radio-cancel").hidden = true; });
$("#radio-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "radios" + (editingRadio ? "/" + editingRadio : ""), Object.fromEntries(new FormData(e.target)));
    toast("Radio saved"); editingRadio = null; e.target.reset(); $("#radio-cancel").hidden = true; loadRadio();
  });
});
function renderChannels() {
  const statewide = $("#ch-statewide").checked;
  const list = R.channels.filter((c) => statewide || c.slot == null || c.slot < 200)
    .sort((a, b) => (a.slot ?? 1e6) - (b.slot ?? 1e6) || a.sort - b.sort);
  $("#ch-table tbody").replaceChildren(...(list.length ? list.map((c) => h("tr", {},
    h("td", {}, c.slot ?? ""),
    h("td", {}, c.name, c.status && STATUS[c.status] ? h("span", { class: "tag " + STATUS[c.status][1] }, STATUS[c.status][0]) : null,
      c.watch ? h("span", { class: "tag" }, "watch") : null, c.notes ? h("div", { class: "muted" }, c.notes) : null),
    h("td", {}, c.system || c.grp),
    h("td", {}, c.rx_mhz.toFixed(4)), h("td", {}, c.rx_only ? "RX only" : txText(c)), h("td", {}, tonePair(c)), h("td", {}, c.in_plan ? "Yes" : ""),
    h("td", {}, h("button", { class: "link", onclick: () => editChannel(c) }, "Edit"),
      h("button", { class: "danger", onclick: () => confirm(`Delete channel ${c.name}?`) &&
        guarded(async () => { await api("DELETE", "channels/" + c.id); loadRadio(); }) }, "Delete"))))
    : [h("tr", {}, h("td", { colspan: 8, class: "muted" }, "No channels yet. Add the standard set to start."))]));
}
function editChannel(c) {
  editingCh = c.id;
  const f = $("#ch-form");
  for (const k of ["name", "grp", "service", "rx_mhz", "duplex", "offset_mhz", "mode", "notes", "tx_tone", "rx_tone", "short", "system", "status"]) f[k].value = c[k] ?? "";
  f.slot.value = c.slot ?? "";
  for (const k of ["in_plan", "scan", "watch", "rx_only", "wiresx"]) f[k].checked = !!c[k];
  $("#ch-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
$("#ch-cancel").addEventListener("click", () => { editingCh = null; $("#ch-form").reset(); $("#ch-cancel").hidden = true; });
$("#ch-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  for (const k of ["in_plan", "scan", "watch", "rx_only", "wiresx"]) data[k] = e.target[k].checked;
  guarded(async () => {
    await api("POST", "channels" + (editingCh ? "/" + editingCh : ""), data);
    toast("Channel saved"); editingCh = null; e.target.reset(); $("#ch-cancel").hidden = true; loadRadio();
  });
});
$("#ch-statewide").addEventListener("change", () => renderChannels());
$("#ch-standard").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "channels/standard"); toast(`Added ${r.added} channel(s)`); loadRadio();
}));
$("#ch-import").addEventListener("click", () => {
  const file = $("#ch-import-file").files[0];
  if (!file) return toast("Choose a CSV file first", true);
  guarded(async () => {
    const r = await api("POST", "channels/import", { csv: await file.text(), group: $("#ch-import-group").value });
    toast(`Imported ${r.added} channel(s)`); loadRadio();
  });
});
function renderFirmware() {
  $("#fw-boards").replaceChildren(...Object.entries(R.boards).map(([id, b]) => {
    const files = R.firmware.filter((f) => f.item_id.endsWith("-" + id));
    const groups = {};
    for (const f of files) (groups[f.item_id] ||= []).push(f);
    return h("div", { class: "card" },
      h("h2", {}, b.label, h("span", { class: "tag" }, b.flash === "uf2" ? "drag-and-drop UF2" : "esptool")),
      Object.keys(groups).length ? h("ul", { class: "plan-list" }, ...Object.entries(groups).map(([item, fs]) => h("li", {},
        h("span", {}, item.startsWith("meshtastic") ? `Meshtastic ${fs[0].version}` :
          `MeshCore ${fs[0].version}: ${R.meshcore_types[item.replace(/^meshcore-/, "").replace("-" + id, "")] || item}`),
        h("span", {}, ...fs.map((f) => h("a", { class: "btn small", href: "/store/" + f.path }, f.path.split("/").pop().replace(/-v?[\d.]+-\w+/, "")))))))
        : h("p", { class: "muted" }, "Nothing stored for this board yet."),
      h("details", {}, h("summary", {}, "How to flash with no internet"),
        h("ol", {}, ...R.flashing[b.flash].map((step) => h("li", {}, step)))));
  }));
}

/* ---------- comms: nets and their log, ICS-213 messages, neighbors, licenses, partner hubs ---------- */
let CM = null, PT = null, editingNet = null, editingIcs = null, editingNb = null, editingLic = null, editingPartner = null;
const commsWhen = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
async function loadComms() {
  const [c, p] = await Promise.all([guarded(() => api("GET", "comms")), guarded(() => api("GET", "partners"))]);
  if (c) { CM = c; renderComms(); }
  if (p) { PT = p; renderPartners(); }
  $("#partner-setup").hidden = !isAdmin();
  if (isAdmin()) loadShares();
}
function fillOnce(sel, pairs) {
  const el = $(sel);
  if (!el.options.length) el.replaceChildren(...pairs.map(([v, t]) => h("option", { value: v }, t)));
}
function checkList(box, pairs, name) {
  if (!box.children.length) box.replaceChildren(...pairs.map(([v, t]) => h("label", {}, h("input", { type: "checkbox", name, value: v }), t)));
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); return; } catch {}
  const t = h("textarea", { style: "position:fixed;top:0;left:0;opacity:0" }); t.value = text; document.body.append(t); t.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch {}
  t.remove();
  ok ? toast("Copied") : prompt("Copy this:", text);
}
function renderComms() {
  fillOnce("#net-mode", CM.modes.map((m) => [m, m]));
  fillOnce("#net-day", [["-1", "Every day"], ...CM.weekdays.map((d, i) => [String(i), d])]);
  fillOnce("#lic-kind", Object.entries(CM.kinds));
  if ($("#lic-class").options.length === 1) $("#lic-class").append(...CM.classes.map((c) => h("option", { value: c }, c)));
  checkList($("#nb-skills"), Object.entries(CM.skills), "skills");
  if ($("#nb-filter").options.length === 1) $("#nb-filter").append(...Object.entries(CM.skills).map(([k, v]) => h("option", { value: k }, v)));
  const edit = CM.can_edit && CM.hub;
  $("#net-edit").hidden = !edit; $("#nb-edit").hidden = !edit; $("#ics-edit").hidden = !CM.can_edit;
  $("#lic-edit").hidden = !CM.hub || !(CM.can_edit || CM.me);
  $("#lic-person").replaceChildren(...CM.people.filter((p) => CM.can_edit || p.id === CM.me).map((p) => h("option", { value: p.id }, p.name)));
  renderNets(edit); renderIcs(); renderNeighbours(edit); renderLicences();
}
function renderNets(edit) {
  $("#net-list").replaceChildren(...(CM.nets.length ? CM.nets.map((n) => h("li", {},
    h("span", { class: n.active ? "" : "muted" }, h("strong", {}, n.name), h("span", { class: "tag" }, n.mode),
      h("div", { class: "muted" }, [n.when, n.freq, n.net_control && "Net control " + n.net_control, n.notes].filter(Boolean).join(" · "))),
    edit ? h("span", {}, h("button", { class: "link", onclick: () => editNet(n) }, "Edit"),
      h("button", { class: "danger", onclick: () => confirm(`Remove the ${n.name} net? Its log entries stay.`) &&
        guarded(async () => { await api("DELETE", "nets/" + n.id); loadComms(); }) }, "Remove")) : null))
    : [h("li", { class: "muted" }, "No nets yet.")]));
  $("#netlog-form").hidden = !CM.nets.length;
  $("#netlog-net").replaceChildren(...CM.nets.filter((n) => n.active).map((n) => h("option", { value: n.id }, n.name)));
  $("#netlog-table tbody").replaceChildren(...(CM.net_log.length ? CM.net_log.map((l) => h("tr", {},
    h("td", {}, commsWhen(l.at)), h("td", {}, l.net_name), h("td", {}, l.who), h("td", {}, l.signal), h("td", {}, l.notes),
    h("td", {}, CM.can_edit ? h("button", { class: "danger", onclick: () => confirm("Remove this entry?") &&
      guarded(async () => { await api("DELETE", "net-log/" + l.id); loadComms(); }) }, "Remove") : null)))
    : [h("tr", {}, h("td", { colspan: 6, class: "muted" }, CM.nets.length ? "No check-ins logged yet." : "Add a net first."))]));
}
function editNet(n) {
  editingNet = n.id;
  const f = $("#net-form");
  for (const k of ["name", "freq", "mode", "time", "net_control", "notes"]) f[k].value = n[k] ?? "";
  f.weekday.value = String(n.weekday);
  $("#net-edit").open = true; $("#net-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
$("#net-cancel").addEventListener("click", () => { editingNet = null; $("#net-form").reset(); $("#net-cancel").hidden = true; });
$("#net-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "nets" + (editingNet ? "/" + editingNet : ""), Object.fromEntries(new FormData(e.target)));
    toast("Net saved"); editingNet = null; e.target.reset(); $("#net-cancel").hidden = true; loadComms();
  });
});
$("#netlog-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "net-log", Object.fromEntries(new FormData(e.target)));
    toast("Logged"); e.target.who.value = ""; e.target.signal.value = ""; e.target.notes.value = ""; loadComms();
  });
});
function renderIcs() {
  $("#ics-list").replaceChildren(...(CM.messages.length ? CM.messages.map((m) => h("li", {},
    h("span", {}, h("strong", {}, m.subject || "(no subject)"), h("span", { class: "tag" }, m.direction === "received" ? "received" : "sent"),
      h("div", { class: "muted" }, `To ${m.to_name} · from ${m.from_name} · ${m.msg_date} ${m.msg_time}`)),
    h("span", {}, h("button", { class: "small", onclick: () => copyText(m.text) }, "Copy as plain text"), " ",
      h("a", { class: "btn small", href: `/api/ics213/${m.id}/print.html`, target: "_blank", rel: "noopener" }, "Print"),
      CM.can_edit ? h("button", { class: "link", onclick: () => editIcs(m) }, "Edit") : null,
      CM.can_edit ? h("button", { class: "danger", onclick: () => confirm("Delete this message?") &&
        guarded(async () => { await api("DELETE", "ics213/" + m.id); loadComms(); }) }, "Delete") : null)))
    : [h("li", { class: "muted" }, "No messages yet.")]));
}
function editIcs(m) {
  editingIcs = m.id;
  const f = $("#ics-form");
  for (const k of ["direction", "incident", "to_name", "to_position", "from_name", "from_position", "subject", "msg_date", "msg_time",
    "message", "approved_name", "approved_position", "reply", "reply_name", "reply_position", "reply_at"]) f[k].value = m[k] ?? "";
  $("#ics-edit").open = true; $("#ics-summary").textContent = "Edit message"; $("#ics-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
function resetIcs() { editingIcs = null; $("#ics-form").reset(); $("#ics-cancel").hidden = true; $("#ics-summary").textContent = "Write a message"; }
$("#ics-cancel").addEventListener("click", resetIcs);
$("#ics-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "ics213" + (editingIcs ? "/" + editingIcs : ""), Object.fromEntries(new FormData(e.target)));
    toast("Message saved"); resetIcs(); loadComms();
  });
});
function renderNeighbours(edit) {
  const want = $("#nb-filter").value;
  const list = CM.neighbours.filter((n) => !want || n.skills.includes(want));
  $("#nb-list").replaceChildren(...(list.length ? list.map((n) => h("div", { class: "med" },
    h("div", { class: "t" }, n.name, ...n.skill_labels.map((s) => h("span", { class: "chip" }, s))),
    h("div", { class: "m" }, [n.place, n.callsign].filter(Boolean).join(" · "),
      n.phone ? [n.place || n.callsign ? " · " : "", h("a", { href: "tel:" + n.phone.replace(/[^\d+]/g, "") }, n.phone)] : null,
      n.contact ? ` · ${n.contact}` : ""),
    n.notes ? h("div", { class: "m muted" }, n.notes) : null,
    edit ? h("div", { class: "m" }, h("button", { class: "link", onclick: () => editNeighbour(n) }, "Edit"), " · ",
      h("button", { class: "link", onclick: () => confirm(`Remove ${n.name}?`) && guarded(async () => { await api("DELETE", "neighbours/" + n.id); loadComms(); }) }, "Remove")) : null))
    : [h("p", { class: "muted" }, CM.neighbours.length ? "Nobody with that skill yet." : "No neighbors yet.")]));
}
$("#nb-filter").addEventListener("change", () => CM && renderNeighbours(CM.can_edit && CM.hub));
function editNeighbour(n) {
  editingNb = n.id;
  const f = $("#nb-form");
  for (const k of ["name", "place", "phone", "callsign", "contact", "lat", "lon", "skills_other", "notes"]) f[k].value = n[k] ?? "";
  $$("#nb-skills input").forEach((i) => (i.checked = n.skills.includes(i.value)));
  $("#nb-edit").open = true; $("#nb-summary").textContent = `Edit ${n.name}`; $("#nb-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
function resetNb() { editingNb = null; $("#nb-form").reset(); $("#nb-cancel").hidden = true; $("#nb-summary").textContent = "Add a neighbor"; }
$("#nb-cancel").addEventListener("click", resetNb);
$("#nb-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.skills = $$("#nb-skills input:checked").map((i) => i.value);
  guarded(async () => { await api("POST", "neighbours" + (editingNb ? "/" + editingNb : ""), data); toast("Saved"); resetNb(); loadComms(); });
});
const LIC_STATE = { soon: ["warn", (s) => `in ${s.days} d`], expired: ["bad", () => "expired"], done: ["", () => "done"], later: ["", () => ""] };
function renderLicences() {
  $("#lic-due").replaceChildren(CM.due.length ? h("ul", { class: "plain" }, ...CM.due.map((d) => h("li", {},
    h("strong", { class: d.status.state === "expired" ? "bad" : "warn" }, d.message)))) : null);
  const mine = (l) => CM.hub && (CM.can_edit || l.person_id === CM.me);
  $("#lic-table tbody").replaceChildren(...(CM.licences.length ? CM.licences.map((l) => {
    const st = l.status && LIC_STATE[l.status.state];
    return h("tr", {}, h("td", {}, l.person), h("td", {}, l.title, l.notes ? h("div", { class: "muted" }, l.notes) : null),
      h("td", {}, [l.callsign, l.number].filter(Boolean).join(" · ")),
      h("td", {}, l.expires ? h("span", { class: st ? st[0] : "" }, l.expires + (st && st[1](l.status) ? ` · ${st[1](l.status)}` : "")) : h("span", { class: "muted" }, "—")),
      h("td", {}, mine(l) ? [h("button", { class: "link", onclick: () => editLicence(l) }, "Edit"),
        h("button", { class: "danger", onclick: () => confirm(`Remove ${l.person}'s ${l.title}?`) &&
          guarded(async () => { await api("DELETE", "licences/" + l.id); loadComms(); }) }, "Remove")] : null));
  }) : [h("tr", {}, h("td", { colspan: 5, class: "muted" }, "Nothing yet. Add ham and GMRS licenses, CPR and first aid cards."))]));
}
function editLicence(l) {
  editingLic = l.id;
  const f = $("#lic-form");
  for (const k of ["person_id", "kind", "label", "class", "callsign", "number", "expires", "notes"]) f[k].value = l[k] ?? "";
  $("#lic-edit").open = true; $("#lic-summary").textContent = "Edit"; $("#lic-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
function resetLic() { editingLic = null; $("#lic-form").reset(); $("#lic-cancel").hidden = true; $("#lic-summary").textContent = "Add a license or certificate"; }
$("#lic-cancel").addEventListener("click", resetLic);
$("#lic-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { await api("POST", "licences" + (editingLic ? "/" + editingLic : ""), Object.fromEntries(new FormData(e.target)));
    toast("Saved"); resetLic(); loadComms(); });
});
function partnerChannel(c) {
  const tx = c.duplex === "+" ? c.rx_mhz + c.offset_mhz : c.duplex === "-" ? c.rx_mhz - c.offset_mhz : c.duplex === "split" ? c.offset_mhz : null;
  return [c.rx_mhz != null ? c.rx_mhz.toFixed(4) : "", tx != null ? `TX ${tx.toFixed(4)}` : c.duplex === "off" ? "listen only" : "",
    c.tx_tone || c.rx_tone ? tonePair(c) : "", c.mode].filter(Boolean).join(" · ");
}
function renderPartners() {
  $("#partner-cards").replaceChildren(...PT.partners.map((p) => {
    const d = p.data || {};
    const has = (k) => Array.isArray(d[k]) && d[k].length;
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("h2", {}, p.name, h("span", { class: "tag" }, "partner")),
        h("span", { class: "muted" }, d.as_of ? `as of ${commsWhen(d.as_of)}` : "not read yet")),
      p.last_error ? h("p", { class: "warn" }, `Couldn't update: ${p.last_error}.` + (d.as_of ? " Showing the last copy." : "")) : null,
      d.call ? h("p", {}, h("strong", { class: "bad" }, `${d.call.drill ? "DRILL: " : ""}${d.call.label}${d.call.place ? " " + d.call.place : ""}`),
        d.call.message ? ` · ${d.call.message}` : "", h("span", { class: "muted" }, ` · since ${commsWhen(d.call.created_at)}`)) : null,
      has("board") ? h("ul", { class: "plan-list" }, ...d.board.map((b) => h("li", {}, h("span", {}, b.name),
        h("span", { class: b.status === "help" ? "bad" : b.status === "safe" ? "good" : "" }, b.status_label || "no check-in",
          b.at ? h("span", { class: "muted" }, ` · ${commsWhen(b.at)}`) : null)))) : null,
      has("nets") ? h("div", {}, h("h3", {}, "Their nets"), h("ul", { class: "plan-list" }, ...d.nets.map((n) => h("li", {},
        h("span", {}, h("strong", {}, n.name), h("div", { class: "muted" }, [n.when, n.freq, n.mode, n.net_control && "NC " + n.net_control].filter(Boolean).join(" · "))))))) : null,
      has("channels") ? h("div", {}, h("h3", {}, "Their channels"), h("ul", { class: "plan-list" }, ...d.channels.map((c) => h("li", {},
        h("span", {}, c.name), h("span", { class: "muted" }, partnerChannel(c)))))) : null,
      has("radios") ? h("p", { class: "muted" }, "Radios: " + d.radios.map((r) => [r.name, r.model].filter(Boolean).join(" ")).join(", ")) : null,
      has("callsigns") ? h("p", { class: "muted" }, "Callsigns: " + d.callsigns.map((c) => `${c.name} ${c.callsign}`).join(", ")) : null);
  }));
  if (!isAdmin()) return;
  $("#partner-list").replaceChildren(...PT.partners.map((p) => h("li", {},
    h("span", {}, h("strong", {}, p.name), h("div", { class: "muted" }, [p.url, p.has_token ? "token saved" : "no token",
      p.fetched_at ? "read " + commsWhen(p.fetched_at) : "", p.last_error].filter(Boolean).join(" · "))),
    h("span", {}, h("button", { class: "small", onclick: () => guarded(async () => { PT = await api("POST", `partners/${p.id}/pull`); renderPartners(); toast(PT.partners.find((x) => x.id === p.id)?.last_error || "Updated"); }) }, "Read now"),
      h("button", { class: "link", onclick: () => editPartner(p) }, "Edit"),
      h("button", { class: "danger", onclick: () => confirm(`Stop reading ${p.name}? Their saved copy and token are deleted.`) &&
        guarded(async () => { await api("DELETE", "partners/" + p.id); loadComms(); }) }, "Remove")))));
}
function editPartner(p) {
  editingPartner = p.id;
  const f = $("#partner-form");
  f.name.value = p.name; f.url.value = p.url; f.token.value = "";
  $("#partner-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
$("#partner-cancel").addEventListener("click", () => { editingPartner = null; $("#partner-form").reset(); $("#partner-cancel").hidden = true; });
$("#partner-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  guarded(async () => {
    await api("POST", "partners" + (editingPartner ? "/" + editingPartner : ""), data);
    toast("Saved. Reading it now…"); editingPartner = null; e.target.reset(); $("#partner-cancel").hidden = true;
    setTimeout(loadComms, 3000);
  });
});
let SHARES = null;
async function loadShares() {
  const r = await guarded(() => api("GET", "partner-shares"));
  if (!r) return;
  SHARES = r;
  checkList($("#share-parts"), Object.entries(r.parts), "parts");
  $("#share-list").replaceChildren(...r.shares.map((s) => h("li", {},
    h("span", { class: s.revoked_at ? "muted" : "" }, h("strong", {}, s.name), s.revoked_at ? h("span", { class: "tag" }, "turned off") : null,
      h("div", { class: "muted" }, [s.parts.map((p) => r.parts[p]).join(", "), `made ${commsWhen(s.created_at)}`,
        s.last_used_at ? `last read ${commsWhen(s.last_used_at)} (${s.uses}×)` : "never read"].join(" · "))),
    s.revoked_at ? null : h("button", { class: "danger", onclick: () => confirm(`Turn off the share for ${s.name}? Their hub stops getting updates.`) &&
      guarded(async () => { await api("DELETE", "partner-shares/" + s.id); toast("Turned off"); loadShares(); }) }, "Turn off"))));
}
$("#share-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const parts = $$("#share-parts input:checked").map((i) => i.value);
  guarded(async () => {
    const r = await api("POST", "partner-shares", { name: e.target.name.value, parts });
    e.target.reset();
    $("#share-new").replaceChildren(h("div", { class: "card" },
      h("p", {}, h("strong", {}, "Give these two to your partner now. The token is shown only this once.")),
      h("p", {}, "Address: ", h("code", {}, r.url), " ", h("button", { class: "small", onclick: () => copyText(r.url) }, "Copy")),
      h("p", {}, "Token: ", h("code", { style: "word-break:break-all" }, r.token), " ", h("button", { class: "small", onclick: () => copyText(r.token) }, "Copy")),
      h("button", { class: "link", onclick: () => $("#share-new").replaceChildren() }, "Done, hide it")));
    loadShares();
  });
});

/* ---------- medical ---------- */
let MED = null, editingMed = null;
async function loadMedical() {
  const r = await guarded(() => api("GET", "medical"));
  if (!r) return;
  MED = r;
  $("#med-due").replaceChildren(r.due.length ? h("div", {}, h("h3", {}, "To do"), h("ul", { class: "plain" }, ...r.due.map((d) =>
    h("li", {}, h("strong", { class: d.kind === "out" ? "bad" : "" }, d.text), d.critical ? h("span", { class: "chip warn" }, "essential") : null))))
    : h("p", { class: "muted" }, "Nothing to refill or replace this week."));
  $("#med-people").replaceChildren(...r.people.map(medPersonCard));
}
function daysChip(st) {
  if (st.days_left == null) return null;
  const cls = st.days_left <= 0 ? "bad" : st.days_left <= MED.lead_days ? "warn" : "";
  return h("span", { class: "chip " + cls }, st.days_left <= 0 ? "out" : `${st.days_left} day${st.days_left === 1 ? "" : "s"} left`);
}
function medPersonCard(p) {
  const facts = [["Allergies", p.allergies || "None known"], ["Blood type", p.blood_type], ["Conditions", p.conditions], ["Devices", p.devices],
    ["Pharmacy", p.pharmacy]].filter(([, v]) => v);
  const meds = p.meds.map((m) => h("div", { class: "med" },
    h("div", { class: "t" }, m.name, m.dose ? ` ${m.dose}` : "", daysChip(m.status), m.critical ? h("span", { class: "chip warn" }, "essential") : null,
      m.cold ? h("span", { class: "chip" }, "keep cold") : null),
    h("div", { class: "m" }, [m.how_often, m.status.refill_by && m.status.days_left != null ? `refill by ${new Date(m.status.refill_by + "T12:00").toLocaleDateString([], { month: "short", day: "numeric" })}` : "",
      m.bag_count ? `go-bag: ${fmtNum(m.bag_count)}` + (m.bag_expires ? `, expires ${m.bag_expires}` : "") : (m.critical ? "none in the go-bag" : ""),
      m.rx_number ? `Rx ${m.rx_number}` : ""].filter(Boolean).join(" · ")),
    m.status.todo.length ? h("div", { class: "m bad" }, m.status.todo.map((t) => t.text).join(" · ")) : null,
    p.can_edit ? h("div", { class: "m" },
      h("button", { class: "link", onclick: () => {
        const n = prompt(`How many doses of ${m.name} came in?`, m.per_day ? String(Math.round(m.per_day * 30)) : "30");
        if (n === null) return;
        if (!(+n >= 0)) return toast("Type a number", true);
        guarded(async () => { await api("POST", `meds/${m.id}/refilled`, { amount: +n }); toast("Supply updated"); loadMedical(); refresh(); });
      } }, "Refilled"), " · ",
      h("button", { class: "link", onclick: () => { editingMed = m; loadMedical(); } }, "Edit"), " · ",
      h("button", { class: "link", onclick: () => confirm(`Remove ${m.name}?`) && guarded(async () => { await api("DELETE", `meds/${m.id}`); loadMedical(); }) }, "Remove"))
      : null));
  const editing = editingMed && editingMed.person_id === p.id ? editingMed : null;
  return h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", {}, p.name, p.remote ? h("span", { class: "tag" }, "lives elsewhere") : null)),
    facts.length ? h("dl", { class: "med-facts" }, ...facts.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])) : null,
    meds.length ? meds : h("p", { class: "muted" }, p.medications ? `Listed on the Family tab: ${p.medications}. Add them here to track supply.` : "No medicines."),
    p.can_edit ? h("details", { open: editing ? true : null }, h("summary", { class: "link" }, editing ? `Edit ${editing.name}` : "Add a medicine"), medForm(p, editing)) : null);
}
function medForm(p, m) {
  m = m || {};
  const inp = (name, label, attrs = {}) => h("label", attrs.span ? { class: "span2" } : {}, label,
    h("input", { name, value: m[name] ?? "", ...Object.fromEntries(Object.entries(attrs).filter(([k]) => k !== "span")) }));
  const box = (name, label) => h("label", { class: "check" }, h("input", { type: "checkbox", name, checked: m[name] ? true : null }), label);
  return h("form", { class: "form-grid", onsubmit: (e) => {
    e.preventDefault();
    const f = e.target, body = Object.fromEntries(new FormData(f));
    body.person_id = p.id; body.critical = f.critical.checked; body.cold = f.cold.checked;
    if (m.id && +body.on_hand !== +m.on_hand) body.on_hand_at = new Date().toISOString().slice(0, 10);
    else if (m.id) body.on_hand_at = m.on_hand_at;
    guarded(async () => { await api("POST", m.id ? `meds/${m.id}` : "meds", body); editingMed = null; toast("Saved"); loadMedical(); refresh(); });
  } },
    inp("name", "Medicine", { required: true }), inp("dose", "Dose", { placeholder: "10 mg" }),
    inp("how_often", "How often", { placeholder: "twice a day" }), inp("per_day", "Doses a day", { type: "number", step: "0.5", min: "0", placeholder: "0 = as needed" }),
    inp("on_hand", "Doses at home now", { type: "number", step: "1", min: "0" }), inp("refill_by", "Refill by (optional)", { type: "date" }),
    inp("bag_count", "Doses in the go-bag", { type: "number", step: "1", min: "0" }), inp("bag_expires", "Go-bag doses expire", { type: "date" }),
    inp("pharmacy", "Pharmacy"), inp("rx_number", "Rx number"), inp("prescriber", "Prescriber"), inp("notes", "Notes", { span: true }),
    box("critical", "Essential: can't go without it"), box("cold", "Needs the fridge"),
    h("div", { class: "actions span-all" }, h("button", { type: "submit", class: "primary" }, m.id ? "Save" : "Add"),
      m.id ? h("button", { type: "button", onclick: () => { editingMed = null; loadMedical(); } }, "Cancel") : null));
}

/* ---------- power ---------- */
let PW = null, editingDev = null;
async function loadPower() {
  const r = await guarded(() => api("GET", "power"));
  if (!r) return;
  PW = r;
  const canEdit = isAdmin() || S.can_call;
  $("#pw-roof").textContent = "Rooftop solar with no battery switches off during an outage (so line crews are safe)" +
    (r.settings.inverter ? `. Check whether your ${r.settings.inverter} has a daytime emergency outlet; until then, this plan counts the roof as nothing.`
      : ". Add your inverter brand below; a few have a daytime emergency outlet. Until then, this plan counts the roof as nothing.");
  $("#pw-suggest").replaceChildren(...r.suggestions.map((s) => h("p", {}, h("strong", {}, s.why), " ",
    canEdit ? h("button", { class: "small", onclick: () => guarded(async () => { await api("POST", "power/preset", { key: s.preset }); toast("Added"); loadPower(); }) },
      `Add ${r.presets[s.preset].name}`) : null)));
  const lvlText = (l) => l.days == null ? "nothing planned" : l.days === "forever" ? "solar keeps up" :
    l.days >= 1 ? `${l.days} days` : `${l.hours} hours`;
  $("#pw-results").replaceChildren(...r.plan.batteries.map((b) => h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", {}, b.battery), h("span", { class: "muted" }, `${b.wh ? fmtNum(b.wh) + " Wh" : "capacity not set"}` +
      (b.solar_wh_day ? ` · solar adds ~${b.solar_wh_day} Wh a day` : ""))),
    ...b.warnings.map((w) => h("p", { class: "warn" }, w)),
    h("table", { class: "pw-levels" }, ...b.levels.map((l) => h("tr", {}, h("td", {}, l.label), h("td", { class: "muted" }, l.wh_day ? `${l.wh_day} Wh a day` : ""),
      h("td", {}, lvlText(l))))),
    b.devices.length ? devList(b.devices, canEdit) : h("p", { class: "muted" }, "Nothing plugged into it yet."))),
    r.plan.unpowered.length ? h("div", { class: "card" }, h("h2", {}, "Not on a battery"),
      h("p", { class: "muted" }, "These go dark in an outage."), devList(r.plan.unpowered, canEdit)) : null,
    !r.plan.batteries.length && !r.plan.unpowered.length ? h("div", { class: "card" }, h("p", { class: "muted" }, "Add your batteries and the devices that need power.")) : null);
  const keep = document.activeElement?.closest("#pw-batt-form");
  if (!keep) $("#pw-batts").replaceChildren(...(r.settings.batteries.length ? r.settings.batteries : [{ name: "Portable power station" }]).map(battRow));
  $("#pw-batt-form").inverter.value = r.settings.inverter || "";
  const bsel = $("#pw-dev-batt"), keepB = bsel.value;
  bsel.replaceChildren(h("option", { value: "" }, "No battery"), ...r.settings.batteries.map((b) => h("option", { value: b.name }, b.name)));
  bsel.value = keepB;
  if (!$("#pw-preset").options.length) $("#pw-preset").replaceChildren(h("option", { value: "" }, "Choose to fill in the form…"),
    ...Object.entries(r.presets).map(([k, p]) => h("option", { value: k }, `${p.name} (${p.watts} W)`)));
  $("#pw-batt-form").querySelectorAll("input, button").forEach((x) => (x.disabled = !canEdit));
  $("#pw-dev-form").querySelectorAll("input, select, button").forEach((x) => (x.disabled = !canEdit));
  $("#pw-assume").textContent = `Estimates: about ${Math.round(r.assumptions.usable * 100)}% of a battery's rating is usable through its outlets; ` +
    `a portable panel gives ~${r.assumptions.sun_hours} full-sun hours a day at ~${Math.round(r.assumptions.panel_eff * 100)}% of its rating. ` +
    "A plug-in watt meter (about $20) gives real numbers for your devices.";
}
function devList(devs, canEdit) {
  return h("ul", { class: "plain" }, ...devs.map((d) => h("li", {}, h("strong", {}, d.name), h("span", { class: "tag" }, PW.priorities[d.priority]),
    h("span", { class: "muted" }, ` ${fmtNum(d.watts)} W × ${fmtNum(d.hours)} h = ${Math.round(d.watts * d.hours)} Wh a day`),
    d.notes ? h("div", { class: "muted" }, d.notes) : null,
    canEdit ? h("div", {}, h("button", { class: "link", onclick: () => editDev(d) }, "Edit"), " · ",
      h("button", { class: "link", onclick: () => confirm(`Remove ${d.name}?`) && guarded(async () => { await api("DELETE", `power/devices/${d.id}`); loadPower(); }) }, "Remove")) : null)));
}
function battRow(b) {
  const f = (name, label, attrs = {}) => h("label", {}, label, h("input", { name, value: b[name] ?? "", ...attrs }));
  return h("div", { class: "pw-batt" }, f("name", "Battery"), f("wh", "Wh", { type: "number", min: 0 }), f("max_w", "Output W", { type: "number", min: 0 }),
    f("solar_w", "Solar W", { type: "number", min: 0 }), h("button", { type: "button", class: "link", onclick: (e) => e.target.closest(".pw-batt").remove() }, "Remove"));
}
$("#pw-batt-add").addEventListener("click", () => $("#pw-batts").append(battRow({})));
$("#pw-batt-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const batteries = $$("#pw-batts .pw-batt").map((row) => Object.fromEntries($$("input", row).map((i) => [i.name, i.value])));
  guarded(async () => { await api("POST", "power/settings", { batteries, inverter: e.target.inverter.value }); toast("Saved"); e.target.querySelector("button[type=submit]").focus(); loadPower(); });
});
$("#pw-preset").addEventListener("change", (e) => {
  const p = PW.presets[e.target.value];
  if (!p) return;
  const f = $("#pw-dev-form");
  for (const k of ["name", "watts", "hours", "surge_w", "priority", "notes"]) f[k].value = p[k] ?? "";
  e.target.value = "";
});
function editDev(d) {
  editingDev = d;
  const f = $("#pw-dev-form");
  for (const k of ["name", "watts", "hours", "surge_w", "priority", "battery", "notes"]) f[k].value = d[k] ?? "";
  $("#pw-dev-save").textContent = "Save"; $("#pw-dev-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "center" });
}
$("#pw-dev-cancel").addEventListener("click", () => { editingDev = null; $("#pw-dev-form").reset(); $("#pw-dev-save").textContent = "Add"; $("#pw-dev-cancel").hidden = true; });
$("#pw-dev-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  guarded(async () => {
    await api("POST", editingDev ? `power/devices/${editingDev.id}` : "power/devices", body);
    toast("Saved"); $("#pw-dev-cancel").click(); loadPower(); refresh();
  });
});

/* ---------- recovery: home inventory, food plan, money and accounts ---------- */
let RC = null, editingRc = null, FIN = null, editingFin = null;
const usd = (v) => v == null ? "" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
async function loadRecovery() {
  loadMealPlan();
  const r = await guarded(() => api("GET", "recovery"));
  if (!r) return;
  RC = r;
  const canEdit = isAdmin() || S.can_call;
  $("#rc-edit-box").hidden = !canEdit;
  $("#rc-totals").textContent = r.totals.items ? `${r.totals.items} items. Paid ${usd(r.totals.price_paid)}, to replace ${usd(r.totals.replacement_value)}.`
    : "Nothing here yet. Start with the big things: TVs, computers, furniture, appliances, jewelry, tools.";
  const sel = $("#rc-item-room"), keep = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "No room"), ...r.rooms.map((x) => h("option", { value: x.id }, x.name)));
  sel.value = keep;
  if (!$("#rc-cats").options.length) $("#rc-cats").replaceChildren(...r.categories.map((c) => h("option", { value: c })));
  $("#rc-rooms").replaceChildren(...r.rooms.map((x) => h("span", { class: "tag" }, `${x.name} (${x.item_count})`,
    canEdit ? h("button", { class: "link", "aria-label": `Rename ${x.name}`, onclick: () => {
      const name = prompt("Room name", x.name);
      if (name) guarded(async () => { await api("POST", `recovery/rooms/${x.id}`, { name }); loadRecovery(); });
    } }, " ✎") : null,
    canEdit ? h("button", { class: "link", "aria-label": `Remove ${x.name}`, onclick: () => confirm(`Remove the room ${x.name}? Its items stay, with no room.`) &&
      guarded(async () => { await api("DELETE", `recovery/rooms/${x.id}`); loadRecovery(); }) }, " ×") : null)));
  const groups = new Map();
  for (const it of r.items) { const k = it.room || "No room"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(it); }
  $("#rc-items").replaceChildren(...[...groups].map(([room, list]) => h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", {}, room), h("span", { class: "muted" },
      `${list.length} items · to replace ${usd(list.reduce((a, i) => a + (i.replacement_value || 0), 0))}`)),
    h("ul", { class: "plain" }, ...list.map((it) => rcItem(it, canEdit))))));
  if (isAdmin()) loadFinance(); else $("#fin-card").hidden = true;
}
function rcItem(it, canEdit) {
  const photos = it.files.filter((f) => f.kind === "photo"), receipts = it.files.filter((f) => f.kind === "receipt");
  const pick = (kind) => {
    const inp = h("input", { type: "file", accept: kind === "photo" ? "image/jpeg,image/png,image/webp" : "image/jpeg,image/png,image/webp,application/pdf", hidden: true });
    inp.addEventListener("change", () => inp.files[0] && guarded(async () => {
      const f = inp.files[0];
      if (f.size > RC.limits.max_mb * 1048576) throw new Error(`Files can be up to ${RC.limits.max_mb} MB.`);
      const res = await fetch(`/api/recovery/items/${it.id}/files?kind=${kind}&name=${encodeURIComponent(f.name)}`,
        { method: "POST", headers: { "X-Bunkr": "1", "Content-Type": f.type || "application/octet-stream" }, body: f });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      toast("Added"); loadRecovery();
    }));
    document.body.append(inp);
    inp.click();
    setTimeout(() => inp.remove(), 60000);
  };
  const delFile = (f) => confirm(`Remove this ${f.kind}?`) && guarded(async () => { await api("DELETE", `recovery/files/${f.id}`); loadRecovery(); });
  return h("li", {},
    h("strong", {}, it.name), it.category ? h("span", { class: "tag" }, it.category) : null,
    h("div", { class: "muted" }, [it.make_model, it.serial ? `Serial ${it.serial}` : "", it.purchased ? `bought ${it.purchased}` : "",
      it.price_paid != null ? `paid ${usd(it.price_paid)}` : "", it.replacement_value != null ? `to replace ${usd(it.replacement_value)}` : ""].filter(Boolean).join(" · ")),
    it.notes ? h("div", { class: "muted" }, it.notes) : null,
    photos.length ? h("div", { class: "rc-thumbs" }, ...photos.map((f) => h("span", {},
      h("a", { href: `/api/recovery/files/${f.id}`, target: "_blank", rel: "noopener" }, h("img", { src: `/api/recovery/files/${f.id}`, alt: `Photo of ${it.name}`, loading: "lazy" })),
      canEdit ? h("button", { class: "link", "aria-label": "Remove photo", onclick: () => delFile(f) }, "×") : null))) : null,
    receipts.length ? h("div", {}, "Receipts: ", ...receipts.map((f, n) => h("span", {}, n ? ", " : "",
      h("a", { href: `/api/recovery/files/${f.id}`, target: "_blank", rel: "noopener" }, f.name || `receipt ${n + 1}`),
      h("button", { class: "link", "aria-label": "Remove receipt", onclick: () => delFile(f) }, " ×")))) : null,
    canEdit ? h("div", {}, h("button", { class: "link", onclick: () => pick("photo") }, "Add photo"),
      isAdmin() ? [" · ", h("button", { class: "link", onclick: () => pick("receipt") }, "Add receipt")] : null, " · ",
      h("button", { class: "link", onclick: () => editRc(it) }, "Edit"), " · ",
      h("button", { class: "link", onclick: () => confirm(`Remove ${it.name} and its photos?`) && guarded(async () => { await api("DELETE", `recovery/items/${it.id}`); loadRecovery(); }) }, "Remove")) : null);
}
function editRc(it) {
  editingRc = it;
  const f = $("#rc-item-form");
  for (const k of ["name", "room_id", "category", "make_model", "serial", "purchased", "price_paid", "replacement_value", "notes"]) if (f[k]) f[k].value = it[k] ?? "";
  $("#rc-edit-box").open = true;
  $("#rc-item-save").textContent = "Save"; $("#rc-item-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "center" });
}
$("#rc-item-cancel").addEventListener("click", () => { editingRc = null; $("#rc-item-form").reset(); $("#rc-item-save").textContent = "Add item"; $("#rc-item-cancel").hidden = true; });
$("#rc-item-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  if (!isAdmin()) delete body.serial;
  guarded(async () => {
    await api("POST", editingRc ? `recovery/items/${editingRc.id}` : "recovery/items", body);
    const room = e.target.room_id.value;
    toast("Saved"); $("#rc-item-cancel").click(); e.target.room_id.value = room; e.target.name.focus(); loadRecovery();
  });
});
$("#rc-room-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { const r = await api("POST", "recovery/rooms", { name: e.target.name.value }); e.target.reset(); await loadRecovery(); $("#rc-item-room").value = r.id; });
});

async function loadMealPlan() {
  const days = Math.max(1, Math.min(90, parseInt($("#mp-days").value, 10) || 7));
  $("#mp-print").href = `/api/meal-plan.html?days=${days}`;
  const m = await guarded(() => api("GET", `meal-plan?days=${days}`));
  if (!m) return;
  const unitsTxt = (x) => `${fmtNum(x.units)} ${x.unit || ""}`.trim();
  $("#mp-out").replaceChildren(
    h("p", {}, h("strong", {}, `Food lasts about ${fmtNum(m.food_days)} days`), ` for ${m.people} people (${m.kcal_day.toLocaleString()} kcal a day).`),
    m.eat_first.length ? h("div", {}, h("h3", {}, "Eat these first"), h("ul", { class: "plain" }, ...m.eat_first.slice(0, 12).map((f) =>
      h("li", { class: f.expired ? "bad" : "" }, h("strong", {}, f.name), ` ${unitsTxt(f)} · ${f.expired ? "expired" : "use by"} ${f.expires}` + (f.location ? ` · ${f.location}` : ""))))) : null,
    h("details", {}, h("summary", {}, `Day by day (${m.days} days)`), h("ul", { class: "plain" }, ...m.plan.map((d) => h("li", {},
      h("strong", {}, `Day ${d.day}: `), d.eat.length ? d.eat.map((x) => `${unitsTxt(x)} ${x.name}`).join(", ") : "nothing left",
      d.short ? h("span", { class: "warn" }, ` (short ${d.short.toLocaleString()} kcal)`) : null)))),
    m.no_kcal.length ? h("p", { class: "muted" }, `No calories set, so not in the plan: ${m.no_kcal.join(", ")}. Add them in Inventory.`) : null);
}
$("#mp-days").addEventListener("change", loadMealPlan);

async function loadFinance() {
  const r = await guarded(() => api("GET", "finance"));
  $("#fin-card").hidden = !r;
  if (!r) return;
  FIN = r;
  if (!$("#fin-kind").options.length) $("#fin-kind").replaceChildren(...Object.entries(r.kinds).map(([k, v]) => h("option", { value: k }, v)));
  $("#fin-list").replaceChildren(r.accounts.length ? h("ul", { class: "plain" }, ...r.accounts.map((a) => h("li", {},
    h("strong", {}, a.institution), h("span", { class: "tag" }, r.kinds[a.kind] || "Other"), a.last4 ? ` ends in ${a.last4}` : "", a.holder ? ` · ${a.holder}` : "",
    h("div", { class: "muted" }, [a.phone, a.website, a.contact ? `Call: ${a.contact}` : "", a.paperwork ? `Paperwork: ${a.paperwork}` : ""].filter(Boolean).join(" · ")),
    a.notes ? h("div", { class: "muted" }, a.notes) : null,
    h("div", {}, h("button", { class: "link", onclick: () => editFin(a) }, "Edit"), " · ",
      h("button", { class: "link", onclick: () => confirm(`Remove ${a.institution}?`) && guarded(async () => { await api("DELETE", `finance/${a.id}`); loadFinance(); }) }, "Remove")))))
    : h("p", { class: "muted" }, "No accounts yet: bank, credit cards, home and car insurance, retirement, loans, utilities."));
}
function editFin(a) {
  editingFin = a;
  const f = $("#fin-form");
  for (const k of ["institution", "kind", "holder", "last4", "phone", "website", "contact", "paperwork", "notes"]) f[k].value = a[k] ?? "";
  $("#fin-box").open = true;
  $("#fin-save").textContent = "Save"; $("#fin-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth", block: "center" });
}
$("#fin-cancel").addEventListener("click", () => { editingFin = null; $("#fin-form").reset(); $("#fin-save").textContent = "Add"; $("#fin-cancel").hidden = true; });
$("#fin-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  guarded(async () => {
    await api("POST", editingFin ? `finance/${editingFin.id}` : "finance", body);
    toast("Saved"); $("#fin-cancel").click(); loadFinance();
  });
});

/* ---------- family ---------- */
let F = null, editingPerson = null, editingContact = null;
async function loadFamily() {
  const r = await guarded(() => api("GET", "family"));
  if (!r) return;
  F = r;
  const keepSite = $("#person-site").value, keepUser = $("#person-user").value;
  $("#person-site").replaceChildren(h("option", { value: "" }, "None"), ...S.places.map((p) => h("option", { value: p.id }, p.name)));
  $("#person-site").value = keepSite;
  if (isAdmin() && S.require_login) {
    const users = await api("GET", "users").catch(() => []);
    $("#person-user").replaceChildren(h("option", { value: "" }, "No login"), ...users.map((u) => h("option", { value: u.id }, u.username)));
    $("#person-user").value = keepUser;
  }
  const pf = $("#planinfo-form");
  for (const k of ["household_name", "mesh_channels", "notes"]) if (document.activeElement !== pf[k]) pf[k].value = F.plan_info[k] || "";
  renderPeople(); renderContacts(); renderCalendar();
}
$("#planinfo-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { await api("POST", "plan-info", Object.fromEntries(new FormData(e.target))); toast("Saved"); });
});
function renderPeople() {
  $("#people-cards").replaceChildren(...F.people.map((p) => {
    const st = p.phone_status;
    const checks = JSON.parse(p.phone_checks || "{}");
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("h2", {}, p.name, h("span", { class: "tag" }, p.role), p.call_sign ? h("span", { class: "tag" }, p.call_sign) : null,
        p.remote ? h("span", { class: "tag" }, "lives elsewhere") : null),
        h("div", { class: "row-right" }, h("button", { class: "link", onclick: () => editPerson(p) }, "Edit"),
          h("button", { class: "danger", onclick: () => confirm(`Remove ${p.name}?`) && guarded(async () => { await api("DELETE", "people/" + p.id); loadFamily(); refresh(); }) }, "Remove"))),
      p.allergies || p.medications || p.conditions ? h("p", { class: "muted" }, [p.allergies && "Allergies: " + p.allergies,
        p.medications && "Medications: " + p.medications, p.conditions && "Conditions: " + p.conditions].filter(Boolean).join(" · ")) : null,
      p.phone === "none" ? null : h("details", {},
        h("summary", {}, `Phone readiness: ${st.done} of ${st.total}`, " · ",
          h("span", { class: st.checked_at ? "" : "warn" }, st.checked_at ? `last checked ${st.checked_at}` : "never checked")),
        h("ul", { class: "steps" }, ...st.items.map((c) => h("li", {}, h("label", {},
          h("input", { type: "checkbox", "data-check": c.id, checked: !!checks[c.id] }),
          h("span", {}, c.label, c.optional ? h("span", { class: "tag" }, "optional") : null, c.note ? h("span", { class: "muted" }, ` (${c.note})`) : null))))),
        h("button", { class: "primary small", onclick: (e) => {
          const box = e.target.closest("details");
          const sel = Object.fromEntries([...box.querySelectorAll("[data-check]")].map((i) => [i.dataset.check, i.checked]));
          guarded(async () => { await api("POST", `people/${p.id}/phone`, { checks: sel, mark_checked: true }); toast("Phone checked today"); loadFamily(); });
        } }, "Save and mark checked today")));
  }));
}
/* ---------- MeshCore family channel ---------- */
const MESH_STATE = { off: "Off", connecting: "Connecting to the radio…", connected: "Connected", error: "Can't reach the radio" };
async function loadMesh() {
  const r = await api("GET", "mesh").catch(() => null);
  if (!r) return;
  const st = r.status || {}, s = r.settings || {};
  $("#mesh-state").replaceChildren(h("span", { class: "chip " + (st.state === "connected" ? "ok" : st.state === "error" ? "missing" : "") },
    MESH_STATE[st.state] || st.state || "Off"),
    st.radio && st.radio.name ? h("span", { class: "muted" }, ` ${st.radio.name}${st.radio.model ? " (" + st.radio.model + ")" : ""}`
      + (st.radio.freq_mhz ? ` · ${st.radio.freq_mhz} MHz SF${st.radio.sf}` : "")) : null,
    st.error ? h("div", { class: "bad" }, st.error) : null);
  const chans = r.channels || [];
  const cmds = [["?", "the list of commands"], ["WHO", "everyone's last check-in"], ["READY", "days of water and food", 1],
    ["ALERTS", "what's happening near our places"], ["FLIGHT", "flights we're following"], ["NEWS", "the morning brief in a line"],
    ["PING", "is BUNKR listening"], ["!have water", "what we have and where", 1], ["!add 2 cases water @ garage", "add to the inventory (adults)", 1],
    ["!used 1 water", "take some away (adults)", 1], ["!home", "Home Assistant: what you can run; !home lights, !home status", 1]];
  $("#mesh-join").replaceChildren(chans.length
    ? [h("p", {}, "On your phone, install the MeshCore app and pair your radio, then add ", chans.length > 1 ? "these channels:" : "the channel:"),
      h("ul", { class: "plain mesh-chans" }, ...chans.map((c) => h("li", {}, h("a", { href: c.link }, `Add "${c.name}" to MeshCore`),
        h("span", { class: "muted" }, c.kind === "group" ? " · trusted group" : " · family"),
        isAdmin() && !c.shared ? h("span", { class: "tag" }, "admins only") : null))),
      h("p", {}, "To check in, send on the channel:"),
      h("ul", { class: "plain mesh-words" },
        h("li", {}, h("strong", {}, "SAFE"), " (and where you are, if you like)"),
        h("li", {}, h("strong", {}, "OMW"), " for on my way"),
        h("li", {}, h("strong", {}, "HELP"), " and what's wrong")),
      h("p", { class: "muted" }, "MECP reports from Mesh-Client count too: MAYDAY and URGENT are Need help, B03 is I'm safe. Use your BUNKR name as your name in MeshCore, or an adult can set your mesh name under Household members."),
      chans.some((c) => c.bot !== "off") ? h("details", { class: "mesh-cmds" }, h("summary", {}, "Ask BUNKR from the radio"),
        h("ul", { class: "plain" }, ...cmds.map(([c, d, full]) => h("li", {}, h("strong", {}, c), " — " + d + (full && chans.some((x) => x.bot === "basic") ? " (family channel only)" : "")))),
        h("p", { class: "muted" }, "One word on its own, or ! in front of anything longer, so ordinary chat never sets BUNKR off. HELP always means you need help.")) : null]
    : h("p", { class: "muted" }, "Not set up yet. An admin connects a MeshCore radio below."));
  const admin = isAdmin();
  $("#mesh-admin").hidden = !admin;
  if (!admin) return;
  MESH_ADMIN = r;
  const f = $("#mesh-form");
  if (!f.contains(document.activeElement)) {
    f.enabled.checked = !!s.enabled; f.connect.value = s.connect || "";
    $("#mesh-channels").replaceChildren(...chans.map(meshChannelRow));
  }
  renderMeshNodes(r.nodes || []);
  $("#mesh-log").replaceChildren(...(r.log.length ? r.log.map((x) => h("li", {},
    h("span", { class: "muted" }, shortWhen(x.at) + (x.direction === "out" ? " sent " : " heard ") + (x.channel && chans.length > 1 ? `on ${x.channel} ` : "")),
    x.sender ? h("strong", {}, x.sender + ": ") : null, x.text, x.result && x.result !== "sent" ? h("span", { class: "tag" }, x.result) : null))
    : [h("li", { class: "muted" }, "Nothing yet.")]));
}
let MESH_ADMIN = null;
const personOptions = (sel) => [h("option", { value: "" }, "— nobody yet —"),
  ...((S && S.board && S.board.people) || []).map((p) => h("option", { value: p.uid, selected: p.uid === sel }, p.name))];
function renderMeshNodes(list) {
  const box = $("#mesh-nodes");
  if (box.contains(document.activeElement)) return;  // don't redraw under someone typing a note
  $("#mesh-node-form .mesh-person-select").replaceChildren(...personOptions(""));
  const row = (n) => h("li", { class: "mesh-node" + (n.person_uid ? " mine" : "") },
    h("div", { class: "mesh-node-head" },
      h("strong", {}, n.name || "(no name yet)"), n.label ? h("span", { class: "muted" }, ` · ${n.label}`) : null,
      h("span", { class: "tag" }, n.type_name),
      n.name_clash ? h("span", { class: "tag warn", title: "Nodes of different people share this name, so BUNKR can't tell which one sent a channel message. Rename one in the MeshCore app." }, "name shared") : null),
    h("div", { class: "muted mesh-node-meta" },
      h("code", { title: n.public_key }, n.public_key.slice(0, 8) + "…" + n.public_key.slice(-4)), " ",
      h("button", { type: "button", class: "link", onclick: () => navigator.clipboard?.writeText(n.public_key).then(() => toast("Key copied")) }, "copy"),
      n.hops != null ? ` · ${n.hops === 0 ? "direct" : n.hops + " hop" + (n.hops === 1 ? "" : "s")}` : "",
      n.last_heard ? ` · talked ${sinceIso(n.last_heard)}` : n.last_seen ? ` · heard ${sinceIso(n.last_seen)}` : " · added by hand, not heard yet"),
    h("div", { class: "mesh-node-acts" },
      h("select", { "aria-label": `Whose is ${n.name}`, onchange: (e) => guarded(async () => {
        await api("POST", "mesh/nodes/" + n.public_key, { person_uid: e.target.value }); toast("Saved"); loadMesh(); }) }, ...personOptions(n.person_uid)),
      h("input", { value: n.label || "", maxlength: 40, placeholder: "note", "aria-label": `Note for ${n.name}`, onchange: (e) => guarded(async () => {
        await api("POST", "mesh/nodes/" + n.public_key, { person_uid: n.person_uid, label: e.target.value }); toast("Saved"); }) }),
      h("button", { type: "button", class: "link danger", onclick: () => confirm(`Forget ${n.name || "this node"}? If the radio hears it again it comes back, without an owner.`) &&
        guarded(async () => { await api("DELETE", "mesh/nodes/" + n.public_key); loadMesh(); }) }, "Forget")));
  const people = list.filter((n) => n.person_uid || n.node_type === 1 || n.node_type === 0);
  const infra = list.filter((n) => !people.includes(n));
  box.replaceChildren(list.length ? [
    h("ul", { class: "plain mesh-node-list" }, ...people.map(row)),
    infra.length ? h("details", {}, h("summary", {}, `Repeaters, room servers and sensors (${infra.length})`),
      h("ul", { class: "plain mesh-node-list" }, ...infra.map(row))) : null]
    : h("p", { class: "muted" }, "None yet. Nodes show up here as the radio hears their adverts."));
}
$("#mesh-node-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  guarded(async () => {
    await api("POST", "mesh/nodes", { public_key: f.public_key.value, name: f.name.value, person_uid: f.person_uid.value, label: f.label.value });
    f.reset(); toast("Node added"); loadMesh();
  });
});
function meshChannelRow(c) {
  const r = MESH_ADMIN, sw = (name, label) => h("label", { class: "check" }, h("input", { type: "checkbox", name, checked: !!c[name] }), label);
  const row = h("fieldset", { class: "mesh-chan" },
    h("legend", {}, c.name || "New channel", h("span", { class: "muted" }, " · " + (r.kinds[c.kind] || c.kind))),
    h("input", { type: "hidden", name: "kind", value: c.kind }),
    h("div", { class: "form-grid" },
      h("label", {}, "Name", h("input", { name: "name", maxlength: 31, value: c.name || "", placeholder: c.kind === "group" ? "Circle" : "Family" })),
      h("label", {}, "Slot on this radio", h("input", { name: "index", type: "number", min: 1, max: 39, value: c.index || "" })),
      h("label", { class: "span2" }, "Key", h("span", { class: "inline-pair" },
        h("input", { name: "secret", value: c.secret || "", autocomplete: "off", spellcheck: "false", class: "mono", placeholder: "empty: BUNKR makes one" }),
        h("button", { type: "button", class: "small", onclick: (e) => { const i = e.target.closest(".mesh-chan").querySelector("[name=secret]");
          if (!i.value || confirm("Make a new key? Everyone on this channel has to add it again.")) { i.value = ""; toast("Save to make the new key"); } } }, "New key"))),
      h("label", {}, "Commands", h("select", { name: "bot" }, ...Object.entries(r.bot_levels).map(([k, v]) => h("option", { value: k, selected: c.bot === k }, v)))),
      h("div", { class: "span-all mesh-switches" }, sw("checkins", "Check-ins count"), sw("reply", "Answer check-ins"),
        sw("send_calls", "Send family calls"), sw("send_help", "Send \"needs help\""), sw("shared", "Shown to the family"))),
    h("p", { class: "muted mesh-chan-acts" }, c.link ? [h("a", { href: c.link }, "Add to MeshCore"), " · ",
        h("button", { type: "button", class: "link", onclick: () => navigator.clipboard?.writeText(c.link).then(() => toast("Link copied")) }, "Copy the link"), " · ",
        h("button", { type: "button", class: "link", onclick: () => guarded(async () => { const x = await api("POST", "mesh/test", { index: c.index });
          toast(`Test queued on ${x.channel}`); setTimeout(loadMesh, 5000); }) }, "Send a test"), " · "] : null,
      h("button", { type: "button", class: "link danger", onclick: (e) => confirm(`Remove ${c.name || "this channel"} from BUNKR? (Save to apply.)`) && e.target.closest(".mesh-chan").remove() }, "Remove")));
  return row;
}
$("#mesh-form").addEventListener("click", (e) => {
  const b = e.target.closest("[data-add-kind]");
  if (!b || !MESH_ADMIN) return;
  const used = new Set($$("#mesh-channels [name=index]").map((i) => +i.value));
  let index = 1; while (used.has(index)) index++;
  const kind = b.dataset.addKind;
  $("#mesh-channels").append(meshChannelRow({ ...MESH_ADMIN.defaults[kind], kind, name: "", index, secret: "" }));
});
$("#mesh-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const channels = $$("#mesh-channels .mesh-chan").map((row) => {
    const v = (n) => row.querySelector(`[name=${n}]`);
    return { name: v("name").value, index: v("index").value, secret: v("secret").value, kind: v("kind").value, bot: v("bot").value,
      ...Object.fromEntries(["checkins", "reply", "send_calls", "send_help", "shared"].map((k) => [k, v(k).checked])) };
  });
  guarded(async () => {
    await api("POST", "mesh", { enabled: f.enabled.checked, connect: f.connect.value, channels });
    toast("Saved. The radio reconnects in a few seconds."); document.activeElement?.blur(); setTimeout(loadMesh, 4000); loadMesh();
  });
});
function editPerson(p) {
  editingPerson = p.id;
  const f = $("#person-form");
  for (const k of ["name", "role", "phone", "call_sign", "gmrs_call", "mesh_name", "blood_type", "allergies", "medications", "conditions", "doctor",
    "devices", "pharmacy", "insurance"]) f[k].value = p[k] || "";
  f.remote.checked = !!p.remote; f.site_id.value = p.site_id ?? ""; f.user_id.value = p.user_id ?? "";
  $("#person-cancel").hidden = false; f.scrollIntoView({ behavior: "smooth" });
}
$("#person-cancel").addEventListener("click", () => { editingPerson = null; $("#person-form").reset(); $("#person-cancel").hidden = true; });
$("#person-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    const data = Object.fromEntries(new FormData(e.target));
    if (!isAdmin()) delete data.user_id;
    await api("POST", "people" + (editingPerson ? "/" + editingPerson : ""), data);
    toast("Saved"); editingPerson = null; e.target.reset(); $("#person-cancel").hidden = true; loadFamily(); refresh();
  });
});
function renderContacts() {
  $("#contact-table tbody").replaceChildren(...(F.contacts.length ? F.contacts.map((c) => h("tr", {},
    h("td", {}, c.name, c.out_of_area ? h("span", { class: "tag hz" }, "out of area") : null), h("td", {}, c.relation),
    h("td", {}, c.phone), h("td", {}, c.email),
    h("td", {}, h("button", { class: "link", onclick: () => editContact(c) }, "Edit"),
      h("button", { class: "danger", onclick: () => confirm(`Remove ${c.name}?`) && guarded(async () => { await api("DELETE", "contacts/" + c.id); loadFamily(); }) }, "Remove"))))
    : [h("tr", {}, h("td", { colspan: 5, class: "muted" }, "No contacts yet."))]));
}
function editContact(c) {
  editingContact = c.id;
  const f = $("#contact-form");
  for (const k of ["name", "relation", "phone", "email", "notes"]) f[k].value = c[k];
  f.out_of_area.checked = !!c.out_of_area;
  $("#contact-cancel").hidden = false;
}
$("#contact-cancel").addEventListener("click", () => { editingContact = null; $("#contact-form").reset(); $("#contact-cancel").hidden = true; });
$("#contact-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.out_of_area = e.target.out_of_area.checked;
  guarded(async () => {
    await api("POST", "contacts" + (editingContact ? "/" + editingContact : ""), data);
    toast("Saved"); editingContact = null; e.target.reset(); $("#contact-cancel").hidden = true; loadFamily();
  });
});
function renderCalendar() {
  $("#cal-list").replaceChildren(...F.calendar.slice(0, 40).map((c) => h("li", {},
    h("span", {}, h("span", { class: c.overdue ? "warn" : "" }, c.title), h("span", { class: "why" }, ` · ${c.repeat}`)),
    h("span", {}, h("span", { class: "num " + (c.overdue ? "warn" : "") }, c.overdue ? "due now" : c.due), " ",
      c.kind === "task" ? h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", `tasks/${c.id}/done`); toast("Done"); loadFamily(); }) }, "Done") :
      c.kind === "node" && isAdmin() ? h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", `nodes/${c.id}/reminder`, { kind: c.reminder }); loadFamily(); }) }, "Done") :
      c.kind === "renew" ? h("button", { class: "link", onclick: () => confirm(`Mark "${c.title}" done? Reminders stop. Set the next date on the Documents tab.`) &&
        guarded(async () => { await api("POST", `documents/${c.id}/renewed`); toast("Done"); loadFamily(); }) }, "Done") :
      c.kind === "licence" ? h("button", { class: "link", onclick: () => confirm(`Mark "${c.title}" done? Reminders stop. Set the new date on the Comms tab.`) &&
        guarded(async () => { await api("POST", `licences/${c.id}/renewed`); toast("Done"); loadFamily(); }) }, "Done") : null))));
  $("#task-list").replaceChildren(...F.tasks.map((t) => h("li", {},
    h("span", { class: t.active ? "" : "muted" }, t.title, h("span", { class: "why" }, ` · ${t.every_days ? "every " + t.every_days + " days" : "yearly " + t.yearly}`)),
    h("span", {}, h("button", { class: "link", onclick: () => guarded(async () => { await api("POST", `tasks/${t.id}/active`, { active: !t.active }); loadFamily(); }) }, t.active ? "Turn off" : "Turn on"),
      t.key ? null : h("button", { class: "danger", onclick: () => guarded(async () => { await api("DELETE", "tasks/" + t.id); loadFamily(); }) }, "Delete")))));
}
$("#task-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => { await api("POST", "tasks", Object.fromEntries(new FormData(e.target))); e.target.reset(); toast("Task added"); loadFamily(); });
});

/* ---------- scenarios ---------- */
const STATUS_ICON = { ok: "✓", short: "!", missing: "✗" };
const STATUS_CLASS = { ok: "good", short: "warn", missing: "bad" };
function scenarioCard(scn, compact = false) {
  const run = scn.run;
  const stepList = (phase) => h("ul", { class: "steps" }, ...scn.steps[phase].map((text, i) => {
    const key = `${phase}-${i}`;
    const done = run && run.checked[key];
    return h("li", {}, run ? h("label", {}, h("input", { type: "checkbox", checked: !!done, onchange: (e) =>
      guarded(async () => { await api("POST", `scenarios/${scn.id}/check`, { step: key, done: e.target.checked }); await refresh(); if (!$("#tab-scenarios").hidden) renderScenarios(); }) }),
      h("span", {}, text, done ? h("span", { class: "muted" }, ` (${done.by})`) : null)) : text);
  }));
  const body = (content) => compact ? content : h("details", { class: "scn-body", open: !!run },
    h("summary", {}, run ? "Checklist" : "Checklist and supplies"), content);
  return h("div", { class: "card" + (run ? " run-card" : ""), id: compact ? null : "scn-" + scn.id },
    h("div", { class: "scn-head" }, h("h2", {}, scn.label),
      scn.why.length ? h("span", { class: "tag hz" }, scn.why.join(", ")) : null,
      h("span", { class: "tag" }, `supplies ${scn.ready} of ${scn.total}`),
      h("span", { class: "row-right" }, run
        ? h("button", { onclick: () => confirm(`End the ${scn.label} checklist?`) && guarded(async () => { await api("POST", `scenarios/${scn.id}/end`); await refresh(); renderScenarios(); }) }, "End")
        : h("button", { class: "primary", onclick: () => guarded(async () => { await api("POST", `scenarios/${scn.id}/start`); await refresh(); renderScenarios(); }) }, "Start"))),
    run ? h("p", { class: "warn" }, `In progress since ${when(run.started_at)}`) : null,
    body(h("div", { class: "grid2" },
      h("div", {}, run ? null : h("h3", {}, "Before"), run ? null : stepList("before"),
        h("h3", {}, "During"), stepList("during"),
        scn.power_plan && scn.power_plan.length ? [h("h3", {}, "Power plan"), h("ul", { class: "steps" }, ...scn.power_plan.map((t) => h("li", {}, t))),
          h("p", {}, h("button", { class: "link", onclick: () => showTab("power") }, "Open the power plan"))] : null,
        h("h3", {}, "After"), stepList("after")),
      compact ? null : h("div", {}, h("h3", {}, "Supplies"), ...scn.supplies.map((sp) => h("div", { class: "supply" },
        h("span", { class: STATUS_CLASS[sp.status] }, STATUS_ICON[sp.status]),
        h("span", {}, sp.label, h("span", { class: "muted" }, ` · ${sp.detail}`)),
        sp.items.length ? h("span", { class: "where" }, sp.items.slice(0, 4).map((i) =>
          `${i.name}${i.text ? ": " + i.text : i.quantity !== undefined ? " ×" + fmtNum(i.quantity) : ""} (${i.location})`).join("; ") + (sp.items.length > 4 ? "…" : "")) : null))))));
}
function renderScenarios() {
  $("#scn-cards").replaceChildren(...S.scenarios.map((x) => scenarioCard(x)));
  $("#scn-cards details.scn-body")?.setAttribute("open", "");  // the most likely one for here
}
function renderRunBanner() {
  const active = S.scenarios.filter((x) => x.run);
  $("#run-card").hidden = !active.length;
  if (active.length) $("#run-card").replaceChildren(...active.map((x) => scenarioCard(x, true)));
}

/* ---------- phone header: tuck it away while scrolling down or typing ---------- */
(() => {
  const top = $(".top"), phone = matchMedia("(max-width: 720px)");
  let lastY = scrollY;
  const typing = () => phone.matches && document.activeElement?.matches("input:not([type=checkbox]):not([type=radio]), textarea, select");
  addEventListener("scroll", () => {
    const y = scrollY;
    if (!phone.matches || y < top.offsetHeight) top.classList.remove("tucked");
    else if (y > lastY + 8) top.classList.add("tucked");
    else if (y < lastY - 8 && !typing()) top.classList.remove("tucked");
    lastY = y;
  }, { passive: true });
  document.addEventListener("focusin", () => { if (typing()) top.classList.add("tucked"); });
  document.addEventListener("focusout", () => setTimeout(() => {
    if (!typing() && scrollY < top.offsetHeight) top.classList.remove("tucked");
  }, 0));
})();

/* ---------- boot ---------- */
guarded(async () => {
  await refresh();
  let tab = "home";
  try { tab = localStorage.getItem("bunkr-tab") || tab; } catch {}
  if (location.hash.length > 1 && $(`#tabs [data-tab="${location.hash.slice(1)}"]`)) tab = location.hash.slice(1);
  if (!$(`#tabs [data-tab="${tab}"]`) || $(`#tabs [data-tab="${tab}"]`).hidden) tab = "home";
  showTab(tab);
  if (isAdmin()) pollJob();
});

// a shortened paragraph in the brief opens in full when tapped
document.addEventListener("click", (e) => { const p = e.target.closest(".story .agree"); if (p) p.classList.toggle("full"); });
