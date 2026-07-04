# -*- coding: utf-8 -*-
"""
ingest.py — асинхронный конвейер загрузки файлов и RAG-инъекции JARVIS OS.

Стадии (file_attachments_registry.ingest_status):
    pending → parsing → embedding → ready | failed

Требования, воплощённые здесь:
    • Проверка целостности + дедуп по sha256 (уникальность на владельца).
    • Определение MIME (по сигнатуре + расширению, без внешних зависимостей).
    • Парсинг текста/кода (utf-8); pdf/docx — best-effort (если есть pypdf/
      python-docx), иначе честная деградация со статусом failed и подсказкой.
    • Чанкинг по токенам с overlap (оценка токенов — та же эвристика, что у llm).
    • Эмбеддинги: локальный, БЕЗ внешних зависимостей и VRAM (hashing-bag
      384-мерный) — детерминирован, оффлайн, мгновенный. Интерфейс embed()
      единая точка для замены на модельные эмбеддинги (vLLM /v1/embeddings).
    • Семантический поиск (cosine) по чанкам сессии → инъекция в RAG-контекст.

ВСЕ тяжёлые операции (парсинг/эмбеддинг/поиск) — через db-пул и чистый CPU,
event loop не блокируется (обёртки в db.execute/query уже offloaded).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
import time
from pathlib import Path
from typing import Any, Optional

from . import db

# Корневой каталог ядра (том данных). Тот же, что у db.py/config.py.
CORE_DIR = Path(os.environ.get("JARVIS_CORE_DIR", "./.jarvis_core"))
FILES_DIR = CORE_DIR / "files"
EMBED_DIM = 384
CHUNK_TOKENS = int(os.environ.get("JARVIS_RAG_CHUNK_TOKENS", "400"))
CHUNK_OVERLAP = int(os.environ.get("JARVIS_RAG_CHUNK_OVERLAP", "60"))

# Расширения, которые парсим как обычный текст/код (основной кейс sysadmin).
_TEXT_EXT = {
    ".txt", ".md", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".csv", ".tsv", ".xml", ".html", ".css",
    ".py", ".js", ".ts", ".tsx", ".sh", ".bash", ".ps1", ".bat", ".sql",
    ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".java", ".rb", ".php",
    ".dockerfile", ".tf", ".hcl", ".service", ".nginx",
}


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_mime(filename: str, data: bytes) -> str:
    """MIME по сигнатуре + расширению (без python-magic)."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        # docx/xlsx/zip — по расширению уточним
        ext = Path(filename).suffix.lower()
        return {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                }.get(ext.lstrip("."), "application/zip")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    ext = Path(filename).suffix.lower()
    if ext in _TEXT_EXT:
        return "text/plain"
    # эвристика: если декодируется как utf-8 без «мусора» — текст
    try:
        data[:2048].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def parse_text(data: bytes, mime: str, filename: str) -> tuple[Optional[str], Optional[str]]:
    """
    Извлечь текст. Возврат (text, error). text=None + error → деградация.
    """
    ext = Path(filename).suffix.lower()
    if mime.startswith("text/") or ext in _TEXT_EXT or mime in ("application/json", "application/xml"):
        return data.decode("utf-8", errors="replace"), None
    if mime == "application/pdf":
        try:
            import io
            from pypdf import PdfReader  # опциональная зависимость
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages), None
        except Exception as exc:  # noqa: BLE001
            return None, f"PDF не распознан (нужен пакет pypdf): {exc}"
    if mime.endswith("wordprocessingml.document"):
        try:
            import io
            import docx  # python-docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs), None
        except Exception as exc:  # noqa: BLE001
            return None, f"DOCX не распознан (нужен пакет python-docx): {exc}"
    if mime.startswith("image/"):
        return None, "Изображения не парсятся в текст (нужен OCR/VL-модель)."
    # последняя попытка — как текст
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"Бинарный файл ({mime}) — извлечение текста не поддержано."


def _estimate_tokens(text: str) -> int:
    return int(len(text) / 3.0) + 1


def chunk_text(text: str, max_tokens: int = CHUNK_TOKENS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Разбить текст на чанки ~max_tokens с overlap (по абзацам/строкам)."""
    text = text.strip()
    if not text:
        return []
    # делим по абзацам, затем добираем до бюджета
    paras = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for para in paras:
        ptok = _estimate_tokens(para)
        if ptok > max_tokens:
            # длинный абзац — режем по строкам
            for line in para.splitlines():
                ltok = _estimate_tokens(line)
                if buf_tok + ltok > max_tokens and buf:
                    chunks.append("\n".join(buf)); buf, buf_tok = [], 0
                buf.append(line); buf_tok += ltok
            continue
        if buf_tok + ptok > max_tokens and buf:
            chunks.append("\n".join(buf))
            # overlap: перенести хвост
            tail = "\n".join(buf)[-overlap * 3:]
            buf, buf_tok = ([tail] if tail else []), _estimate_tokens(tail)
        buf.append(para); buf_tok += ptok
    if buf:
        chunks.append("\n".join(buf))
    return [c.strip() for c in chunks if c.strip()]


# --------------------------------------------------------------------------- #
# Эмбеддинги: провайдер local (hashing-bag, оффлайн) ИЛИ vllm (/v1/embeddings).
#   JARVIS_EMBED_PROVIDER=local|vllm  (по умолчанию local — ноль зависимостей)
#   JARVIS_EMBED_URL=http://vllm-qwen-coder:8001/v1  JARVIS_EMBED_MODEL=<id>
# Модельный провайдер — с АВТО-FALLBACK на local при любой ошибке (никогда не
# роняет ingestion). L2-нормализация в обоих случаях → cosine=dot.
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[a-zA-Zа-яёА-ЯЁ0-9_]+")


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """
    Локальный хеширующий bag-of-words эмбеддинг (детерминирован, оффлайн, ноль
    VRAM). Ловит лексическое пересечение — достаточно для RAG по конфигам/логам/
    коду и служит FALLBACK при недоступности модельного провайдера.
    """
    vec = [0.0] * dim
    for w in _WORD_RE.findall(text.lower()):
        h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 8) & 1 else -1.0
        vec[idx] += sign
    return _l2(vec)


def _embed_provider() -> str:
    return os.environ.get("JARVIS_EMBED_PROVIDER", "local").strip().lower()


async def _embed_vllm(text: str) -> Optional[list[float]]:
    """Запросить эмбеддинг у OpenAI-совместимого vLLM /embeddings. None при сбое."""
    import httpx
    base = os.environ.get("JARVIS_EMBED_URL",
                          os.environ.get("JARVIS_QWEN_URL", "http://vllm-qwen-coder:8001/v1"))
    model = os.environ.get("JARVIS_EMBED_MODEL", "qwen-coder")
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.post(f"{base}/embeddings", json={"model": model, "input": text[:8000]})
            r.raise_for_status()
            vec = r.json()["data"][0]["embedding"]
            return _l2([float(x) for x in vec])
    except Exception:  # noqa: BLE001
        return None


async def embed_async(text: str) -> list[float]:
    """
    Асинхронный эмбеддинг по активному провайдеру. Модельный провайдер (vllm) —
    с авто-fallback на локальный при любой ошибке, чтобы ingestion/поиск никогда
    не падали. Возвращает L2-нормализованный вектор.
    """
    if _embed_provider() == "vllm":
        vec = await _embed_vllm(text)
        if vec:
            return vec
    return embed(text)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # оба L2-нормализованы → dot=cosine


# Векторный бэкенд поиска: numpy (векторизованный exact-cosine, быстрый) при
# наличии, иначе чистый Python. Опционально — sqlite-vec для ANN на больших
# объёмах (probe безопасен: возвращает False, если расширение не собрано).
try:
    import numpy as _np  # noqa: N816
except Exception:  # noqa: BLE001
    _np = None


def vector_backend() -> str:
    return "numpy" if _np is not None else "python"


def sqlite_vec_available() -> bool:
    """Безопасная проверка доступности расширения sqlite-vec (ANN)."""
    import sqlite3
    try:
        con = sqlite3.connect(":memory:")
        con.enable_load_extension(True)
        try:
            con.load_extension("vec0")
            con.close()
            return True
        except Exception:  # noqa: BLE001
            con.close()
            return False
    except Exception:  # noqa: BLE001
        return False


def _rank(qv: list[float], rows: list[dict[str, Any]], k: int) -> list[tuple[float, dict[str, Any]]]:
    """Top-k по cosine. numpy-векторизация при наличии, иначе Python-цикл."""
    # оставляем только чанки той же размерности, что и запрос
    same = [(r, _unpack(r["embedding"])) for r in rows
            if r["embedding"] and len(r["embedding"]) // 4 == len(qv)]
    if not same:
        return []
    if _np is not None:
        mat = _np.asarray([e for _, e in same], dtype=_np.float32)
        q = _np.asarray(qv, dtype=_np.float32)
        sims = mat @ q                      # оба L2-нормализованы → dot=cosine
        idx = _np.argsort(-sims)[:k]
        return [(float(sims[i]), same[i][0]) for i in idx]
    scored = [(_cosine(qv, e), r) for r, e in same]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


# --------------------------------------------------------------------------- #
# Основной вход: приём и обработка файла
# --------------------------------------------------------------------------- #
async def ingest_file(*, filename: str, data: bytes, owner_id: str = "local-admin",
                      session_id: Optional[str] = None, message_id: Optional[str] = None,
                      origin: str = "user") -> dict[str, Any]:
    """
    Полный конвейер: dedup → сохранение → parse → chunk → embed → ready.
    Возвращает запись реестра (dict) с итоговым ingest_status.
    """
    sha = compute_sha256(data)
    # дедуп: тот же контент у того же владельца — не дублируем, возвращаем прежний
    existing = await db.query_one(
        "SELECT * FROM file_attachments_registry WHERE sha256=? AND owner_id=?",
        (sha, owner_id))
    if existing:
        # привяжем к текущей сессии/сообщению, если заданы
        await db.execute(
            "UPDATE file_attachments_registry SET session_id=COALESCE(?,session_id), "
            "message_id=COALESCE(?,message_id) WHERE id=?",
            (session_id, message_id, existing["id"]))
        existing["deduplicated"] = True
        return existing

    mime = detect_mime(filename, data)
    fid = db.new_id()
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    storage = FILES_DIR / f"{fid}_{Path(filename).name}"
    try:
        storage.write_bytes(data)
    except OSError as exc:
        return {"id": fid, "ingest_status": "failed", "ingest_error": str(exc)}

    await db.execute(
        "INSERT INTO file_attachments_registry "
        "(id,file_name,storage_ref,sha256,mime_type,size_bytes,message_id,session_id,"
        "owner_id,origin,ingest_status) VALUES (?,?,?,?,?,?,?,?,?,?, 'parsing')",
        (fid, filename, str(storage), sha, mime, len(data), message_id, session_id,
         owner_id, origin))

    text, err = parse_text(data, mime, filename)
    if text is None:
        await db.execute(
            "UPDATE file_attachments_registry SET ingest_status='failed', ingest_error=? WHERE id=?",
            (err, fid))
        return await db.query_one("SELECT * FROM file_attachments_registry WHERE id=?", (fid,))

    await db.execute("UPDATE file_attachments_registry SET ingest_status='embedding' WHERE id=?", (fid,))
    chunks = chunk_text(text)
    total_tokens = 0
    for i, ch in enumerate(chunks):
        tok = _estimate_tokens(ch)
        total_tokens += tok
        vec = await embed_async(ch)      # модельный провайдер или local-fallback
        await db.execute(
            "INSERT INTO file_chunks (id,file_id,chunk_index,content,token_count,embedding,embedding_dim) "
            "VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), fid, i, ch, tok, _pack(vec), len(vec)))

    await db.execute(
        "UPDATE file_attachments_registry SET ingest_status='ready', token_count=?, "
        "chunk_count=? WHERE id=?",
        (total_tokens, len(chunks), fid))
    return await db.query_one("SELECT * FROM file_attachments_registry WHERE id=?", (fid,))


async def search_chunks(query: str, *, session_id: Optional[str] = None,
                        k: int = 5, owner_id: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Семантический поиск по чанкам (cosine). Ограничение сессией/владельцем, если
    заданы. Возвращает топ-k чанков с score и именем файла для RAG-инъекции.
    """
    qv = await embed_async(query)
    where, params = [], []
    if session_id:
        where.append("f.session_id = ?"); params.append(session_id)
    if owner_id:
        where.append("f.owner_id = ?"); params.append(owner_id)
    where.append("f.ingest_status = 'ready'")
    clause = "WHERE " + " AND ".join(where)
    rows = await db.query(
        f"SELECT c.content, c.embedding, c.chunk_index, f.file_name, f.id AS file_id "
        f"FROM file_chunks c JOIN file_attachments_registry f ON c.file_id = f.id "
        f"{clause}", params)
    out = []
    for score, r in _rank(qv, rows, k):
        out.append({"score": round(score, 4), "file_name": r["file_name"],
                    "file_id": r["file_id"], "chunk_index": r["chunk_index"],
                    "content": r["content"]})
    return out


async def reembed_all() -> dict[str, Any]:
    """
    Пересчитать эмбеддинги всех чанков активным провайдером (после смены
    JARVIS_EMBED_PROVIDER). Идемпотентно, батчами по строкам.
    """
    rows = await db.query("SELECT id, content FROM file_chunks")
    n = 0
    for r in rows:
        vec = await embed_async(r["content"])
        await db.execute("UPDATE file_chunks SET embedding=?, embedding_dim=? WHERE id=?",
                         (_pack(vec), len(vec), r["id"]))
        n += 1
    return {"reembedded": n, "provider": _embed_provider()}


async def build_rag_context(query: str, *, session_id: Optional[str] = None,
                            k: int = 5, max_chars: int = 6000) -> str:
    """Собрать текстовый RAG-контекст из релевантных чанков (для инъекции в промпт)."""
    hits = await search_chunks(query, session_id=session_id, k=k)
    parts, used = [], 0
    for h in hits:
        block = f"[{h['file_name']}#{h['chunk_index']} score={h['score']}]\n{h['content']}"
        if used + len(block) > max_chars:
            break
        parts.append(block); used += len(block)
    return "\n\n".join(parts)
