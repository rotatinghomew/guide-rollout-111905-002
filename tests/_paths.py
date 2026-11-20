"""测试共享引导：把仓库根目录加入 sys.path。

unittest discover 以 tests/ 为起始目录时，测试模块按顶层模块导入，
不会自动把仓库根放到 sys.path；各测试模块先 ``from _paths import ROOT``
再导入 lottery。
"""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
