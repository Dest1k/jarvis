# -*- coding: utf-8 -*-
"""
gui_agent.py — GUI-суб-агент JARVIS-OS («TARS»): визуальный исполнитель на базе
UI-TARS. Это «руки и глаза» системы там, где нет CLI-способа — только мышь и
клавиатура поверх живого экрана.

Архитектура (ровно как в ТЗ: Оркестратор → GUI-актуатор):

    Оркестратор (Gemma) формулирует ЦЕЛЬ ("открой настройки и включи тёмную тему")
        │
        ▼
    GuiAgent.run(goal)  ── ЦИКЛ ──────────────────────────────────────────────┐
        1. снять скриншот хоста (через RPC-мост, кросс-платформенно)           │
        2. отдать UI-TARS: системный промпт + ЦЕЛЬ + история прошлых шагов +   │
           текущий скриншот                                                    │
        3. получить ОДНО атомарное действие (click/type/hotkey/scroll/drag/…)  │
        4. исполнить его на хосте через мост                                   │
        5. повторять, пока модель не вернёт finished(...) / fail(...) / лимит  │
        └──────────────────────────────────────────────────────────────────────┘

Почему цикл живёт ЗДЕСЬ, а не в оркестраторе:
    • UI-TARS работает итеративно и должен ВИДЕТЬ свою историю ходов — иначе
      «залипает» на одном и том же неверном клике. Раньше каждый шаг был
      отдельным вызовом инструмента, оркестратор терял нить и упирался в лимит
      шагов. Теперь ОДИН вызов gui доводит визуальную подзадачу до конца.
    • Каждый шаг стримится в чат (мысль + действие UI-TARS) через `emit`, так
      что пользователь ВИДИТ, как «зрение» системы реально тыкает по экрану.

Координаты — нормированные 0–1000 (resolution-independent): модель не зависит от
фактического разрешения экрана, мы домножаем на размеры скриншота. Если модель
вернула уже пиксели (значение > 1000) — берём как есть. Это устойчиво к любому
монитору и не требует пересчёта resize-фактора.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

from . import llm

log = logging.getLogger("jarvis.gui")

# Сколько атомарных действий максимум на одну визуальную подзадачу.
GUI_MAX_STEPS = 14
# Сколько прошлых ходов (мысль+действие) держим в контексте UI-TARS.
GUI_HISTORY_KEEP = 8

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


# Системный промпт UI-TARS: строгий формат «Thought/Action», нормированные
# координаты 0–1000, фиксированное пространство действий.
_UITARS_SYSTEM = (
    "You are UI-TARS, an autonomous GUI agent operating a real computer through "
    "screenshots. You are given a TASK, the history of your previous steps, and "
    "the CURRENT screenshot. Decide the single best NEXT action that moves toward "
    "completing the task, then stop and wait for the next screenshot.\n\n"
    "## Coordinate system\n"
    "All coordinates are NORMALIZED integers in [0,1000]: x is left→right, y is "
    "top→bottom, relative to the whole screenshot. (0,0)=top-left, (1000,1000)=bottom-right.\n\n"
    "## Action space (output EXACTLY ONE)\n"
    "click(start_box='(x,y)')\n"
    "left_double(start_box='(x,y)')\n"
    "right_single(start_box='(x,y)')\n"
    "drag(start_box='(x,y)', end_box='(x,y)')\n"
    "hotkey(key='ctrl s')            # space-separated keys, e.g. 'ctrl c', 'alt tab', 'enter'\n"
    "type(content='text to type')    # types text into the focused field\n"
    "scroll(start_box='(x,y)', direction='down')   # up | down | left | right\n"
    "wait()                          # screen is still loading/animating\n"
    "finished(content='short result')  # the TASK is fully done\n"
    "fail(content='why it is impossible')  # truly cannot be done via GUI\n\n"
    "## Output format — EXACTLY two lines, nothing else\n"
    "Thought: <one short sentence: what you see and why this action>\n"
    "Action: <one action from the space above>"
)


class GuiAgent:
    """Многошаговый визуальный исполнитель поверх UI-TARS."""

    def __init__(self, bridge: Any, max_steps: int = GUI_MAX_STEPS) -> None:
        self.bridge = bridge
        self.max_steps = max_steps

    # ----------------------------------------------------------------- public
    async def run(self, goal: str, emit: Optional[EmitFn] = None) -> dict[str, Any]:
        """
        Довести визуальную цель до конца. Возвращает {ok, content, steps}.
        `emit` (если задан) получает события чата по каждому шагу (actor=ui-tars).
        """
        goal = (goal or "").strip()
        if not goal:
            return {"ok": False, "content": "Не задана цель визуального шага (goal)."}
        if self.bridge is None:
            return {"ok": False, "content": "RPC-мост недоступен — управление GUI невозможно."}

        history: list[str] = []     # человекочитаемая история ходов для UI-TARS
        done_steps: list[str] = []  # краткий отчёт для оркестратора

        for step in range(1, self.max_steps + 1):
            shot = await self._screenshot()
            if not shot.get("ok"):
                msg = shot.get("error", "не удалось снять скриншот")
                await self._emit(emit, "thought", text=f"UI-TARS: {msg}")
                return {"ok": False, "content": f"GUI: {msg}", "steps": done_steps}

            try:
                raw = await self._ask_uitars(goal, history, shot["image_b64"],
                                             shot.get("image_fmt", "png"))
            except Exception as exc:  # noqa: BLE001
                log.warning("UI-TARS недоступен: %s", exc)
                return {"ok": False, "content": f"UI-TARS недоступен: {exc}",
                        "steps": done_steps}

            act = _parse_uitars_action(raw)
            if not act:
                await self._emit(emit, "thought",
                                 text=f"UI-TARS дал неразборчивый ответ: {raw[:160]}")
                # один «холостой» ход допустим, но не зацикливаемся
                history.append(f"Step {step}: (неразборчивый ответ модели)")
                continue

            thought = act.get("thought") or ""
            kind = act["kind"]
            if thought:
                await self._emit(emit, "thought", text=f"[шаг {step}] {thought}")

            # --- терминальные исходы ---
            if kind in ("finished", "done", "complete"):
                content = act.get("content") or "цель достигнута"
                await self._emit(emit, "tool_result", ok=True,
                                 summary=f"UI-TARS завершил: {content}")
                done_steps.append(f"finished: {content}")
                return {"ok": True,
                        "content": f"Визуальная задача выполнена за {step} шаг(ов). {content}",
                        "steps": done_steps}
            if kind in ("fail", "impossible"):
                content = act.get("content") or "не удалось выполнить визуально"
                await self._emit(emit, "tool_result", ok=False,
                                 summary=f"UI-TARS остановился: {content}")
                return {"ok": False, "content": f"GUI не справился: {content}",
                        "steps": done_steps}
            if kind == "wait":
                await self._emit(emit, "thought", text="UI-TARS ждёт: экран ещё меняется.")
                history.append(f"Step {step}: wait()")
                continue

            # --- исполнить действие на хосте ---
            await self._emit(emit, "tool_call", tool=f"gui.{kind}",
                             args=self._action_args(act))
            ok, desc = await self._execute(act, shot["screen_w"], shot["screen_h"])
            await self._emit(emit, "tool_result", ok=ok, summary=desc)
            done_steps.append(desc)
            history.append(f"Step {step}: {self._action_repr(act)}"
                           + ("" if ok else "  → НЕ УДАЛОСЬ"))
            history = history[-GUI_HISTORY_KEEP:]

        # лимит шагов исчерпан — отдаём, что есть (не падаем)
        tail = "; ".join(done_steps[-4:]) or "без видимого прогресса"
        await self._emit(emit, "thought",
                         text=f"UI-TARS: достигнут лимит {self.max_steps} шагов.")
        return {"ok": False,
                "content": f"GUI: достигнут лимит {self.max_steps} шагов. Последнее: {tail}",
                "steps": done_steps}

    # ----------------------------------------------------------------- internals
    async def _screenshot(self) -> dict[str, Any]:
        shot = await self.bridge.call("screenshot", {"return_b64": True})
        result = (shot.get("result", {}) or {})
        img = result.get("image_b64")
        if not shot.get("ok") or not img:
            return {"ok": False, "error": shot.get("error", "нет image_b64 в ответе моста")}
        return {"ok": True, "image_b64": img,
                "image_fmt": result.get("image_fmt", "png"),
                "screen_w": int(result.get("screen_w", 1920) or 1920),
                "screen_h": int(result.get("screen_h", 1080) or 1080)}

    async def _ask_uitars(self, goal: str, history: list[str], img_b64: str,
                          img_fmt: str = "png") -> str:
        hist_text = "\n".join(history) if history else "(это первый шаг)"
        user_text = (f"## Task\n{goal}\n\n"
                     f"## Your previous steps\n{hist_text}\n\n"
                     "Look at the current screenshot and output the next action.")
        messages = [
            {"role": "system", "content": _UITARS_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{img_fmt};base64,{img_b64}"}},
            ]},
        ]
        return await llm.chat(messages, base_url=llm.UITARS_URL, model=llm.UITARS_MODEL,
                              temperature=0.0, max_tokens=256, timeout=90)

    async def _execute(self, act: dict[str, Any], screen_w: int,
                       screen_h: int) -> tuple[bool, str]:
        """Преобразовать действие UI-TARS в вызов RPC-моста."""
        kind = act["kind"]

        def px(v: Any, dim: int) -> int:
            iv = int(v)
            return iv if iv > 1000 else int(iv / 1000.0 * dim)

        if kind in ("click", "left_single", "left_double", "double_click",
                    "right_single", "right_click"):
            button = "right" if "right" in kind else "left"
            double = "double" in kind
            res = await self.bridge.call("mouse_click", {
                "x": px(act.get("x", 0), screen_w), "y": px(act.get("y", 0), screen_h),
                "button": button, "double": double})
            return self._ok(res, f"{'двойной ' if double else ''}клик "
                                 f"{button} в ({act.get('x')},{act.get('y')})")
        if kind == "type":
            res = await self.bridge.call("type_text", {"text": act.get("content", "")})
            return self._ok(res, f"ввод текста: {act.get('content', '')[:60]}")
        if kind == "hotkey":
            keys = str(act.get("key", "")).replace(" ", "+")
            res = await self.bridge.call("key_press", {"keys": keys})
            return self._ok(res, f"горячие клавиши: {keys}")
        if kind == "scroll":
            direction = act.get("direction", "down")
            if "x" in act:
                await self.bridge.call("mouse_move",
                                       {"x": px(act["x"], screen_w), "y": px(act["y"], screen_h)})
            amount = {"down": -3, "up": 3, "left": -3, "right": 3}.get(direction, -3)
            res = await self.bridge.call("scroll", {"amount": amount, "direction": direction})
            return self._ok(res, f"прокрутка {direction}")
        if kind == "drag":
            res = await self.bridge.call("drag", {
                "x": px(act.get("x", 0), screen_w), "y": px(act.get("y", 0), screen_h),
                "x2": px(act.get("x2", 0), screen_w), "y2": px(act.get("y2", 0), screen_h)})
            return self._ok(res, f"перетаскивание ({act.get('x')},{act.get('y')})→"
                                 f"({act.get('x2')},{act.get('y2')})")
        return False, f"неизвестное GUI-действие '{kind}'"

    @staticmethod
    def _ok(res: dict[str, Any], desc: str) -> tuple[bool, str]:
        if res.get("ok"):
            return True, desc
        return False, f"{desc} — ошибка: {res.get('error', 'мост вернул сбой')}"

    @staticmethod
    def _action_args(act: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in act.items() if k not in ("kind", "thought")}

    @staticmethod
    def _action_repr(act: dict[str, Any]) -> str:
        kind = act["kind"]
        if kind in ("click", "left_double", "right_single", "scroll"):
            return f"{kind}({act.get('x')},{act.get('y')})"
        if kind == "drag":
            return f"drag({act.get('x')},{act.get('y')}->{act.get('x2')},{act.get('y2')})"
        if kind == "type":
            return f"type('{act.get('content', '')[:40]}')"
        if kind == "hotkey":
            return f"hotkey('{act.get('key', '')}')"
        return kind

    @staticmethod
    async def _emit(emit: Optional[EmitFn], etype: str, **fields: Any) -> None:
        if emit is None:
            return
        try:
            await emit({"type": etype, "actor": "ui-tars", **fields})
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Разбор ответа UI-TARS «Thought: …\nAction: click(start_box='(x,y)')»
# --------------------------------------------------------------------------- #
def _parse_uitars_action(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    thought = ""
    tm = re.search(r"Thought\s*:\s*(.+?)(?:\n\s*Action\s*:|\Z)", text,
                   re.IGNORECASE | re.DOTALL)
    if tm:
        thought = tm.group(1).strip().replace("\n", " ")[:240]

    m = re.search(r"Action\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    seg = (m.group(1) if m else text).strip()
    call = re.search(r"([A-Za-z_]+)\s*\((.*)\)", seg, re.DOTALL)
    low = seg.lower()
    if not call:
        if any(w in low for w in ("finish", "done", "complete")):
            return {"kind": "finished", "content": seg[:160], "thought": thought}
        if "wait" in low:
            return {"kind": "wait", "thought": thought}
        if any(w in low for w in ("fail", "impossible", "cannot")):
            return {"kind": "fail", "content": seg[:160], "thought": thought}
        return None

    out: dict[str, Any] = {"kind": call.group(1).lower(), "thought": thought}
    arg = call.group(2)
    sb = re.search(r"start_box\s*=\s*['\"]?\(?\s*(\d+)\s*,\s*(\d+)", arg)
    if sb:
        out["x"], out["y"] = int(sb.group(1)), int(sb.group(2))
    # запасной разбор «point='(x,y)'» / голой пары «(x,y)» (формат UI-TARS-1.5)
    if "x" not in out:
        pt = re.search(r"(?:point\s*=\s*)?['\"]?\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", arg)
        if pt:
            out["x"], out["y"] = int(pt.group(1)), int(pt.group(2))
    eb = re.search(r"end_box\s*=\s*['\"]?\(?\s*(\d+)\s*,\s*(\d+)", arg)
    if eb:
        out["x2"], out["y2"] = int(eb.group(1)), int(eb.group(2))
    cont = re.search(r"content\s*=\s*'([^']*)'|content\s*=\s*\"([^\"]*)\"", arg)
    if cont:
        out["content"] = cont.group(1) if cont.group(1) is not None else cont.group(2)
    key = re.search(r"key\s*=\s*'([^']*)'|key\s*=\s*\"([^\"]*)\"", arg)
    if key:
        out["key"] = key.group(1) if key.group(1) is not None else key.group(2)
    direction = re.search(r"direction\s*=\s*['\"]?(\w+)", arg)
    if direction:
        out["direction"] = direction.group(1).lower()
    return out
