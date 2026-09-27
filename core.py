"""语录核心逻辑：解析、合并、去重、抽取。

纯标准库实现，不依赖 N.E.K.O SDK，也不做任何网络请求，
方便独立测试；网络部分见 fetchers.py，SDK 对接见 __init__.py。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any

# 常见字段名的兼容映射
_TEXT_KEYS = ("text", "quote", "content", "sentence", "value", "body")
_AUTHOR_KEYS = ("author", "by", "speaker")
_SOURCE_KEYS = ("source", "origin", "from")
_TAG_KEYS = ("tag", "tags", "category")
_ID_KEYS = ("id", "uid", "key")

# 纯文本中 "语录 —— 作者" 的作者分隔符
_AUTHOR_SEP_RE = re.compile(r"^(?P<text>.+?)\s+[-—–]{1,2}\s+(?P<author>\S.{0,64})$")

_HISTORY_CAP = 200


def normalize_text(text: str) -> str:
    """压缩空白后的规范文本，作为去重键。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def quote_key(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


def _decode(data: bytes) -> str:
    """按 UTF-8(BOM) → GBK → UTF-8(忽略错误) 顺序解码。"""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def parse_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，/、|\s]+", value)
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = [str(value)]
    return [str(p).strip() for p in parts if str(p).strip()]


def raw_quote(text: Any, author: Any = None, source: Any = None, tags: Any = None) -> dict | None:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = normalize_text(text)
    if not text:
        return None
    return {
        "text": text,
        "author": str(author).strip() if isinstance(author, str) else ("" if author is None else str(author)),
        "source": str(source).strip() if isinstance(source, str) else ("" if source is None else str(source)),
        "tags": parse_tags(tags),
    }


def _parse_json_node(node: Any) -> list[dict]:
    if isinstance(node, str):
        # 字符串里可能又嵌了一层 JSON 或纯文本
        stripped = node.strip()
        if stripped.startswith(("[", "{")):
            try:
                return _parse_json_node(json.loads(stripped))
            except json.JSONDecodeError:
                return _parse_txt(stripped)
        return _parse_txt(stripped)
    if isinstance(node, list):
        out: list[dict] = []
        for item in node:
            if isinstance(item, str):
                q = raw_quote(item)
            elif isinstance(item, dict):
                text = _first(item, _TEXT_KEYS)
                if text is None and not any(k in item for k in _TEXT_KEYS):
                    # {"作者": ["语录1", ...]} 这类反向结构不做花哨支持，跳过
                    continue
                q = raw_quote(
                    text,
                    _first(item, _AUTHOR_KEYS),
                    _first(item, _SOURCE_KEYS),
                    _first(item, _TAG_KEYS),
                )
            else:
                q = raw_quote(item)
            if q:
                out.append(q)
        return out
    if isinstance(node, dict):
        for wrapper in ("quotes", "data", "items", "results", "list"):
            if wrapper in node and isinstance(node[wrapper], (list, str)):
                return _parse_json_node(node[wrapper])
        # 单个对象视作一条语录
        q = raw_quote(
            _first(node, _TEXT_KEYS),
            _first(node, _AUTHOR_KEYS),
            _first(node, _SOURCE_KEYS),
            _first(node, _TAG_KEYS),
        )
        return [q] if q else []
    return []


_SENTENCE_ENDINGS = ("。", "！", "？", "…", "」", "”", ".", "!", "?")
_AUTHOR_LINE_ENDINGS = ("。", "！", "？", "，", "、", ".", "!", "?", ",", ":")


def _parse_txt(text: str) -> list[dict]:
    out: list[dict] = []
    # 空行分段优先；没有空行则逐行解析
    blocks = re.split(r"\n\s*\n", text) if re.search(r"\n\s*\n", text) else text.splitlines()
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines: list[str] = []
        for ln in block.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("#"):  # 井号开头视为注释行
                continue
            lines.append(ln)
        if len(lines) > 1:
            # 多行段落：正文以句号收尾、且最后一行是短署名时，才把末行视为作者
            maybe_author = lines[-1]
            body = " ".join(lines[:-1])
            if (
                len(maybe_author) <= 32
                and not maybe_author.endswith(_AUTHOR_LINE_ENDINGS)
                and body.endswith(_SENTENCE_ENDINGS)
            ):
                m = _AUTHOR_SEP_RE.match(body)
                q = (
                    raw_quote(m.group("text"), m.group("author"))
                    if m
                    else raw_quote(body, maybe_author)
                )
                if q:
                    out.append(q)
                    continue
        for line in lines:
            m = _AUTHOR_SEP_RE.match(line)
            q = raw_quote(m.group("text"), m.group("author")) if m else raw_quote(line)
            if q:
                out.append(q)
    return out


def parse_quotes_payload(data: bytes) -> list[dict]:
    """把导入的原始字节解析成语录列表（原始字段，未入库）。"""
    text = _decode(data).strip()
    if not text:
        return []
    try:  # JSON 优先（含裸字符串、嵌套字符串等合法形式）
        return _parse_json_node(json.loads(text))
    except json.JSONDecodeError:
        return _parse_txt(text)


def parse_d1_rows(rows: list[dict]) -> list[dict]:
    """把 D1 查询行转成语录列表。"""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        q = raw_quote(
            _first(row, _TEXT_KEYS),
            _first(row, _AUTHOR_KEYS),
            _first(row, _SOURCE_KEYS),
            _first(row, _TAG_KEYS),
        )
        if q:
            out.append(q)
    return out


class QuoteLibrary:
    """语录库 + 抽取历史，可整体 JSON 序列化。"""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.quotes: dict[str, dict] = {}
        self.next_id: int = int(data.get("next_id", 1))
        self.history: list[str] = [str(i) for i in data.get("history", [])]
        self.last_sync_at: int = int(data.get("last_sync_at", 0))
        self.last_sync_source: str = str(data.get("last_sync_source", ""))
        for item in data.get("quotes", []):
            if isinstance(item, dict) and item.get("key"):
                self.quotes[str(item["key"])] = dict(item)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "next_id": self.next_id,
            "history": self.history,
            "last_sync_at": self.last_sync_at,
            "last_sync_source": self.last_sync_source,
            "quotes": list(self.quotes.values()),
        }

    # ---- 写入 ----

    def merge(self, raw_quotes: list[dict], *, now: int | None = None, source: str = "") -> dict:
        """合并导入语录，按规范文本去重；返回 {imported, updated, skipped, total}。"""
        now = int(now if now is not None else time.time())
        imported = updated = skipped = 0
        for raw in raw_quotes:
            key = quote_key(raw["text"])
            old = self.quotes.get(key)
            if old is None:
                self.quotes[key] = {
                    "key": key,
                    "id": self.next_id,
                    "text": raw["text"],
                    "author": raw.get("author", ""),
                    "source": raw.get("source", ""),
                    "tags": list(raw.get("tags", [])),
                    "added_at": now,
                    "updated_at": now,
                }
                self.next_id += 1
                imported += 1
            else:
                changed = False
                for field in ("author", "source"):
                    new = raw.get(field, "")
                    if new and new != old.get(field, ""):
                        old[field] = new
                        changed = True
                for tag in raw.get("tags", []):
                    if tag not in old["tags"]:
                        old["tags"].append(tag)
                        changed = True
                if changed:
                    old["updated_at"] = now
                    updated += 1
                else:
                    skipped += 1
        self.last_sync_at = now
        self.last_sync_source = source
        return {
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "total": len(self.quotes),
        }

    # ---- 查询 ----

    def candidates(self, tag: str = "") -> list[dict]:
        tag = (tag or "").strip()
        if not tag:
            return list(self.quotes.values())
        return [q for q in self.quotes.values() if tag in q["tags"]]

    def by_id(self, quote_id: int) -> dict | None:
        for q in self.quotes.values():
            if q["id"] == quote_id:
                return q
        return None

    def remove(self, *, quote_id: int = -1, text: str = "") -> dict | None:
        key = None
        if quote_id > 0:
            q = self.by_id(quote_id)
            key = q["key"] if q else None
        elif text.strip():
            key = quote_key(text)
        return self.quotes.pop(key, None) if key else None

    # ---- 抽取 ----

    def draw(self, tag: str = "", no_repeat_count: int = 50,
             rng: random.Random | None = None) -> dict | None:
        """随机抽取一条；库为空时返回 None。no_repeat_count>0 时近期抽过的不重复。"""
        pool = self.candidates(tag)
        if not pool:
            return None
        rng = rng or random.Random()
        if no_repeat_count > 0 and len(pool) > 1:
            recent = set(self.history[-no_repeat_count:])
            fresh = [q for q in pool if q["key"] not in recent]
            pool = fresh or pool
        picked = rng.choice(pool)
        self.history.append(picked["key"])
        if len(self.history) > _HISTORY_CAP:
            self.history = self.history[-_HISTORY_CAP:]
        return picked

    def stats(self) -> dict:
        tag_count: dict[str, int] = {}
        for q in self.quotes.values():
            for t in q["tags"]:
                tag_count[t] = tag_count.get(t, 0) + 1
        return {
            "total": len(self.quotes),
            "tags": dict(sorted(tag_count.items(), key=lambda kv: -kv[1])),
            "history_len": len(self.history),
            "last_sync_at": self.last_sync_at,
            "last_sync_source": self.last_sync_source,
        }
