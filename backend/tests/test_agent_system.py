# -*- coding: utf-8 -*-
"""
test_agent_system.py — самодостаточный тест агентской системы JARVIS-OS.

Проверяет ЛОГИКУ оркестрации без GPU/Windows/vLLM — через фейковый «мозг» (llm)
и фейковый RPC-мост. Запуск:

    python backend/tests/test_agent_system.py     # печатает PASS/FAIL, код выхода
    pytest backend/tests/test_agent_system.py     # как обычные тесты

Покрывает: простой ответ без инструментов, вызов инструмента, многошаговый
GUI-суб-агент (UI-TARS) со стримингом шагов, авто-fallback CLI→GUI, разбор
действий UI-TARS и кросс-платформенный Linux-бэкенд моста (xdotool/scrot/...).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Память агента пишем во временную папку — не трогаем реальные данные.
os.environ.setdefault("JARVIS_MEMORY_DIR", "/tmp/jarvis-test-memory")

from orchestrator import agent, gui_agent, llm  # noqa: E402


# --------------------------------------------------------------------------- #
# Фейковые «мозги» и мост
# --------------------------------------------------------------------------- #
# 1×1 прозрачный PNG (валидный заголовок → _png_size вернёт 1×1).
_PNG_1x1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)


class FakeBridge:
    """Записывает все RPC-вызовы; отдаёт заранее заданные результаты."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    async def call(self, action, payload=None, timeout=200):
        self.calls.append((action, payload or {}))
        if action == "screenshot":
            return {"ok": True, "result": {
                "image_b64": _PNG_1x1, "screen_w": 1920, "screen_h": 1080}}
        if action in self.results:
            return self.results[action]
        return {"ok": True, "result": {"stdout": "", "stderr": ""}}


def install_fake_llm(planner_script, uitars_script=None, answer="Готово."):
    """Подменить llm.chat/chat_stream скриптом ответов."""
    planner = list(planner_script)
    uitars = list(uitars_script or [])

    async def fake_chat(messages, *, base_url=llm.QWEN_URL, model=llm.QWEN_MODEL,
                        temperature=0.2, max_tokens=1024, timeout=180, stop=None,
                        extra_body=None):
        if base_url == llm.UITARS_URL:
            return uitars.pop(0) if uitars else "Thought: done\nAction: finished(content='ok')"
        sys_txt = messages[0].get("content", "") if messages else ""
        if isinstance(sys_txt, str) and sys_txt.startswith("Сожми диалог"):
            return "Краткая сводка диалога."
        return planner.pop(0) if planner else '{"action":"answer","thought":"итог"}'

    async def fake_stream(messages, *, base_url=llm.QWEN_URL, model=llm.QWEN_MODEL,
                          temperature=0.3, max_tokens=2048, timeout=None):
        for chunk in (answer[i:i + 5] for i in range(0, len(answer), 5)):
            yield chunk

    llm.chat = fake_chat
    llm.chat_stream = fake_stream


async def collect(session, text, bridge=None):
    events = []
    async for ev in agent.run_chat(session, text, bridge=bridge):
        events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# Тесты оркестратора
# --------------------------------------------------------------------------- #
async def test_simple_answer():
    install_fake_llm(['{"action":"answer","thought":"просто ответ"}'], answer="Привет!")
    ev = await collect("t_simple", "привет")
    types = [e["type"] for e in ev]
    assert "assistant_start" in types, types
    done = [e for e in ev if e["type"] == "assistant_done"][0]
    assert done["content"] == "Привет!", done
    return "простой ответ без инструментов"


async def test_tool_call_calculator():
    install_fake_llm([
        '{"action":"calculator","thought":"посчитаю","action_input":{"expression":"2+2"}}',
        '{"action":"answer","thought":"готов"}',
    ], answer="Четыре.")
    ev = await collect("t_calc", "сколько 2+2")
    calls = [e for e in ev if e["type"] == "tool_call"]
    results = [e for e in ev if e["type"] == "tool_result"]
    assert calls and calls[0]["tool"] == "calculator", calls
    assert results and results[0]["ok"] is True, results
    assert "2+2 = 4" in results[0]["summary"], results[0]["summary"]
    return "вызов инструмента calculator"


async def test_gui_subagent_streams_steps():
    """GUI-суб-агент: click → finished, со стримингом шагов в чат."""
    install_fake_llm(
        planner_script=[
            '{"action":"gui","thought":"визуально","action_input":{"goal":"нажми кнопку OK"}}',
            '{"action":"answer","thought":"готово"}',
        ],
        uitars_script=[
            "Thought: вижу кнопку OK\nAction: click(start_box='(500,500)')",
            "Thought: готово\nAction: finished(content='нажал OK')",
        ],
        answer="Нажал OK.",
    )
    bridge = FakeBridge()
    ev = await collect("t_gui", "нажми OK", bridge=bridge)
    # был хотя бы один реальный клик на хосте
    clicks = [c for c in bridge.calls if c[0] == "mouse_click"]
    assert clicks, bridge.calls
    assert clicks[0][1]["x"] == int(500 / 1000 * 1920), clicks[0][1]
    # шаги UI-TARS попали в чат как события actor=ui-tars
    uitars_events = [e for e in ev if e.get("actor") == "ui-tars"]
    assert any(e["type"] == "tool_call" for e in uitars_events), uitars_events
    assert any(e["type"] == "tool_result" for e in uitars_events), uitars_events
    # итог инструмента gui — успех
    gui_result = [e for e in ev if e["type"] == "tool_result" and e.get("tool") == "gui"][0]
    assert gui_result["ok"] is True, gui_result
    return "многошаговый GUI-суб-агент + стриминг шагов"


async def test_cli_to_gui_fallback_hint():
    """Команда windows провалилась → в наблюдении появляется подсказка [fallback]."""
    install_fake_llm([
        '{"action":"windows","thought":"запущу","action_input":{"action":"exec","command":"badcmd"}}',
        '{"action":"answer","thought":"итог"}',
    ], answer="Командой не вышло — попробую иначе.")
    bridge = FakeBridge(results={"exec": {"ok": False, "error": "boom"}})
    ev = await collect("t_fallback", "сделай что-то", bridge=bridge)
    res = [e for e in ev if e["type"] == "tool_result" and e.get("tool") == "windows"][0]
    assert res["ok"] is False, res
    assert "[fallback]" in res["summary"] and "gui" in res["summary"], res["summary"]
    return "авто-fallback CLI→GUI (подсказка мозгу)"


# --------------------------------------------------------------------------- #
# Тесты разбора действий UI-TARS
# --------------------------------------------------------------------------- #
async def test_parse_uitars_actions():
    p = gui_agent._parse_uitars_action
    a = p("Thought: жму\nAction: click(start_box='(123,456)')")
    assert a["kind"] == "click" and a["x"] == 123 and a["y"] == 456, a
    a = p("Action: type(content='привет мир')")
    assert a["kind"] == "type" and a["content"] == "привет мир", a
    a = p("Action: hotkey(key='ctrl s')")
    assert a["kind"] == "hotkey" and a["key"] == "ctrl s", a
    a = p("Action: drag(start_box='(10,20)', end_box='(30,40)')")
    assert a["x2"] == 30 and a["y2"] == 40, a
    a = p("Action: finished(content='all done')")
    assert a["kind"] == "finished", a
    a = p("Thought: ok\nAction: click(point='(700,800)')")  # формат UI-TARS-1.5
    assert a["x"] == 700 and a["y"] == 800, a
    # box-токены UI-TARS-1.5/2.0
    a = p("Action: click(start_box='<|box_start|>(640,360)<|box_end|>')")
    assert a["x"] == 640 and a["y"] == 360, a
    # ведущий мусор '<' (как в реальном логе) — всё равно парсим
    a = p("Thought: жму\nAction: < click(start_box='(11,22)')")
    assert a and a["kind"] == "click" and a["x"] == 11, a
    # координаты через пробел без запятой
    a = p("Action: click(start_box='(120 240)')")
    assert a["x"] == 120 and a["y"] == 240, a
    # чистый мусор (вырождение) → None, не падаем
    assert p("assistant assistant assistant assistant") is None
    assert p("") is None
    return "разбор действий UI-TARS (форматы + box-токены + мусор)"


# --------------------------------------------------------------------------- #
# Тесты кросс-платформенного Linux-бэкенда моста
# --------------------------------------------------------------------------- #
async def test_linux_bridge_commands():
    import windows_rpc_bridge as wb

    wb.shutil.which = lambda n: "/usr/bin/" + n   # всё «установлено»
    ex = wb.LinuxHostExecutor()
    ex.is_wayland = False
    captured = []

    async def fake_run(cmd, shell=False, timeout=120, hidden=False):
        captured.append(cmd)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    ex._run = fake_run  # подменяем фактический запуск процессов

    await ex.mouse_click(100, 200, "left", False)
    assert captured[-1][:1] == ["xdotool"] and "click" in captured[-1], captured[-1]

    await ex.mouse_click(0, 0, "right", True)
    assert "--repeat" in captured[-1] and captured[-1][-1] == "3", captured[-1]

    await ex.key_press("ctrl+s")
    assert captured[-1] == ["xdotool", "key", "--clearmodifiers", "ctrl+s"], captured[-1]

    await ex.key_press("enter")
    assert captured[-1][-1] == "Return", captured[-1]

    await ex.type_text("Привет")
    assert captured[-1][:3] == ["xdotool", "type", "--clearmodifiers"], captured[-1]

    await ex.scroll(-3, "down")
    assert captured[-1] == ["xdotool", "click", "--repeat", "3", "5"], captured[-1]

    await ex.drag(1, 2, 3, 4)
    assert "mousedown" in captured[-1] and "mouseup" in captured[-1], captured[-1]

    await ex.screenshot(out_path="/tmp/x.png", return_b64=False)
    assert captured[-1][0] in ("scrot", "maim") or "import" in str(captured[-1]), captured[-1]

    await ex.exec_command("uname -a")
    assert captured[-1] == ["bash", "-lc", "uname -a"], captured[-1]

    return "Linux-бэкенд моста: xdotool/scrot/bash команды"


async def test_key_translation():
    import windows_rpc_bridge as wb
    assert wb._to_xdotool_key("Ctrl+Shift+S") == "ctrl+shift+s"
    assert wb._to_xdotool_key("alt tab") == "alt+Tab"
    assert wb._to_xdotool_key("enter") == "Return"
    # .NET SendKeys → xdotool
    assert "ctrl+s" in wb._sendkeys_to_xdotool("^s")
    assert "alt+F4" in wb._sendkeys_to_xdotool("%{F4}")
    assert "Return" in wb._sendkeys_to_xdotool("{ENTER}")
    return "перевод клавиш (xdotool + SendKeys)"


async def test_interactive_guard():
    """exec/powershell интерактивных команд НЕ должны зависать — быстрый отказ."""
    import windows_rpc_bridge as wb
    assert wb._looks_interactive("powershell -NoExit -Command Get-Process")
    assert wb._looks_interactive("cmd /k dir")
    assert wb._looks_interactive("something ; pause")
    assert not wb._looks_interactive("Get-Process | Out-String")
    assert not wb._looks_interactive("dir")
    win = wb.WindowsHostExecutor()
    res = await win.exec_command("powershell -NoExit -Command Get-Process")
    assert res["returncode"] == 1 and "интерактив" in res["stderr"].lower(), res
    res = await win.powershell("cmd /k dir")
    assert res["returncode"] == 1, res
    return "защита от зависания на интерактивных командах (-NoExit/-k)"


async def test_linux_send_keys_routing():
    """send_keys на Linux принимает и 'ctrl+s', и SendKeys '^s'."""
    import windows_rpc_bridge as wb
    wb.shutil.which = lambda n: "/usr/bin/" + n
    ex = wb.LinuxHostExecutor()
    captured = []

    async def fake_run(cmd, shell=False, timeout=120, hidden=False):
        captured.append(cmd)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    ex._run = fake_run
    await ex.send_keys("ctrl+s")            # обычный вид
    assert captured[-1][-1] == "ctrl+s", captured[-1]
    await ex.send_keys("^s")                # SendKeys
    assert captured[-1][-1] == "ctrl+s", captured[-1]
    await ex.send_keys("{ENTER}")           # SendKeys спец-клавиша
    assert captured[-1][-1] == "Return", captured[-1]
    return "Linux send_keys: 'ctrl+s' и '^s'/'{ENTER}'"


async def test_open_app_nonblocking():
    """open_app должен запускать детачем без захвата pipe (иначе виснет на calc/браузере)."""
    import subprocess as sp
    import windows_rpc_bridge as wb
    calls: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd
            calls["kw"] = kw
        # НЕТ wait()/communicate() — если бы open_app их звал, тест бы это вскрыл

    orig = wb.subprocess.Popen
    wb.subprocess.Popen = FakePopen
    try:
        res = await wb.WindowsHostExecutor().open_app("calc")
    finally:
        wb.subprocess.Popen = orig
    assert res["returncode"] == 0, res
    assert 'start ""' in calls["cmd"] and "calc" in calls["cmd"], calls
    assert calls["kw"].get("stdout") == sp.DEVNULL, calls["kw"]
    assert calls["kw"].get("stderr") == sp.DEVNULL, calls["kw"]
    return "open_app не блокирует (детач + DEVNULL, без захвата pipe)"


async def test_windows_screenshot_png_dims():
    """Скриншот: надёжный PNG + размеры экрана из заголовка PNG + image_b64."""
    import base64 as _b64
    import os as _os
    import tempfile
    import windows_rpc_bridge as wb
    win = wb.WindowsHostExecutor()

    async def fake_ps(cmd, hidden=False):
        return {"returncode": 0, "stdout": ""}

    win.powershell = fake_ps
    tf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tf.write(_b64.b64decode(_PNG_1x1))   # валидный 1x1 PNG
    tf.close()
    try:
        res = await win.screenshot(out_path=tf.name, return_b64=True)
    finally:
        _os.unlink(tf.name)
    assert res.get("image_b64") and res.get("image_fmt") == "png", res
    assert res.get("screen_w") == 1 and res.get("screen_h") == 1, res
    return "Windows screenshot: PNG + размеры экрана из заголовка + image_b64"


async def test_llm_client_reuse():
    """Один и тот же httpx-клиент переиспользуется в пределах event loop."""
    a = llm._get_client()
    b = llm._get_client()
    assert a is b, "клиент должен переиспользоваться (пул соединений)"
    assert not a.is_closed
    return "переиспользование httpx-клиента (keep-alive пул)"


async def test_see_screen():
    """see_screen: снимает скриншот и возвращает текстовое описание экрана."""
    async def fake_chat(messages, *, base_url=llm.QWEN_URL, **kw):
        return "На экране: рабочий стол Windows, открыт Chrome, панель задач снизу."

    llm.chat = fake_chat
    bridge = FakeBridge()
    res = await gui_agent.describe_screen(bridge, "что на экране")
    assert res["ok"] and "рабочий стол" in res["content"].lower(), res
    assert any(c[0] == "screenshot" for c in bridge.calls), bridge.calls
    return "see_screen: скриншот → текстовое описание экрана"


async def test_web_search_merge():
    """web_search: слияние нескольких движков, дедуп по URL, метка источника."""
    from orchestrator import tools as T

    async def ddg(q):
        return [{"title": "A", "url": "https://a.com", "snippet": "a"},
                {"title": "B", "url": "https://b.com", "snippet": "b"}]

    async def bing(q):
        return [{"title": "B2", "url": "http://www.b.com/", "snippet": "b2"},
                {"title": "C", "url": "https://c.com", "snippet": "c"}]

    async def moj(q):
        return []

    T._search_ddg, T._search_bing, T._search_mojeek = ddg, bing, moj
    res = await T.tool_web_search({"query": "x"}, T.ToolContext())
    c = res["content"]
    assert res["ok"], res
    assert "a.com" in c and "c.com" in c, c
    assert c.count("b.com") == 1, c           # дубль b.com схлопнут
    assert "DuckDuckGo" in c and "Bing" in c, c
    return "web_search: слияние движков + дедупликация по URL"


# --------------------------------------------------------------------------- #
# Pytest-обёртки (чтобы работал и `pytest`, и прямой запуск)
# --------------------------------------------------------------------------- #
_TESTS = [
    test_simple_answer, test_tool_call_calculator, test_gui_subagent_streams_steps,
    test_cli_to_gui_fallback_hint, test_parse_uitars_actions,
    test_linux_bridge_commands, test_key_translation,
    test_interactive_guard, test_linux_send_keys_routing, test_open_app_nonblocking,
    test_windows_screenshot_png_dims, test_llm_client_reuse, test_see_screen,
    test_web_search_merge,
]


def test_pytest_entry():
    """Единая точка для pytest: прогоняет все async-проверки."""
    asyncio.run(_run_all())


async def _run_all() -> int:
    failed = 0
    for t in _TESTS:
        try:
            label = await t()
            print(f"  PASS · {label}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL · {t.__name__}: {exc}")
            traceback.print_exc()
    print("-" * 60)
    print(f"Итого: {len(_TESTS) - failed}/{len(_TESTS)} прошло.")
    return failed


if __name__ == "__main__":
    print("JARVIS-OS · тест агентской системы\n" + "-" * 60)
    sys.exit(1 if asyncio.run(_run_all()) else 0)
