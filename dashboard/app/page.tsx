"use client";
/**
 * page.tsx — главная страница Command Center.
 * Переключает четыре основных представления и держит глобальный HITL-гейт.
 */
import { useState } from "react";
import DeploymentView from "@/components/DeploymentView";
import DesktopViewer from "@/components/DesktopViewer";
import CodeStudio from "@/components/CodeStudio";
import AudioStream from "@/components/AudioStream";
import ControlPanel from "@/components/ControlPanel";
import HitlGate from "@/components/HitlGate";
import StatusBar from "@/components/StatusBar";

type View = "control" | "deploy" | "desktop" | "code" | "audio";

const NAV: { id: View; label: string }[] = [
  { id: "control", label: "🛠️ Пульт управления" },
  { id: "deploy", label: "🚀 Развёртывание" },
  { id: "desktop", label: "🖥️ Виртуальный десктоп" },
  { id: "code", label: "🧩 Code Studio" },
  { id: "audio", label: "🎙️ Аудио-поток" },
];

export default function Page() {
  const [view, setView] = useState<View>("control");

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

      <main className="content">
        {view === "control" && <ControlPanel />}
        {view === "deploy" && <DeploymentView />}
        {view === "desktop" && <DesktopViewer />}
        {view === "code" && <CodeStudio />}
        {view === "audio" && <AudioStream />}
      </main>

      {/* Глобальный HITL-гейт: всплывает при деструктивных командах */}
      <HitlGate />
    </div>
  );
}
