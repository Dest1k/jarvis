"use client";
/**
 * MonitorView.tsx — «Мониторная»: живые логи сервисов JARVIS в одной сетке.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

type Level = "info" | "ok" | "warn" | "err";
interface Line { line: string; level: Level }

const SERVICE_LABELS: Record<string, string> = {
  qwen: "🧠 Gemma Core (dispatcher)",
  uitars: "👁️ Vision route",
  audio: "🎙️ Audio (ASR+TTS)",
  backend: "⚙️ Backend (ядро+агент)",
  sandbox: "📦 Sandbox (исполнение кода)",
};
const DEFAULT_SERVICES = ["backend", "qwen", "audio", "sandbox"];
const MAX_LINES = 400;

function classify(line: string): Level {
  const l = line.toLowerCase();
  if (l.includes("error") || l.includes("traceback") || l.includes("ошиб") || l.includes("oom") || l.includes("fail")) return "err";
  if (l.includes("warn") || l.includes("пред")) return "warn";
  if (l.includes("ready") || l.includes("started") || l.includes("готов") || l.includes("running") || l.includes("uvicorn running")) return "ok";
  return "info";
}

export default function MonitorView() {
  const [services, setServices] = useState<string[]>(DEFAULT_SERVICES);
  const [logs, setLogs] = useState<Record<string, Line[]>>({});
  const [conn, setConn] = useState("connecting");
  const [paused, setPaused] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const sockRef = useRef<JarvisSocket | null>(null);
  const pausedRef = useRef(false);

  useEffect(() => { pausedRef.current = paused; }, [paused]);

  useEffect(() => {
    const sock = new JarvisSocket("/ws/deploy", {
      onState: setConn,
      onJson: (msg: JarvisMessage) => {
        if (msg.type === "hello" && Array.isArray(msg.services)) {
          setServices((msg.services as string[]).filter((s) => s !== "uitars"));
        } else if (msg.type === "log") {
          if (pausedRef.current) return;
          const svc = String(msg.service ?? "backend");
          const line = String(msg.line ?? "");
          setLogs((prev) => {
            const arr = prev[svc] ? [...prev[svc]] : [];
            arr.push({ line, level: classify(line) });
            if (arr.length > MAX_LINES) arr.splice(0, arr.length - MAX_LINES);
            return { ...prev, [svc]: arr };
          });
        }
      },
    });
    sock.connect();
    sockRef.current = sock;
    setTimeout(() => sock.sendJson({ type: "tail_all" }), 400);
    return () => sock.close();
  }, []);

  const clearAll = () => setLogs({});
  const shown = useMemo(() => (expanded ? [expanded] : services), [expanded, services]);

  return (
    <div style={{ display: "grid", gridTemplateRows: "auto 1fr", gap: 12, height: "100%", minHeight: 0 }}>
      <div className="panel monitor-bar">
        <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
        <strong>Мониторная</strong>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>живые логи · {services.length} сервис(ов)</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button className={`btn ${paused ? "danger" : ""}`} onClick={() => setPaused((v) => !v)}>{paused ? "▶ Возобновить" : "⏸ Пауза"}</button>
          <button className="btn" onClick={clearAll}>🧹 Очистить</button>
          {expanded && <button className="btn" onClick={() => setExpanded(null)}>⤢ Свернуть</button>}
        </div>
      </div>
      <div className={`monitor-grid ${expanded ? "single" : ""}`}>
        {shown.map((svc) => <LogPane key={svc} svc={svc} label={SERVICE_LABELS[svc] ?? svc} lines={logs[svc] ?? []} expanded={expanded === svc} onToggle={() => setExpanded(expanded === svc ? null : svc)} />)}
      </div>
    </div>
  );
}

function LogPane({ svc, label, lines, expanded, onToggle }: { svc: string; label: string; lines: Line[]; expanded: boolean; onToggle: () => void }) {
  const bottomRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => { bottomRef.current?.scrollIntoView(); }, [lines]);
  return <div className="panel log-pane"><div className="log-pane-head"><strong>{label}</strong><span style={{ color: "var(--muted)", fontSize: 11 }}>{lines.length}</span><button className="btn pane-btn" onClick={onToggle} title="Развернуть/свернуть">{expanded ? "⤡" : "⤢"}</button></div><div className="log-stream log-pane-body" data-svc={svc}>{lines.length === 0 && <span style={{ color: "var(--muted)" }}>ожидание логов…</span>}{lines.map((l, i) => <div key={i} className={`log-line ${l.level}`}>{l.line}</div>)}<div ref={bottomRef} /></div></div>;
}
