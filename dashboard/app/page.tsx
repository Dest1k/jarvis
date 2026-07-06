"use client";
/**
 * page.tsx — Command Center JARVIS-OS.
 */
import { useState } from "react";
import ControlPanelGemma from "@/components/ControlPanelGemma";
import ChatView from "@/components/ChatView";
import MonitorView from "@/components/MonitorView";
import CognitiveView from "@/components/CognitiveView";
import AgentOpsView from "@/components/AgentOpsView";
import CommandPalette from "@/components/CommandPalette";
import HitlGate from "@/components/HitlGate";
import StatusBar from "@/components/StatusBar";
import GpuMeter from "@/components/GpuMeter";

type View = "chat" | "control" | "monitor" | "cognitive" | "ops";

const NAV: { id: View; label: string; sub: string }[] = [
  { id: "chat", label: "💬 Чат", sub: "диалог" },
  { id: "ops", label: "🧭 Операции", sub: "автономия" },
  { id: "cognitive", label: "🧠 Разум", sub: "память" },
  { id: "control", label: "🛠️ Пульт", sub: "стек" },
  { id: "monitor", label: "🖥️ Мониторная", sub: "логи" },
];

export default function Page() {
  const [view, setView] = useState<View>("chat");
  const nav = (v: string) => setView(v as View);

  return (
    <div className="app-grid command-deck">
      <div className="aurora aurora-a" />
      <div className="aurora aurora-b" />
      <header className="topbar glass-topbar">
        <div className="presence-core"><span className="presence-ring" /><span className="brand">JARVIS</span></div>
        <span className="presence-text">Online · Gemma 4 Command Core</span>
        <span className="pill ok" title="Autonomy runtime is observable from the Operations tab">autonomy visible</span>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 14 }}>
          <CommandPalette onNavigate={nav} />
          <GpuMeter />
          <span className="topbar-sep" />
          <StatusBar />
        </div>
      </header>

      <nav className="sidebar glass-sidebar">
        <div className="side-orb"><div className="jarvis-orb" /><span>Good evening, sir.</span></div>
        {NAV.map((n) => (
          <button key={n.id} className={`nav-item ${view === n.id ? "active" : ""}`} onClick={() => setView(n.id)}>
            <span>{n.label}</span><small>{n.sub}</small>
          </button>
        ))}
        <div className="advisor-mini"><strong>Advisor</strong><span>Для больших целей: “оформи как mission plan”.</span></div>
      </nav>

      <main className="content deck-content">
        <div style={{ display: view === "chat" ? "block" : "none", height: "100%" }}><ChatView /></div>
        {view === "ops" && <AgentOpsView />}
        {view === "cognitive" && <CognitiveView />}
        {view === "control" && <ControlPanelGemma />}
        {view === "monitor" && <MonitorView />}
      </main>

      <HitlGate />
    </div>
  );
}
