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
        <span className="presence-text">На связи · командное ядро Gemma 4</span>
        <span className="pill ok" title="Автономный контур виден во вкладке «Операции»">автономия под наблюдением</span>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 14, minWidth: 0 }}>
          <CommandPalette onNavigate={nav} />
          <GpuMeter />
          <span className="topbar-sep" />
          <StatusBar />
        </div>
      </header>

      <nav className="sidebar glass-sidebar">
        <div className="side-orb"><div className="jarvis-orb" /><span>Вечер добрый, сэр.</span></div>
        {NAV.map((n) => (
          <button key={n.id} className={`nav-item ${view === n.id ? "active" : ""}`} onClick={() => setView(n.id)}>
            <span>{n.label}</span><small>{n.sub}</small>
          </button>
        ))}
        <div className="advisor-mini"><strong>Советник</strong><span>Для больших целей: «оформи как план миссии».</span></div>
      </nav>

      <main className="content deck-content">
        <div className={`view-host ${view === "chat" ? "active" : ""}`}><ChatView /></div>
        {view === "ops" && <AgentOpsView />}
        {view === "cognitive" && <CognitiveView />}
        {view === "control" && <ControlPanelGemma />}
        {view === "monitor" && <MonitorView />}
      </main>

      <HitlGate />
    </div>
  );
}
