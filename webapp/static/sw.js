/* Service Worker — 鰹鮪 ECDF-HSI 漁場預測系統 PWA
 * 策略：
 *  - 殼層資源（HTML/CSS/JS/圖示/Plotly CDN）：cache-first + 背景更新
 *  - /api/ 一律 network-only（衛星資料與運算結果不可快取舊值）
 *  - 離線時導覽請求回退到快取的首頁殼層
 */
const CACHE = "fri-hsi-pwa-v1";

const SHELL = [
  "/",
  "/static/css/style.css",
  "/static/js/app.js",
  "/static/img/fri_logo.jpg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.webmanifest",
  "https://cdn.plot.ly/plotly-2.35.2.min.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.allSettled(SHELL.map((url) => cache.add(url)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API 與非 GET 請求：純網路，不快取
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) {
    return; // 交給瀏覽器預設處理
  }

  // 導覽請求：網路優先，離線時回退殼層
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put("/", copy));
          return resp;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // 靜態資源：cache-first，背景更新
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetching = fetch(event.request)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(event.request, copy));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || fetching;
    })
  );
});
