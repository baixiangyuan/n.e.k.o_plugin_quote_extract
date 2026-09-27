"""把插件根目录加入 sys.path，使 tests 可以直接 import core / fetchers。

插件主体 __init__.py 依赖 N.E.K.O SDK（plugin.sdk.plugin），
仅在 N.E.K.O 源码树内运行时才可导入；独立运行时相关测试会跳过。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
