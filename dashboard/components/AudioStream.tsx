"use client";
/**
 * AudioStream.tsx — Multimodal Audio Stream.
 * Асинхронный WebSocket-канал /ws/audio:
 *   • захват микрофона (Web Audio API), даунсэмплинг в PCM16 @ 16 кГц,
 *     отправка бинарных чанков (VAD выполняется на сервере, аудио-слой);
 *   • приём JSON-транскриптов (частичные/финальные);
 *   • приём бинарных TTS-чанков Kokoro и немедленное низколатентное
 *     воспроизведение через AudioContext.
 */
import { useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

const TARGET_SR = 16000;

export default function AudioStream() {
  const [listening, setListening] = useState(false);
  const [conn, setConn] = useState("connecting");
  const [transcript, setTranscript] = useState<string[]>([]);
  const [level, setLevel] = useState(0);
  const [ttsText, setTtsText] = useState("");

  const sockRef = useRef<JarvisSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const playCtxRef = useRef<AudioContext | null>(null);
  const playTimeRef = useRef<number>(0);

  useEffect(() => {
    const sock = new JarvisSocket("/ws/audio", {
      onState: setConn,
      onJson: (msg: JarvisMessage) => {
        if (msg.type === "final" || msg.type === "partial") {
          setTranscript((p) => [...p.slice(-30), String(msg.text ?? "")]);
        }
      },
      onBinary: (buf) => playTtsChunk(buf),
    });
    sock.connect();
    sockRef.current = sock;
    return () => {
      sock.close();
      stopMic();
    };
  }, []);

  // --- Захват микрофона и стрим PCM16 на сервер ---
  const startMic = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    streamRef.current = stream;
    const ctx = new AudioContext({ sampleRate: 48000 });
    ctxRef.current = ctx;
    const source = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    procRef.current = proc;

    proc.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      // VU-метр
      let sum = 0;
      for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
      setLevel(Math.min(1, Math.sqrt(sum / input.length) * 4));
      // Даунсэмплинг 48к → 16к и конвертация в PCM16
      const pcm16 = downsampleToPcm16(input, ctx.sampleRate, TARGET_SR);
      sockRef.current?.sendBinary(pcm16.buffer);
    };
    source.connect(proc);
    proc.connect(ctx.destination);
    setListening(true);
  };

  const stopMic = () => {
    procRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    ctxRef.current?.close().catch(() => {});
    setListening(false);
    setLevel(0);
    sockRef.current?.sendJson({ type: "end_utterance" });
  };

  // --- Низколатентное воспроизведение TTS-чанков ---
  const playTtsChunk = (buf: ArrayBuffer) => {
    if (!playCtxRef.current) {
      playCtxRef.current = new AudioContext({ sampleRate: TARGET_SR });
      playTimeRef.current = playCtxRef.current.currentTime;
    }
    const ctx = playCtxRef.current;
    // Пропускаем WAV-заголовок, если присутствует (RIFF)
    let pcm = new Int16Array(buf);
    const head = new Uint8Array(buf.slice(0, 4));
    if (head[0] === 0x52 && head[1] === 0x49) {
      pcm = new Int16Array(buf.slice(44));
    }
    if (pcm.length === 0) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const audioBuf = ctx.createBuffer(1, f32.length, TARGET_SR);
    audioBuf.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(ctx.destination);
    const start = Math.max(ctx.currentTime, playTimeRef.current);
    src.start(start);
    playTimeRef.current = start + audioBuf.duration;
  };

  const speak = () => {
    if (ttsText.trim()) sockRef.current?.sendJson({ type: "speak", text: ttsText });
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div className="panel" style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
        <strong>Мультимодальный аудио-канал (VAD + ASR + TTS)</strong>
        <button
          className={`btn ${listening ? "danger" : "primary"}`}
          style={{ marginLeft: "auto" }}
          onClick={() => (listening ? stopMic() : startMic())}
        >
          {listening ? "⏹ Остановить микрофон" : "🎙️ Говорить"}
        </button>
      </div>

      <div className="panel">
        <span style={{ fontSize: 12, color: "var(--muted)" }}>Уровень сигнала</span>
        <div className="vu-meter" style={{ marginTop: 6 }}>
          <div className="vu-fill" style={{ width: `${level * 100}%` }} />
        </div>
      </div>

      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>Распознанная речь</strong>
        <div className="log-stream" style={{ height: "30vh" }}>
          {transcript.length === 0 ? (
            <span style={{ color: "var(--muted)" }}>Нажмите «Говорить» и произнесите команду…</span>
          ) : (
            transcript.map((t, i) => <div key={i} className="log-line">▸ {t}</div>)
          )}
        </div>
      </div>

      <div className="panel" style={{ display: "flex", gap: 10 }}>
        <input
          value={ttsText}
          onChange={(e) => setTtsText(e.target.value)}
          placeholder="Текст для синтеза речи (Kokoro TTS)…"
          style={{
            flex: 1, padding: "10px 12px", borderRadius: 8,
            background: "#05080d", border: "1px solid var(--border)", color: "var(--text)",
          }}
          onKeyDown={(e) => e.key === "Enter" && speak()}
        />
        <button className="btn" onClick={speak}>🔊 Синтезировать</button>
      </div>
    </div>
  );
}

/** Даунсэмплинг Float32 → Int16 PCM с целевой частотой дискретизации. */
function downsampleToPcm16(input: Float32Array, fromSr: number, toSr: number): Int16Array {
  const ratio = fromSr / toSr;
  const outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const sample = input[Math.floor(i * ratio)];
    out[i] = Math.max(-32768, Math.min(32767, sample * 32768));
  }
  return out;
}
