"use client";
/**
 * DeploymentView.tsx — Dynamic Deployment View.
 * Потоковая визуализация прогресса установки, загрузки слоёв Docker
 * и сырых логов vLLM в реальном времени (канал /ws/deploy).
 */
import { useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

interface LogLine {
  service: string;
  line: string;
  level: "info" | "ok" | "warn" | "err";
}

const SERVICES = [
  "vllm-qwen-coder",
  "vllm-ui-tars",
  "audio-layer",
  "backend",
  "sandbox",
];

function classify(line: string): LogLine["level"] {
  const l = line.toLowerCase();
  if (l.includes("error") || l.includes("traceback") || l.includes("ошиб")) return "err";
  if (l.includes("warn") || l.includes("пред")) return "warn";
  if (l.includes("ready") || l.includes("started") || l.includes("готов") || l.includes("ок")) return "ok";
  return "info";
}

export default function DeploymentView() {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [stage, setStage] = useState<string>("Ожидание…");
  const [conn, setConn] = useState("connecting");
  const sockRef = useRef<JarvisSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const sock = new JarvisSocket("/ws/deploy", {
      onState: setConn,
      onJson: (msg: JarvisMessage) => {
        if (msg.type === "log") {
          const line = String(msg.line ?? "");
          setLogs((prev) => [
            ...prev.slice(-500),
            { service: String(msg.service ?? "-"), line, level: classify(line) },
          ]);
        } else if (msg.type === "status") {
          setStage(String(msg.message ?? ""));
        }
      },
    });
    sock.connect();
    sockRef.current = sock;
    // По умолчанию подписываемся на логи диспетчера vLLM
    setTimeout(() => sock.sendJson({ type: "tail_logs", service: "vllm-qwen-coder" }), 500);
    return () => sock.close();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const tail = (service: string) =>
    sockRef.current?.sendJson({ type: "tail_logs", service });

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
          <strong>Этап развёртывания:</strong> <span>{stage}</span>
        </div>
        <div style={{ marginTop: 12, display: "flex", gap: 8, flexWrap: "wrap" }}>
          {SERVICES.map((s) => (
            <button key={s} className="btn" onClick={() => tail(s)}>
              📜 {s}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>
          Сырой поток логов (vLLM / Docker)
        </strong>
        <div className="log-stream">
          {logs.length === 0 && (
            <div style={{ color: "var(--muted)" }}>
              Ожидание событий развёртывания… Запустите bootstrap_installer.py.
            </div>
          )}
          {logs.map((l, i) => (
            <div key={i} className={`log-line ${l.level}`}>
              <span style={{ color: "var(--muted)" }}>[{l.service}]</span> {l.line}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}
