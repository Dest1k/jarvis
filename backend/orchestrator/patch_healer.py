# -*- coding: utf-8 -*-
"""patch_healer.py — patch-candidate engine for JARVIS self-heal.

The idle loop detects anomalies; this module turns them into a candidate unified
diff on a staging branch, validates it, and leaves a visible report. Patch apply
is opt-in through JARVIS_SELF_HEAL_APPLY_PATCH=1. Without that flag it still
produces a candidate `.diff` file and validation metadata.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

HostExec = Callable[[str], Awaitable[dict[str, Any]]]

_ALLOWED_PREFIXES = (
    "backend/", "dashboard/", "docs/", "wsl/", ".env.example", "README.md",
    "windows_rpc_bridge.py", "jarvis.py", "bootstrap_installer.py",
)


@dataclass
class PatchCandidateResult:
    ok: bool
    applied: bool
    patch_path: str
    explanation: str
    validation: list[dict[str, Any]]
    error: str = ""


def _safe_repo_path(path: str) -> bool:
    p = (path or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or ".." in p:
        return False
    return p.startswith(_ALLOWED_PREFIXES)


def _extract_unified_diff(text: str) -> str:
    raw = text or ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            raw = str(data.get("unified_diff") or data.get("diff") or raw)
    except Exception:
        pass
    m = re.search(r"```(?:diff|patch)?\s*(.*?)```", raw, re.S | re.I)
    if m:
        raw = m.group(1)
    raw = raw.strip()
    idx = raw.find("diff --git ")
    if idx >= 0:
        raw = raw[idx:]
    return raw


def _diff_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            p = line[6:].strip()
            if p != "/dev/null":
                paths.append(p)
        elif line.startswith("diff --git "):
            parts = line.split()
            for part in parts[-2:]:
                if part.startswith("a/") or part.startswith("b/"):
                    paths.append(part[2:])
    return sorted(set(paths))


def _candidate_paths(kind: str, anomaly: str) -> list[str]:
    paths: list[str] = []
    if kind in ("cuda_uva", "vram_oom"):
        paths += ["wsl/docker-compose.agents.yml", ".env.example", "wsl/profiles.json"]
    elif kind == "mcp":
        paths += ["backend/orchestrator/mcp_client.py", "backend/mcp_servers.json", "backend/requirements.txt"]
    elif kind == "network":
        paths += ["backend/orchestrator/network_resilience.py", "wsl/docker-compose.agents.yml", ".env.example"]
    else:
        for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.py)", anomaly or ""):
            p = m.group(1).replace("\\", "/")
            if _safe_repo_path(p):
                paths.append(p)
        paths += ["backend/server.py", "backend/orchestrator/agent.py"]
    out: list[str] = []
    for p in paths:
        if p not in out and _safe_repo_path(p):
            out.append(p)
    return out[:6]


def _py_write_file_command(repo_path: str, rel_path: str, content: str) -> str:
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    py = (
        "import base64,pathlib;"
        f"p=pathlib.Path(r'{repo_path}')/r'{rel_path}';"
        "p.parent.mkdir(parents=True,exist_ok=True);"
        f"p.write_bytes(base64.b64decode('{b64}'));"
        "print(p)"
    )
    return f'python -c "{py}"'


class PatchCandidateHealer:
    def __init__(self, *, repo_path: str, host_exec: HostExec) -> None:
        self.repo_path = repo_path or "."
        self.host_exec = host_exec
        self.apply_patch = os.environ.get("JARVIS_SELF_HEAL_APPLY_PATCH", "0") == "1"

    async def _run(self, cmd: str) -> dict[str, Any]:
        res = await self.host_exec(cmd)
        return {"cmd": cmd, "ok": bool(res.get("ok")), "out": (res.get("out") or "")[-4000:]}

    async def _read_file(self, path: str) -> str:
        py = (
            "import pathlib;"
            f"p=pathlib.Path(r'{self.repo_path}')/r'{path}';"
            "print(p.read_text(encoding='utf-8',errors='replace')[:12000] if p.exists() else '')"
        )
        res = await self.host_exec(f'python -c "{py}"')
        return (res.get("out") or "")[:12000]

    async def _collect_context(self, paths: list[str]) -> str:
        parts = []
        status = await self.host_exec(f'git -C "{self.repo_path}" status --short')
        parts.append("# git status\n" + (status.get("out") or "")[:2000])
        for p in paths:
            content = await self._read_file(p)
            parts.append(f"\n# file: {p}\n```\n{content}\n```")
        return "\n".join(parts)[:42000]

    async def _draft_patch(self, anomaly: str, classification: dict[str, Any], diagnosis: str, context: str) -> dict[str, str]:
        try:
            from . import llm
            system = (
                "Ты — Coder-Agent JARVIS self-heal. Верни СТРОГО JSON: "
                "{\"explanation\":\"...\",\"unified_diff\":\"...\"}. "
                "unified_diff должен быть git unified diff с путями a/... b/... . "
                "Меняй только показанные файлы. Не добавляй секреты, команды удаления данных, сетевой обход или force git. "
                "Если безопасного патча нет — unified_diff пустой, explanation объясняет почему."
            )
            user = f"classification={classification}\nAnomaly:\n{anomaly}\nDiagnosis:\n{diagnosis}\n\nContext:\n{context}"
            raw = await llm.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], temperature=0.1, max_tokens=2500, timeout=160)
            try:
                data = json.loads(raw)
                return {"explanation": str(data.get("explanation", ""))[:2000], "diff": _extract_unified_diff(str(data.get("unified_diff", "")))}
            except Exception:
                return {"explanation": "LLM returned non-JSON patch candidate.", "diff": _extract_unified_diff(raw)}
        except Exception as exc:  # noqa: BLE001
            return {"explanation": f"Patch LLM unavailable: {exc}", "diff": ""}

    async def prepare(self, *, anomaly: str, classification: dict[str, Any], diagnosis: str) -> dict[str, Any]:
        kind = str(classification.get("kind") or "traceback")
        paths = _candidate_paths(kind, anomaly)
        context = await self._collect_context(paths)
        drafted = await self._draft_patch(anomaly, classification, diagnosis, context)
        diff = drafted["diff"].strip()
        explanation = drafted["explanation"].strip()
        ts = int(time.time())
        patch_path = f"data/jarvis_core/self_heal/patch_{ts}_{kind}.diff"
        validation: list[dict[str, Any]] = []

        if not diff:
            note = f"# No safe patch generated\n\n{explanation}\n"
            wr = await self._run(_py_write_file_command(self.repo_path, patch_path, note))
            validation.append(wr)
            return asdict(PatchCandidateResult(False, False, patch_path, explanation, validation, "empty patch"))

        bad_paths = [p for p in _diff_paths(diff) if not _safe_repo_path(p)]
        if bad_paths:
            note = diff + "\n\n# Rejected unsafe paths: " + ", ".join(bad_paths)
            validation.append(await self._run(_py_write_file_command(self.repo_path, patch_path, note)))
            return asdict(PatchCandidateResult(False, False, patch_path, explanation, validation, "unsafe patch paths"))

        validation.append(await self._run(_py_write_file_command(self.repo_path, patch_path, diff + "\n")))
        check = await self._run(f'git -C "{self.repo_path}" apply --check "{patch_path}"')
        validation.append(check)
        if not check["ok"]:
            return asdict(PatchCandidateResult(False, False, patch_path, explanation, validation, "git apply --check failed"))

        if not self.apply_patch:
            return asdict(PatchCandidateResult(True, False, patch_path, explanation, validation, "apply disabled"))

        validation.append(await self._run(f'git -C "{self.repo_path}" apply "{patch_path}"'))
        validation.append(await self._run(f'python -m compileall "{self.repo_path}/backend"'))
        return asdict(PatchCandidateResult(all(v.get("ok") for v in validation), True, patch_path, explanation, validation))
