/**
 * Конфигурация Next.js 15 для дашборда JARVIS-OS.
 * Прокси WebSocket/REST на FastAPI-ядро (порт 8000) задаётся через
 * переменную окружения NEXT_PUBLIC_CORE_URL (по умолчанию http://localhost:8000).
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
    const core = process.env.CORE_PROXY_URL || "http://localhost:8000";
    return [{ source: "/api/core/:path*", destination: `${core}/:path*` }];
  },
};

export default nextConfig;
