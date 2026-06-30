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

Координаты — ПРОБЛЕМА совместимости версий UI-TARS:
    • UI-TARS-2B-SFT отдаёт НОРМИРОВАННЫЕ координаты [0,1000].
    • UI-TARS-1.5-7B / 2.0 отдают АБСОЛЮТНЫЕ пиксели в системе координат картинки,
      которую им прислали.
Чтобы работали ОБЕ, режим задаётся env JARVIS_UITARS_COORD_MODE (auto|absolute|
normalized) — в профиле. Скриншот ужимается до размера в бюджете процессора
Qwen-VL (≤~1 Мпикс), его реальные размеры (img_w/img_h) приходят от моста, и
абсолютные пиксели мапятся в экран как coord/img*screen. Нормированные — /1000*screen.

Надёжность генерации: UI-TARS-1.5 в AWQ склонен к вырождению («assistant
assistant…»). Лечим stop-токенами и repetition_penalty, а парсер действий —
максимально толерантный (box-токены, point=, голые пары, мусор по краям).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional

from . import llm

log = logging.getLogger("jarvis.gui")

# Сколько атомарных действий максимум на одну визуальную подзадачу.
GUI_MAX_STEPS = int(os.environ.get("JARVIS_GUI_MAX_STEPS", "14"))
# Сколько прошлых ходов (мысль+действие) держим в контексте UI-TARS.
GUI_HISTORY_KEEP = 8
# Режим координат UI-TARS: auto | absolute | normalized. Задаётся профилем.
COORD_MODE = os.environ.get("JARVIS_UITARS_COORD_MODE", "auto").strip().lower()

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


def _uitars_system(mode: str, img_w: int, img_h: int) -> str:
    if mode == "normalized":
        coord = ("All coordinates are NORMALIZED integers in [0,1000]: x left→right, "
                 "y top→bottom, relative to the whole screenshot.")
    else:  # absolute (и auto — поведение модели 1.5/2.0)
        coord = (f"All coordinates are ABSOLUTE pixel positions (x,y) inside the "
                 f"screenshot, which is {img_w}x{img_h} pixels. (0,0) is top-left.")
    return (
        "You are UI-TARS, an autonomous GUI agent operating a real computer through "
        "screenshots. You are given a TASK, the history of your previous steps, and "
        "the CURRENT screenshot. Output the single best NEXT action that advances the "
        "task, then stop.\n\n"
        f"## Coordinate system\n{coord}\n\n"
        "## Action space (output EXACTLY ONE action)\n"
        "click(start_box='(x,y)')\n"
        "left_double(start_box='(x,y)')\n"
        "right_single(start_box='(x,y)')\n"
        "drag(start_box='(x,y)', end_box='(x,y)')\n"
        "hotkey(key='ctrl s')            # space-separated keys: 'ctrl c', 'alt tab', 'enter'\n"
        "type(content='text to type')    # types text into the focused field\n"
        "scroll(start_box='(x,y)', direction='down')   # up | down | left | right\n"
        "wait()                          # screen still loading/animating\n"
        "finished(content='short result')  # the TASK is fully done\n"
        "fail(content='why impossible')    # truly cannot be done via GUI\n\n"
        "## Output format — EXACTLY two lines, nothing else, no extra words\n"
        "Thought: <one short sentence>\n"
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
        bad = 0                     # подряд неразборчивых ответов модели

        for step in range(1, self.max_steps + 1):
            shot = await self._screenshot()
            if not shot.get("ok"):
                msg = shot.get("error", "не удалось снять скриншот")
                await self._emit(emit, "thought", text=f"UI-TARS: {msg}")
                return {"ok": False, "content": f"GUI: {msg}", "steps": done_steps}

            try:
                raw = await self._ask_uitars(goal, history, shot)
            except Exception as exc:  # noqa: BLE001
                log.warning("UI-TARS недоступен: %s", exc)
                return {"ok": False, "content": f"UI-TARS недоступен: {exc}",
                        "steps": done_steps}

            act = _parse_uitars_action(raw)
            if not act:
                bad += 1
                await self._emit(emit, "thought",
                                 text=f"UI-TARS дал неразборчивый ответ ({bad}/3).")
                history.append(f"Step {step}: (модель ответила неразборчиво)")
                if bad >= 3:        # три подряд — выходим, а не крутим до лимита
                    return {"ok": False,
                            "content": "GUI: UI-TARS выдаёт неразборчивые ответы "
                                       "(возможно, не та версия/квантизация модели). "
                                       "Лучше решить задачу через CLI.",
                            "steps": done_steps}
                continue
            bad = 0

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
            ok, desc = await self._execute(act, shot)
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
        sw = int(result.get("screen_w", 1920) or 1920)
        sh = int(result.get("screen_h", 1080) or 1080)
        # img_w/h — реальные размеры присланной картинки (она могла быть ужата).
        # Если мост их не дал — считаем, что картинка == экран.
        return {"ok": True, "image_b64": img,
                "image_fmt": result.get("image_fmt", "png"),
                "screen_w": sw, "screen_h": sh,
                "img_w": int(result.get("img_w", sw) or sw),
                "img_h": int(result.get("img_h", sh) or sh)}

    async def _ask_uitars(self, goal: str, history: list[str],
                          shot: dict[str, Any]) -> str:
        hist_text = "\n".join(history) if history else "(это первый шаг)"
        user_text = (f"## Task\n{goal}\n\n"
                     f"## Your previous steps\n{hist_text}\n\n"
                     "Look at the current screenshot and output the next action.")
        messages = [
            {"role": "system", "content": _uitars_system(COORD_MODE,
                                                         shot["img_w"], shot["img_h"])},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{shot['image_fmt']};base64,"
                                      f"{shot['image_b64']}"}},
            ]},
        ]
        # stop + repetition_penalty — против вырождения генерации UI-TARS.
        return await llm.chat(
            messages, base_url=llm.UITARS_URL, model=llm.UITARS_MODEL,
            temperature=0.0, max_tokens=300, timeout=90,
            stop=["<|im_end|>", "<|im_start|>", "\nThought:", "\nuser", "<|eot_id|>"],
            extra_body={"repetition_penalty": 1.1, "skip_special_tokens": True})

    async def _execute(self, act: dict[str, Any], shot: dict[str, Any]) -> tuple[bool, str]:
        """Преобразовать действие UI-TARS в вызов RPC-моста (с маппингом координат)."""
        kind = act["kind"]
        sw, sh = shot["screen_w"], shot["screen_h"]
        iw, ih = shot["img_w"], shot["img_h"]

        def px(v: Any, screen_dim: int, img_dim: int) -> int:
            v = int(v)
            if COORD_MODE == "normalized":
                return max(0, min(screen_dim, int(v / 1000.0 * screen_dim)))
            if COORD_MODE == "absolute":
                base = img_dim or screen_dim
                return max(0, min(screen_dim, int(v / base * screen_dim)))
            # auto: >1000 → точно пиксели картинки; иначе считаем нормировкой.
            if v > 1000:
                base = img_dim or screen_dim
                return max(0, min(screen_dim, int(v / base * screen_dim)))
            return max(0, min(screen_dim, int(v / 1000.0 * screen_dim)))

        if kind in ("click", "left_single", "left_double", "double_click",
                    "right_single", "right_click"):
            button = "right" if "right" in kind else "left"
            double = "double" in kind
            res = await self.bridge.call("mouse_click", {
                "x": px(act.get("x", 0), sw, iw), "y": px(act.get("y", 0), sh, ih),
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
                                       {"x": px(act["x"], sw, iw), "y": px(act["y"], sh, ih)})
            amount = {"down": -3, "up": 3, "left": -3, "right": 3}.get(direction, -3)
            res = await self.bridge.call("scroll", {"amount": amount, "direction": direction})
            return self._ok(res, f"прокрутка {direction}")
        if kind == "drag":
            res = await self.bridge.call("drag", {
                "x": px(act.get("x", 0), sw, iw), "y": px(act.get("y", 0), sh, ih),
                "x2": px(act.get("x2", 0), sw, iw), "y2": px(act.get("y2", 0), sh, ih)})
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


_DESCRIBE_SYSTEM = (
    "You are a precise vision assistant looking at a screenshot of a computer screen. "
    "Describe what is actually visible: the foreground/active window and its title, "
    "open applications, taskbar, desktop icons, key buttons/menus and any readable "
    "text. Be concrete and factual — do NOT guess or invent. "
    "Answer in RUSSIAN, в 3-7 коротких пунктов."
)


async def describe_screen(bridge: Any, question: str = "") -> dict[str, Any]:
    """
    Снять скриншот и ОПИСАТЬ словами, что на экране (через зрение UI-TARS).
    Это «глаза» оркестратора: позволяет ответить «что на рабочем столе/экране».
    Возвращает {ok, content}.
    """
    if bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен — не вижу экран."}
    shot = await bridge.call("screenshot", {"return_b64": True})
    result = (shot.get("result", {}) or {})
    img = result.get("image_b64")
    if not shot.get("ok") or not img:
        return {"ok": False,
                "content": f"Не удалось снять скриншот: {shot.get('error', 'нет image_b64')}."}
    fmt = result.get("image_fmt", "png")
    q = (question or "").strip()
    user_text = ("Опиши, что сейчас на экране." if not q
                 else f"Глядя на экран, ответь: {q}")
    messages = [
        {"role": "system", "content": _DESCRIBE_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url",
             "image_url": {"url": f"data:image/{fmt};base64,{img}"}},
        ]},
    ]
    # Описание экрана делает МУЛЬТИМОДАЛЬНЫЙ ДИСПЕТЧЕР (в профиле gemma12 это
    # Gemma-4-12B — настоящий VLM, описывает куда лучше, чем action-модель UI-TARS).
    # Если диспетчер текстовый (qwen-classic) — он отвергнет картинку, тогда
    # откатываемся на UI-TARS. Порядок настраивается JARVIS_VISION_MODEL.
    pref = os.environ.get("JARVIS_VISION_MODEL", "auto").strip().lower()
    if pref == "uitars":
        targets = [(llm.UITARS_URL, llm.UITARS_MODEL)]
    elif pref == "dispatcher":
        targets = [(llm.QWEN_URL, llm.QWEN_MODEL)]
    else:  # auto — сначала умный диспетчер, потом UI-TARS
        targets = [(llm.QWEN_URL, llm.QWEN_MODEL), (llm.UITARS_URL, llm.UITARS_MODEL)]

    last_err = ""
    for url, model in targets:
        try:
            text = await llm.chat(messages, base_url=url, model=model, temperature=0.1,
                                  max_tokens=400, timeout=90,
                                  stop=["<|im_end|>", "<|im_start|>"],
                                  extra_body={"repetition_penalty": 1.1})
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
        text = (text or "").strip()
        if text:
            return {"ok": True, "content": text}
    return {"ok": False,
            "content": f"Не удалось получить описание экрана от моделей зрения. {last_err}".strip()}


# --------------------------------------------------------------------------- #
# Разбор ответа UI-TARS — максимально толерантный к версиям и мусору.
# Поддержка: 'Action: click(start_box='(x,y)')', point='<point>x y</point>',
# '<|box_start|>(x,y)<|box_end|>', голые пары '(x,y)', координаты с пробелом/без
# запятой, ведущий мусор ('<', 'gui:').
# --------------------------------------------------------------------------- #
def _parse_uitars_action(text: str) -> Optional[dict[str, Any]]:
    if not text or not text.strip():
        return None
    thought = ""
    tm = re.search(r"Thought\s*:\s*(.+?)(?:\n\s*Action\s*:|\Z)", text,
                   re.IGNORECASE | re.DOTALL)
    if tm:
        thought = tm.group(1).strip().replace("\n", " ")[:240]

    m = re.search(r"Action\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    seg = (m.group(1) if m else text).strip()
    seg = seg.lstrip("<> \t")            # срезаем ведущий мусор вида '< Action'
    low = seg.lower()
    call = re.search(r"([A-Za-z_]+)\s*\((.*)\)", seg, re.DOTALL)
    if not call:
        # действие без скобок/обрезанное — пытаемся понять намерение
        if any(w in low for w in ("finish", "done", "complete", "success")):
            return {"kind": "finished", "content": seg[:160], "thought": thought}
        if "wait" in low:
            return {"kind": "wait", "thought": thought}
        if any(w in low for w in ("fail", "impossible", "cannot", "can't")):
            return {"kind": "fail", "content": seg[:160], "thought": thought}
        return None

    out: dict[str, Any] = {"kind": call.group(1).lower(), "thought": thought}
    arg = call.group(2)

    # координаты: первая пара чисел (start), вторая (end). Разделитель — запятая
    # ИЛИ пробел; терпим '<|box_start|>', 'point=', скобки и кавычки.
    nums = re.findall(r"-?\d+", arg)
    sb = re.search(r"start_box\s*=\s*['\"]?[^0-9-]*(-?\d+)[,\s]+(-?\d+)", arg)
    pt = re.search(r"(?:point|box)\s*=\s*['\"<|]*[^0-9-]*(-?\d+)[,\s]+(-?\d+)", arg)
    if sb:
        out["x"], out["y"] = int(sb.group(1)), int(sb.group(2))
    elif pt:
        out["x"], out["y"] = int(pt.group(1)), int(pt.group(2))
    elif len(nums) >= 2 and out["kind"] in (
            "click", "left_double", "left_single", "double_click",
            "right_single", "right_click", "scroll", "drag", "move", "hover"):
        out["x"], out["y"] = int(nums[0]), int(nums[1])

    eb = re.search(r"end_box\s*=\s*['\"]?[^0-9-]*(-?\d+)[,\s]+(-?\d+)", arg)
    if eb:
        out["x2"], out["y2"] = int(eb.group(1)), int(eb.group(2))
    elif out["kind"] == "drag" and len(nums) >= 4:
        out["x2"], out["y2"] = int(nums[2]), int(nums[3])

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
