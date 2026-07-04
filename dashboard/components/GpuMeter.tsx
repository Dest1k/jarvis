"use client";
/**
 * GpuMeter.tsx — всегда видимая телеметрия GPU/VRAM в топбаре Command Center.
 *
 * Обновление «в реальном времени»: подписка на SSE-поток ядра
 * (/api/core/api/gpu/stream) — сервер сам пушит кадры с каденцией семплера
 * (по умолчанию ~1.5 с). Если SSE недоступен (прокси/сеть) — мягкий откат на
 * опрос /api/core/api/gpu каждые 1.5 с. Реального напряга нет: backend отдаёт
 * кэш фонового семплера, а не дёргает nvidia-smi на каждый запрос.
 */
import { useEffect, useRef, useState } from "react";

interface Gpu {
  index: number;
  name: string;
  util: number | null;
  mem_used: number | null;
  mem_total: number | null;
  mem_pct: number | null;
  temp: number | null;
  power: number | null;
  power_limit: number | null;
}

interface GpuPayload {
  ok: boolean;
  gpus: Gpu[];
  error?: string;
  age_ms?: number | null;
  cadence_ms?: number;
}

const GPU_URL = "/api/core/api/gpu";
const GPU_STREAM_URL = "/api/core/api/gpu/stream";

/** Цвет заливки по загрузке: зелёный → янтарь → красный. */
function loadColor(pct: number | null): string {
  if (pct == null) return "var(--muted)";
  if (pct >= 90) return "var(--err)";
  if (pct >= 70) return "var(--warn)";
  return "var(--ok)";
}

/** МБ → «21.4» ГБ (одна цифра после точки). */
function gb(mb: number | null): string {
  return mb == null ? "—" : (mb / 1024).toFixed(1);
}

/** Короткое имя карты: «NVIDIA GeForce RTX 5090» → «RTX 5090». */
function shortName(name: string): string {
  const m = name.match(/(RTX|GTX|RX|A\d|H\d|L\d)\s?\w[\w\s]*$/i);
  return (m ? m[0] : name).replace(/NVIDIA\s+GeForce\s+/i, "").trim();
}

export default function GpuMeter() {
  const [data, setData] = useState<GpuPayload | null>(null);
  const [live, setLive] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let alive = true;

    const apply = (d: GpuPayload) => {
      if (alive) setData(d);
    };

    const startPolling = () => {
      if (pollRef.current) return;
      const tick = async () => {
        try {
          const r = await fetch(GPU_URL, { cache: "no-store" });
          apply((await r.json()) as GpuPayload);
          if (alive) setLive(true);
        } catch {
          if (alive) setLive(false);
        }
      };
      tick();
      pollRef.current = setInterval(tick, 1500);
    };

    // Предпочитаем SSE (push сервером); при ошибке — постоянный откат на опрос.
    if (typeof EventSource !== "undefined") {
      try {
        const es = new EventSource(GPU_STREAM_URL);
        esRef.current = es;
        es.onmessage = (e) => {
          try {
            apply(JSON.parse(e.data) as GpuPayload);
            if (alive) setLive(true);
          } catch {
            /* keep-alive/битый кадр — игнор */
          }
        };
        es.onerror = () => {
          if (alive) setLive(false);
          es.close();
          esRef.current = null;
          startPolling();
        };
      } catch {
        startPolling();
      }
    } else {
      startPolling();
    }

    return () => {
      alive = false;
      esRef.current?.close();
      esRef.current = null;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  const gpus = data?.gpus ?? [];

  if (!data || !data.ok || gpus.length === 0) {
    return (
      <div
        className="gpu-meter gpu-off"
        title={data?.error || "GPU-телеметрия недоступна (нет RPC-моста?)"}
      >
        <span className="gpu-ico">🎮</span>
        <span className="gpu-na">GPU —</span>
      </div>
    );
  }

  return (
    <div className="gpu-meter" title={live ? "Живая телеметрия GPU" : "GPU (опрос)"}>
      {gpus.map((g) => (
        <div className="gpu-card" key={g.index}>
          <span className="gpu-ico" title={g.name}>
            🎮
          </span>
          <span className="gpu-name">{shortName(g.name)}</span>
          <div className="gpu-metrics">
            <div className="gpu-row">
              <span className="gpu-lbl">GPU</span>
              <div className="gpu-bar">
                <div
                  className="gpu-fill"
                  style={{ width: `${g.util ?? 0}%`, background: loadColor(g.util) }}
                />
              </div>
              <span className="gpu-val">{g.util ?? "—"}%</span>
            </div>
            <div className="gpu-row">
              <span className="gpu-lbl">VRAM</span>
              <div className="gpu-bar">
                <div
                  className="gpu-fill"
                  style={{ width: `${g.mem_pct ?? 0}%`, background: loadColor(g.mem_pct) }}
                />
              </div>
              <span className="gpu-val">
                {gb(g.mem_used)}/{gb(g.mem_total)} ГБ
              </span>
            </div>
          </div>
          {g.temp != null && <span className="gpu-temp">{Math.round(g.temp)}°</span>}
          <span className={`gpu-live ${live ? "on" : "off"}`} title={live ? "live" : "poll"} />
        </div>
      ))}
    </div>
  );
}
