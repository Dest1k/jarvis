"use client";
/**
 * page.tsx — главная страница Command Center JARVIS-OS.
 *
 * Вкладки:
 *   💬 Чат        — диалог с Core JARVIS.
 *   🧠 Разум      — cognitive/RAG/knowledge graph.
 *   🧭 Операции   — статус автономного runtime, MCP, GPU, инцидентов и навыков.
 *   🛠️ Пульт      — сервисы, профили, модели, очистка.
 *   🖥️ Мониторная — живые логи.
 */
import { useState } from "react";
import ControlPanel from "@/components/ControlPanel";
import ChatView from "@/components/ChatView";
import MonitorView from "@/components/MonitorView";
import CognitiveView from "@/components/CognitiveView";
import AgentOpsView from "@/components/AgentOpsView";
import HitlGate from "@/components/HitlGate";
import StatusBar from "@/components/StatusBar";
import GpuMeter from "@/components/GpuMeter";

type View = "chat" | "control" | "monitor" | "cognitive" | "ops";

const NAV: { id: View; label: string }[] = [
  { id: "chat", label: "💬 Чат" },
  { id: "cognitive", label: "🧠 Разум" },
  { id: "ops", label: "🧭 Операции" },
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
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 18 }}>
          <GpuMeter />
          <span className="topbar-sep" />
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

      <main className="content">
        <div style={{ display: view === "chat" ? "block" : "none", height: "100%" }}>
          <ChatView />
        </div>
        {view === "cognitive" && <CognitiveView />}
        {view === "ops" && <AgentOpsView />}
        {view === "control" && <ControlPanel />}
        {view === "monitor" && <MonitorView />}
      </main>

      <HitlGate />
    </div>
  );
}
