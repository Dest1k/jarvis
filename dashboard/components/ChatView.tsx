"use client";
/**
 * ChatView.tsx — универсальный чат с агентом JARVIS (Telegram-подобный).
 *
 * Возможности:
 *   • Лента сообщений «пузырями»: пользователь справа, JARVIS слева.
 *   • Потоковый ответ (живая «печать» токенов) по каналу /ws/chat.
 *   • Прозрачные ШАГИ агента: мысли, вызовы инструментов и их результаты
 *     показываются компактной раскрывающейся вставкой в пузыре ответа.
 *   • Голосовой ввод (кнопка 🎤): микрофон → /ws/audio (Whisper ASR) →
 *     распознанный текст попадает в поле ввода.
 *   • Озвучка ответов (кнопка 🔊): финальный ответ → Kokoro TTS → воспроизведение.
 *   • Память: панель управления оперативным контекстом и долговременной памятью.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

const TARGET_SR = 16000;
const API = "/api/core/api/agent";

type StepKind = "thought" | "tool_call" | "tool_result";
interface Step {
  kind: StepKind;
  text: string;
  tool?: string;
  ok?: boolean;
}
interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  replyTo?: string;
  steps: Step[];
  streaming?: boolean;
  error?: boolean;
}

interface MemoryItem { id: string; kind: string; text: string; tags: string[] }
interface MemoryOverview {
  summary: string;
  recent_count: number;
  longterm: MemoryItem[];
  longterm_count: number;
}

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

export default function ChatView() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [conn, setConn] = useState("connecting");
  const [listening, setListening] = useState(false);
  const [speak, setSpeak] = useState(false);
  const [level, setLevel] = useState(0);
  const [memOpen, setMemOpen] = useState(false);
  const [mem, setMem] = useState<MemoryOverview | null>(null);

  const chatRef = useRef<JarvisSocket | null>(null);
  const audioRef = useRef<JarvisSocket | null>(null);
  const speakRef = useRef(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // mic
  const micCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micProcRef = useRef<ScriptProcessorNode | null>(null);
  // tts playback
  const playCtxRef = useRef<AudioContext | null>(null);
  const playTimeRef = useRef(0);

  useEffect(() => { speakRef.current = speak; }, [speak]);

  // --- единое обновление сообщения по replyTo-id ---
  const upsertAssistant = useCallback(
    (id: string, mutate: (m: ChatMessage) => void) => {
      setMessages((prev) => {
        const next = [...prev];
        let idx = next.findIndex((m) => m.role === "assistant" && m.replyTo === id);
        if (idx === -1) {
          next.push({ id: uid(), role: "assistant", text: "", replyTo: id, steps: [], streaming: true });
          idx = next.length - 1;
        }
        const copy = { ...next[idx], steps: [...next[idx].steps] };
        mutate(copy);
        next[idx] = copy;
        return next;
      });
    },
    [],
  );

  // --- WebSocket чата ---
  useEffect(() => {
    const sock = new JarvisSocket("/ws/chat", {
      onState: setConn,
      onJson: (msg: JarvisMessage) => {
        const id = String(msg.id ?? "");
        switch (msg.type) {
          case "thought":
            if (id) upsertAssistant(id, (m) => m.steps.push({ kind: "thought", text: String(msg.text ?? "") }));
            break;
          case "tool_call":
            if (id) upsertAssistant(id, (m) =>
              m.steps.push({ kind: "tool_call", tool: String(msg.tool ?? ""),
                text: JSON.stringify(msg.args ?? {}) }));
            break;
          case "tool_result":
            if (id) upsertAssistant(id, (m) =>
              m.steps.push({ kind: "tool_result", tool: String(msg.tool ?? ""),
                ok: Boolean(msg.ok), text: String(msg.summary ?? "") }));
            break;
          case "assistant_start":
            if (id) upsertAssistant(id, (m) => { m.streaming = true; });
            break;
          case "token":
            if (id) upsertAssistant(id, (m) => { m.text += String(msg.content ?? ""); });
            break;
          case "assistant_done":
            if (id) upsertAssistant(id, (m) => { m.streaming = false; m.text = String(msg.content ?? m.text); });
            if (speakRef.current && msg.content) audioRef.current?.sendJson({ type: "speak", text: String(msg.content) });
            break;
          case "error":
            if (id) upsertAssistant(id, (m) => { m.streaming = false; m.error = true; m.text += `\n⚠ ${String(msg.error ?? "")}`; });
            break;
          case "memory":
            // системное уведомление о памяти — лёгкий тост в ленте
            setMessages((p) => [...p, { id: uid(), role: "assistant", text: `🧠 ${String(msg.text ?? "")}`, steps: [] }]);
            break;
          default:
            break;
        }
      },
    });
    sock.connect();
    chatRef.current = sock;

    // аудио-сокет для ASR/TTS (готов заранее)
    const asock = new JarvisSocket("/ws/audio", {
      onJson: (msg: JarvisMessage) => {
        if (msg.type === "final" || msg.type === "partial") {
          const t = String(msg.text ?? "").trim();
          if (t) setInput((prev) => (prev ? prev + " " + t : t));
        }
      },
      onBinary: (buf) => playTtsChunk(buf),
    });
    asock.connect();
    audioRef.current = asock;

    return () => { sock.close(); asock.close(); stopMic(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upsertAssistant]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // --- отправка сообщения ---
  const send = () => {
    const text = input.trim();
    if (!text) return;
    const id = uid();
    setMessages((p) => [...p, { id, role: "user", text, steps: [] }]);
    chatRef.current?.sendJson({ type: "user_message", text, id });
    setInput("");
  };

  // --- микрофон → PCM16 → /ws/audio ---
  const startMic = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      micStreamRef.current = stream;
      const ctx = new AudioContext({ sampleRate: 48000 });
      micCtxRef.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      micProcRef.current = proc;
      proc.onaudioprocess = (e) => {
        const inp = e.inputBuffer.getChannelData(0);
        let sum = 0;
        for (let i = 0; i < inp.length; i++) sum += inp[i] * inp[i];
        setLevel(Math.min(1, Math.sqrt(sum / inp.length) * 4));
        const pcm = downsampleToPcm16(inp, ctx.sampleRate, TARGET_SR);
        audioRef.current?.sendBinary(pcm.buffer as ArrayBuffer);
      };
      source.connect(proc);
      proc.connect(ctx.destination);
      setListening(true);
    } catch {
      setListening(false);
    }
  };

  const stopMic = () => {
    micProcRef.current?.disconnect();
    micStreamRef.current?.getTracks().forEach((t) => t.stop());
    micCtxRef.current?.close().catch(() => {});
    micProcRef.current = null;
    micStreamRef.current = null;
    setListening(false);
    setLevel(0);
    audioRef.current?.sendJson({ type: "end_utterance" });
  };

  const playTtsChunk = (buf: ArrayBuffer) => {
    if (!playCtxRef.current) {
      playCtxRef.current = new AudioContext({ sampleRate: TARGET_SR });
      playTimeRef.current = playCtxRef.current.currentTime;
    }
    const ctx = playCtxRef.current;
    let pcm = new Int16Array(buf);
    const head = new Uint8Array(buf.slice(0, 4));
    if (head[0] === 0x52 && head[1] === 0x49) pcm = new Int16Array(buf.slice(44)); // RIFF
    if (pcm.length === 0) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const ab = ctx.createBuffer(1, f32.length, TARGET_SR);
    ab.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = ab;
    src.connect(ctx.destination);
    const start = Math.max(ctx.currentTime, playTimeRef.current);
    src.start(start);
    playTimeRef.current = start + ab.duration;
  };

  // --- память ---
  const loadMem = async () => {
    try {
      const r = await fetch(`${API}/memory`, { cache: "no-store" });
      setMem(await r.json());
    } catch { setMem(null); }
  };
  const memAction = async (action: string, extra: Record<string, unknown> = {}) => {
    await fetch(`${API}/memory`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...extra }),
    });
    loadMem();
  };
  useEffect(() => { if (memOpen) loadMem(); }, [memOpen]);

  return (
    <div className="chat-wrap">
      <div className="chat-head panel">
        <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
        <strong>JARVIS</strong>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          универсальный ассистент · голос, код, веб, управление ПК
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button className={`btn ${speak ? "primary" : ""}`} onClick={() => setSpeak((v) => !v)}
                  title="Озвучивать ответы (TTS)">🔊</button>
          <button className="btn" onClick={() => setMemOpen(true)} title="Память">🧠 Память</button>
        </div>
      </div>

      <div className="chat-feed">
        {messages.length === 0 && (
          <div className="chat-hello">
            <p>Привет! Я JARVIS. Спросите что угодно:</p>
            <ul>
              <li>«Какая погода завтра в Москве?»</li>
              <li>«Напиши hello world на C++ и запусти»</li>
              <li>«Спарси заголовки с example.com»</li>
              <li>«Открой Блокнот» / «Поставь паузу в плеере»</li>
            </ul>
            <p style={{ color: "var(--muted)" }}>Можно голосом — нажмите 🎤.</p>
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} m={m} />
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input panel">
        <button
          className={`btn mic ${listening ? "danger" : ""}`}
          onClick={() => (listening ? stopMic() : startMic())}
          title="Голосовой ввод"
        >
          {listening ? "⏺" : "🎤"}
        </button>
        {listening && (
          <div className="vu-meter mic-vu"><div className="vu-fill" style={{ width: `${level * 100}%` }} /></div>
        )}
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Сообщение JARVIS…"
          rows={1}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          }}
        />
        <button className="btn primary send" onClick={send}>➤</button>
      </div>

      {memOpen && (
        <MemoryPanel mem={mem} onClose={() => setMemOpen(false)} onAction={memAction} />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
function MessageBubble({ m }: { m: ChatMessage }) {
  const [open, setOpen] = useState(false);
  const isUser = m.role === "user";
  return (
    <div className={`msg-row ${isUser ? "user" : "assistant"}`}>
      <div className={`bubble ${isUser ? "user" : "assistant"} ${m.error ? "error" : ""}`}>
        {!isUser && m.steps.length > 0 && (
          <div className="steps">
            <button className="steps-toggle" onClick={() => setOpen((v) => !v)}>
              {open ? "▾" : "▸"} JARVIS работает ({m.steps.length})
            </button>
            {open && (
              <div className="steps-body">
                {m.steps.map((s, i) => (
                  <div key={i} className={`step ${s.kind}`}>
                    {s.kind === "thought" && <span>💭 {s.text}</span>}
                    {s.kind === "tool_call" && <span>🔧 <code>{s.tool}</code> {s.text}</span>}
                    {s.kind === "tool_result" && (
                      <span>{s.ok ? "✅" : "⚠️"} <code>{s.tool}</code>: {s.text}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="bubble-text">{renderContent(m.text)}</div>
        {m.streaming && <span className="caret">▋</span>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
function MemoryPanel({
  mem, onClose, onAction,
}: {
  mem: MemoryOverview | null;
  onClose: () => void;
  onAction: (a: string, extra?: Record<string, unknown>) => void;
}) {
  const [note, setNote] = useState("");
  return (
    <div className="hitl-modal" onClick={onClose}>
      <div className="hitl-card mem-card" onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0 }}>🧠 Память JARVIS</h3>
        <div className="mem-section">
          <strong>Оперативный контекст</strong>
          <p style={{ color: "var(--muted)", fontSize: 13, margin: "4px 0" }}>
            Реплик в активном окне: {mem?.recent_count ?? "—"}
          </p>
          {mem?.summary && (
            <pre className="log-stream" style={{ height: "12vh" }}>{mem.summary}</pre>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={() => onAction("flush")}>📥 Сжать в сводку</button>
            <button className="btn danger" onClick={() => onAction("reset")}>🧹 Очистить контекст</button>
          </div>
        </div>

        <div className="mem-section">
          <strong>Долговременная память ({mem?.longterm_count ?? 0})</strong>
          <div className="log-stream" style={{ height: "22vh", marginTop: 6 }}>
            {(mem?.longterm ?? []).length === 0 && <span style={{ color: "var(--muted)" }}>пусто</span>}
            {(mem?.longterm ?? []).map((it) => (
              <div key={it.id} className="log-line">
                <span style={{ color: "var(--accent)" }}>[{it.kind}]</span> {it.text}
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <input value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder="Запомнить факт…" style={{ flex: 1 }} />
            <button className="btn" onClick={() => { if (note.trim()) { onAction("save", { text: note.trim() }); setNote(""); } }}>
              ＋ Запомнить
            </button>
            <button className="btn danger" onClick={() => onAction("clear_longterm")}>Очистить всё</button>
          </div>
        </div>

        <div style={{ textAlign: "right", marginTop: 10 }}>
          <button className="btn" onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
/** Минимальный рендер контента: текст + markdown-блоки кода (без внешних зависимостей). */
function renderContent(text: string) {
  if (!text) return null;
  const parts = text.split(/```/);
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n");
      const lang = nl > -1 ? part.slice(0, nl).trim() : "";
      const code = nl > -1 ? part.slice(nl + 1) : part;
      return (
        <pre key={i} className="code-block">
          {lang && <span className="code-lang">{lang}</span>}
          <code>{code}</code>
        </pre>
      );
    }
    return <span key={i} style={{ whiteSpace: "pre-wrap" }}>{part}</span>;
  });
}

/** Даунсэмплинг Float32 → Int16 PCM с целевой частотой дискретизации. */
function downsampleToPcm16(input: Float32Array, fromSr: number, toSr: number): Int16Array {
  const ratio = fromSr / toSr;
  const outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const s = input[Math.floor(i * ratio)];
    out[i] = Math.max(-32768, Math.min(32767, s * 32768));
  }
  return out;
}
