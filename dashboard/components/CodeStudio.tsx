"use client";
/**
 * CodeStudio.tsx — Code Studio (split-view Monaco Diff Editor).
 * Отображает живые структурные Git-style диффы, генерируемые кодер-агентом
 * (Qwen2.5-Coder) при исполнении задач в dockerized-sandbox.
 * Слева — исходное состояние, справа — изменения агента. Внизу — результат
 * исполнения кода в sandbox.
 */
import { useEffect, useRef, useState } from "react";
import { DiffEditor } from "@monaco-editor/react";
import { JarvisSocket, JarvisMessage } from "@/lib/ws";

interface ExecResult {
  returncode?: number;
  stdout?: string;
  stderr?: string;
}

export default function CodeStudio() {
  const [original, setOriginal] = useState<string>("# Исходный код появится здесь\n");
  const [modified, setModified] = useState<string>("# Код, сгенерированный агентом\n");
  const [exec, setExec] = useState<ExecResult | null>(null);
  const [task, setTask] = useState("");
  const chatRef = useRef<JarvisSocket | null>(null);

  useEffect(() => {
    // Канал code получает диффы; canal chat — статусы. Подписываемся на оба
    // через единый /ws/chat (ядро мультиплексирует события по полю channel).
    const sock = new JarvisSocket("/ws/chat", {
      onJson: (msg: JarvisMessage) => {
        if (msg.type === "code_diff") {
          setModified(String(msg.code ?? ""));
          setExec((msg.exec_result as ExecResult) ?? null);
        } else if (msg.type === "route" && msg.plan) {
          // показываем план как комментарий в «исходнике»
          setOriginal(`# План агента:\n# ${String(msg.plan).replace(/\n/g, "\n# ")}\n`);
        }
      },
    });
    sock.connect();
    chatRef.current = sock;
    return () => sock.close();
  }, []);

  const submitTask = async () => {
    if (!task.trim()) return;
    await fetch("/api/core/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task }),
    });
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div className="panel" style={{ display: "flex", gap: 10 }}>
        <input
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="Задача для кодер-агента (например: «напиши функцию быстрой сортировки и проверь её»)"
          style={{
            flex: 1, padding: "10px 12px", borderRadius: 8,
            background: "#05080d", border: "1px solid var(--border)", color: "var(--text)",
          }}
          onKeyDown={(e) => e.key === "Enter" && submitTask()}
        />
        <button className="btn primary" onClick={submitTask}>▶ Выполнить</button>
      </div>

      <div className="panel" style={{ height: "55vh" }}>
        <DiffEditor
          height="100%"
          theme="vs-dark"
          language="python"
          original={original}
          modified={modified}
          options={{ renderSideBySide: true, readOnly: true, minimap: { enabled: false } }}
        />
      </div>

      {exec && (
        <div className="panel">
          <strong>
            Результат исполнения в sandbox{" "}
            <span className={`status-dot ${exec.returncode === 0 ? "ok" : "err"}`} />
            код возврата: {exec.returncode}
          </strong>
          <pre className="log-stream" style={{ height: "20vh", marginTop: 8 }}>
            {exec.stdout}
            {exec.stderr ? `\n[stderr]\n${exec.stderr}` : ""}
          </pre>
        </div>
      )}
    </div>
  );
}
