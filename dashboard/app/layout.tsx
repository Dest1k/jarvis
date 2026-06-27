/**
 * layout.tsx — корневой layout дашборда JARVIS-OS Command Center.
 */
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "JARVIS-OS · Command Center",
  description: "Командный центр самособирающейся локальной мультиагентной системы",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
