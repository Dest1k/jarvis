/* sw.js — service worker JARVIS OS PWA.
 * Офлайн-шелл (app shell) + пуш-уведомления о критических алертах и pending-HITL.
 * НЕ кэшируем /api/* (динамика ядра) — только статический шелл. */
const CACHE = "jarvis-shell-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Ядро/API и WebSocket — всегда из сети (никакого кэша).
  if (url.pathname.startsWith("/api/") || e.request.method !== "GET") return;
  // Статический шелл — cache-first с фолбэком в сеть.
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return resp;
    }).catch(() => caches.match("/")))
  );
});

/* Пуш-уведомления: критические алерты и запросы подтверждения (HITL). */
self.addEventListener("push", (e) => {
  let data = { title: "JARVIS", body: "Событие системы", tag: "jarvis" };
  try { if (e.data) data = { ...data, ...e.data.json() }; } catch (_) {}
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    tag: data.tag,
    icon: "/icon.svg",
    badge: "/icon.svg",
    requireInteraction: data.critical === true,
    data: data.url || "/",
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((cs) => {
    for (const c of cs) if ("focus" in c) return c.focus();
    return self.clients.openWindow(e.notification.data || "/");
  }));
});
