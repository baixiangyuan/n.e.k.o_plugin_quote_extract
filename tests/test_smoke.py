"""插件主体冒烟测试。

依赖根目录 __init__.py 的 SDK 优雅降级：
在 N.E.K.O 宿主内用真实 SDK，独立环境用空实现垫片，两种情况下都应可导入。
"""

from __future__ import annotations

import importlib


def _import_plugin():
    try:  # 独立环境：conftest 已把插件根目录加入 sys.path
        return importlib.import_module("quote_extract")
    except ImportError:  # N.E.K.O 源码树内：以 plugin.plugins 包形式存在
        return importlib.import_module("plugin.plugins.quote_extract")


def test_plugin_class_importable():
    mod = _import_plugin()
    assert mod.SDK_AVAILABLE in (True, False)
    assert hasattr(mod, "QuotePlugin")


def test_all_entries_and_hooks_declared():
    cls = _import_plugin().QuotePlugin
    for name in (
        "on_startup",
        "on_shutdown",
        "on_config_change",
        "draw",
        "import_quotes",
        "add_quote",
        "remove_quote",
        "stats",
        "auto_sync",
        "draw_quote",
    ):
        assert callable(getattr(cls, name, None)), f"缺少方法：{name}"
