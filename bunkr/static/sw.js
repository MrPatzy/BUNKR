// BUNKR service worker: keeps the last copy of the app and of what you last saw, so BUNKR still opens with no
// connection (a plane, a dead cell tower), and shows notifications sent by the hub.
// - The app itself (page, script, styles, icons, map): fetched fresh when the hub answers, the saved copy when it doesn't.
// - Data: only what the hub marks with X-Bunkr-Keep (the family board, plans, contacts, medical cards, alerts, news),
//   never admin screens. Firearms are taken out before anything is saved. Logging out deletes the saved data.
const SHELL = "bunkr-shell-v1", DATA = "bunkr-data-v1";
const SHELL_FILES = ["/", "/app.js", "/style.css", "/world.svg", "/logo.jpg", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/mark-96.png"];
const WAIT_MS = 6000;  // a plane's Wi-Fi with no internet hangs instead of failing: give up and use the saved copy

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => Promise.all(SHELL_FILES.map((u) =>
    fetch(u, { credentials: "same-origin" }).then((r) => r.ok && !r.redirected ? c.put(u, r) : null).catch(() => null)))));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  for (const k of await caches.keys()) if (![SHELL, DATA].includes(k)) await caches.delete(k);
  await self.clients.claim();
})()));
self.addEventListener("message", (event) => {
  if (event.data === "forget") event.waitUntil(caches.delete(DATA));
});

function timed(req) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout")), WAIT_MS);
    fetch(req).then((r) => { clearTimeout(t); resolve(r); }, (e) => { clearTimeout(t); reject(e); });
  });
}

async function keepData(req, res) {
  const keep = res.headers.get("X-Bunkr-Keep");
  if (!keep || !res.ok) return;
  let body = await res.clone().text();
  const drop = (res.headers.get("X-Bunkr-Drop") || "").split(",").filter(Boolean);
  if (drop.length) {
    try {
      const j = JSON.parse(body);
      if (Array.isArray(j.items)) j.items = j.items.filter((i) => !drop.includes(i.category));
      for (const k of ["ammo", "magazines"]) if (k in j) j[k] = Array.isArray(j[k]) ? [] : {};
      body = JSON.stringify(j);
    } catch (e) { return; }  // can't clean it: don't keep it
  }
  const c = await caches.open(DATA);
  await c.put(req.url, new Response(body, { headers: { "Content-Type": "application/json", "X-Bunkr-Saved": new Date().toISOString() } }));
}

self.addEventListener("fetch", (event) => {
  const req = event.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) {
    if (url.pathname.startsWith("/api/sync/")) return;
    event.respondWith((async () => {
      try {
        const res = await timed(req);
        if (res.status === 401) await caches.delete(DATA);
        else event.waitUntil(keepData(req, res).catch(() => {}));
        return res;
      } catch (e) {
        const saved = await (await caches.open(DATA)).match(req.url);
        if (saved) return saved;
        return new Response(JSON.stringify({ error: "No connection to BUNKR, and nothing saved on this phone for this yet.", offline: true }),
          { status: 503, headers: { "Content-Type": "application/json", "X-Bunkr-Offline": "1" } });
      }
    })());
    return;
  }
  const page = req.mode === "navigate" && (url.pathname === "/" || url.pathname === "/index.html");
  const shell = page || SHELL_FILES.includes(url.pathname) || url.pathname.startsWith("/icons/");
  if (!shell) return;
  event.respondWith((async () => {
    const key = page ? "/" : url.pathname;
    try {
      const res = await timed(req);
      // the login page comes back as a redirect: don't save that over the app
      if (res.ok && !res.redirected && res.type === "basic") {
        const copy = res.clone();
        event.waitUntil(caches.open(SHELL).then((c) => c.put(key, copy)).catch(() => {}));
      }
      return res;
    } catch (e) {
      const saved = await caches.match(key, { cacheName: SHELL });
      if (saved) return saved;
      throw e;
    }
  })());
});

self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch (e) { msg = { title: "BUNKR", body: event.data ? event.data.text() : "" }; }
  const urgent = msg.tier === "flash" || (msg.tier === "family" && /help|call/i.test(msg.title || ""));
  // an open BUNKR updates its bell straight away
  self.clients.matchAll({ type: "window" }).then((ws) => ws.forEach((w) => w.postMessage("pushed"))).catch(() => {});
  event.waitUntil(self.registration.showNotification(msg.title || "BUNKR", {
    body: msg.body || "",
    tag: msg.tag || undefined,          // a newer message about the same thing replaces the old one
    renotify: !!urgent,
    requireInteraction: !!urgent,       // life-safety stays on screen until tapped
    icon: "/icons/icon-192.png",
    badge: "/icons/mark-96.png",
    data: { url: msg.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data && event.notification.data.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin) { await w.focus(); w.navigate(url); return; }
    }
    await self.clients.openWindow(url);
  })());
});
