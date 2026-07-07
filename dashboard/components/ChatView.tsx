"use client";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

const API = "/api/core/api/agent";
const COG = "/api/core/api/cognitive";
const LS_TABS = "jarvis_tabs";
const LS_CHATS = "jarvis_chats";
const TARGET_SR = 16000;

const ACTORS: Record<string, { icon: string; name: string }> = {
  dispatcher: { icon: "🧠", name: "Ядро" }, researcher: { icon: "🔎", name: "Поиск" },
  coder: { icon: "🧑‍💻", name: "Код" }, sysadmin: { icon: "🛠️", name: "Админ" },
  critic: { icon: "🛡️", name: "Контроль" }, "ui-tars": { icon: "👁️", name: "Зрение" },
  sandbox: { icon: "📦", name: "Песочница" }, host: { icon: "🪟", name: "Система" },
  web: { icon: "🌐", name: "Веб" }, memory: { icon: "💾", name: "Память" },
  local: { icon: "⚡", name: "Локально" }, mcp: { icon: "🧩", name: "MCP" }, mission: { icon: "🧭", name: "Миссия" },
};
const actorInfo = (a?: string) => ACTORS[a || ""] || { icon: "•", name: a || "" };

const TOOL_LABELS: Record<string, string> = {
  windows: "Windows",
  shell: "песочница",
  run_code: "код",
  gui: "экранный агент",
  see_screen: "чтение экрана",
  open_url: "браузер",
  web_search: "поиск в интернете",
  web_fetch: "чтение страницы",
  weather: "погода",
  wikipedia: "справка",
  http_request: "HTTP-запрос",
  exchange_rate: "курс валют",
  define: "словарь",
  translate: "перевод",
  memory_save: "память",
  memory_search: "поиск в памяти",
  list_memory: "память",
  calculator: "калькулятор",
  now: "время",
  list_dir: "папка",
  system_info: "система",
};

const SUGGESTIONS = [
  "Оформи это как план миссии: ",
  "Проведи диагностику JARVIS и предложи улучшения",
  "Сделай самопроверку JARVIS и объясни результат",
  "Разложи задачу на шаги, риски и план реализации",
  "Проверь систему через встроенные инструменты Windows",
];

const uid = () => Math.random().toString(36).slice(2, 10);
const isRecord = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const clip = (text: string, max = 240) => text.length > max ? `${text.slice(0, max - 1)}…` : text;
const pickText = (data: Record<string, unknown>, keys: string[]) => {
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
  }
  return "";
};
const humanAction = (action: string) => action.replace(/_/g, " ").trim() || "действие";
const formatToolName = (tool = "") => {
  if (!tool) return "инструмент";
  if (tool.startsWith("mcp_")) return `MCP «${tool.slice(4).replace(/_/g, " ")}»`;
  return TOOL_LABELS[tool] || tool.replace(/_/g, " ");
};
function formatToolCall(tool: string, args: unknown): string {
  const label = formatToolName(tool);
  if (!isRecord(args)) return `Запускаю ${label}.`;
  const action = pickText(args, ["action", "fn", "method", "operation"]);
  const goal = pickText(args, ["goal", "task", "instruction", "query", "url", "path", "command", "text", "location"]);

  if (tool === "windows") {
    const command = pickText(args, ["command", "cmd", "app", "path"]);
    if (action === "powershell") return `PowerShell: ${clip(command || "выполняю команду")}`;
    if (action === "exec") return `Команда ОС: ${clip(command || "выполняю команду")}`;
    if (action === "open_app") return `Открываю приложение: ${clip(command || goal || "указанное окно")}`;
    if (action === "open_url") return `Открываю ссылку: ${clip(command || goal || "указанный адрес")}`;
    if (action === "paste_text") return "Вставляю подготовленный текст в активное окно.";
    if (action === "send_keys") return `Отправляю клавиши: ${clip(pickText(args, ["keys", "text"]) || "комбинация")}`;
    return `Windows: ${humanAction(action)}${command ? ` — ${clip(command)}` : ""}.`;
  }
  if (tool === "gui") return `Работаю с интерфейсом: ${clip(goal || "выполняю визуальный шаг")}`;
  if (tool === "see_screen") return "Смотрю, что сейчас на экране.";
  if (tool === "shell") return `Команда в песочнице: ${clip(goal || "выполняю команду")}`;
  if (tool === "run_code") return "Запускаю код в изолированной среде.";
  if (tool === "calculator") return `Считаю: ${clip(goal || "выражение")}`;
  if (tool === "weather") return `Проверяю погоду: ${clip(goal || "указанное место")}`;
  if (tool === "web_search") return `Ищу в интернете: ${clip(goal || "запрос")}`;
  if (tool === "web_fetch") return `Читаю страницу: ${clip(goal || "ссылка")}`;
  if (tool.startsWith("mcp_")) return `${label}: ${clip(goal || action || "выполняю действие")}`;
  return `${label}: ${clip(goal || humanAction(action) || "выполняю действие")}`;
}

type StepKind = "thought" | "tool_call" | "tool_result";
interface Step { kind: StepKind; text: string; tool?: string; ok?: boolean; actor?: string }
interface ChatMessage { id: string; role: "user" | "assistant"; text: string; replyTo?: string; steps: Step[]; streaming?: boolean; error?: boolean }
interface Tab { id: string; title: string }
interface Attach { id: string; name: string; size: number; status: "uploading" | "ready" | "failed"; percent: number; fileId?: string; chunks?: number; error?: string }
interface MemoryItem { id: string; kind: string; text: string; tags: string[] }
interface MemoryOverview { summary: string; recent_count: number; longterm: MemoryItem[]; longterm_count: number; incidents?: unknown[]; skills?: unknown[] }
interface WhyItem { content: string; entry_type: string }

const cleanMsg = (m: Partial<ChatMessage>): ChatMessage => ({
  id: String(m.id || uid()), role: m.role === "user" ? "user" : "assistant",
  text: String(m.text || ""), replyTo: m.replyTo ? String(m.replyTo) : undefined,
  steps: [], streaming: false, error: Boolean(m.error),
});

function loadTabs(): { tabs: Tab[]; chats: Record<string, ChatMessage[]> } {
  if (typeof window === "undefined") return { tabs: [], chats: {} };
  try {
    const tabs = JSON.parse(localStorage.getItem(LS_TABS) || "[]") as Tab[];
    const raw = JSON.parse(localStorage.getItem(LS_CHATS) || "{}") as Record<string, ChatMessage[]>;
    const chats: Record<string, ChatMessage[]> = {};
    for (const [sid, list] of Object.entries(raw || {})) chats[sid] = Array.isArray(list) ? list.slice(-100).map(cleanMsg) : [];
    if (tabs.length) return { tabs, chats };
  } catch { /* ignore */ }
  return { tabs: [{ id: "default", title: "Чат 1" }], chats: { default: [] } };
}

export default function ChatView() {
  const [tabs, setTabs] = useState<Tab[]>([{ id: "default", title: "Чат 1" }]);
  const [active, setActive] = useState("default");
  const [chats, setChats] = useState<Record<string, ChatMessage[]>>({ default: [] });
  const [hydrated, setHydrated] = useState(false);
  const [input, setInput] = useState("");
  const [conn, setConn] = useState("connecting");
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
  const [epoch, setEpoch] = useState<Record<string, number>>({});

  const chatRef = useRef<JarvisSocket | null>(null);
  const audioRef = useRef<JarvisSocket | null>(null);
  const speakRef = useRef(false);
  const activeRef = useRef(active);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const feedRef = useRef<HTMLDivElement | null>(null);
  const workingIds = useRef<Record<string, string>>({});
  const msgToSession = useRef<Record<string, string>>({});
  const micCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micProcRef = useRef<ScriptProcessorNode | null>(null);
  const playCtxRef = useRef<AudioContext | null>(null);
  const playTimeRef = useRef(0);

  useEffect(() => { speakRef.current = speak; }, [speak]);
  useEffect(() => { activeRef.current = active; }, [active]);
  useEffect(() => { const s = loadTabs(); setTabs(s.tabs); setChats(s.chats); setActive(s.tabs[0]?.id || "default"); setHydrated(true); }, []);
  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(LS_TABS, JSON.stringify(tabs));
      const slim: Record<string, ChatMessage[]> = {};
      for (const [sid, list] of Object.entries(chats)) slim[sid] = list.slice(-100).map((m) => ({ ...m, steps: [], streaming: false }));
      localStorage.setItem(LS_CHATS, JSON.stringify(slim));
    } catch { /* quota */ }
  }, [tabs, chats, hydrated]);

  const setWorking = (session: string, on: boolean) => setWorkingSessions((p) => on ? [...new Set([...p, session])] : p.filter((s) => s !== session));
  const bump = (session: string) => setEpoch((p) => ({ ...p, [session]: (p[session] || 0) + 1 }));
  const clearUI = useCallback((session: string, mode: "reset" | "flush") => {
    setChats((p) => mode === "reset" ? { ...p, [session]: [] } : { ...p, [session]: (p[session] || []).map((m) => ({ ...m, steps: [], streaming: false })) });
    setWorkingSessions((p) => p.filter((s) => s !== session));
    setActorBySession((p) => ({ ...p, [session]: "" }));
    delete workingIds.current[session];
    for (const [id, sid] of Object.entries(msgToSession.current)) if (sid === session) delete msgToSession.current[id];
    bump(session);
  }, []);

  const upsert = useCallback((session: string, id: string, mut: (m: ChatMessage) => void) => {
    if (!id) return;
    setChats((p) => {
      const list = [...(p[session] || [])];
      let i = list.findIndex((m) => m.role === "assistant" && m.replyTo === id);
      if (i === -1) { list.push({ id: uid(), role: "assistant", text: "", replyTo: id, steps: [], streaming: true }); i = list.length - 1; }
      const copy = { ...list[i], steps: [...list[i].steps] };
      mut(copy);
      list[i] = copy;
      return { ...p, [session]: list };
    });
  }, []);

  useEffect(() => {
    const sessionOf = (id: string, explicit?: unknown) => typeof explicit === "string" && explicit ? explicit : (msgToSession.current[id] || activeRef.current);
    const sock = new JarvisSocket("/ws/chat", { onState: setConn, onJson: (msg: JarvisMessage) => {
      const id = String(msg.id ?? "");
      const session = sessionOf(id, msg.session);
      const actor = String(msg.actor ?? "");
      if (actor) setActorBySession((p) => ({ ...p, [session]: actor }));
      const clearActor = () => setActorBySession((p) => ({ ...p, [session]: "" }));
      switch (msg.type) {
        case "thought": upsert(session, id, (m) => m.steps.push({ kind: "thought", text: String(msg.text ?? ""), actor })); break;
        case "tool_call": upsert(session, id, (m) => m.steps.push({ kind: "tool_call", tool: String(msg.tool ?? ""), actor, text: formatToolCall(String(msg.tool ?? ""), msg.args ?? {}) })); break;
        case "tool_result": upsert(session, id, (m) => m.steps.push({ kind: "tool_result", tool: String(msg.tool ?? ""), actor, ok: Boolean(msg.ok), text: String(msg.summary ?? "") })); break;
        case "assistant_start": upsert(session, id, (m) => { m.streaming = true; }); break;
        case "token": upsert(session, id, (m) => { m.text += String(msg.content ?? ""); }); break;
        case "assistant_done":
          upsert(session, id, (m) => { m.streaming = false; m.text = String(msg.content ?? m.text); });
          setWorking(session, false); clearActor();
          if (speakRef.current && session === activeRef.current && msg.content) audioRef.current?.sendJson({ type: "speak", text: String(msg.content) });
          break;
        case "error": upsert(session, id, (m) => { m.streaming = false; m.error = true; m.text += `\n⚠ ${String(msg.error ?? "")}`; }); setWorking(session, false); clearActor(); break;
        case "cancelled": upsert(session, id, (m) => { m.streaming = false; m.steps = []; m.text += (m.text ? "\n" : "") + "⏹ Остановлено."; }); setWorking(session, false); clearActor(); bump(session); break;
        case "memory": {
          const event = String(msg.event ?? "");
          if (event === "reset") { clearUI(session, "reset"); break; }
          if (event === "flushed" || event === "summarized") clearUI(session, "flush");
          if (!id && !msg.session) break;
          setChats((p) => ({ ...p, [session]: [...(p[session] || []), { id: uid(), role: "assistant", text: `🧠 ${String(msg.text ?? "")}`, steps: [] }] }));
          break;
        }
      }
    }});
    sock.connect(); chatRef.current = sock;
    const asock = new JarvisSocket("/ws/audio", { onJson: (m) => { if (m.type === "final" || m.type === "partial") { const t = String(m.text ?? "").trim(); if (t) setInput((p) => p ? `${p} ${t}` : t); } }, onBinary: (buf) => playTtsChunk(buf) });
    asock.connect(); audioRef.current = asock;
    return () => { sock.close(); asock.close(); stopMic(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upsert, clearUI]);

  const messages = chats[active] || [];
  useEffect(() => {
    const feed = feedRef.current;
    if (!feed || messages.length === 0) return;
    const id = window.requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; });
    return () => window.cancelAnimationFrame(id);
  }, [messages]);

  const newTab = () => { const id = "s_" + uid(); setTabs((t) => [...t, { id, title: `Чат ${t.length + 1}` }]); setChats((c) => ({ ...c, [id]: [] })); setActive(id); };
  const closeTab = (id: string) => { setTabs((t) => { const left = t.filter((x) => x.id !== id); if (!left.length) { setActive("default"); return [{ id: "default", title: "Чат 1" }]; } if (active === id) setActive(left[0].id); return left; }); setChats((c) => { const n = { ...c }; delete n[id]; return n; }); clearUI(id, "reset"); chatRef.current?.sendJson({ type: "reset_context", session: id }); };
  const send = () => { const text = input.trim(); if (!text || workingSessions.includes(active)) return; const id = uid(); msgToSession.current[id] = active; workingIds.current[active] = id; setWorking(active, true); setChats((p) => ({ ...p, [active]: [...(p[active] || []), { id, role: "user", text, steps: [] }] })); chatRef.current?.sendJson({ type: "user_message", text, id, session: active }); setInput(""); setAttachments((a) => a.filter((x) => x.status === "uploading")); };
  const cancel = () => { chatRef.current?.sendJson({ type: "cancel", session: active, id: workingIds.current[active] || "" }); setWorking(active, false); clearUI(active, "flush"); };
  const prime = (text: string) => { setInput(text); setTimeout(() => document.querySelector<HTMLTextAreaElement>(".chat-input textarea")?.focus(), 0); };

  const uploadFile = (file: File) => {
    const id = uid(); setAttachments((a) => [...a, { id, name: file.name, size: file.size, status: "uploading", percent: 0 }]);
    const patch = (p: Partial<Attach>) => setAttachments((a) => a.map((x) => x.id === id ? { ...x, ...p } : x));
    const fd = new FormData(); fd.append("file", file); fd.append("session_id", active);
    const xhr = new XMLHttpRequest(); xhr.open("POST", `${COG}/files/upload`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) patch({ percent: Math.round((e.loaded / e.total) * 100) }); };
    xhr.onload = () => { try { const r = JSON.parse(xhr.responseText); const rec = r?.data?.file; patch({ status: rec?.ingest_status === "ready" ? "ready" : "failed", fileId: rec?.id, chunks: rec?.chunk_count, error: rec?.ingest_error, percent: 100 }); } catch { patch({ status: "failed", error: "Некорректный ответ сервера" }); } };
    xhr.onerror = () => patch({ status: "failed", error: "Ошибка сети" }); xhr.send(fd);
  };
  const dismissAttach = (a: Attach) => { setAttachments((list) => list.filter((x) => x.id !== a.id)); if (a.fileId) fetch(`${COG}/files/${a.fileId}`, { method: "DELETE" }).catch(() => {}); };
  const onDrop = (e: React.DragEvent) => { e.preventDefault(); setDragOver(false); Array.from(e.dataTransfer.files || []).forEach(uploadFile); };

  const startMic = async () => { setMicError(""); try { const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); micStreamRef.current = stream; const ctx = new AudioContext({ sampleRate: 48000 }); micCtxRef.current = ctx; const src = ctx.createMediaStreamSource(stream); const proc = ctx.createScriptProcessor(4096, 1, 1); micProcRef.current = proc; proc.onaudioprocess = (e) => { const inp = e.inputBuffer.getChannelData(0); let sum = 0; for (let i = 0; i < inp.length; i++) sum += inp[i] * inp[i]; setLevel(Math.min(1, Math.sqrt(sum / inp.length) * 4)); audioRef.current?.sendBinary(downsampleToPcm16(inp, ctx.sampleRate, TARGET_SR).buffer as ArrayBuffer); }; src.connect(proc); proc.connect(ctx.destination); setListening(true); } catch { setListening(false); setMicError("Не удалось включить микрофон. Разрешите доступ в браузере."); } };
  const stopMic = () => { micProcRef.current?.disconnect(); micStreamRef.current?.getTracks().forEach((t) => t.stop()); micCtxRef.current?.close().catch(() => {}); micProcRef.current = null; micStreamRef.current = null; setListening(false); setLevel(0); audioRef.current?.sendJson({ type: "end_utterance" }); };
  const playTtsChunk = (buf: ArrayBuffer) => { if (!playCtxRef.current) { playCtxRef.current = new AudioContext({ sampleRate: TARGET_SR }); playTimeRef.current = playCtxRef.current.currentTime; } const ctx = playCtxRef.current; let pcm = new Int16Array(buf); const head = new Uint8Array(buf.slice(0, 4)); if (head[0] === 0x52 && head[1] === 0x49) pcm = new Int16Array(buf.slice(44)); if (!pcm.length) return; const f32 = new Float32Array(pcm.length); for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768; const ab = ctx.createBuffer(1, f32.length, TARGET_SR); ab.getChannelData(0).set(f32); const src = ctx.createBufferSource(); src.buffer = ab; src.connect(ctx.destination); const start = Math.max(ctx.currentTime, playTimeRef.current); src.start(start); playTimeRef.current = start + ab.duration; };
  const loadMem = useCallback(async () => { try { const r = await fetch(`${API}/memory?session=${encodeURIComponent(active)}`, { cache: "no-store" }); setMem(await r.json()); } catch { setMem(null); } }, [active]);
  const memAction = async (action: string, extra: Record<string, unknown> = {}) => { const r = await fetch(`${API}/memory`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, session: active, ...extra }) }); if (r.ok) { if (action === "reset") clearUI(active, "reset"); if (action === "flush") clearUI(active, "flush"); } loadMem(); };
  useEffect(() => { if (memOpen) loadMem(); }, [memOpen, loadMem]);

  const activeWorking = workingSessions.includes(active);
  const activeActor = actorBySession[active] || "";
  const activeEpoch = epoch[active] || 0;
  const ready = conn === "open";

  return (
    <div className={`chat-wrap ${dragOver ? "drag-over" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }} onDrop={onDrop}>
      {dragOver && <div className="drop-overlay"><div className="drop-hint">📎 Отпустите файлы — подключу к памяти диалога</div></div>}
      <div className="jarvis-hero panel">
        <div className={`jarvis-orb ${activeWorking ? "thinking" : ""}`}><span /></div>
        <div className="jarvis-hero-copy">
          <div className="eyebrow">ЯДРО JARVIS · GEMMA 4</div>
          <h2>{activeWorking ? "Выполняю задачу, сэр." : "Готов к работе."}</h2>
          <p>{activeWorking && activeActor ? `Сейчас активен контур «${actorInfo(activeActor).name}». Я сохраню контекст, проверю риски и верну аккуратный результат.` : "Сформулируйте цель обычными словами — я превращу её в план, инструменты и проверяемый результат."}</p>
        </div>
        <div className="jarvis-hero-actions">
          <span className={`pill ${ready ? "ok" : "warn"}`}>{ready ? "на связи" : "подключение"}</span>
          <button className={`btn ${speak ? "primary" : ""}`} onClick={() => setSpeak((v) => !v)} title="Озвучивать ответы">🔊 Голос</button>
          <button className="btn" onClick={() => setMemOpen(true)} title="Память">🧠 Память</button>
        </div>
      </div>
      <div className="tab-bar">{tabs.map((t) => <div key={t.id} className={`tab ${t.id === active ? "active" : ""}`} onClick={() => setActive(t.id)}><span className="tab-title">{t.title}{workingSessions.includes(t.id) ? " •" : ""}</span><span className="tab-close" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }} title="Закрыть диалог">×</span></div>)}<button className="tab-new" onClick={newTab} title="Новый диалог">＋</button></div>
      <div className="chat-feed" ref={feedRef}>{messages.length === 0 && <EmptyState onPick={prime} />}{messages.map((m) => <MessageBubble key={`${m.id}:${activeEpoch}`} m={m} session={active} epoch={activeEpoch} />)}<div ref={bottomRef} /></div>
      {attachments.length > 0 && <div className="attach-row">{attachments.map((a) => <div key={a.id} className={`attach-card ${a.status}`} title={a.error || a.name}><span className="attach-icon">{a.status === "uploading" ? "⏳" : a.status === "ready" ? "📄" : "⚠"}</span><span className="attach-name">{a.name}</span>{a.status === "uploading" && <span className="attach-bar"><span className="attach-fill" style={{ width: `${a.percent}%` }} /></span>}{a.status === "ready" && <span className="attach-meta">✓ {a.chunks ?? 0} фрагм.</span>}{a.status === "failed" && <span className="attach-meta err">ошибка</span>}<button className="attach-x" onClick={() => dismissAttach(a)} title="Убрать">×</button></div>)}</div>}
      {micError && <div className="mic-error">⚠ {micError}</div>}
      <div className="prompt-strip">{SUGGESTIONS.map((s) => <button key={s} className="prompt-chip" onClick={() => prime(s)}>{s.replace(": ", "")}</button>)}</div>
      <div className={`chat-input panel ${listening ? "recording" : ""}`}><input ref={fileInputRef} type="file" multiple style={{ display: "none" }} onChange={(e) => { Array.from(e.target.files || []).forEach(uploadFile); e.target.value = ""; }} /><button className="btn attach-btn" onClick={() => fileInputRef.current?.click()} title="Прикрепить файл">＋</button><button className={`btn mic ${listening ? "recording" : ""}`} onClick={() => (listening ? stopMic() : startMic())}>{listening ? "⏹" : "🎤"}</button>{listening ? <div className="rec-banner"><span className="rec-dot" /><span className="rec-text">Слушаю. Говорите спокойно — я соберу мысль в задачу.</span><div className="vu-meter mic-vu"><div className="vu-fill" style={{ width: `${level * 100}%` }} /></div></div> : <textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder="Сообщение JARVIS… например: оформи это как план миссии" rows={1} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />}{activeWorking ? <button className="btn danger send" onClick={cancel}>⏹</button> : <button className="btn primary send" onClick={send} disabled={listening}>➤</button>}</div>
      {memOpen && <MemoryPanel mem={mem} session={active} onClose={() => setMemOpen(false)} onAction={memAction} />}
    </div>
  );
}

function EmptyState({ onPick }: { onPick: (s: string) => void }) {
  return <div className="chat-hello cinematic"><div className="hello-orb"><span /></div><div className="eyebrow">Премиальный режим ассистента</div><h2>Что строим сегодня?</h2><p>Можно дать цель целиком: я разложу её на план миссии, подключу роли, проверю риски и сохраню полезные выводы в память.</p><div className="hello-grid">{SUGGESTIONS.slice(0, 4).map((s) => <button key={s} className="hello-card" onClick={() => onPick(s)}>{s}</button>)}</div><p className="hello-foot">Файлы можно просто перетащить сюда — подключу их к контексту диалога.</p></div>;
}

function MessageBubble({ m, session, epoch }: { m: ChatMessage; session: string; epoch: number }) {
  const [open, setOpen] = useState(false);
  const [why, setWhy] = useState<WhyItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const isUser = m.role === "user";
  useEffect(() => { setOpen(false); setWhy(null); }, [epoch]);
  const explain = async () => { if (why !== null) { setWhy(null); return; } setBusy(true); try { const r = await fetch(`/api/core/api/cognitive/db/episodic_memory_logs?q=${encodeURIComponent(session)}&limit=8`, { cache: "no-store" }); const d = await r.json(); setWhy((d?.data?.rows || []).map((x: any) => ({ content: String(x.content || ""), entry_type: String(x.entry_type || "") }))); } catch { setWhy([]); } setBusy(false); };
  return <div className={`msg-row ${isUser ? "user" : "assistant"}`}><div className={`bubble ${isUser ? "user" : "assistant"} ${m.error ? "error" : ""}`}>{!isUser && m.steps.length > 0 && <div className="steps"><button className="steps-toggle" onClick={() => setOpen((v) => !v)}>{open ? "▾" : "▸"} Ход выполнения ({m.steps.length})</button>{open && <div className="steps-body">{m.steps.map((s, i) => <div key={i} className={`step ${s.kind}`}><span className="step-actor" title={actorInfo(s.actor).name}>{actorInfo(s.actor).icon}</span>{" "}{s.kind === "thought" && <span>{s.text}</span>}{s.kind === "tool_call" && <span className="tool-call-summary">🔧 {s.text}</span>}{s.kind === "tool_result" && <span>{s.ok ? "✅" : "⚠️"} {formatToolName(s.tool)}: {s.text}</span>}</div>)}</div>}</div>}<div className="bubble-text">{renderContent(m.text)}</div>{m.streaming && <span className="caret">▋</span>}{!isUser && !m.streaming && m.text && m.steps.length > 0 && <div className="why-row"><button className="why-btn" onClick={explain} disabled={busy}>{busy ? "…" : why !== null ? "скрыть ▲" : "почему? ▾"}</button>{why && <div className="why-panel">{why.length === 0 && <div className="why-empty">Трасса пуста для этого диалога.</div>}{why.map((w, i) => <div key={i} className={`why-item why-${w.entry_type}`}><span className="why-type">{w.entry_type}</span> {w.content.slice(0, 240)}</div>)}</div>}</div>}</div></div>;
}

function MemoryPanel({ mem, session, onClose, onAction }: { mem: MemoryOverview | null; session: string; onClose: () => void; onAction: (a: string, extra?: Record<string, unknown>) => void }) {
  const [note, setNote] = useState("");
  return <div className="hitl-modal" onClick={onClose}><div className="hitl-card mem-card" onClick={(e) => e.stopPropagation()}><h3 style={{ marginTop: 0 }}>🧠 Память JARVIS · диалог «{session}»</h3><div className="mem-section"><strong>Оперативный контекст</strong><p style={{ color: "var(--muted)", fontSize: 13, margin: "4px 0" }}>Реплик в активном окне: {mem?.recent_count ?? "—"}. След выполнения и блок «почему» очищаются вместе со сбросом или сжатием.</p>{mem?.summary && <pre className="log-stream" style={{ height: "12vh" }}>{mem.summary}</pre>}<div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}><button className="btn" onClick={() => onAction("flush")}>📥 Сжать в сводку и скрыть «почему»</button><button className="btn danger" onClick={() => onAction("reset")}>🧹 Очистить контекст и экран</button></div></div><div className="mem-section"><strong>Долговременная память ({mem?.longterm_count ?? 0})</strong><div className="log-stream" style={{ height: "22vh", marginTop: 6 }}>{(mem?.longterm ?? []).length === 0 && <span style={{ color: "var(--muted)" }}>пусто</span>}{(mem?.longterm ?? []).map((it) => <div key={it.id} className="log-line"><span style={{ color: "var(--accent)" }}>[{it.kind}]</span> {it.text}</div>)}</div><div style={{ display: "flex", gap: 8, marginTop: 8 }}><input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Запомнить факт…" style={{ flex: 1 }} /><button className="btn" onClick={() => { if (note.trim()) { onAction("save", { text: note.trim() }); setNote(""); } }}>＋ Запомнить</button><button className="btn danger" onClick={() => onAction("clear_longterm")}>Очистить всё</button></div></div><div style={{ textAlign: "right", marginTop: 10 }}><button className="btn" onClick={onClose}>Закрыть</button></div></div></div>;
}

function renderContent(text: string) {
  if (!text) return null;
  return text.split(/```/).map((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n"); const lang = nl > -1 ? part.slice(0, nl).trim() : ""; const code = nl > -1 ? part.slice(nl + 1) : part;
      return <pre key={i} className="code-block">{lang && <span className="code-lang">{lang}</span>}<code>{code}</code></pre>;
    }
    return <span key={i} style={{ whiteSpace: "pre-wrap" }}>{part}</span>;
  });
}
function downsampleToPcm16(input: Float32Array, fromSr: number, toSr: number): Int16Array { const ratio = fromSr / toSr; const out = new Int16Array(Math.floor(input.length / ratio)); for (let i = 0; i < out.length; i++) { const s = input[Math.floor(i * ratio)]; out[i] = Math.max(-32768, Math.min(32767, s * 32768)); } return out; }
