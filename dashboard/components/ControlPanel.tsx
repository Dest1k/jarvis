"use client";
/**
 * ControlPanel.tsx — Пульт управления JARVIS-OS («управление всем из дашборда»).
 * Управление сервисами (старт/стоп/рестарт/логи), GPU/VRAM, моделями (просмотр,
 * замена, докачка) и LM Studio (список/загрузка/выгрузка), редактор конфигурации.
 * Команды идут на хост через backend → защищённый RPC-мост.
 */
import { useCallback, useEffect, useState } from "react";

const API = "/api/core/api/control";

interface BrainPreset {
  label: string; repo: string; name: string;
  quant: string; dtype: string; gpu_util: string; max_len: string; note?: string;
}

// Пресеты быстрой замены «мозга»-диспетчера. Профили VRAM ориентировочные —
// при OOM снизьте util/max-len в полях ниже или в редакторе .env.
const BRAIN_PRESETS: BrainPreset[] = [
  { label: "Qwen2.5-Coder-14B · AWQ (текущий)",
    repo: "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ", name: "qwen-coder-14b",
    quant: "awq_marlin", dtype: "half", gpu_util: "0.45", max_len: "16384" },
  { label: "Qwen2.5-Coder-32B · AWQ (умнее, тяжелее)",
    repo: "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ", name: "qwen-coder-32b",
    quant: "awq_marlin", dtype: "half", gpu_util: "0.60", max_len: "16384",
    note: "~17–18 ГБ. Рядом с UI-TARS+аудио — впритык; при OOM снизьте util до 0.55 / max-len до 12288." },
  { label: "Gemma-2-9B-it · FP16 (лёгкая, gated)",
    repo: "google/gemma-2-9b-it", name: "gemma-2-9b",
    quant: "none", dtype: "bfloat16", gpu_util: "0.62", max_len: "8192",
    note: "Gated-модель: нужен HF-токен в hf_token.txt. Контекст 8k. Без AWQ (fp16)." },
  { label: "Своя модель…",
    repo: "", name: "", quant: "awq_marlin", dtype: "half", gpu_util: "0.45", max_len: "16384",
    note: "Вставьте HF-repo и подберите квантование/тип/util под вашу VRAM (32 ГБ — резерв ~9 ГБ под UI-TARS+аудио+рабочий стол)." },
];

const fieldStyle: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 3, fontSize: 12, color: "var(--muted)",
};

interface Overview {
  services: string;
  gpu: string;
  models: string[];
  lmstudio_models: string[];
  config: string;
  bridge_connected: boolean;
}

async function postJson(url: string, body: unknown) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export default function ControlPanel() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [busy, setBusy] = useState("");
  const [logs, setLogs] = useState<{ svc: string; text: string } | null>(null);
  const [cfg, setCfg] = useState("");
  const [dlRepo, setDlRepo] = useState("");
  const [lmsModel, setLmsModel] = useState("");
  const [brain, setBrain] = useState<BrainPreset>(BRAIN_PRESETS[0]);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${API}/overview`, { cache: "no-store" });
      const data = (await r.json()) as Overview;
      setOv(data);
      setCfg(data.config || "");
    } catch {
      setOv(null);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const serviceAction = async (service: string, action: string) => {
    setBusy(`${service}:${action}`);
    await postJson(`${API}/service`, { service, action });
    setBusy("");
    refresh();
  };

  const showLogs = async (svc: string) => {
    setLogs({ svc, text: "загрузка…" });
    const r = await fetch(`${API}/logs/${svc}?tail=200`);
    const d = await r.json();
    setLogs({ svc, text: d.out || JSON.stringify(d) });
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
    alert("Загрузка запущена на хосте (отдельное окно). Обновите список моделей позже.");
  };

  const setModel = async (service: string, model_path: string) => {
    if (!model_path) return;
    setBusy(`set:${service}`);
    await postJson(`${API}/model`, { action: "set", service, model_path });
    setBusy("");
    refresh();
  };

  // --- Быстрая замена «мозга»-диспетчера (qwen) ---
  const downloadBrain = async () => {
    if (!brain.repo.trim()) { alert("Укажите HF-repo модели."); return; }
    setBusy("brain-dl");
    await postJson(`${API}/model`, {
      action: "download", repo: brain.repo.trim(),
      name: brain.name.trim() || undefined,
    });
    setBusy("");
    alert("Загрузка модели запущена на хосте (отдельное окно с прогрессом). " +
      "Дождитесь завершения, затем нажмите «Применить как диспетчер».");
  };

  const applyBrain = async () => {
    const name = brain.name.trim() || brain.repo.trim().split("/").pop() || "";
    if (!name) { alert("Укажите имя папки модели или HF-repo."); return; }
    if (!confirm(`Заменить диспетчер на /models/${name} (${brain.quant}, ${brain.dtype}, ` +
      `util ${brain.gpu_util}, max-len ${brain.max_len})? Сервис будет пересоздан.`)) return;
    setBusy("brain-apply");
    await postJson(`${API}/model`, {
      action: "set", service: "qwen", model_path: `/models/${name}`,
      quantization: brain.quant, dtype: brain.dtype,
      gpu_util: brain.gpu_util, max_len: brain.max_len,
    });
    setBusy("");
    alert("Применено. Диспетчер пересоздаётся с новой моделью — следите за логами " +
      "в «Мониторной» (сервис Qwen). Первый старт может занять несколько минут.");
    setTimeout(refresh, 2000);
  };

  const lms = async (action: string, model?: string) => {
    setBusy(`lms:${action}`);
    await postJson(`${API}/lmstudio`, { action, model });
    setBusy("");
  };

  const stack = async (action: string) => {
    setBusy(`stack:${action}`);
    await postJson(`${API}/stack`, { action });
    setBusy("");
    setTimeout(refresh, 1500);
  };

  const services = (ov?.services || "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const [name, state, ...rest] = l.split("|");
      return { name, state, status: rest.join("|") };
    });

  const svcKey = (name: string) =>
    name.replace("jarvis-", "").replace("vllm-", "").replace("-coder", "")
      .replace("-layer", "").replace("ui-tars", "uitars").replace("qwen", "qwen");

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="panel" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <strong>🛠️ Пульт управления</strong>
        <span className={`status-dot ${ov?.bridge_connected ? "ok" : "err"}`} />
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          RPC-мост: {ov?.bridge_connected ? "подключён" : "нет (запустите windows_rpc_bridge.py)"}
        </span>
        <button className="btn" style={{ marginLeft: "auto" }} onClick={refresh}>↻ Обновить</button>
      </div>

      {/* Стек целиком */}
      <div className="panel" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong>Стек целиком</strong>
        <button className="btn primary" disabled={!!busy} onClick={() => stack("up")}>▶ Поднять всё</button>
        <button className="btn danger" disabled={!!busy} onClick={() => stack("down")}>⏹ Остановить всё</button>
        <button className="btn" disabled={!!busy} onClick={() => stack("restart")}>↻ Рестарт всех</button>
        <button className="btn" disabled={!!busy} onClick={() => stack("build")}>🔨 Пересобрать образы</button>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          «Поднять»/«Пересобрать» — в отдельном окне на хосте.
        </span>
      </div>

      {/* GPU / VRAM */}
      <div className="panel">
        <strong>GPU / VRAM</strong>
        <pre style={{ margin: "8px 0 0", color: "var(--accent)" }}>
          {ov?.gpu || "нет данных (нужен RPC-мост)"}
        </pre>
      </div>

      {/* Сервисы */}
      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>Сервисы</strong>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <tbody>
            {services.length === 0 && (
              <tr><td style={{ color: "var(--muted)" }}>Нет данных (нужен RPC-мост и поднятый стек).</td></tr>
            )}
            {services.map((s) => (
              <tr key={s.name} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ padding: "6px 4px" }}>
                  <span className={`status-dot ${s.state === "running" ? "ok" : "err"}`} />
                  {s.name}
                </td>
                <td style={{ color: "var(--muted)" }}>{s.status}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn" onClick={() => serviceAction(svcKey(s.name), "restart")}
                          disabled={!!busy}>↻ Рестарт</button>{" "}
                  <button className="btn" onClick={() => serviceAction(svcKey(s.name), "stop")}
                          disabled={!!busy}>⏹ Стоп</button>{" "}
                  <button className="btn" onClick={() => serviceAction(svcKey(s.name), "start")}
                          disabled={!!busy}>▶ Старт</button>{" "}
                  <button className="btn" onClick={() => showLogs(svcKey(s.name))}>📜 Логи</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Модели */}
      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>Модели (локальные, в data/models)</strong>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
          {(ov?.models || []).map((m) => <span key={m} className="btn" style={{ cursor: "default" }}>{m}</span>)}
          {(!ov || ov.models.length === 0) && <span style={{ color: "var(--muted)" }}>нет данных</span>}
        </div>
        <div style={{ display: "grid", gap: 6 }}>
          {(["qwen", "uitars"] as const).map((svc) => (
            <div key={svc} style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span style={{ width: 70 }}>{svc}:</span>
              <select id={`m-${svc}`} className="btn" style={{ minWidth: 200 }} defaultValue="">
                <option value="">— назначить модель —</option>
                {(ov?.models || []).map((m) => (
                  <option key={m} value={`/models/${m}`}>{`/models/${m}`}</option>
                ))}
              </select>
              <button className="btn primary" disabled={!!busy}
                      onClick={() => setModel(svc, (document.getElementById(`m-${svc}`) as HTMLSelectElement)?.value)}>
                Применить + пересоздать
              </button>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
          <input value={dlRepo} onChange={(e) => setDlRepo(e.target.value)}
                 placeholder="HF repo, напр. Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
                 style={{ flex: 1, padding: "8px 10px", borderRadius: 8, background: "#05080d",
                          border: "1px solid var(--border)", color: "var(--text)" }} />
          <button className="btn" onClick={downloadModel} disabled={!!busy}>⬇ Скачать</button>
        </div>
      </div>

      {/* Быстрая замена мозга-диспетчера */}
      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>
          🧠 Мозг-диспетчер — быстрая замена модели (Qwen → другая)
        </strong>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <select className="btn" style={{ minWidth: 300 }} value={brain.label}
                  onChange={(e) => {
                    const p = BRAIN_PRESETS.find((x) => x.label === e.target.value);
                    if (p) setBrain({ ...p });
                  }}>
            {BRAIN_PRESETS.map((p) => <option key={p.label} value={p.label}>{p.label}</option>)}
          </select>
        </div>
        {brain.note && (
          <p style={{ fontSize: 12, color: "var(--muted)", margin: "6px 0 0" }}>ℹ {brain.note}</p>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))",
                      gap: 8, marginTop: 10 }}>
          <label style={fieldStyle}>HF-repo
            <input value={brain.repo} onChange={(e) => setBrain({ ...brain, repo: e.target.value })}
                   placeholder="org/model" />
          </label>
          <label style={fieldStyle}>Имя папки
            <input value={brain.name} onChange={(e) => setBrain({ ...brain, name: e.target.value })}
                   placeholder="в data/models" />
          </label>
          <label style={fieldStyle}>Квантование
            <select value={brain.quant} onChange={(e) => setBrain({ ...brain, quant: e.target.value })}>
              <option value="awq_marlin">awq_marlin</option>
              <option value="awq">awq</option>
              <option value="gptq">gptq</option>
              <option value="none">none (fp16)</option>
            </select>
          </label>
          <label style={fieldStyle}>dtype
            <select value={brain.dtype} onChange={(e) => setBrain({ ...brain, dtype: e.target.value })}>
              <option value="half">half (fp16)</option>
              <option value="bfloat16">bfloat16</option>
              <option value="auto">auto</option>
            </select>
          </label>
          <label style={fieldStyle}>gpu util
            <input value={brain.gpu_util} onChange={(e) => setBrain({ ...brain, gpu_util: e.target.value })} />
          </label>
          <label style={fieldStyle}>max-len
            <input value={brain.max_len} onChange={(e) => setBrain({ ...brain, max_len: e.target.value })} />
          </label>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn" disabled={!!busy} onClick={downloadBrain}>⬇ 1. Скачать модель</button>
          <button className="btn primary" disabled={!!busy} onClick={applyBrain}>✅ 2. Применить как диспетчер</button>
          <span style={{ fontSize: 12, color: "var(--muted)" }}>
            Сначала «Скачать» (идёт в отдельном окне на хосте) → дождаться → затем «Применить».
          </span>
        </div>
      </div>

      {/* LM Studio */}
      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>LM Studio (мозг установки)</strong>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <select className="btn" style={{ minWidth: 220 }} value={lmsModel}
                  onChange={(e) => setLmsModel(e.target.value)}>
            <option value="">— модель LM Studio —</option>
            {(ov?.lmstudio_models || []).map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <button className="btn primary" disabled={!!busy || !lmsModel}
                  onClick={() => lms("load", lmsModel)}>Загрузить</button>
          <button className="btn danger" disabled={!!busy}
                  onClick={() => lms("unload")}>Выгрузить всё</button>
          <span style={{ fontSize: 12, color: "var(--muted)" }}>
            (для управления нужен CLI <code>lms</code> на хосте)
          </span>
        </div>
      </div>

      {/* Конфигурация */}
      <div className="panel">
        <strong style={{ display: "block", marginBottom: 8 }}>Конфигурация (wsl/.env)</strong>
        <textarea value={cfg} onChange={(e) => setCfg(e.target.value)} spellCheck={false}
                  style={{ width: "100%", height: "22vh", fontFamily: "monospace", fontSize: 12.5,
                           background: "#05080d", color: "var(--text)", border: "1px solid var(--border)",
                           borderRadius: 8, padding: 10 }} />
        <div style={{ marginTop: 8 }}>
          <button className="btn primary" onClick={saveConfig} disabled={!!busy}>💾 Сохранить .env</button>
          <span style={{ fontSize: 12, color: "var(--muted)", marginLeft: 10 }}>
            После сохранения пересоздайте соответствующие сервисы (кнопка «Рестарт»).
          </span>
        </div>
      </div>

      {/* Модал логов */}
      {logs && (
        <div className="hitl-modal" onClick={() => setLogs(null)}>
          <div className="hitl-card" style={{ maxWidth: "80vw", width: 900 }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginTop: 0 }}>Логи: {logs.svc}</h3>
            <pre className="log-stream" style={{ height: "55vh" }}>{logs.text}</pre>
            <div style={{ textAlign: "right", marginTop: 8 }}>
              <button className="btn" onClick={() => setLogs(null)}>Закрыть</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
