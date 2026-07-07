"use client";
import { useCallback, useEffect, useMemo, useState } from "react";

const API = "/api/core/api/control";
const DOCKER_API = "/api/docker-cleanup";
async function postJson(path: string, body: unknown) { const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); return r.json(); }
async function getJson(path: string) { const r = await fetch(path, { cache: "no-store" }); return r.json(); }

function resultText(data: any) {
  if (!data) return "";
  const rows = Array.isArray(data.results) ? data.results : [];
  if (!rows.length) return JSON.stringify(data, null, 2);
  return rows.map((r: any) => `$ ${r.cmd}\n${r.out || ""}`).join("\n\n---\n\n");
}

function DockerCleanupPanel() {
  const [info, setInfo] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [confirmDeep, setConfirmDeep] = useState(false);
  const [confirmVolumes, setConfirmVolumes] = useState(false);
  const [compactText, setCompactText] = useState("");
  const [last, setLast] = useState<any>(null);
  const text = useMemo(() => resultText(last || info), [last, info]);
  const refresh = async () => { setBusy("scan"); try { const d = await getJson(DOCKER_API); setInfo(d); setLast(null); } finally { setBusy(""); } };
  const run = async (mode: string, body: any = {}) => { setBusy(mode); try { const d = await postJson(DOCKER_API, { mode, ...body }); setLast(d); setTimeout(refresh, 800); } finally { setBusy(""); } };
  useEffect(() => { refresh(); }, []);
  return <div className="panel"><h3>🧹 Docker storage</h3><p className="muted">Безопасная уборка не трогает jarvis-модели и named volumes. Глубокий режим чистит неиспользуемые образы и build-cache. Сжатие VHDX запускается отдельно и может остановить Docker Desktop/WSL на время операции.</p><div className="mode-actions" style={{ marginBottom: 10 }}><button className="btn" disabled={!!busy} onClick={refresh}>↻ Пересчитать</button><button className="btn primary" disabled={!!busy} onClick={() => run("safe")}>Безопасная уборка</button><label className="pill"><input type="checkbox" checked={confirmDeep} onChange={(e) => setConfirmDeep(e.target.checked)} /> глубокий режим</label><button className="btn danger" disabled={!!busy || !confirmDeep} onClick={() => run("deep", { confirm: true })}>Глубокая уборка</button></div><div className="mode-actions" style={{ marginBottom: 10 }}><label className="pill"><input type="checkbox" checked={confirmVolumes} onChange={(e) => setConfirmVolumes(e.target.checked)} /> чистить чужие неиспользуемые volumes</label><button className="btn danger" disabled={!!busy || !confirmVolumes} onClick={() => run("volumes", { confirm: true })}>Volumes без jarvis*</button><input value={compactText} onChange={(e) => setCompactText(e.target.value)} placeholder="для VHDX введите: СЖАТЬ" style={{ width: 190 }} /><button className="btn danger" disabled={!!busy || compactText !== "СЖАТЬ"} onClick={() => run("compact", { confirmText: compactText })}>Сжать docker_data.vhdx</button></div><pre className="log-stream" style={{ height: "34vh" }}>{busy ? `Выполняю: ${busy}…\n\n` : ""}{text || "ожидание данных…"}</pre></div>;
}

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
    <DockerCleanupPanel />
    <div className="panel"><h3>Конфигурация</h3><textarea value={cfg} onChange={(e) => { setCfg(e.target.value); if (cfgError) setCfgError(""); }} rows={12} style={{ width: "100%" }} />{cfgError && <p style={{ color: "var(--err)", fontSize: 13 }}>{cfgError}</p>}<p><button className="btn" disabled={!!busy} onClick={refresh}>↻ Обновить</button> <button className="btn primary" disabled={!!busy} onClick={save}>Сохранить</button></p></div>
  </div>;
}
