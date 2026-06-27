"use client";
/**
 * HitlGate.tsx — Human-in-the-Loop гейт.
 * Слушает канал /ws/hitl. При получении hitl_request от RPC-моста (деструктивная
 * команда) показывает модальное окно и БЛОКИРУЕТ исполнение до визуального
 * подтверждения/отклонения оператором. Решение отправляется обратно в ядро,
 * которое транслирует его в windows_rpc_bridge.py.
 */
import { useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

interface HitlRequest {
  approval_id: string;
  action: string;
  summary: string;
  created_at?: number;
}

export default function HitlGate() {
  const [queue, setQueue] = useState<HitlRequest[]>([]);
  const sockRef = useRef<JarvisSocket | null>(null);

  useEffect(() => {
    const sock = new JarvisSocket("/ws/hitl", {
      onJson: (msg: JarvisMessage) => {
        if (msg.type === "hitl_request") {
          setQueue((q) => [
            ...q,
            {
              approval_id: String(msg.approval_id),
              action: String(msg.action),
              summary: String(msg.summary),
              created_at: Number(msg.created_at),
            },
          ]);
        }
      },
    });
    sock.connect();
    sockRef.current = sock;
    return () => sock.close();
  }, []);

  const decide = (approval_id: string, approved: boolean) => {
    sockRef.current?.sendJson({ type: "hitl_decision", approval_id, approved });
    setQueue((q) => q.filter((r) => r.approval_id !== approval_id));
  };

  if (queue.length === 0) return null;
  const current = queue[0];

  return (
    <div className="hitl-modal">
      <div className="hitl-card">
        <h3 style={{ marginTop: 0, color: "var(--err)" }}>
          ⚠ Требуется подтверждение оператора
        </h3>
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          Агент запросил выполнение потенциально деструктивной операции.
          Исполнение приостановлено до вашего решения.
        </p>
        <div
          style={{
            background: "#05080d", border: "1px solid var(--border)",
            borderRadius: 8, padding: 12, margin: "12px 0",
            fontFamily: "monospace", fontSize: 13, wordBreak: "break-all",
          }}
        >
          <div style={{ color: "var(--accent)" }}>action: {current.action}</div>
          <div>{current.summary}</div>
        </div>
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
          <button className="btn danger" onClick={() => decide(current.approval_id, false)}>
            ✕ Отклонить
          </button>
          <button className="btn primary" onClick={() => decide(current.approval_id, true)}>
            ✓ Подтвердить выполнение
          </button>
        </div>
        {queue.length > 1 && (
          <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 10 }}>
            В очереди ещё {queue.length - 1} запрос(ов).
          </p>
        )}
      </div>
    </div>
  );
}
