"""插件主体冒烟测试。

按文件路径加载插件根 __init__.py（submodule_search_locations 提供包上下文），
不依赖检出的目录名，也不依赖 sys.path；
在 N.E.K.O 源码树内则优先以 plugin.plugins 包形式导入。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _import_plugin():
    try:  # N.E.K.O 源码树内
        return importlib.import_module("plugin.plugins.quote_extract")
    except ImportError:
        pass
    # 独立环境：按路径作为包加载
    spec = importlib.util.spec_from_file_location(
        "quote_extract_standalone",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
