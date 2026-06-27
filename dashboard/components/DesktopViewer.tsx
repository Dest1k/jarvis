"use client";
/**
 * DesktopViewer.tsx — Real-time OS Streaming.
 * Canvas-вьюер изолированного виртуального десктопа (Xvfb), где UI-TARS
 * двигает курсор, печатает и навигирует приложения. Кадры приходят как
 * бинарные JPEG-фреймы по каналу /ws/desktop. Поддерживается проброс
 * ввода оператора (мышь/клавиатура) обратно в виртуальный десктоп.
 */
import { useEffect, useRef, useState } from "react";
import { JarvisSocket } from "@/lib/ws";

export default function DesktopViewer() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const sockRef = useRef<JarvisSocket | null>(null);
  const [fps, setFps] = useState(10);
  const [conn, setConn] = useState("connecting");
  const [interactive, setInteractive] = useState(false);

  useEffect(() => {
    const sock = new JarvisSocket("/ws/desktop", {
      onState: setConn,
      onBinary: (buf) => drawFrame(buf),
    });
    sock.connect();
    sockRef.current = sock;
    return () => sock.close();
  }, []);

  const drawFrame = (buf: ArrayBuffer) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const blob = new Blob([buf], { type: "image/jpeg" });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      const ctx = canvas.getContext("2d");
      if (ctx) {
        canvas.width = img.width;
        canvas.height = img.height;
        ctx.drawImage(img, 0, 0);
      }
      URL.revokeObjectURL(url);
    };
    img.src = url;
  };

  const setStreamFps = (v: number) => {
    setFps(v);
    sockRef.current?.sendJson({ type: "set_fps", fps: v });
  };

  // Проброс ввода оператора в виртуальный десктоп
  const toVirtualCoords = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const x = Math.round(((e.clientX - rect.left) / rect.width) * canvas.width);
    const y = Math.round(((e.clientY - rect.top) / rect.height) * canvas.height);
    return { x, y };
  };

  const onMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    const { x, y } = toVirtualCoords(e);
    sockRef.current?.sendJson({ type: "input", input_kind: "move", x, y });
  };
  const onClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    sockRef.current?.sendJson({ type: "input", input_kind: "click", button: 1 });
  };
  const onKey = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    e.preventDefault();
    sockRef.current?.sendJson({ type: "input", input_kind: "key", key: e.key });
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div className="panel" style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
        <strong>Виртуальный десктоп (UI-TARS)</strong>
        <label style={{ marginLeft: 16, fontSize: 13, color: "var(--muted)" }}>
          FPS: {fps}
          <input
            type="range" min={1} max={30} value={fps}
            onChange={(e) => setStreamFps(Number(e.target.value))}
            style={{ marginLeft: 8, verticalAlign: "middle" }}
          />
        </label>
        <button
          className={`btn ${interactive ? "danger" : ""}`}
          style={{ marginLeft: "auto" }}
          onClick={() => setInteractive((v) => !v)}
        >
          {interactive ? "⏸ Перехват ввода ВКЛ" : "🎮 Взять управление"}
        </button>
      </div>

      <div className="panel">
        <canvas
          ref={canvasRef}
          className="canvas-stage"
          tabIndex={0}
          onMouseMove={onMove}
          onClick={onClick}
          onKeyDown={onKey}
          style={{ cursor: interactive ? "crosshair" : "default" }}
        />
        <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
          Изолированный X11-дисплей (:99). Агент UI-TARS управляет автономно;
          включите «Взять управление», чтобы перехватить мышь и клавиатуру.
        </p>
      </div>
    </div>
  );
}
