# -*- coding: utf-8 -*-
"""
network_resilience.py — safe Researcher connectivity recovery.

This module intentionally avoids hard-coded DPI-bypass/stealth strategies. It
performs DNS/HTTP diagnostics and can restart only explicitly allow-listed host
services via WMI/PowerShell through the RPC bridge. Operators may wire their own
local recovery scripts with JARVIS_NETWORK_RECOVERY_CMD, but JARVIS will not tune
circumvention parameters autonomously.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

import httpx

HostExec = Callable[[str], Awaitable[dict[str, Any]]]

CHECK_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.github.com/meta",
    "https://huggingface.co/robots.txt",
]


async def http_probe() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as cli:
        for url in CHECK_URLS:
            try:
                r = await cli.get(url)
                results.append({"url": url, "ok": r.status_code < 500, "status": r.status_code})
            except Exception as exc:  # noqa: BLE001
                results.append({"url": url, "ok": False, "error": str(exc)[:200]})
    return {"ok": any(r.get("ok") for r in results), "results": results}


async def host_dns_probe(host_exec: HostExec) -> dict[str, Any]:
    # WMI/CIM-friendly PowerShell object output; no parsing of tasklist/netstat.
    cmd = (
        "$targets=@('github.com','huggingface.co','cloudflare.com');"
        "$out=@(); foreach($t in $targets){"
        "try{$r=Resolve-DnsName $t -ErrorAction Stop | Select-Object -First 1;"
        "$out += [pscustomobject]@{target=$t; ok=$true; address=$r.IPAddress}}"
        "catch{$out += [pscustomobject]@{target=$t; ok=$false; error=$_.Exception.Message}}};"
        "$out | ConvertTo-Json -Compress"
    )
    return await host_exec(f"powershell -NoProfile -NonInteractive -Command {cmd}")


async def recover_if_allowed(host_exec: HostExec) -> dict[str, Any]:
    services = [s.strip() for s in os.environ.get("JARVIS_NETWORK_RECOVERY_SERVICES", "").split(",") if s.strip()]
    custom = os.environ.get("JARVIS_NETWORK_RECOVERY_CMD", "").strip()
    actions: list[dict[str, Any]] = []

    for svc in services:
        if not svc.replace("-", "").replace("_", "").isalnum():
            actions.append({"service": svc, "ok": False, "error": "unsafe service token"})
            continue
        ps = (
            f"$s=Get-CimInstance Win32_Service -Filter \"Name='{svc}'\"; "
            "if($null -eq $s){throw 'service not found'}; "
            f"Restart-Service -Name '{svc}' -Force; "
            "Start-Sleep -Seconds 2; "
            f"Get-CimInstance Win32_Service -Filter \"Name='{svc}'\" | Select Name,State | ConvertTo-Json -Compress"
        )
        actions.append({"service": svc, "result": await host_exec(f"powershell -NoProfile -NonInteractive -Command {ps}")})

    if custom:
        # Operator-provided hook only. The bridge/HITL policy still applies.
        actions.append({"custom_hook": True, "result": await host_exec(custom)})

    return {"ok": True, "actions": actions, "note": "No DPI strategy tuning is performed by JARVIS."}


async def diagnose_and_optionally_recover(host_exec: HostExec | None = None) -> dict[str, Any]:
    probe = await http_probe()
    out: dict[str, Any] = {"container_http": probe}
    if host_exec is not None:
        out["host_dns"] = await host_dns_probe(host_exec)
    if not probe.get("ok") and host_exec is not None:
        out["recovery"] = await recover_if_allowed(host_exec)
    return out
