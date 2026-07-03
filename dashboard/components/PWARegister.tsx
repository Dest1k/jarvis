"use client";
/**
 * PWARegister.tsx — регистрация service worker и (по запросу) подписка на пуши.
 * Тихий компонент без UI: монтируется в layout, работает один раз при загрузке.
 * Голосовой канал (WS /ws/audio) и офлайн-шелл делают приложение мобильным
 * компаньоном; критические алерты/HITL приходят пушем через sw.js.
 */
import { useEffect } from "react";

export default function PWARegister() {
  useEffect(() => {
    if (typeof window === "undefined" || !("serviceWorker" in navigator)) return;
    const onLoad = () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* регистрация не критична: приложение работает и без PWA */
      });
    };
    if (document.readyState === "complete") onLoad();
    else window.addEventListener("load", onLoad);
    return () => window.removeEventListener("load", onLoad);
  }, []);
  return null;
}

/**
 * Помощник для включения пуш-уведомлений (вызывается по кнопке из UI, требует
 * жеста пользователя и VAPID-ключа сервера). Экспортируется для будущего
 * «Team & Health»-дашборда.
 */
export async function enablePushNotifications(vapidPublicKey: string): Promise<boolean> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return false;
  const reg = await navigator.serviceWorker.ready;
  await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: vapidPublicKey,
  });
  return true;
}
