/**
 * layout.tsx — корневой layout дашборда JARVIS-OS Command Center.
 * PWA: манифест, theme-color, регистрация service worker (офлайн-шелл + пуши).
 */
import type { Metadata, Viewport } from "next";
import "./globals.css";
import PWARegister from "@/components/PWARegister";

export const metadata: Metadata = {
  title: "JARVIS-OS · Command Center",
  description: "Командный центр самособирающейся локальной мультиагентной системы",
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "JARVIS" },
  icons: { icon: "/icon.svg", apple: "/icon.svg" },
};

export const viewport: Viewport = {
  themeColor: "#0b0f16",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body>
        {children}
        <PWARegister />
      </body>
    </html>
  );
}
