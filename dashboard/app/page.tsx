"use client";
/**
 * page.tsx — главная страница Command Center JARVIS-OS.
 *
 * Три вкладки (по итогам рефакторинга):
 *   🛠️  Пульт управления — системные/сервисные функции (без изменений по сути).
 *   💬  Чат              — универсальный Telegram-подобный чат с агентом
 *                          (текст + голос + память + шаги выполнения).
 *   🖥️  Мониторная       — живые логи всех сервисов в одной сетке.
 *
 * Глобальный HITL-гейт остаётся поверх всего: при деструктивной команде на хосте
 * всплывает окно подтверждения оператора.
 */
import { useState } from "react";
import ControlPanel from "@/components/ControlPanel";
import ChatView from "@/components/ChatView";
import MonitorView from "@/components/MonitorView";
import CognitiveView from "@/components/CognitiveView";
import HitlGate from "@/components/HitlGate";
import StatusBar from "@/components/StatusBar";

type View = "chat" | "control" | "monitor" | "cognitive";

const NAV: { id: View; label: string }[] = [
  { id: "chat", label: "💬 Чат" },
  { id: "cognitive", label: "🧠 Разум" },
  { id: "control", label: "🛠️ Пульт управления" },
  { id: "monitor", label: "🖥️ Мониторная" },
];

export default function Page() {
  const [view, setView] = useState<View>("chat");

  return (
    <div className="app-grid">
      <header className="topbar">
        <span className="brand">JARVIS-OS</span>
        <span style={{ color: "var(--muted)", fontSize: 13 }}>Command Center</span>
        <div style={{ marginLeft: "auto" }}>
          <StatusBar />
        </div>
      </header>

      <nav className="sidebar">
        {NAV.map((n) => (
          <button
            key={n.id}
            className={`nav-item ${view === n.id ? "active" : ""}`}
            onClick={() => setView(n.id)}
          >
            {n.label}
          </button>
        ))}
      </nav>

      {/* content: чат держим всегда смонтированным (live WS/аудио), прочие — по выбору */}
      <main className="content">
        <div style={{ display: view === "chat" ? "block" : "none", height: "100%" }}>
          <ChatView />
        </div>
        {view === "cognitive" && <CognitiveView />}
        {view === "control" && <ControlPanel />}
        {view === "monitor" && <MonitorView />}
      </main>

      <HitlGate />
    </div>
  );
}
