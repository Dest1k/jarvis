/**
 * Конфигурация Next.js 15 для дашборда JARVIS-OS.
 * Прокси REST на FastAPI-ядро (порт 8000) задаётся через переменную окружения
 * CORE_PROXY_URL (по умолчанию http://127.0.0.1:8000).
 *
 * ВАЖНО: цель прокси — именно 127.0.0.1, а НЕ localhost. На Windows Node 18+
 * резолвит `localhost` в IPv6 `::1`, тогда как Docker Desktop публикует порт
 * только на IPv4 `127.0.0.1`. Из-за этого server-side прокси Next.js валился
 * с ETIMEDOUT/ECONNRESET, хотя контейнер backend был жив.
 */
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Monaco тянет воркеры — отключаем строгую проверку для них на этапе сборки
  webpack: (config) => {
    config.resolve.fallback = { ...config.resolve.fallback, fs: false };
    return config;
  },
  async rewrites() {
    const core = process.env.CORE_PROXY_URL || "http://127.0.0.1:8000";
    return [{ source: "/api/core/:path*", destination: `${core}/:path*` }];
  },
};

export default nextConfig;
