# -*- coding: utf-8 -*-
"""Native host operations for JARVIS.

This module exposes host operations that prefer Windows native/object models over
text parsing: CIM/WMI for system administration, Win32 HWND enumeration and
message posting for focus-free window targeting, and UI Automation discovery when
available. The implementation intentionally rides over the existing authenticated
RPC bridge, so the same HITL/security envelope still applies.
"""

from __future__ import annotations

from typing import Any

from .tools import Tool, ToolContext

_MAX = 12000


def _trunc(text: str, limit: int = _MAX) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    h = limit // 2
    return text[:h] + f"\n…[cut {len(text) - limit} chars]…\n" + text[-(limit - h):]


async def _ps(ctx: ToolContext, command: str, timeout: int = 80) -> dict[str, Any]:
    if ctx.bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен."}
    res = await ctx.bridge.call("powershell", {"command": command}, timeout=timeout)
    result = (res or {}).get("result", {}) or {}
    out = (result.get("stdout") or "") + (result.get("stderr") or "")
    return {"ok": bool((res or {}).get("ok")), "content": _trunc(out), "raw": res}


def _json_ps(expr: str) -> str:
    return (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"{expr} | ConvertTo-Json -Depth 6 -Compress"
    )


def _filter_arg(filter_text: str) -> str:
    return f" -Filter \"{filter_text}\"" if filter_text else ""


async def tool_native_host(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """WMI/CIM-first host administration queries."""
    action = str(args.get("action") or "overview").strip().lower()
    limit = max(1, min(int(args.get("limit", 25) or 25), 200))
    name = str(args.get("name") or "").strip().replace("'", "''").replace('"', '')

    if action == "overview":
        ps = _json_ps(
            "$os=Get-CimInstance Win32_OperatingSystem;"
            "$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1;"
            "$gpu=Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion;"
            "$disk=Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | Select-Object DeviceID,Size,FreeSpace;"
            "[pscustomobject]@{os=$os.Caption;version=$os.Version;uptime=$os.LastBootUpTime;"
            "cpu=$cpu.Name;ramTotalKB=$os.TotalVisibleMemorySize;ramFreeKB=$os.FreePhysicalMemory;gpu=$gpu;disk=$disk}"
        )
        return await _ps(ctx, ps)

    if action == "processes":
        filt = f"Name LIKE '%{name}%'" if name else ""
        ps = _json_ps(
            f"Get-CimInstance Win32_Process{_filter_arg(filt)} | "
            f"Select-Object -First {limit} ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate"
        )
        return await _ps(ctx, ps)

    if action == "services":
        filt = f"Name LIKE '%{name}%' OR DisplayName LIKE '%{name}%'" if name else ""
        ps = _json_ps(
            f"Get-CimInstance Win32_Service{_filter_arg(filt)} | "
            f"Select-Object -First {limit} Name,DisplayName,State,StartMode,ProcessId,PathName"
        )
        return await _ps(ctx, ps)

    if action == "events":
        log = str(args.get("log") or "System").strip().replace("'", "''")
        ps = _json_ps(
            f"Get-WinEvent -LogName '{log}' -MaxEvents {limit} | "
            "Select-Object Id,ProviderName,LevelDisplayName,TimeCreated,Message"
        )
        return await _ps(ctx, ps, timeout=120)

    if action == "hardware":
        ps = _json_ps(
            "$bios=Get-CimInstance Win32_BIOS;"
            "$board=Get-CimInstance Win32_BaseBoard;"
            "$gpu=Get-CimInstance Win32_VideoController;"
            "$net=Get-CimInstance Win32_NetworkAdapterConfiguration | Where-Object {$_.IPEnabled};"
            "[pscustomobject]@{bios=$bios;board=$board;gpu=$gpu;net=$net}"
        )
        return await _ps(ctx, ps)

    return {"ok": False, "content": f"Неизвестное native_host action='{action}'. Доступно: overview|processes|services|events|hardware."}


_WIN32_PREFIX = r'''
$ErrorActionPreference='Stop';
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class JWin32 {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, UInt32 Msg, IntPtr wParam, IntPtr lParam);
}
"@;
'''


async def tool_native_window(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Win32 HWND discovery and focus-free message posting."""
    action = str(args.get("action") or "list").strip().lower()
    query = str(args.get("query") or "").strip().replace("'", "''")
    limit = max(1, min(int(args.get("limit", 40) or 40), 200))

    if action in ("list", "find"):
        filter_line = (
            f"if($title -notlike '*{query}*' -and $class -notlike '*{query}*'){{return $true}};"
            if query else ""
        )
        ps = _WIN32_PREFIX + f'''
$wins=New-Object System.Collections.Generic.List[object];
$cb=[JWin32+EnumWindowsProc]{{ param([IntPtr]$h,[IntPtr]$p)
  if(-not [JWin32]::IsWindowVisible($h)){{return $true}}
  $sb=New-Object System.Text.StringBuilder 512; [void][JWin32]::GetWindowText($h,$sb,$sb.Capacity); $title=$sb.ToString();
  $csb=New-Object System.Text.StringBuilder 256; [void][JWin32]::GetClassName($h,$csb,$csb.Capacity); $class=$csb.ToString();
  if([string]::IsNullOrWhiteSpace($title) -and [string]::IsNullOrWhiteSpace($class)){{return $true}}
  {filter_line}
  [uint32]$pid=0; [void][JWin32]::GetWindowThreadProcessId($h,[ref]$pid);
  $wins.Add([pscustomobject]@{{hwnd=$h.ToInt64();pid=$pid;title=$title;class=$class}});
  return $wins.Count -lt {limit}
}};
[JWin32]::EnumWindows($cb,[IntPtr]::Zero) | Out-Null;
$wins | ConvertTo-Json -Depth 4 -Compress
'''
        return await _ps(ctx, ps)

    if action == "post_text":
        hwnd = int(args.get("hwnd") or 0)
        text = str(args.get("text") or "")[:2000]
        if hwnd <= 0 or not text:
            return {"ok": False, "content": "Нужны hwnd и text."}
        cps = ",".join(str(ord(ch)) for ch in text)
        ps = _WIN32_PREFIX + f'''
$h=[IntPtr]{hwnd}; $chars=@({cps});
foreach($c in $chars){{ [JWin32]::PostMessage($h,0x0102,[IntPtr]$c,[IntPtr]0) | Out-Null; Start-Sleep -Milliseconds 8 }}
[pscustomobject]@{{ok=$true;hwnd={hwnd};chars=$chars.Count}} | ConvertTo-Json -Compress
'''
        return await _ps(ctx, ps)

    if action == "post_enter":
        hwnd = int(args.get("hwnd") or 0)
        if hwnd <= 0:
            return {"ok": False, "content": "Нужен hwnd."}
        ps = _WIN32_PREFIX + f'''
$h=[IntPtr]{hwnd};
[JWin32]::PostMessage($h,0x0100,[IntPtr]13,[IntPtr]0) | Out-Null;
[JWin32]::PostMessage($h,0x0101,[IntPtr]13,[IntPtr]0) | Out-Null;
[pscustomobject]@{{ok=$true;hwnd={hwnd};key='enter'}} | ConvertTo-Json -Compress
'''
        return await _ps(ctx, ps)

    return {"ok": False, "content": "Неизвестное native_window action. Доступно: list|find|post_text|post_enter."}


async def tool_native_ui(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Windows UI Automation discovery."""
    action = str(args.get("action") or "tree").strip().lower()
    name = str(args.get("name") or "").strip().replace("'", "''")
    limit = max(1, min(int(args.get("limit", 80) or 80), 300))

    if action == "tree":
        ps = _json_ps(
            "Add-Type -AssemblyName UIAutomationClient;"
            "$root=[System.Windows.Automation.AutomationElement]::RootElement;"
            "$cond=[System.Windows.Automation.Condition]::TrueCondition;"
            "$els=$root.FindAll([System.Windows.Automation.TreeScope]::Children,$cond);"
            "$out=@(); foreach($e in $els){$out += [pscustomobject]@{name=$e.Current.Name;class=$e.Current.ClassName;type=$e.Current.ControlType.ProgrammaticName;enabled=$e.Current.IsEnabled;pid=$e.Current.ProcessId}};"
            f"$out | Select-Object -First {limit}"
        )
        return await _ps(ctx, ps)

    if action == "find":
        ps = _json_ps(
            "Add-Type -AssemblyName UIAutomationClient;"
            "$root=[System.Windows.Automation.AutomationElement]::RootElement;"
            "$cond=[System.Windows.Automation.Condition]::TrueCondition;"
            "$els=$root.FindAll([System.Windows.Automation.TreeScope]::Subtree,$cond);"
            "$out=@(); foreach($e in $els){$n=$e.Current.Name; if($n -like '*" + name + "*'){$out += [pscustomobject]@{name=$n;class=$e.Current.ClassName;type=$e.Current.ControlType.ProgrammaticName;enabled=$e.Current.IsEnabled;pid=$e.Current.ProcessId}}};"
            f"$out | Select-Object -First {limit}"
        )
        return await _ps(ctx, ps, timeout=120)

    return {"ok": False, "content": "Неизвестное native_ui action. Доступно: tree|find."}


def register(registry: Any) -> None:
    registry.add(Tool(
        "native_host",
        "Нативные WMI/CIM-запросы к хосту: overview, processes, services, events, hardware. Используй до CLI-парсинга.",
        {"action": "overview|processes|services|events|hardware", "name": "фильтр имени", "limit": "лимит", "log": "System|Application"},
        tool_native_host,
    ))
    registry.add(Tool(
        "native_window",
        "Win32 HWND: list/find окон и focus-free PostMessage ввод в целевое окно по hwnd.",
        {"action": "list|find|post_text|post_enter", "query": "поиск title/class", "hwnd": "handle окна", "text": "текст для post_text"},
        tool_native_window,
    ))
    registry.add(Tool(
        "native_ui",
        "Windows UI Automation discovery: дерево верхних окон и поиск элементов без захвата фокуса.",
        {"action": "tree|find", "name": "фрагмент имени элемента", "limit": "лимит"},
        tool_native_ui,
    ))
