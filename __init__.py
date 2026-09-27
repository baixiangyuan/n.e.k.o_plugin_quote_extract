"""语录抽取 —— N.E.K.O 插件。

从用户自己配置的外部来源（R2 / D1 / WebDAV / 直链 / 本地文件）导入语录，
支持随机抽取、手动增删、统计与定时自动同步；不内置任何语录。
核心逻辑见 core.py，网络导入见 fetchers.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

try:  # 正常包导入（N.E.K.O 宿主 / 普通测试环境）
    from . import fetchers
    from .core import QuoteLibrary, parse_d1_rows, parse_quotes_payload, parse_tags, raw_quote
except ImportError:  # pytest 8 的 Package.setup 会把插件根 __init__.py 当顶级模块导入，
    import fetchers  # 此时没有父包上下文，回退到绝对导入（插件根目录在 sys.path 上）
    from core import QuoteLibrary, parse_d1_rows, parse_quotes_payload, parse_tags, raw_quote

try:  # N.E.K.O 宿主内：使用真实 SDK
    from plugin.sdk.plugin import (
        Err,
        NekoPluginBase,
        Ok,
        SdkError,
        lifecycle,
        llm_tool,
        neko_plugin,
        plugin_entry,
        timer_interval,
    )

    SDK_AVAILABLE = True
except ModuleNotFoundError:  # 独立环境（静态检查/单测/打包校验）：SDK 缺席时退化为空实现
    SDK_AVAILABLE = False

    def neko_plugin(cls):
        return cls

    class NekoPluginBase:  # noqa: D101 —— 占位基类
        def __init__(self, ctx=None):
            self.ctx = ctx

    class Ok:
        def __init__(self, value):
            self.value = value

    class Err:
        def __init__(self, error):
            self.error = error

    class SdkError(Exception):
        pass

    def _passthrough_decorator(**_kwargs):
        def decorator(fn):
            return fn

        return decorator

    plugin_entry = lifecycle = timer_interval = llm_tool = _passthrough_decorator

LIB_FILE = "quotes.json"


@neko_plugin
class QuotePlugin(NekoPluginBase):

    def __init__(self, ctx):
        super().__init__(ctx)
        self._lib = QuoteLibrary()
        self._loaded = False
        self._import_cfg: dict = {}
        self._default_tag = ""
        self._no_repeat = 50
        self._sync_interval = 3600.0
        self._last_auto_sync = 0.0

    # ---- 配置与存储 ----

    async def _reload_config(self):
        cfg = await self.config.dump()
        quote_cfg = cfg.get("quote") or {}
        self._import_cfg = dict(cfg.get("import") or {})
        self._default_tag = str(quote_cfg.get("default_tag") or "").strip()
        try:
            self._no_repeat = max(0, int(quote_cfg.get("no_repeat_count", 50)))
        except (TypeError, ValueError):
            self._no_repeat = 50
        try:
            minutes = float(self._import_cfg.get("sync_interval_minutes", 60))
        except (TypeError, ValueError):
            minutes = 60.0
        self._sync_interval = max(60.0, minutes * 60.0)

    def _read_library_sync(self) -> dict | None:
        path = self.data_path(LIB_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            self.logger.warning("语录库文件读取失败，将重建：{}", e)
            return None

    def _write_library_sync(self, payload: str):
        path = self.data_path(LIB_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)

    async def _load_library(self):
        data = await asyncio.to_thread(self._read_library_sync)
        self._lib = QuoteLibrary(data)
        self._loaded = True

    async def _save_library(self):
        payload = json.dumps(self._lib.to_dict(), ensure_ascii=False, indent=1)
        await asyncio.to_thread(self._write_library_sync, payload)

    # ---- 导入 ----

    async def _do_import(self, source_override: str = "") -> dict:
        cfg = dict(self._import_cfg)
        if source_override:
            if not fetchers.known_source(source_override) or source_override == "none":
                raise fetchers.FetchError(
                    f"未知的导入源：{source_override}（可选：url / file / webdav / r2 / d1）"
                )
            cfg["source"] = source_override
        result = await asyncio.to_thread(fetchers.fetch_quotes, cfg)
        raw = parse_d1_rows(result) if isinstance(result, list) else parse_quotes_payload(result)
        if not raw:
            raise fetchers.FetchError(
                "来源返回了 0 条可识别语录：请检查内容格式（支持 JSON 数组/对象，或纯文本一行一条）"
            )
        stats = self._lib.merge(raw, source=str(cfg.get("source", "")))
        await self._save_library()
        return stats

    # ---- 生命周期 ----

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._reload_config()
        await self._load_library()
        self.logger.info("语录抽取已启动，当前 {} 条语录", len(self._lib.quotes))
        return Ok({"status": "ready", "total": len(self._lib.quotes)})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        if self._loaded:
            await self._save_library()
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, old_config, new_config, mode):
        await self._reload_config()
        self.logger.info("配置已更新，无需重启即生效")
        return Ok({"status": "config_updated"})

    # ---- 入口 ----

    @plugin_entry(
        id="draw",
        name="抽取语录",
        description="从语录库中随机抽取一条，可按标签筛选",
        llm_result_fields=["text", "author", "source", "tags"],
    )
    async def draw(self, tag: str = ""):
        use_tag = (tag or "").strip() or self._default_tag
        picked = self._lib.draw(use_tag, self._no_repeat)
        if picked is None:
            if self._lib.quotes and use_tag:
                return Err(SdkError(f"语录库中没有标签为「{use_tag}」的语录"))
            return Err(SdkError(
                "语录库还是空的：本插件不内置任何语录。请先在配置中设置导入来源"
                "（R2 / D1 / WebDAV / 直链 / 本地文件），再触发「导入语录」。"
            ))
        result: dict = {"text": picked["text"], "id": picked["id"]}
        if picked.get("author"):
            result["author"] = picked["author"]
        if picked.get("source"):
            result["source"] = picked["source"]
        if picked.get("tags"):
            result["tags"] = picked["tags"]
        result["total"] = len(self._lib.quotes)
        return Ok(result)

    @plugin_entry(
        id="import_quotes",
        name="导入语录",
        description="从配置的外部来源（R2/D1/WebDAV/直链/本地文件）拉取语录并合并入库",
        timeout=60.0,
    )
    async def import_quotes(self, source: str = ""):
        try:
            stats = await self._do_import(source.strip().lower())
        except fetchers.FetchError as e:
            return Err(SdkError(str(e)))
        except Exception as e:  # 网络库的意外异常也转成用户可读信息
            self.logger.warning("导入出现意外错误：{}", e)
            return Err(SdkError(f"导入失败：{e}"))
        return Ok({
            "message": (
                f"导入完成：新增 {stats['imported']} 条，更新 {stats['updated']} 条，"
                f"重复跳过 {stats['skipped']} 条，现有 {stats['total']} 条"
            ),
            **stats,
        })

    @plugin_entry(id="add_quote", name="添加语录", description="手动添加一条语录")
    async def add_quote(self, text: str, author: str = "", source: str = "", tag: str = ""):
        text = (text or "").strip()
        if not text:
            return Err(SdkError("语录内容不能为空"))
        stats = self._lib.merge(
            [raw_quote(text, author, source, parse_tags(tag)) or {"text": ""}],
            source="manual",
        )
        if stats["imported"]:
            await self._save_library()
            return Ok({"message": "已添加", "id": self._lib.next_id - 1, **stats})
        return Err(SdkError("这条语录已存在（按内容去重）"))

    @plugin_entry(id="remove_quote", name="删除语录", description="按 ID 或完整原文删除一条语录")
    async def remove_quote(self, quote_id: int = 0, text: str = ""):
        removed = self._lib.remove(quote_id=int(quote_id or 0), text=text or "")
        if removed is None:
            return Err(SdkError("没有找到这条语录：请提供准确的 quote_id，或与原文完全一致的 text"))
        await self._save_library()
        preview = removed["text"][:30] + ("…" if len(removed["text"]) > 30 else "")
        return Ok({"message": f"已删除：{preview}", "id": removed["id"]})

    @plugin_entry(id="stats", name="语录统计", description="查看语录库统计与导入源状态")
    async def stats(self):
        summary = self._lib.stats()
        return Ok({
            **summary,
            "import_source": str(self._import_cfg.get("source") or "none"),
            "auto_sync": bool(self._import_cfg.get("auto_sync")),
        })

    # ---- 定时自动同步 ----

    @timer_interval(id="auto_sync", seconds=60.0, name="自动同步语录", auto_start=True)
    async def auto_sync(self, **_):
        if not self._loaded or not self._import_cfg.get("auto_sync"):
            return Ok({"skipped": "disabled"})
        now = time.monotonic()
        if now - self._last_auto_sync < self._sync_interval:
            return Ok({"skipped": "interval"})
        self._last_auto_sync = now
        try:
            stats = await self._do_import()
        except fetchers.FetchError as e:
            self.logger.warning("自动同步失败：{}", e)
            return Ok({"skipped": str(e)})
        self.logger.info("自动同步完成：新增 {} 条，共 {} 条", stats["imported"], stats["total"])
        return Ok(stats)

    # ---- 对话期 LLM 工具 ----

    @llm_tool(
        name="draw_quote",
        description="从用户的语录库中随机抽取一条语录。当用户想要听语录、来一句名言，"
                    "或你自己在对话中想引用一条语录时调用。",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "可选的标签筛选，例如：励志"},
            },
        },
    )
    async def draw_quote(self, *, tag: str = ""):
        picked = self._lib.draw((tag or "").strip() or self._default_tag, self._no_repeat)
        if picked is None:
            return {
                "empty": True,
                "message": "语录库为空或该标签下没有语录，请提示用户先配置导入来源并执行导入",
            }
        out: dict = {"text": picked["text"]}
        if picked.get("author"):
            out["author"] = picked["author"]
        if picked.get("source"):
            out["source"] = picked["source"]
        return out
