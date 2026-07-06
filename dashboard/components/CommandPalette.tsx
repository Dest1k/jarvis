"use client";
import { useEffect, useMemo, useState } from "react";

type Command = { id: string; title: string; hint: string; keys?: string; run: () => void };

export default function CommandPalette({ onNavigate }: { onNavigate: (view: string) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");

  const commands = useMemo<Command[]>(() => [
    { id: "chat", title: "Открыть чат", hint: "Вернуться к прямому общению с JARVIS", keys: "C", run: () => onNavigate("chat") },
    { id: "ops", title: "Операционный центр", hint: "Runtime, MCP, миссии, GPU и self-heal", keys: "O", run: () => onNavigate("ops") },
    { id: "cognitive", title: "Когнитивное ядро", hint: "RAG, знания, DB state", keys: "R", run: () => onNavigate("cognitive") },
    { id: "control", title: "Пульт управления", hint: "Профили Gemma 4, стек, модели, конфиг", keys: "P", run: () => onNavigate("control") },
    { id: "monitor", title: "Мониторная", hint: "Живые логи сервисов", keys: "M", run: () => onNavigate("monitor") },
    { id: "smoke", title: "Smoke check", hint: "Команда: python smoke_check.py", run: () => navigator.clipboard?.writeText("python smoke_check.py") },
    { id: "mono", title: "Запуск Gemma 4 Mono", hint: "Команда: python jarvis.py up --profile gemma4-mono --no-audio", run: () => navigator.clipboard?.writeText("python jarvis.py up --profile gemma4-mono --no-audio") },
    { id: "turbo", title: "Запуск Gemma 4 Turbo", hint: "Команда: python jarvis.py up --profile gemma4-turbo --no-audio", run: () => navigator.clipboard?.writeText("python jarvis.py up --profile gemma4-turbo --no-audio") },
  ], [onNavigate]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setOpen((v) => !v); }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const filtered = commands.filter((c) => (c.title + c.hint + c.id).toLowerCase().includes(q.toLowerCase())).slice(0, 8);
  return <>
    <button className="cmd-chip" onClick={() => setOpen(true)}>⌘K Command</button>
    {open && <div className="cmd-backdrop" onClick={() => setOpen(false)}>
      <div className="cmd-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cmd-orb" />
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Что прикажете, сэр?" className="cmd-input" />
        <div className="cmd-list">
          {filtered.map((c) => <button key={c.id} className="cmd-row" onClick={() => { c.run(); setOpen(false); setQ(""); }}>
            <span><strong>{c.title}</strong><small>{c.hint}</small></span>{c.keys && <kbd>{c.keys}</kbd>}
          </button>)}
          {filtered.length === 0 && <div className="cmd-empty">Ничего не нашёл. Даже мне иногда приходится признать: формулировка весьма творческая.</div>}
        </div>
      </div>
    </div>}
  </>;
}
