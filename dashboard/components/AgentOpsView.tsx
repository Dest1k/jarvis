"use client";
import { useCallback, useEffect, useState, type ReactNode } from "react";

type AnyJson = Record<string, unknown>;

const CORE = "/api/core";
const pretty = (v: unknown) => JSON.stringify(v, null, 2);

async function getJson(path: string): Promise<AnyJson> {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return await r.json();
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return <div className="panel" style={{ padding: 14, minHeight: 120 }}><h3 style={{ marginTop: 0 }}>{title}</h3>{children}</div>;
}

function Pre({ data, h = 220 }: { data: unknown; h?: number }) {
  return <pre className="log-stream" style={{ height: h, whiteSpace: "pre-wrap" }}>{pretty(data)}</pre>;
}

function dataOf(v: AnyJson | null): AnyJson {
  const d = v?.data;
  return (d && typeof d === "object" ? d : v || {}) as AnyJson;
}

function StatusLine({ label, value }: { label: string; value: ReactNode }) {
  return <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13, padding: "3px 0" }}><span style={{ color: "var(--muted)" }}>{label}</span><strong>{value}</strong></div>;
}

export default function AgentOpsView() {
  const [status, setStatus] = useState<AnyJson | null>(null);
  const [gpu, setGpu] = useState<AnyJson | null>(null);
  const [mcp, setMcp] = useState<AnyJson | null>(null);
  const [incidents, setIncidents] = useState<AnyJson | null>(null);
  const [skills, setSkills] = useState<AnyJson | null>(null);
  const [cognitive, setCognitive] = useState<AnyJson | null>(null);
  const [cogState, setCogState] = useState<AnyJson | null>(null);
  const [plans, setPlans] = useState<AnyJson | null>(null);
  const [tasks, setTasks] = useState<AnyJson | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const [s, g, m, i, sk, c, cs, p, t] = await Promise.allSettled([
        getJson(`${CORE}/status`),
        getJson(`${CORE}/api/gpu`),
        getJson(`${CORE}/api/agent/mcp`),
        getJson(`${CORE}/api/agent/incidents?limit=25`),
        getJson(`${CORE}/api/agent/skills`),
        getJson(`${CORE}/api/cognitive/health`),
        getJson(`${CORE}/api/cognitive/state`),
        getJson(`${CORE}/api/cognitive/db/project_plans?limit=12`),
        getJson(`${CORE}/api/cognitive/db/project_tasks?limit=40`),
      ]);
      if (s.status === "fulfilled") setStatus(s.value); else setStatus({ ok: false, error: String(s.reason) });
      if (g.status === "fulfilled") setGpu(g.value); else setGpu({ ok: false, error: String(g.reason) });
      if (m.status === "fulfilled") setMcp(m.value); else setMcp({ ok: false, error: String(m.reason) });
      if (i.status === "fulfilled") setIncidents(i.value); else setIncidents({ ok: false, error: String(i.reason) });
      if (sk.status === "fulfilled") setSkills(sk.value); else setSkills({ ok: false, error: String(sk.reason) });
      if (c.status === "fulfilled") setCognitive(c.value); else setCognitive({ ok: false, error: String(c.reason) });
      if (cs.status === "fulfilled") setCogState(cs.value); else setCogState({ ok: false, error: String(cs.reason) });
      if (p.status === "fulfilled") setPlans(p.value); else setPlans({ ok: false, error: String(p.reason) });
      if (t.status === "fulfilled") setTasks(t.value); else setTasks({ ok: false, error: String(t.reason) });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 7000);
    return () => clearInterval(timer);
  }, [refresh]);

  const sk = skills || {};
  const runtime = sk.runtime as AnyJson | undefined;
  const cluster = sk.cluster as AnyJson | undefined;
  const mcpServers = (mcp?.servers || {}) as AnyJson;
  const planRows = ((dataOf(plans).rows || []) as AnyJson[]).slice(0, 8);
  const taskRows = ((dataOf(tasks).rows || []) as AnyJson[]).slice(0, 16);

  return (
    <div style={{ height: "100%", overflow: "auto", padding: 16 }}>
      <div className="panel" style={{ padding: 14, marginBottom: 12, display: "flex", alignItems: "center", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>🧭 Agent Operations Center</h2>
          <div style={{ color: "var(--muted)", fontSize: 13 }}>
            Наблюдаемая автономия: миссии, self-heal, MCP, GPU, cognitive core, incidents и навыки.
          </div>
        </div>
        <button className="btn primary" style={{ marginLeft: "auto" }} onClick={refresh} disabled={busy}>
          {busy ? "Обновляю…" : "Обновить"}
        </button>
      </div>
      {error && <div className="mic-error">⚠ {error}</div>}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12, marginBottom: 12 }}>
        <Card title="Autonomy Runtime">
          <StatusLine label="background" value={String(runtime?.enabled ?? false)} />
          <StatusLine label="idle active" value={String((runtime?.idle_loop as AnyJson | undefined)?.active ?? false)} />
          <StatusLine label="self-heal" value={String((runtime?.idle_loop as AnyJson | undefined)?.self_heal_enabled ?? false)} />
          <StatusLine label="native tools" value={((sk.native_tools as string[] | undefined) || []).join(", ") || "—"} />
          <StatusLine label="mission tool" value={String(sk.mission_tool || "—")} />
        </Card>
        <Card title="Cluster">
          <StatusLine label="nodes" value={String((cluster?.nodes as unknown[] | undefined)?.length ?? 0)} />
          <StatusLine label="enabled" value={String(cluster ? true : false)} />
          <Pre data={cluster || {}} h={130} />
        </Card>
        <Card title="MCP quick view">
          <StatusLine label="tools" value={String(mcp?.tool_count ?? 0)} />
          <StatusLine label="servers" value={String(Object.keys(mcpServers).length)} />
          <Pre data={mcpServers} h={130} />
        </Card>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 12 }}>
        <Card title="Mission plans"><Pre data={{ plans: planRows, tasks: taskRows }} /></Card>
        <Card title="Cognitive state"><Pre data={cogState} /></Card>
        <Card title="Core / Services"><Pre data={status} /></Card>
        <Card title="GPU / VRAM"><Pre data={gpu} /></Card>
        <Card title="Resolved incidents"><Pre data={incidents} /></Card>
        <Card title="Compiled skills / runtime raw"><Pre data={skills} /></Card>
        <Card title="Cognitive health"><Pre data={cognitive} /></Card>
      </div>
      <div className="panel" style={{ padding: 14, marginTop: 12 }}>
        <h3 style={{ marginTop: 0 }}>Как этим пользоваться</h3>
        <p style={{ color: "var(--muted)", lineHeight: 1.5 }}>
          Для большой цели попроси JARVIS «оформи это как mission plan». Он создаст durable plan в cognitive DB, сможет дернуть роли Researcher/Coder/Critic и покажет прогресс здесь. Если MCP или cognitive core краснеют — сначала смотри этот экран, затем «Мониторную».
        </p>
      </div>
    </div>
  );
}
