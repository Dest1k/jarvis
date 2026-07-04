"use client";
/**
 * ChatView.tsx — универсальный чат с агентом JARVIS (Telegram-подобный),
 * с НЕСКОЛЬКИМИ ВКЛАДКАМИ-диалогами. Каждая вкладка — отдельная сессия
 * (session_id) со своей оперативной памятью на сервере и авто-сжатием
 * контекста при наборе критической массы.
 *
 * Возможности: лента «пузырями», потоковый ответ, прозрачные шаги агента,
 * голосовой ввод (Whisper ASR), озвучка ответов (Kokoro TTS), панель памяти,
 * аварийная остановка, создание/переключение/закрытие вкладок (localStorage).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

const TARGET_SR = 16000;
const API = "/api/core/api/agent";
const LS_TABS = "jarvis_tabs";
const LS_CHATS = "jarvis_chats";

// Кто сейчас работает над задачей (для визуализации «какая модель трудится»).
const ACTORS: Record<string, { icon: string; name: string }> = {
  dispatcher: { icon: "🧠", name: "Диспетчер" },
  "ui-tars": { icon: "👁️", name: "UI-TARS" },
  sandbox: { icon: "📦", name: "Sandbox" },
  host: { icon: "🪟", name: "Хост" },
  web: { icon: "🌐", name: "Веб" },
  memory: { icon: "💾", name: "Память" },
  local: { icon: "⚡", name: "Локально" },
  mcp: { icon: "🧩", name: "MCP" },
};
const actorInfo = (a?: string) => ACTORS[a || ""] || { icon: "•", name: a || "" };

type StepKind = "thought" | "tool_call" | "tool_result";
interface Step { kind: StepKind; text: string; tool?: string; ok?: boolean; actor?: string }
interface ChatMessage {
  id: string; role: "user" | "assistant"; text: string;
  replyTo?: string; steps: Step[]; streaming?: boolean; error?: boolean;
}
interface Tab { id: string; title: string }
interface Attach {
  id: string; name: string; size: number;
  status: "uploading" | "ready" | "failed";
  percent: number; fileId?: string; chunks?: number; error?: string;
}
interface MemoryItem { id: string; kind: string; text: string; tags: string[] }
interface MemoryOverview {
  summary: string; recent_count: number; longterm: MemoryItem[]; longterm_count: number;
}

function uid(): string { return Math.random().toString(36).slice(2, 10); }

function loadTabs(): { tabs: Tab[]; chats: Record<string, ChatMessage[]> } {
  if (typeof window === "undefined") return { tabs: [], chats: {} };
  try {
    const tabs = JSON.parse(localStorage.getItem(LS_TABS) || "[]") as Tab[];
    const chats = JSON.parse(localStorage.getItem(LS_CHATS) || "{}") as Record<string, ChatMessage[]>;
    if (tabs.length) return { tabs, chats };
  } catch { /* ignore */ }
  const id = "default";
  return { tabs: [{ id, title: "Чат 1" }], chats: { [id]: [] } };
}

export default function ChatView() {
  // ВАЖНО: первый рендер — детерминированный дефолт (совпадает с серверным
  // HTML). Сохранённые вкладки/чаты из localStorage подгружаем ПОСЛЕ монтирования
  // (useEffect ниже), иначе React падает на hydration mismatch.
  const [tabs, setTabs] = useState<Tab[]>([{ id: "default", title: "Чат 1" }]);
  const [active, setActive] = useState<string>("default");
  const [chats, setChats] = useState<Record<string, ChatMessage[]>>({ default: [] });
  const [hydrated, setHydrated] = useState(false);
  const [input, setInput] = useState("");
  const [conn, setConn] = useState("connecting");
  // v3: вложения чата (RAG). Каждое — со статусом/прогрессом для реактивности.
  const [attachments, setAttachments] = useState<Attach[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [listening, setListening] = useState(false);
  const [speak, setSpeak] = useState(false);
  const [level, setLevel] = useState(0);
  const [memOpen, setMemOpen] = useState(false);
  const [mem, setMem] = useState<MemoryOverview | null>(null);
  const [micError, setMicError] = useState("");
  const [workingSessions, setWorkingSessions] = useState<string[]>([]);
  const [actorBySession, setActorBySession] = useState<Record<string, string>>({});

  const chatRef = useRef<JarvisSocket | null>(null);
  const audioRef = useRef<JarvisSocket | null>(null);
  const speakRef = useRef(false);
  const activeRef = useRef(active);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const workingIds = useRef<Record<string, string>>({});  // session -> last msg id
  const msgToSession = useRef<Record<string, string>>({}); // msg id -> session
  // mic / tts
  const micCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micProcRef = useRef<ScriptProcessorNode | null>(null);
  const playCtxRef = useRef<AudioContext | null>(null);
  const playTimeRef = useRef(0);

  useEffect(() => { speakRef.current = speak; }, [speak]);
  useEffect(() => { activeRef.current = active; }, [active]);

  // Подгрузка сохранённого состояния ТОЛЬКО на клиенте после монтирования.
  useEffect(() => {
    const { tabs: t, chats: c } = loadTabs();
    setTabs(t);
    setChats(c);
    setActive(t[0]?.id || "default");
    setHydrated(true);
  }, []);

  // persist tabs + chats (только после гидратации — чтобы не затереть
  // сохранённое дефолтным состоянием до его загрузки).
  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(LS_TABS, JSON.stringify(tabs));
      const slim: Record<string, ChatMessage[]> = {};
      for (const [k, v] of Object.entries(chats)) {
        slim[k] = v.slice(-100).map((m) => ({ ...m, steps: m.steps.slice(-12) }));
      }
      localStorage.setItem(LS_CHATS, JSON.stringify(slim));
    } catch { /* quota */ }
  }, [tabs, chats, hydrated]);

  const setWorking = (session: string, on: boolean) =>
    setWorkingSessions((p) => (on ? [...new Set([...p, session])] : p.filter((s) => s !== session)));

  const upsert = useCallback((session: string, id: string, mutate: (m: ChatMessage) => void) => {
    setChats((prev) => {
      const list = [...(prev[session] || [])];
      let idx = list.findIndex((m) => m.role === "assistant" && m.replyTo === id);
      if (idx === -1) {
        list.push({ id: uid(), role: "assistant", text: "", replyTo: id, steps: [], streaming: true });
        idx = list.length - 1;
      }
      const copy = { ...list[idx], steps: [...list[idx].steps] };
      mutate(copy);
      list[idx] = copy;
      return { ...prev, [session]: list };
    });
  }, []);

  // --- WebSocket чата ---
  useEffect(() => {
    const sessionOf = (id: string) => msgToSession.current[id] || activeRef.current;
    const sock = new JarvisSocket("/ws/chat", {
      onState: setConn,
      onJson: (msg: JarvisMessage) => {
        const id = String(msg.id ?? "");
        const session = sessionOf(id);
        if (msg.actor) setActorBySession((p) => ({ ...p, [session]: String(msg.actor) }));
        const stepActor = String(msg.actor ?? "");
        const clearActor = () => setActorBySession((p) => ({ ...p, [session]: "" }));
        switch (msg.type) {
          case "thought":
            upsert(session, id, (m) => m.steps.push({ kind: "thought", text: String(msg.text ?? ""), actor: stepActor }));
            break;
          case "tool_call":
            upsert(session, id, (m) => m.steps.push({
              kind: "tool_call", tool: String(msg.tool ?? ""), actor: stepActor, text: JSON.stringify(msg.args ?? {}) }));
            break;
          case "tool_result":
            upsert(session, id, (m) => m.steps.push({
              kind: "tool_result", tool: String(msg.tool ?? ""), actor: stepActor, ok: Boolean(msg.ok), text: String(msg.summary ?? "") }));
            break;
          case "assistant_start":
            upsert(session, id, (m) => { m.streaming = true; });
            break;
          case "token":
            upsert(session, id, (m) => { m.text += String(msg.content ?? ""); });
            break;
          case "assistant_done":
            upsert(session, id, (m) => { m.streaming = false; m.text = String(msg.content ?? m.text); });
            setWorking(session, false); clearActor();
            if (speakRef.current && session === activeRef.current && msg.content)
              audioRef.current?.sendJson({ type: "speak", text: String(msg.content) });
            break;
          case "error":
            upsert(session, id, (m) => { m.streaming = false; m.error = true; m.text += `\n⚠ ${String(msg.error ?? "")}`; });
            setWorking(session, false); clearActor();
            break;
          case "cancelled":
            upsert(session, id, (m) => { m.streaming = false; m.text += (m.text ? "\n" : "") + "⏹ Остановлено."; });
            setWorking(session, false); clearActor();
            break;
          case "memory":
            setChats((p) => ({ ...p, [session]: [...(p[session] || []),
              { id: uid(), role: "assistant", text: `🧠 ${String(msg.text ?? "")}`, steps: [] }] }));
            break;
          default: break;
        }
      },
    });
    sock.connect();
    chatRef.current = sock;

    const asock = new JarvisSocket("/ws/audio", {
      onJson: (m: JarvisMessage) => {
        if (m.type === "final" || m.type === "partial") {
          const t = String(m.text ?? "").trim();
          if (t) setInput((prev) => (prev ? prev + " " + t : t));
        }
      },
      onBinary: (buf) => playTtsChunk(buf),
    });
    asock.connect();
    audioRef.current = asock;
    return () => { sock.close(); asock.close(); stopMic(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upsert]);

  const messages = chats[active] || [];
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // --- вкладки ---
  const newTab = () => {
    const id = "s_" + uid();
    setTabs((t) => [...t, { id, title: `Чат ${t.length + 1}` }]);
    setChats((c) => ({ ...c, [id]: [] }));
    setActive(id);
  };
  const closeTab = (id: string) => {
    setTabs((t) => {
      const left = t.filter((x) => x.id !== id);
      if (left.length === 0) { const nid = "default"; setActive(nid); return [{ id: nid, title: "Чат 1" }]; }
      if (active === id) setActive(left[0].id);
      return left;
    });
    setChats((c) => { const n = { ...c }; delete n[id]; return n; });
    // очистим серверный контекст этой сессии
    chatRef.current?.sendJson({ type: "reset_context", session: id });
  };

  // --- отправка / остановка ---
  const send = () => {
    const text = input.trim();
    if (!text || workingSessions.includes(active)) return;
    const id = uid();
    msgToSession.current[id] = active;
    workingIds.current[active] = id;
    setWorking(active, true);
    setChats((p) => ({ ...p, [active]: [...(p[active] || []), { id, role: "user", text, steps: [] }] }));
    chatRef.current?.sendJson({ type: "user_message", text, id, session: active });
    setInput("");
    // Готовые вложения уже проиндексированы под эту сессию (RAG подтянет их сам);
    // убираем из стейджинга, оставляя ещё грузящиеся.
    setAttachments((a) => a.filter((x) => x.status === "uploading"));
  };
  const cancel = () => {
    chatRef.current?.sendJson({ type: "cancel", session: active, id: workingIds.current[active] || "" });
    setWorking(active, false);
  };

  // --- вложения (RAG): загрузка с прогрессом, drag-drop, превью ---
  const COG = "/api/core/api/cognitive";
  const uploadFile = (file: File) => {
    const localId = uid();
    setAttachments((a) => [...a, { id: localId, name: file.name, size: file.size, status: "uploading", percent: 0 }]);
    const patch = (p: Partial<Attach>) =>
      setAttachments((a) => a.map((x) => (x.id === localId ? { ...x, ...p } : x)));
    const fd = new FormData();
    fd.append("file", file);
    fd.append("session_id", active);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${COG}/files/upload`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) patch({ percent: Math.round((e.loaded / e.total) * 100) });
    };
    xhr.onload = () => {
      try {
        const r = JSON.parse(xhr.responseText);
        const rec = r?.data?.file;
        patch({ status: rec?.ingest_status === "ready" ? "ready" : "failed",
                fileId: rec?.id, chunks: rec?.chunk_count, error: rec?.ingest_error, percent: 100 });
      } catch { patch({ status: "failed", error: "Некорректный ответ сервера" }); }
    };
    xhr.onerror = () => patch({ status: "failed", error: "Ошибка сети" });
    xhr.send(fd);
  };
  const dismissAttach = (a: Attach) => {
    setAttachments((list) => list.filter((x) => x.id !== a.id));
    if (a.fileId) fetch(`${COG}/files/${a.fileId}`, { method: "DELETE" }).catch(() => {});
  };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false);
    Array.from(e.dataTransfer.files || []).forEach(uploadFile);
  };

  // --- микрофон ---
  const startMic = async () => {
    setMicError("");
    if (!navigator.mediaDevices?.getUserMedia) {
      setMicError("Браузер не даёт доступ к микрофону (нужен https или localhost)."); return;
    }
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
      source.connect(proc); proc.connect(ctx.destination);
      setListening(true);
    } catch {
      setListening(false);
      setMicError("Не удалось включить микрофон. Разрешите доступ в браузере (значок 🎤/замок в адресной строке).");
    }
  };
  const stopMic = () => {
    micProcRef.current?.disconnect();
    micStreamRef.current?.getTracks().forEach((t) => t.stop());
    micCtxRef.current?.close().catch(() => {});
    micProcRef.current = null; micStreamRef.current = null;
    setListening(false); setLevel(0);
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
    if (head[0] === 0x52 && head[1] === 0x49) pcm = new Int16Array(buf.slice(44));
    if (pcm.length === 0) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const ab = ctx.createBuffer(1, f32.length, TARGET_SR);
    ab.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = ab; src.connect(ctx.destination);
    const start = Math.max(ctx.currentTime, playTimeRef.current);
    src.start(start); playTimeRef.current = start + ab.duration;
  };

  // --- память (по активной сессии) ---
  const loadMem = useCallback(async () => {
    try {
      const r = await fetch(`${API}/memory?session=${encodeURIComponent(active)}`, { cache: "no-store" });
      setMem(await r.json());
    } catch { setMem(null); }
  }, [active]);
  const memAction = async (action: string, extra: Record<string, unknown> = {}) => {
    await fetch(`${API}/memory`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, session: active, ...extra }),
    });
    loadMem();
  };
  useEffect(() => { if (memOpen) loadMem(); }, [memOpen, loadMem]);

  const activeWorking = workingSessions.includes(active);
  const activeActor = actorBySession[active] || "";

  return (
    <div className={`chat-wrap ${dragOver ? "drag-over" : ""}`}
         onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
         onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }}
         onDrop={onDrop}>
      {dragOver && (
        <div className="drop-overlay">
          <div className="drop-hint">📎 Отпустите файлы — прикреплю к диалогу (RAG)</div>
        </div>
      )}
      <div className="chat-head panel">
        <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
        <strong>JARVIS</strong>
        {activeWorking && activeActor ? (
          <span className={`actor-badge actor-${activeActor}`}>
            <span className="actor-spin" /> Работает: {actorInfo(activeActor).icon} {actorInfo(activeActor).name}
          </span>
        ) : (
          <span style={{ fontSize: 12, color: "var(--muted)" }}>
            голос, код, веб, управление ПК · память авто-сжимается
          </span>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button className={`btn ${speak ? "primary" : ""}`} onClick={() => setSpeak((v) => !v)}
                  title="Озвучивать ответы (TTS)">🔊</button>
          <button className="btn" onClick={() => setMemOpen(true)} title="Память">🧠 Память</button>
        </div>
      </div>

      {/* Вкладки-диалоги */}
      <div className="tab-bar">
        {tabs.map((t) => (
          <div key={t.id} className={`tab ${t.id === active ? "active" : ""}`}
               onClick={() => setActive(t.id)}>
            <span className="tab-title">
              {t.title}{workingSessions.includes(t.id) ? " •" : ""}
            </span>
            <span className="tab-close" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}
                  title="Закрыть диалог">×</span>
          </div>
        ))}
        <button className="tab-new" onClick={newTab} title="Новый диалог">＋</button>
      </div>

      <div className="chat-feed">
        {messages.length === 0 && (
          <div className="chat-hello">
            <p>Привет! Я JARVIS. Спросите что угодно:</p>
            <ul>
              <li>«Какая погода завтра в Москве?»</li>
              <li>«Открой блокнот и напиши hello world на C++»</li>
              <li>«Спарси заголовки с example.com»</li>
              <li>«Открой страницу с погодой в браузере»</li>
            </ul>
            <p style={{ color: "var(--muted)" }}>Можно голосом — 🎤. Новый диалог — вкладка ＋.</p>
          </div>
        )}
        {messages.map((m) => <MessageBubble key={m.id} m={m} session={active} />)}
        <div ref={bottomRef} />
      </div>

      {/* Превью-карточки прикреплённых файлов (со статусом/прогрессом) */}
      {attachments.length > 0 && (
        <div className="attach-row">
          {attachments.map((a) => (
            <div key={a.id} className={`attach-card ${a.status}`} title={a.error || a.name}>
              <span className="attach-icon">
                {a.status === "uploading" ? "⏳" : a.status === "ready" ? "📄" : "⚠"}
              </span>
              <span className="attach-name">{a.name}</span>
              {a.status === "uploading" && (
                <span className="attach-bar"><span className="attach-fill" style={{ width: `${a.percent}%` }} /></span>
              )}
              {a.status === "ready" && <span className="attach-meta">✓ {a.chunks ?? 0} фрагм.</span>}
              {a.status === "failed" && <span className="attach-meta err">ошибка</span>}
              <button className="attach-x" onClick={() => dismissAttach(a)} title="Убрать">×</button>
            </div>
          ))}
        </div>
      )}

      {micError && <div className="mic-error">⚠ {micError}</div>}
      <div className={`chat-input panel ${listening ? "recording" : ""}`}>
        <input ref={fileInputRef} type="file" multiple style={{ display: "none" }}
               onChange={(e) => { Array.from(e.target.files || []).forEach(uploadFile); e.target.value = ""; }} />
        <button className="btn attach-btn" onClick={() => fileInputRef.current?.click()}
                title="Прикрепить файл (или перетащите в окно)">＋</button>
        <button className={`btn mic ${listening ? "recording" : ""}`}
                onClick={() => (listening ? stopMic() : startMic())}
                title={listening ? "Идёт запись — нажмите, чтобы остановить" : "Голосовой ввод: нажмите и говорите"}>
          {listening ? "⏹" : "🎤"}
        </button>
        {listening ? (
          <div className="rec-banner">
            <span className="rec-dot" />
            <span className="rec-text">Идёт запись — говорите… Нажмите ⏹, чтобы остановить.</span>
            <div className="vu-meter mic-vu"><div className="vu-fill" style={{ width: `${level * 100}%` }} /></div>
          </div>
        ) : (
          <textarea value={input} onChange={(e) => setInput(e.target.value)}
                    placeholder="Сообщение JARVIS…  (или 🎤 для голоса)" rows={1}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        )}
        {activeWorking ? (
          <button className="btn danger send" onClick={cancel} title="Аварийно остановить задачу">⏹</button>
        ) : (
          <button className="btn primary send" onClick={send} disabled={listening} title="Отправить">➤</button>
        )}
      </div>

      {memOpen && <MemoryPanel mem={mem} session={active} onClose={() => setMemOpen(false)} onAction={memAction} />}
    </div>
  );
}

// --------------------------------------------------------------------------- //
function MessageBubble({ m, session }: { m: ChatMessage; session: string }) {
  const [open, setOpen] = useState(false);
  const [why, setWhy] = useState<{ content: string; entry_type: string }[] | null>(null);
  const [whyBusy, setWhyBusy] = useState(false);
  const isUser = m.role === "user";
  const explain = async () => {
    setWhyBusy(true);
    try {
      const r = await fetch(`/api/core/api/cognitive/db/episodic_memory_logs?q=${encodeURIComponent(session)}&limit=8`);
      const d = await r.json();
      setWhy((d?.data?.rows || []).map((x: any) => ({ content: String(x.content || ""), entry_type: String(x.entry_type || "") })));
    } catch { setWhy([]); }
    setWhyBusy(false);
  };
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
                    <span className="step-actor" title={actorInfo(s.actor).name}>
                      {actorInfo(s.actor).icon}
                    </span>{" "}
                    {s.kind === "thought" && <span>{s.text}</span>}
                    {s.kind === "tool_call" && <span>🔧 <code>{s.tool}</code> {s.text}</span>}
                    {s.kind === "tool_result" && <span>{s.ok ? "✅" : "⚠️"} <code>{s.tool}</code>: {s.text}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="bubble-text">{renderContent(m.text)}</div>
        {m.streaming && <span className="caret">▋</span>}
        {!isUser && !m.streaming && m.text && (
          <div className="why-row">
            <button className="why-btn" onClick={explain} disabled={whyBusy}
                    title="Показать цепочку рассуждений (эпизодическая память)">
              {whyBusy ? "…" : "почему?"}
            </button>
            {why && (
              <div className="why-panel">
                {why.length === 0 && <div className="why-empty">Трасса пуста для этого диалога.</div>}
                {why.map((w, i) => (
                  <div key={i} className={`why-item why-${w.entry_type}`}>
                    <span className="why-type">{w.entry_type}</span> {w.content.slice(0, 240)}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
function MemoryPanel({
  mem, session, onClose, onAction,
}: {
  mem: MemoryOverview | null; session: string;
  onClose: () => void; onAction: (a: string, extra?: Record<string, unknown>) => void;
}) {
  const [note, setNote] = useState("");
  return (
    <div className="hitl-modal" onClick={onClose}>
      <div className="hitl-card mem-card" onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0 }}>🧠 Память JARVIS · диалог «{session}»</h3>
        <div className="mem-section">
          <strong>Оперативный контекст</strong>
          <p style={{ color: "var(--muted)", fontSize: 13, margin: "4px 0" }}>
            Реплик в активном окне: {mem?.recent_count ?? "—"} · сжимается автоматически при наборе критической массы.
          </p>
          {mem?.summary && <pre className="log-stream" style={{ height: "12vh" }}>{mem.summary}</pre>}
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={() => onAction("flush")}>📥 Сжать в сводку сейчас</button>
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
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Запомнить факт…" style={{ flex: 1 }} />
            <button className="btn" onClick={() => { if (note.trim()) { onAction("save", { text: note.trim() }); setNote(""); } }}>＋ Запомнить</button>
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
