"use client";
import { useCallback, useEffect, useState } from "react";

const API = "/api/core/api/control";

type AnyJson = Record<string, any>;
interface Overview {
  services: string;
  gpu: string;
  models: string[];
  lmstudio_models: string[];
  config: string;
  bridge_connected: boolean;
}

function fmtMb(mb: number): string {
  if (!mb) return "0 МБ";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} ГБ` : `${mb} МБ`;
}

async function postJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

function Pre({ value, h = 220 }: { value: unknown; h?: number }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return <pre className="log-stream" style={{ height: h, whiteSpace: "pre-wrap" }}>{text || "—"}</pre>;
}

export default function ControlPanel() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [busy, setBusy] = useState("");
  const [logs, setLogs] = useState<{ svc: string; text: string } | null>(null);
  const [cfg, setCfg] = useState("");
  const [profiles, setProfiles] = useState<Record<string, AnyJson>>({});
  const [profileSel, setProfileSel] = useState("gemma4-mono");
  const [dlRepo, setDlRepo] = useState("");
  const [mcp, setMcp] = useState<AnyJson | null>(null);
  const [cleanup, setCleanup] = useState<AnyJson | null>(null);
  const [cleanSel, setCleanSel] = useState<{ cats: string[]; volumes: string[]; containers: string[]; models: string[]; volModels: string[] }>({ cats: [], volumes: [], containers: [], models: [], volModels: [] });
  const [cleanLog, setCleanLog] = useState("");

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${API}/overview`, { cache: "no-store" });
      const data = (await r.json()) as Overview;
      setOv(data);
      setCfg(data.config || "");
    } catch { setOv(null); }
    try {
      const p = await fetch(`${API}/profiles`, { cache: "no-store" });
      const d = await p.json();
      const ps = d.profiles || {};
      setProfiles(ps);
      if (!ps[profileSel]) setProfileSel(Object.keys(ps)[0] || "gemma4-mono");
    } catch { setProfiles({}); }
    try {
      const m = await fetch("/api/core/api/agent/mcp", { cache: "no-store" });
      setMcp(await m.json());
    } catch { setMcp(null); }
  }, [profileSel]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const stack = async (action: string) => {
    setBusy(`stack:${action}`);
    await postJson(`${API}/stack`, { action });
    setBusy("");
    setTimeout(refresh, 1500);
  };

  const serviceAction = async (service: string, action: string) => {
    setBusy(`${service}:${action}`);
    await postJson(`${API}/service`, { service, action });
    setBusy("");
    refresh();
  };

  const showLogs = async (svc: string) => {
    setLogs({ svc, text: "загрузка…" });
    const r = await fetch(`${API}/logs/${svc}?tail=240`);
    const d = await r.json();
    setLogs({ svc, text: d.out || JSON.stringify(d, null, 2) });
  };

  const saveConfig = async () => {
    setBusy("config");
    await postJson(`${API}/config`, { content: cfg });
    setBusy("");
  };

  const downloadModel = async () => {
    if (!dlRepo.trim()) return;
    setBusy("download");
    await postJson(`${API}/model`, { action: "download", repo: dlRepo.trim() });
    setBusy("");
    alert("Загрузка модели запущена на хосте. Обновите список моделей после завершения.");
  };

  const downloadProfile = async () => {
    if (!profileSel) return;
    setBusy("profile-download");
    await postJson(`${API}/profile`, { action: "download", profile: profileSel });
    setBusy("");
    alert("Загрузка моделей профиля запущена. После завершения примените профиль.");
  };

  const applyProfile = async () => {
    if (!profileSel) return;
    if (!confirm(`Применить профиль «${profiles[profileSel]?.label || profileSel}»? Dispatcher будет пересоздан.`)) return;
    setBusy("profile-apply");
    await postJson(`${API}/profile`, { action: "apply", profile: profileSel });
    setBusy("");
    alert("Профиль применяется. Следите за прогревом в «Мониторной».");
    setTimeout(refresh, 2500);
  };

  const services = (ov?.services || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const [name, state, ...rest] = l.split("|");
    return { name, state, status: rest.join("|") };
  });
  const svcKey = (name: string) => name.replace("jarvis-", "").replace("vllm-", "").replace("-layer", "");

  const tog = (arr: string[], v: string) => arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v];
  const selectedMb = (() => {
    if (!cleanup) return 0;
    let mb = 0;
    for (const m of cleanSel.models) mb += cleanup.model_sizes_mb?.[m] || 0;
    for (const n of cleanSel.volModels) mb += (cleanup.vol_models?.find((v: any) => v.name === n)?.mb) || 0;
    if (cleanSel.cats.includes("hf_cache")) mb += cleanup.hf_cache_mb || 0;
    return mb;
  })();

  const cleanupCheck = async () => {
    setBusy("clean-check");
    setCleanup(null);
    setCleanLog("");
    setCleanSel({ cats: [], volumes: [], containers: [], models: [], volModels: [] });
    try {
      const r = await fetch(`${API}/cleanup`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "check" }) });
      setCleanup(await r.json());
    } catch { setCleanup({ ok: false, notes: ["Не удалось связаться с ядром/мостом."] }); }
    setBusy("");
  };

  const cleanupClean = async () => {
    const total = cleanSel.cats.length + cleanSel.volumes.length + cleanSel.containers.length + cleanSel.models.length + cleanSel.volModels.length;
    if (total === 0) { alert("Ничего не выбрано."); return; }
    if (!confirm(`Удалить выбранное (${total} поз., ~${fmtMb(selectedMb)})? Необратимо.`)) return;
    setBusy("clean-do");
    const d = await postJson(`${API}/cleanup`, { action: "clean", categories: cleanSel.cats, volumes: cleanSel.volumes, containers: cleanSel.containers, models: cleanSel.models, vol_models: cleanSel.volModels });
    setBusy("");
    setCleanLog(String(d.log || "Готово."));
    cleanupCheck();
  };

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="panel" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <strong>🛠️ Пульт управления</strong>
        <span className={`status-dot ${ov?.bridge_connected ? "ok" : "err"}`} />
        <span style={{ fontSize: 12, color: "var(--muted)" }}>RPC-мост: {ov?.bridge_connected ? "подключён" : "нет"}</span>
        <button className="btn" style={{ marginLeft: "auto" }} onClick={refresh}>↻ Обновить</button>
      </div>

      <div className="panel" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong>Стек целиком</strong>
        <button className="btn primary" disabled={!!busy} onClick={() => stack("up")}>▶ Поднять всё</button>
        <button className="btn danger" disabled={!!busy} onClick={() => stack("down")}>⏹ Остановить всё</button>
        <button className="btn" disabled={!!busy} onClick={() => stack("restart")}>↻ Рестарт</button>
        <button className="btn" disabled={!!busy} onClick={() => stack("freevram")}>🧹 VRAM</button>
        <button className="btn" disabled={!!busy} onClick={() => stack("build")}>🔨 Пересобрать</button>
      </div>

      <div className="panel"><strong>GPU / VRAM</strong><Pre value={ov?.gpu || "нет данных"} h={150} /></div>

      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>🎛️ Gemma 4 profiles</strong>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <select className="btn" style={{ minWidth: 320 }} value={profileSel} onChange={(e) => setProfileSel(e.target.value)}>
            {Object.entries(profiles).map(([id, p]) => <option key={id} value={id}>{(p as any).label || id}</option>)}
          </select>
          <button className="btn" disabled={!!busy || !profileSel} onClick={downloadProfile}>⬇ Скачать профиль</button>
          <button className="btn primary" disabled={!!busy || !profileSel} onClick={applyProfile}>✅ Применить профиль</button>
        </div>
        {profileSel && profiles[profileSel] && <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 8 }}><div>VRAM: {(profiles[profileSel] as any).vram}</div><div>{(profiles[profileSel] as any).note}</div></div>}
        <p style={{ fontSize: 12, color: "var(--muted)" }}>Активны только `gemma4-mono` и `gemma4-turbo`: единый Gemma dispatcher для диалога, кода, vision и GUI reasoning.</p>
      </div>

      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>Сервисы</strong>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}><tbody>
          {services.length === 0 && <tr><td style={{ color: "var(--muted)" }}>Нет данных.</td></tr>}
          {services.map((s) => <tr key={s.name} style={{ borderTop: "1px solid var(--border)" }}>
            <td style={{ padding: "6px 4px" }}><span className={`status-dot ${s.state === "running" ? "ok" : "err"}`} />{s.name}</td>
            <td style={{ color: "var(--muted)" }}>{s.status}</td>
            <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
              <button className="btn" onClick={() => serviceAction(svcKey(s.name), "restart")} disabled={!!busy}>↻</button>{" "}
              <button className="btn" onClick={() => serviceAction(svcKey(s.name), "stop")} disabled={!!busy}>⏹</button>{" "}
              <button className="btn" onClick={() => serviceAction(svcKey(s.name), "start")} disabled={!!busy}>▶</button>{" "}
              <button className="btn" onClick={() => showLogs(svcKey(s.name))}>📜</button>
            </td>
          </tr>)}
        </tbody></table>
      </div>

      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>Модели в data/models</strong>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>{(ov?.models || []).map((m) => <span key={m} className="btn" style={{ cursor: "default" }}>{m}</span>)}</div>
        <div style={{ display: "flex", gap: 6 }}>
          <input value={dlRepo} onChange={(e) => setDlRepo(e.target.value)} placeholder="HF repo Gemma 4…" style={{ flex: 1, padding: "8px 10px", borderRadius: 8, background: "#05080d", border: "1px solid var(--border)", color: "var(--text)" }} />
          <button className="btn" onClick={downloadModel} disabled={!!busy}>⬇ Скачать</button>
        </div>
      </div>

      <div className="panel"><strong>MCP</strong><Pre value={mcp || {}} h={180} /></div>

      <div className="panel">
        <strong>Конфигурация wsl/.env</strong>
        <textarea value={cfg} onChange={(e) => setCfg(e.target.value)} style={{ width: "100%", height: "28vh", marginTop: 8, background: "#05080d", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8, padding: 10, fontFamily: "monospace" }} />
        <div style={{ textAlign: "right", marginTop: 8 }}><button className="btn primary" onClick={saveConfig} disabled={!!busy}>💾 Сохранить</button></div>
      </div>

      <div className="panel">
        <strong>🧹 Чистильщик</strong>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}><button className="btn" onClick={cleanupCheck} disabled={!!busy}>Проверить</button><button className="btn danger" onClick={cleanupClean} disabled={!!busy || !cleanup}>Удалить выбранное (~{fmtMb(selectedMb)})</button></div>
        {cleanup && <Pre value={cleanup} h={180} />}
        {cleanLog && <Pre value={cleanLog} h={140} />}
        {cleanup?.models?.length > 0 && <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>{cleanup.models.map((m: string) => <button key={m} className={`btn ${cleanSel.models.includes(m) ? "danger" : ""}`} onClick={() => setCleanSel((s) => ({ ...s, models: tog(s.models, m) }))}>{m}</button>)}</div>}
      </div>

      {logs && <div className="hitl-modal" onClick={() => setLogs(null)}><div className="hitl-card" onClick={(e) => e.stopPropagation()}><h3>Логи: {logs.svc}</h3><Pre value={logs.text} h={500} /><div style={{ textAlign: "right", marginTop: 8 }}><button className="btn" onClick={() => setLogs(null)}>Закрыть</button></div></div></div>}
    </div>
  );
}
