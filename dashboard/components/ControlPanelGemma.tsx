"use client";
import { useCallback, useEffect, useState } from "react";

const API = "/api/core/api/control";
async function postJson(path: string, body: unknown) { const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); return r.json(); }
async function getJson(path: string) { const r = await fetch(path, { cache: "no-store" }); return r.json(); }

export default function ControlPanelGemma() {
  const [overview, setOverview] = useState<any>(null);
  const [inf, setInf] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [cfg, setCfg] = useState("");
  const [cfgError, setCfgError] = useState("");
  const refresh = useCallback(async () => {
    try { const ov = await getJson(`${API}/overview`); setOverview(ov); setCfg(ov.config || ""); setCfgError(""); } catch { setOverview(null); }
    try { setInf(await getJson(`${API}/inference`)); } catch { setInf(null); }
  }, []);
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, [refresh]);
  const stack = async (action: string) => { setBusy(action); await postJson(`${API}/stack`, { action }); setBusy(""); setTimeout(refresh, 1200); };
  const mode = async (id: string) => { setBusy(id); await postJson(`${API}/inference`, { action: "apply", mode: id }); setBusy(""); setTimeout(refresh, 2200); };
  const download = async (id: string) => { setBusy(`download:${id}`); await postJson(`${API}/inference`, { action: "download", mode: id }); setBusy(""); };
  const save = async () => {
    if (!cfg.trim() || !cfg.includes("=")) { setCfgError("Пустую или некорректную конфигурацию не сохраняю. Нажмите «Обновить» или вставьте валидные KEY=VALUE строки."); return; }
    setCfgError(""); setBusy("config"); await postJson(`${API}/config`, { content: cfg }); setBusy(""); setTimeout(refresh, 800);
  };
  const services = String(overview?.services || "").split("\n").map((x) => x.trim()).filter(Boolean);
  return <div className="control-v3">
    <div className="panel control-hero"><div><div className="eyebrow">Gemma 4 Runtime</div><h2>Пульт управления</h2><p>Два режима: стабильный Mono и быстрый Turbo. Всё остальное — внутренний legacy-слой совместимости.</p></div><div className="mode-actions"><button className="btn primary" disabled={!!busy} onClick={() => stack("up")}>▶ Поднять</button><button className="btn" disabled={!!busy} onClick={() => stack("freevram")}>🧹 VRAM</button><button className="btn danger" disabled={!!busy} onClick={() => stack("down")}>⏹ Стоп</button></div></div>
    <div className="control-grid"><div className="panel"><h3>🚀 Режим Gemma 4</h3><div className="mode-cards">{Object.entries(inf?.modes || {}).map(([id, m]: any) => <div key={id} className={`mode-card ${inf?.active === id ? "active" : ""}`}><strong>{m.label}</strong><p>{m.summary}</p><small>{m.vram}</small><code>{m.model_repo}</code><div className="mode-actions"><button className="btn" disabled={!!busy} onClick={() => download(id)}>⬇ веса</button><button className="btn primary" disabled={!!busy || inf?.active === id} onClick={() => mode(id)}>Применить</button></div></div>)}</div></div><div className="panel"><h3>🧪 Readiness</h3><pre className="mini-code">python smoke_check.py{"\n"}python jarvis.py up --profile gemma4-mono --no-audio</pre></div></div>
    <div className="control-grid"><div className="panel"><h3>GPU / VRAM</h3><pre className="log-stream small-log">{overview?.gpu || "нет данных"}</pre></div><div className="panel"><h3>Сервисы</h3>{services.map((s) => <div key={s} className="service-row">{s}</div>)}</div></div>
    <div className="panel"><h3>Конфигурация</h3><textarea value={cfg} onChange={(e) => { setCfg(e.target.value); if (cfgError) setCfgError(""); }} rows={12} style={{ width: "100%" }} />{cfgError && <p style={{ color: "var(--err)", fontSize: 13 }}>{cfgError}</p>}<p><button className="btn" disabled={!!busy} onClick={refresh}>↻ Обновить</button> <button className="btn primary" disabled={!!busy} onClick={save}>Сохранить</button></p></div>
  </div>;
}
