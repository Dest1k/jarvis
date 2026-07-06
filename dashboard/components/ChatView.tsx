"use client";
/**
 * ChatView.tsx — универсальный чат с агентом JARVIS.
 *
 * Модель состояния:
 * - текст сообщений — пользовательская история UI;
 * - steps/«почему» — временный runtime trace одного хода агента;
 * - backend ConversationManager — фактический оперативный контекст модели.
 *
 * Поэтому reset/flush контекста чистит не только backend-память, но и runtime
 * trace в браузере. steps не сохраняются в localStorage.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

const TARGET_SR = 16000;
const API = "/api/core/api/agent";
const LS_TABS = "jarvis_tabs";
const LS_CHATS = "jarvis_chats";

const ACTORS: Record<string, { icon: string; name: string }> = {
  dispatcher: { icon: "🧠", name: "Диспетчер" },
  researcher: { icon: "🔎", name: "Researcher" },
  coder: { icon: "🧑‍💻", name: "Coder" },
  sysadmin: { icon: "🛠️", name: "SysAdmin" },
  critic: { icon: "🛡️", name: "Critic" },
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
interface MemoryItem { id: string; kind: string; text: string; tags: string[] }
interface MemoryOverview {
  summary: string; recent_count: number; longterm: MemoryItem[]; longterm_count: number;
}

type ClearMode = "reset" | "flush";

function uid(): string { return Math.random().toString(36).slice(2, 10); }

function sanitizeMessage(raw: Partial<ChatMessage>): ChatMessage {
  return {
    id: String(raw.id || uid()),
    role: raw.role === "user" ? "user" : "assistant",
    text: String(raw.text || ""),
    replyTo: raw.replyTo ? String(raw.replyTo) : undefined,
    steps: [],
    streaming: false,
    error: Boolean(raw.error),
  };
}

function sanitizeChats(raw: Record<string, ChatMessage[]>): Record<string, ChatMessage[]> {
  const out: Record<string, ChatMessage[]> = {};
  for (const [sid, list] of Object.entries(raw || {})) {
    out[sid] = Array.isArray(list) ? list.slice(-100).map(sanitizeMessage) : [];
  }
  return out;
}

function loadTabs(): { tabs: Tab[]; chats: Record<string, ChatMessage[]> } {
  if (typeof window === "undefined") return { tabs: [], chats: {} };
  try {
    const tabs = JSON.parse(localStorage.getItem(LS_TABS) || "[]") as Tab[];
    const chats = sanitizeChats(JSON.parse(localStorage.getItem(LS_CHATS) || "{}"));
    if (tabs.length) return { tabs, chats };
  } catch { /* ignore */ }
  const id = "default";
  return { tabs: [{ id, title: "Чат 1" }], chats: { [id]: [] } };
}

export default function ChatView() {
  const initial = useRef(loadTabs());
  const [tabs, setTabs] = useState<Tab[]>(initial.current.tabs);
  const [active, setActive] = useState<string>(initial.current.tabs[0]?.id || "default");
  const [chats, setChats] = useState<Record<string, ChatMessage[]>>(initial.current.chats);
  const [input, setInput] = useState("");
  const [conn, setConn] = useState("connecting");
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
  const workingIds = useRef<Record<string, string>>({});
  const msgToSession = useRef<Record<string, string>>({});
  const micCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micProcRef = useRef<ScriptProcessorNode | null>(null);
  const playCtxRef = useRef<AudioContext | null>(null);
  const playTimeRef = useRef(0);

  useEffect(() => { speakRef.current = speak; }, [speak]);
  useEffect(() => { activeRef.current = active; }, [active]);

  useEffect(() => {
    try {
      localStorage.setItem(LS_TABS, JSON.stringify(tabs));
      const slim: Record<string, ChatMessage[]> = {};
      for (const [sid, list] of Object.entries(chats)) {
        slim[sid] = list.slice(-100).map((m) => ({ ...m, steps: [], streaming: false }));
      }
      localStorage.setItem(LS_CHATS, JSON.stringify(slim));
    } catch { /* quota */ }
  }, [tabs, chats]);

  const setWorking = (session: string, on: boolean) =>
    setWorkingSessions((p) => (on ? [...new Set([...p, session])] : p.filter((s) => s !== session)));

  const clearSessionUiContext = useCallback((session: string, mode: ClearMode = "reset") => {
    setChats((prev) => {
      if (mode === "reset") return { ...prev, [session]: [] };
      return {
        ...prev,
        [session]: (prev[session] || []).map((m) => ({ ...m, steps: [], streaming: false })),
      };
    });
    setWorkingSessions((p) => p.filter((s) => s !== session));
    setActorBySession((p) => ({ ...p, [session]: "" }));
    delete workingIds.current[session];
    for (const [msgId, sid] of Object.entries(msgToSession.current)) {
      if (sid === session) delete msgToSession.current[msgId];
    }
  }, []);

  const upsert = useCallback((session: string, id: string, mutate: (m: ChatMessage) => void) => {
    if (!id) return;
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

  useEffect(() => {
    const sessionOf = (id: string, explicit?: unknown) =>
      (typeof explicit === "string" && explicit) ? explicit : (msgToSession.current[id] || activeRef.current);

    const sock = new JarvisSocket("/ws/chat", {
      onState: setConn,
      onJson: (msg: JarvisMessage) => {
        const id = String(msg.id ?? "");
        const session = sessionOf(id, msg.session);
        if (msg.actor) setActorBySession((p) => ({ ...p, [session]: String(msg.actor) }));
        const stepActor = String(msg.actor ?? "");
        const clearActor = () => setActorBySession((p) => ({ ...p, [session]: "" }));

        switch (msg.type) {
          case "thought":
            upsert(session, id, (m) => m.steps.push({ kind: "thought", text: String(msg.text ?? ""), actor: stepActor }));
            break;
          case "tool_call":
            upsert(session, id, (m) => m.steps.push({
              kind: "tool_call", tool: String(msg.tool ?? ""), actor: stepActor, text: JSON.stringify(msg.args ?? {}),
            }));
            break;
          case "tool_result":
            upsert(session, id, (m) => m.steps.push({
              kind: "tool_result", tool: String(msg.tool ?? ""), actor: stepActor, ok: Boolean(msg.ok), text: String(msg.summary ?? ""),
            }));
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
            if (speakRef.current && session === activeRef.current && msg.content) {
              audioRef.current?.sendJson({ type: "speak", text: String(msg.content) });
            }
            break;
          case "error":
            upsert(session, id, (m) => { m.streaming = false; m.error = true; m.text += `\n⚠ ${String(msg.error ?? "")}`; });
            setWorking(session, false); clearActor();
            break;
          case "cancelled":
            upsert(session, id, (m) => { m.streaming = false; m.text += (m.text ? "\n" : "") + "⏹ Остановлено."; m.steps = []; });
            setWorking(session, false); clearActor();
            break;
          case "memory": {
            const event = String(msg.event ?? "");
            if (event === "reset") { clearSessionUiContext(session, "reset"); break; }
            if (event === "flushed" || event === "summarized") clearSessionUiContext(session, "flush");
            if (!id && !msg.session) break;
            setChats((p) => ({ ...p, [session]: [...(p[session] || []),
              { id: uid(), role: "assistant", text: `🧠 ${String(msg.text ?? "")}`, steps: [] }] }));
            break;
          }
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
  }, [upsert, clearSessionUiContext]);

  const messages = chats[active] || [];
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

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
    clearSessionUiContext(id, "reset");
    chatRef.current?.sendJson({ type: "reset_context", session: id });
  };

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
  };

  const cancel = () => {
    chatRef.current?.sendJson({ type: "cancel", session: active, id: workingIds.current[active] || "" });
    setWorking(active, false);
    clearSessionUiContext(active, "flush");
  };

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
      setMicError("Не удалось включить микрофон. Разрешите доступ в браузере.");
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

  const loadMem = useCallback(async () => {
    try {
      const r = await fetch(`${API}/memory?session=${encodeURIComponent(active)}`, { cache: "no-store" });
      setMem(await r.json());
    } catch { setMem(null); }
  }, [active]);

  const memAction = async (action: string, extra: Record<string, unknown> = {}) => {
    const r = await fetch(`${API}/memory`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, session: active, ...extra }),
    });
    if (r.ok) {
      if (action === "reset") clearSessionUiContext(active, "reset");
      if (action === "flush") clearSessionUiContext(active, "flush");
    }
    loadMem();
  };

  useEffect(() => { if (memOpen) loadMem(); }, [memOpen, loadMem]);

  const activeWorking = workingSessions.includes(active);
  const activeActor = actorBySession[active] || "";

  return (
    <div className="chat-wrap">
      <div className="chat-head panel">
        <span className={`status-dot ${conn === "open" ? "ok" : "warn"}`} />
        <strong>JARVIS</strong>
        {activeWorking && activeActor ? (
          <span className={`actor-badge actor-${activeActor}`}>
            <span className="actor-spin" /> Работает: {actorInfo(activeActor).icon} {actorInfo(activeActor).name}
          </span>
        ) : (
          <span style={{ fontSize: 12, color: "var(--muted)" }}>
            голос, код, веб, управление ПК · steps/«почему» очищаются вместе с контекстом
          </span>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button className={`btn ${speak ? "primary" : ""}`} onClick={() => setSpeak((v) => !v)}
                  title="Озвучивать ответы (TTS)">🔊</button>
          <button className="btn" onClick={() => setMemOpen(true)} title="Память">🧠 Память</button>
        </div>
      </div>

      <div className="tab-bar">
        {tabs.map((t) => (
          <div key={t.id} className={`tab ${t.id === active ? "active" : ""}`} onClick={() => setActive(t.id)}>
            <span className="tab-title">{t.title}{workingSessions.includes(t.id) ? " •" : ""}</span>
            <span className="tab-close" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }} title="Закрыть диалог">×</span>
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
        {messages.map((m) => <MessageBubble key={m.id} m={m} />)}
        <div ref={bottomRef} />
      </div>

      {micError && <div className="mic-error">⚠ {micError}</div>}
      <div className={`chat-input panel ${listening ? "recording" : ""}`}>
        <button className={`btn mic ${listening ? "recording" : ""}`}
                onClick={() => (listening ? stopMic() : startMic())}
                title={listening ? "Идёт запись — нажмите, чтобы остановить" : "Голосовой ввод"}>
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

function MessageBubble({ m }: { m: ChatMessage }) {
  const [open, setOpen] = useState(false);
  const isUser = m.role === "user";
  return (
    <div className={`msg-row ${isUser ? "user" : "assistant"}`}>
      <div className={`bubble ${isUser ? "user" : "assistant"} ${m.error ? "error" : ""}`}>
        {!isUser && m.steps.length > 0 && (
          <div className="steps">
            <button className="steps-toggle" onClick={() => setOpen((v) => !v)}>
              {open ? "▾" : "▸"} Почему / ход выполнения ({m.steps.length})
            </button>
            {open && (
              <div className="steps-body">
                {m.steps.map((s, i) => (
                  <div key={i} className={`step ${s.kind}`}>
                    <span className="step-actor" title={actorInfo(s.actor).name}>{actorInfo(s.actor).icon}</span>{" "}
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
      </div>
    </div>
  );
}

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
            Реплик в активном backend-окне: {mem?.recent_count ?? "—"} · «почему» хранится отдельно как runtime trace и при reset/flush очищается.
          </p>
          {mem?.summary && <pre className="log-stream" style={{ height: "12vh" }}>{mem.summary}</pre>}
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <button className="btn" onClick={() => onAction("flush")}
                    title="Сжать backend-контекст в summary; видимый текст останется, trace/«почему» очистится">
              📥 Сжать в сводку и скрыть «почему»
            </button>
            <button className="btn danger" onClick={() => onAction("reset")}
                    title="Полностью очистить backend-контекст и видимый диалог этой вкладки">
              🧹 Очистить контекст и экран
            </button>
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
