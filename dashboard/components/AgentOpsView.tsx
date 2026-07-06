"use client";
import { useCallback, useEffect, useState } from "react";

type AnyJson = Record<string, unknown>;

const CORE = "/api/core";
const pretty = (v: unknown) => JSON.stringify(v, null, 2);

async function getJson(path: string): Promise<AnyJson> {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return await r.json();
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="panel" style={{ padding: 14, minHeight: 120 }}><h3 style={{ marginTop: 0 }}>{title}</h3>{children}</div>;
}

function Pre({ data }: { data: unknown }) {
  return <pre className="log-stream" style={{ height: 220, whiteSpace: "pre-wrap" }}>{pretty(data)}</pre>;
}

export default function AgentOpsView() {
  const [status, setStatus] = useState<AnyJson | null>(null);
  const [gpu, setGpu] = useState<AnyJson | null>(null);
  const [mcp, setMcp] = useState<AnyJson | null>(null);
  const [incidents, setIncidents] = useState<AnyJson | null>(null);
  const [skills, setSkills] = useState<AnyJson | null>(null);
  const [cognitive, setCognitive] = useState<AnyJson | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const [s, g, m, i, sk, c] = await Promise.allSettled([
        getJson(`${CORE}/status`),
        getJson(`${CORE}/api/gpu`),
        getJson(`${CORE}/api/agent/mcp`),
        getJson(`${CORE}/api/agent/incidents?limit=25`),
        getJson(`${CORE}/api/agent/skills`),
        getJson(`${CORE}/api/cognitive/health`),
      ]);
      if (s.status === "fulfilled") setStatus(s.value); else setStatus({ ok: false, error: String(s.reason) });
      if (g.status === "fulfilled") setGpu(g.value); else setGpu({ ok: false, error: String(g.reason) });
      if (m.status === "fulfilled") setMcp(m.value); else setMcp({ ok: false, error: String(m.reason) });
      if (i.status === "fulfilled") setIncidents(i.value); else setIncidents({ ok: false, error: String(i.reason) });
      if (sk.status === "fulfilled") setSkills(sk.value); else setSkills({ ok: false, error: String(sk.reason) });
      if (c.status === "fulfilled") setCognitive(c.value); else setCognitive({ ok: false, error: String(c.reason) });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 7000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <div style={{ height: "100%", overflow: "auto", padding: 16 }}>
      <div className="panel" style={{ padding: 14, marginBottom: 12, display: "flex", alignItems: "center", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>🧭 Agent Operations Center</h2>
          <div style={{ color: "var(--muted)", fontSize: 13 }}>
            Живой статус Core JARVIS, MCP, GPU, cognitive core, инцидентов и навыков. Автономия должна быть наблюдаемой, сэр.
          </div>
        </div>
        <button className="btn primary" style={{ marginLeft: "auto" }} onClick={refresh} disabled={busy}>
          {busy ? "Обновляю…" : "Обновить"}
        </button>
      </div>
      {error && <div className="mic-error">⚠ {error}</div>}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 12 }}>
        <Card title="Core / Services"><Pre data={status} /></Card>
        <Card title="GPU / VRAM"><Pre data={gpu} /></Card>
        <Card title="MCP tools"><Pre data={mcp} /></Card>
        <Card title="Cognitive Core"><Pre data={cognitive} /></Card>
        <Card title="Resolved incidents"><Pre data={incidents} /></Card>
        <Card title="Compiled skills"><Pre data={skills} /></Card>
      </div>
      <div className="panel" style={{ padding: 14, marginTop: 12 }}>
        <h3 style={{ marginTop: 0 }}>Как этим пользоваться</h3>
        <p style={{ color: "var(--muted)", lineHeight: 1.5 }}>
          Если MCP или cognitive core краснеют — сначала смотри этот экран, затем «Мониторную».
          Если GPU близко к лимитам — переходи на `--no-audio` или профиль `gemma12-tars7`.
          Если повторяющаяся ошибка попала в incidents, JARVIS будет учитывать её при следующих ходах.
        </p>
      </div>
    </div>
  );
}
