"use client";
/**
 * StatusBar.tsx — компактный индикатор состояния подсистем.
 * Legacy service keys сохраняются как backend compatibility, но UX показывает Gemma-only терминологию.
 */
import { useEffect, useState } from "react";

interface SysStatus {
  core?: boolean;
  qwen_coder?: boolean;
  ui_tars?: boolean;
  audio?: boolean;
  rpc_bridge?: boolean;
}

const LABELS: Record<keyof SysStatus, string> = {
  core: "Ядро",
  qwen_coder: "Gemma Core",
  ui_tars: "Vision route",
  audio: "Аудио",
  rpc_bridge: "RPC-мост",
};

export default function StatusBar() {
  const [status, setStatus] = useState<SysStatus>({});

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const r = await fetch("/api/core/status", { cache: "no-store" });
        const data = (await r.json()) as SysStatus;
        if (alive) setStatus(data);
      } catch {
        if (alive) setStatus({});
      }
    };
    poll();
    const t = setInterval(poll, 4000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  return (
    <div style={{ display: "flex", gap: 14, fontSize: 12 }}>
      {(Object.keys(LABELS) as (keyof SysStatus)[]).map((k) => (
        <span key={k} title={LABELS[k]}>
          <span className={`status-dot ${status[k] ? "ok" : "err"}`} />
          {LABELS[k]}
        </span>
      ))}
    </div>
  );
}
