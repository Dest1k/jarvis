#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hf_downloader.py — надёжный загрузчик моделей с HuggingFace для JARVIS-OS.

Зачем свой загрузчик: штатный hf_transfer/huggingface_hub в контейнере вёл себя
нестабильно («качает то не качает») и почти без индикации. Этот загрузчик:
  • качает напрямую через resolve-URL HuggingFace (requests, без pip/PyPI);
  • НОРМАЛЬНАЯ ДОКАЧКА по HTTP Range (продолжает с места обрыва, .part-файлы);
  • RECHECK целостности: размер всегда + sha256 для LFS-файлов (HF отдаёт sha256
    в tree API); при несовпадении — перекачивает;
  • устойчивые РЕТРАИ с экспоненциальной задержкой при обрывах/SSL;
  • ЖИВАЯ ИНДИКАЦИЯ: прогресс-бар, скорость (МБ/с), скачано/всего, ETA;
  • ТОКЕН HF из файла hf_token.txt / env HF_TOKEN (для gated-моделей и лимитов).

Использование (CLI):
    python hf_downloader.py Qwen/Qwen2.5-Coder-32B-Instruct-AWQ --dest D:\jarvis\data\models\qwen-coder
    python hf_downloader.py <repo> --dest <dir> [--revision main] [--token-file hf_token.txt] [--verify]

Как библиотека:
    import hf_downloader
    hf_downloader.download_repo("Qwen/...", Path(r"D:\jarvis\data\models\qwen-coder"))

Зависимость: requests (стандартная для проекта). Остальное — stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover
    print("[ОШИБКА] Требуется пакет 'requests'. Установите: pip install requests")
    sys.exit(1)


HF_HOST = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
CHUNK = 1 << 20            # 1 МиБ
WINDOW_SEC = 3.0          # окно усреднения скорости
PRINT_EVERY = 0.2         # частота обновления индикатора, с


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
def human_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def fmt_eta(sec: float) -> str:
    if sec <= 0 or sec != sec or sec == float("inf"):
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# Индикатор прогресса (скорость + ETA), обновление через \r
# --------------------------------------------------------------------------- #
class Progress:
    def __init__(self, total: int, label: str, done: int = 0) -> None:
        self.total = max(total, 1)
        self.label = label[:42]
        self.done = done
        self.last_print = 0.0
        self.window: list[tuple[float, int]] = [(time.time(), done)]

    def update(self, n: int) -> None:
        self.done += n
        now = time.time()
        self.window.append((now, self.done))
        while len(self.window) > 1 and now - self.window[0][0] > WINDOW_SEC:
            self.window.pop(0)
        if now - self.last_print >= PRINT_EVERY:
            self.last_print = now
            self._render()

    def _speed(self) -> float:
        if len(self.window) < 2:
            return 0.0
        dt = self.window[-1][0] - self.window[0][0]
        db = self.window[-1][1] - self.window[0][1]
        return db / dt if dt > 0 else 0.0

    def _render(self) -> None:
        speed = self._speed()
        pct = self.done / self.total * 100
        eta = (self.total - self.done) / speed if speed > 0 else 0
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        sys.stdout.write(
            f"\r  {self.label:<42} [{bar}] {pct:5.1f}%  "
            f"{human_bytes(self.done)}/{human_bytes(self.total)}  "
            f"{human_bytes(speed)}/с  ETA {fmt_eta(eta)}   "
        )
        sys.stdout.flush()

    def finish(self) -> None:
        self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Токен
# --------------------------------------------------------------------------- #
def load_token(token_file: Optional[Path] = None, explicit: str = "") -> str:
    """Токен из явного аргумента → env HF_TOKEN → файла hf_token.txt."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if env:
        return env.strip()
    candidates = [token_file] if token_file else []
    candidates.append(Path(__file__).resolve().parent / "hf_token.txt")
    for path in candidates:
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "ВАШ_ТОКЕН" not in line:
                    return line
    return ""


# --------------------------------------------------------------------------- #
# Листинг файлов репозитория (tree API, с пагинацией и LFS-sha256)
# --------------------------------------------------------------------------- #
def list_repo_files(session: requests.Session, repo: str, revision: str,
                    token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{HF_HOST}/api/models/{repo}/tree/{revision}?recursive=1"
    files: list[dict] = []
    while url:
        r = session.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        for e in r.json():
            if e.get("type") != "file":
                continue
            lfs = e.get("lfs") or None
            size = int(lfs["size"]) if lfs else int(e.get("size", 0))
            sha = lfs.get("oid") if lfs else None      # sha256 только для LFS
            files.append({"path": e["path"], "size": size, "sha256": sha})
        # пагинация через Link: <...>; rel="next"
        url = r.links.get("next", {}).get("url", "")
    return files


# --------------------------------------------------------------------------- #
# Скачивание одного файла с докачкой, ретраями и проверкой
# --------------------------------------------------------------------------- #
def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(session: requests.Session, repo: str, revision: str, finfo: dict,
                  dest_dir: Path, token: str, *, retries: int = 8, verify: bool = False,
                  log: Callable[[str], None] = print) -> bool:
    rel = finfo["path"]
    size = finfo["size"]
    sha256 = finfo.get("sha256")
    dest = dest_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    headers_base = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{HF_HOST}/{repo}/resolve/{revision}/{quote(rel, safe='/')}"

    # Уже скачан целиком?
    if dest.exists() and dest.stat().st_size == size:
        if verify and sha256:
            log(f"  проверка {rel}…")
            if _sha256_of(dest) == sha256:
                log(f"  ✓ {rel} (есть, sha256 ок)")
                return True
            log(f"  ✗ {rel}: sha256 не совпал — перекачиваю.")
            dest.unlink()
        else:
            return True   # размер совпал — считаем готовым (быстрый путь)

    for attempt in range(1, retries + 1):
        resume_from = part.stat().st_size if part.exists() else 0
        if resume_from > size:
            part.unlink(missing_ok=True)
            resume_from = 0
        hasher = hashlib.sha256() if sha256 else None
        if resume_from and hasher:                       # досчитываем хеш по уже скачанному
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    hasher.update(chunk)
        headers = dict(headers_base)
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        try:
            r = session.get(url, headers=headers, stream=True, timeout=(15, 120))
            if resume_from and r.status_code == 200:     # сервер проигнорировал Range
                resume_from = 0
                hasher = hashlib.sha256() if sha256 else None
            elif r.status_code not in (200, 206):
                r.raise_for_status()
            mode = "ab" if resume_from else "wb"
            prog = Progress(size, dest.name, done=resume_from)
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK):
                    if not chunk:
                        continue
                    f.write(chunk)
                    if hasher:
                        hasher.update(chunk)
                    prog.update(len(chunk))
            prog.finish()
        except (requests.RequestException, OSError) as exc:
            wait = min(2 ** attempt, 30)
            log(f"  обрыв на {rel} ({exc.__class__.__name__}); докачка через {wait} с "
                f"(попытка {attempt}/{retries})…")
            time.sleep(wait)
            continue

        actual = part.stat().st_size if part.exists() else 0
        if actual != size:
            log(f"  {rel}: размер {human_bytes(actual)}≠{human_bytes(size)} — продолжаю докачку.")
            continue
        if sha256 and hasher and hasher.hexdigest() != sha256:
            log(f"  {rel}: sha256 не совпал — перекачиваю файл с нуля.")
            part.unlink(missing_ok=True)
            continue
        part.replace(dest)
        return True

    log(f"  ✗ {rel}: не удалось скачать за {retries} попыток.")
    return False


# --------------------------------------------------------------------------- #
# Скачивание целого репозитория
# --------------------------------------------------------------------------- #
def download_repo(repo: str, dest_dir: Path, *, token: str = "", revision: str = "main",
                  retries: int = 8, verify: bool = False,
                  log: Callable[[str], None] = print) -> bool:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    token = token or load_token()
    session = requests.Session()
    log(f"→ Репозиторий {repo} → {dest_dir}")
    try:
        files = list_repo_files(session, repo, revision, token)
    except Exception as exc:  # noqa: BLE001
        log(f"  ✗ Не удалось получить список файлов {repo}: {exc}")
        return False
    if not files:
        log(f"  ✗ Список файлов пуст (репозиторий/ревизия не найдены или нет доступа?).")
        return False

    total_bytes = sum(f["size"] for f in files)
    have = 0
    for f in files:
        d = dest_dir / f["path"]
        if d.exists() and d.stat().st_size == f["size"]:
            have += f["size"]
    log(f"  Файлов: {len(files)} | объём: {human_bytes(total_bytes)} | "
        f"уже есть: {human_bytes(have)} ({have / max(total_bytes,1) * 100:.0f}%)")

    ok_all = True
    for i, f in enumerate(files, 1):
        log(f"[{i}/{len(files)}] {f['path']}  ({human_bytes(f['size'])})"
            + ("  [LFS, sha256]" if f.get("sha256") else ""))
        ok = download_file(session, repo, revision, f, dest_dir, token,
                           retries=retries, verify=verify, log=log)
        ok_all = ok_all and ok
    if ok_all:
        log(f"  ✔ Репозиторий {repo} скачан полностью в {dest_dir}")
    else:
        log(f"  ⚠ Репозиторий {repo} скачан НЕ полностью — повторный запуск продолжит докачку.")
    return ok_all


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _setup_console_utf8() -> None:
    """UTF-8 для консоли Windows (кириллица и символы бара), при запуске как CLI."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    _setup_console_utf8()
    p = argparse.ArgumentParser(description="JARVIS-OS — надёжный загрузчик моделей с HuggingFace.")
    p.add_argument("repo", help="ID репозитория HF, напр. Qwen/Qwen2.5-Coder-32B-Instruct-AWQ")
    p.add_argument("--dest", required=True, help="Каталог назначения.")
    p.add_argument("--revision", default="main")
    p.add_argument("--token-file", default="", help="Файл с токеном HF (по умолчанию hf_token.txt).")
    p.add_argument("--token", default="", help="Токен HF напрямую (не рекомендуется).")
    p.add_argument("--retries", type=int, default=8)
    p.add_argument("--verify", action="store_true",
                   help="Перепроверить sha256 уже скачанных LFS-файлов (медленно).")
    args = p.parse_args()

    token = load_token(Path(args.token_file) if args.token_file else None, args.token)
    if token:
        print("Использую токен HF (из файла/окружения).")
    ok = download_repo(args.repo, Path(args.dest), token=token, revision=args.revision,
                       retries=args.retries, verify=args.verify)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl+C). Повторный запуск продолжит докачку.")
        sys.exit(130)
