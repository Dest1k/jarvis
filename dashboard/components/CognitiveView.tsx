"use client";
/**
 * CognitiveView.tsx — вкладка «🧠 Разум» Command Center.
 *
 * Живой интерфейс к «Когнитивному ядру» (/api/cognitive/*):
 *   • Поток        — Past/Present/Future (Успехи/Провалы, что делает сейчас,
 *                    очередь обучения), автообновление.
 *   • Знания       — Humanized DB Explorer: граф навыков/правил, клик по узлу →
 *                    детали + история версий, live-edit / архив / откат.
 *   • Настройки    — Settings & Prompts Editor: hot-swap (БД перекрывает файл),
 *                    сброс к файловому дефолту.
 *   • Админ БД     — spreadsheet-браузер любой (whitelisted) таблицы.
 *
 * UX-реактивность (требование): у каждого действия — явные состояния
 * loading/success/error (спиннеры, дизейбл кнопок, тосты).
 */
import { useCallback, useEffect, useRef, useState } from "react";

const API = "/api/core/api/cognitive";

type ActionState = "idle" | "loading" | "processing" | "success" | "error";

async function api(path: string, opts?: RequestInit): Promise<any> {
  const r = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...opts,
  });
  return r.json();
}

/** Индикатор состояния действия (спиннер/галочка/крест). */
function StateDot({ s }: { s: ActionState }) {
  if (s === "loading" || s === "processing")
    return <span className="actor-spin" title="выполняется" />;
  if (s === "success") return <span style={{ color: "var(--ok, #3ad07a)" }}>✓</span>;
  if (s === "error") return <span style={{ color: "var(--err, #ff6b6b)" }}>✕</span>;
  return null;
}

type Sub = "stream" | "knowledge" | "settings" | "admin";

export default function CognitiveView() {
  const [sub, setSub] = useState<Sub>("stream");
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div className="panel" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong>🧠 Разум</strong>
        {([
          ["stream", "🌊 Поток"],
          ["knowledge", "🕸 Знания"],
          ["settings", "⚙️ Настройки"],
          ["admin", "🗃 Админ БД"],
        ] as [Sub, string][]).map(([id, label]) => (
          <button key={id} className={`btn ${sub === id ? "primary" : ""}`}
                  onClick={() => setSub(id)}>{label}</button>
        ))}
      </div>
      {sub === "stream" && <StreamPanel />}
      {sub === "knowledge" && <KnowledgePanel />}
      {sub === "settings" && <SettingsPanel />}
      {sub === "admin" && <AdminPanel />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Поток: Past / Present / Future (автообновление)                     */
/* ------------------------------------------------------------------ */
function StreamPanel() {
  const [data, setData] = useState<any>(null);
  const [conn, setConn] = useState<ActionState>("loading");
  const timer = useRef<any>(null);

  const tick = useCallback(async () => {
    try {
      const r = await api("/state");
      setData(r.data); setConn("success");
    } catch { setConn("error"); }
  }, []);

  useEffect(() => {
    tick();
    timer.current = setInterval(tick, 2500);
    return () => clearInterval(timer.current);
  }, [tick]);

  const past = data?.past ?? { successes: 0, failures: 0 };
  const present = data?.present ?? {};
  const future = data?.future?.learning_queue ?? [];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))", gap: 12 }}>
      <div className="panel">
        <strong>Прошлое <StateDot s={conn} /></strong>
        <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
          <div><div style={{ fontSize: 28, color: "var(--ok,#3ad07a)" }}>{past.successes}</div>
            <div style={{ fontSize: 12, color: "var(--muted)" }}>успехов</div></div>
          <div><div style={{ fontSize: 28, color: "var(--err,#ff6b6b)" }}>{past.failures}</div>
            <div style={{ fontSize: 12, color: "var(--muted)" }}>провалов</div></div>
        </div>
      </div>
      <div className="panel">
        <strong>Настоящее</strong>
        <div style={{ marginTop: 8 }}>
          <div>Состояние: <b style={{ color: "var(--accent)" }}>{present.state ?? "—"}</b></div>
          {present.active_goal && <div style={{ fontSize: 12, marginTop: 4 }}>Цель: {present.active_goal}</div>}
          {present.last && <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
            {present.last.entry_type}: {String(present.last.content).slice(0, 120)}</div>}
        </div>
      </div>
      <div className="panel">
        <strong>Будущее (очередь обучения)</strong>
        <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 13 }}>
          {future.length === 0 && <li style={{ color: "var(--muted)", listStyle: "none" }}>очередь пуста</li>}
          {future.map((t: any) => (
            <li key={t.id}>{t.title} <span style={{ color: "var(--muted)" }}>({Math.round((t.progress || 0) * 100)}%)</span></li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Знания: DB Explorer (граф → узел → версии → edit/archive/rollback)  */
/* ------------------------------------------------------------------ */
function KnowledgePanel() {
  const [nodes, setNodes] = useState<any[]>([]);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<any>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [busy, setBusy] = useState<ActionState>("idle");
  const [toast, setToast] = useState("");

  const load = useCallback(async () => {
    setBusy("loading");
    try {
      const r = await api(`/graph?q=${encodeURIComponent(q)}&limit=100`);
      setNodes(r.data?.nodes ?? []); setBusy("success");
    } catch { setBusy("error"); }
  }, [q]);
  useEffect(() => { load(); }, [load]);

  const open = async (id: string) => {
    setBusy("loading");
    const r = await api(`/graph/${id}`);
    setSel(r.data?.node ?? null); setVersions(r.data?.versions ?? []); setBusy("success");
  };
  const save = async () => {
    if (!sel) return;
    setBusy("processing");
    const r = await api(`/graph/${sel.id}`, { method: "PUT", body: JSON.stringify({ title: sel.title, body: sel.body }) });
    setBusy(r.ok ? "success" : "error"); setToast(r.ok ? "Сохранено" : r.error || "Ошибка");
    load();
  };
  const archive = async () => {
    if (!sel) return;
    setBusy("processing");
    const r = await api(`/graph/${sel.id}`, { method: "DELETE" });
    setBusy(r.ok ? "success" : "error"); setToast(r.ok ? "В архив" : "Ошибка");
    setSel(null); load();
  };
  const rollback = async (audit_id: number) => {
    setBusy("processing");
    const r = await api(`/audit/rollback`, { method: "POST", body: JSON.stringify({ audit_id }) });
    setToast(r.ok ? "Откат выполнен" : r.error || "Ошибка"); setBusy(r.ok ? "success" : "error");
    if (sel) open(sel.id);
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 12 }}>
      <div className="panel">
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="поиск по знаниям…"
                 style={{ flex: 1, padding: "6px 8px", background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }} />
          <button className="btn" onClick={load} disabled={busy === "loading"}>↻ <StateDot s={busy} /></button>
        </div>
        <div style={{ maxHeight: "60vh", overflow: "auto" }}>
          {nodes.map((n) => (
            <div key={n.id} onClick={() => open(n.id)}
                 style={{ padding: "6px 8px", cursor: "pointer", borderTop: "1px solid var(--border)",
                          background: sel?.id === n.id ? "rgba(80,200,255,.08)" : "transparent" }}>
              <span className={`status-dot ${n.status === "active" ? "ok" : n.status === "rejected" ? "err" : "warn"}`} />
              <b style={{ fontSize: 13 }}>{n.title}</b>
              <span style={{ fontSize: 11, color: "var(--muted)" }}> · {n.kind} · v{n.version} · imp {Number(n.importance).toFixed(2)}</span>
            </div>
          ))}
          {nodes.length === 0 && <div style={{ color: "var(--muted)", padding: 8 }}>пусто</div>}
        </div>
      </div>
      <div className="panel">
        {!sel && <div style={{ color: "var(--muted)" }}>Выберите узел слева для просмотра и правки.</div>}
        {sel && (
          <div style={{ display: "grid", gap: 8 }}>
            <input value={sel.title} onChange={(e) => setSel({ ...sel, title: e.target.value })}
                   style={{ padding: 8, background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)", fontWeight: 600 }} />
            <textarea value={sel.body} onChange={(e) => setSel({ ...sel, body: e.target.value })}
                      style={{ minHeight: 120, padding: 8, background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)", fontFamily: "monospace", fontSize: 13 }} />
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button className="btn primary" onClick={save} disabled={busy === "processing"}>💾 Сохранить <StateDot s={busy} /></button>
              <button className="btn danger" onClick={archive} disabled={busy === "processing"}>🗄 В архив</button>
              <span style={{ fontSize: 12, color: "var(--muted)" }}>{toast}</span>
            </div>
            <div>
              <strong style={{ fontSize: 13 }}>История версий (клик = откат):</strong>
              <div style={{ maxHeight: "22vh", overflow: "auto", marginTop: 4 }}>
                {versions.map((v) => (
                  <div key={v.id} style={{ display: "flex", gap: 8, fontSize: 12, padding: "3px 0", borderTop: "1px solid var(--border)" }}>
                    <span style={{ color: "var(--muted)" }}>#{v.id} {v.op} · {v.actor}</span>
                    <button className="btn" style={{ marginLeft: "auto", padding: "1px 8px" }}
                            onClick={() => rollback(v.id)} title="откатить к этому состоянию">↩ откат</button>
                  </div>
                ))}
                {versions.length === 0 && <div style={{ color: "var(--muted)" }}>нет правок</div>}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Настройки: Settings & Prompts Editor (DB override + reset)          */
/* ------------------------------------------------------------------ */
function SettingsPanel() {
  const [rows, setRows] = useState<any[]>([]);
  const [busy, setBusy] = useState<ActionState>("idle");
  const [edit, setEdit] = useState<Record<string, string>>({});
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");

  const load = useCallback(async () => {
    setBusy("loading");
    try { const r = await api("/settings"); setRows(r.data?.settings ?? []); setBusy("success"); }
    catch { setBusy("error"); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (key: string, value: string) => {
    setBusy("processing");
    const r = await api("/settings", { method: "POST", body: JSON.stringify({ key, value }) });
    setBusy(r.ok ? "success" : "error"); load();
  };
  const reset = async (key: string) => {
    setBusy("processing");
    await api("/settings/reset", { method: "POST", body: JSON.stringify({ key }) });
    setBusy("success"); load();
  };
  const create = async () => {
    if (!newKey.trim()) return;
    await save(newKey.trim(), newVal); setNewKey(""); setNewVal("");
  };

  return (
    <div className="panel">
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <strong>⚙️ Настройки и промпты</strong> <StateDot s={busy} />
        <span style={{ fontSize: 12, color: "var(--muted)" }}>БД перекрывает файловый конфиг в рантайме</span>
        <button className="btn" style={{ marginLeft: "auto" }} onClick={load}>↻ Обновить</button>
      </div>
      <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
        <thead><tr style={{ textAlign: "left", color: "var(--muted)" }}>
          <th>Ключ</th><th>Значение</th><th>Источник</th><th></th></tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.key} style={{ borderTop: "1px solid var(--border)" }}>
              <td style={{ padding: "4px 6px" }}><code>{s.key}</code></td>
              <td>
                <input defaultValue={String(s.value)} onChange={(e) => setEdit({ ...edit, [s.key]: e.target.value })}
                       style={{ width: "100%", padding: "4px 6px", background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }} />
              </td>
              <td style={{ color: s.is_active ? "var(--accent)" : "var(--muted)" }}>{s.is_active ? "БД (override)" : "файл"}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="btn primary" onClick={() => save(s.key, edit[s.key] ?? String(s.value))} disabled={busy === "processing"}>💾</button>{" "}
                <button className="btn" onClick={() => reset(s.key)} title="сбросить к файловому дефолту">↺ файл</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
        <input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="новый ключ (напр. agent.max_steps)"
               style={{ flex: 1, padding: "6px 8px", background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }} />
        <input value={newVal} onChange={(e) => setNewVal(e.target.value)} placeholder="значение"
               style={{ flex: 1, padding: "6px 8px", background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }} />
        <button className="btn primary" onClick={create} disabled={busy === "processing"}>+ Добавить</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Админ БД: spreadsheet-браузер whitelisted таблиц                    */
/* ------------------------------------------------------------------ */
function AdminPanel() {
  const [tables, setTables] = useState<any[]>([]);
  const [table, setTable] = useState("");
  const [data, setData] = useState<any>(null);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<ActionState>("idle");

  useEffect(() => {
    (async () => {
      const r = await api("/db/tables");
      setTables(r.data?.tables ?? []);
      if (r.data?.tables?.length) setTable(r.data.tables[0].table);
    })();
  }, []);

  const load = useCallback(async () => {
    if (!table) return;
    setBusy("loading");
    try { const r = await api(`/db/${table}?q=${encodeURIComponent(q)}&limit=100`); setData(r.data); setBusy("success"); }
    catch { setBusy("error"); }
  }, [table, q]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="panel">
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
        <strong>🗃 Админ БД</strong> <StateDot s={busy} />
        <select className="btn" value={table} onChange={(e) => setTable(e.target.value)}>
          {tables.map((t) => <option key={t.table} value={t.table}>{t.table} ({t.rows})</option>)}
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="поиск…"
               style={{ padding: "6px 8px", background: "#05080d", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }} />
        <button className="btn" onClick={load}>↻</button>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>всего: {data?.total ?? 0}</span>
      </div>
      <div style={{ overflow: "auto", maxHeight: "64vh" }}>
        <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
          <thead><tr>
            {(data?.columns ?? []).map((c: string) => (
              <th key={c} style={{ textAlign: "left", padding: "4px 6px", color: "var(--muted)", position: "sticky", top: 0, background: "var(--panel,#0b0f16)" }}>{c}</th>
            ))}
          </tr></thead>
          <tbody>
            {(data?.rows ?? []).map((row: any, i: number) => (
              <tr key={i} style={{ borderTop: "1px solid var(--border)" }}>
                {(data?.columns ?? []).map((c: string) => (
                  <td key={c} style={{ padding: "3px 6px", maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={String(row[c] ?? "")}>{String(row[c] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
