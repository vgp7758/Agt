"""Agt Agent 框架——src 包。确保包内绝对导入(import config 等)能找到同级模块。

导入顺序敏感（PyPI sdist 构建实测坑）：sys.path hack 必须在 from paths import
之前——build 后端 import src 时 CWD 下没有 paths，同级目录还没进 sys.path；
pip 安装（包名 agt_agent）与桌面平铺打包两种形态都依赖这个顺序。
"""
import sys, os
_pkg = os.path.dirname(os.path.abspath(__file__))
if _pkg not in sys.path:
    sys.path.insert(0, _pkg)
from paths import VERSION as __version__   # 版本唯一真源在 paths.py（桌面平铺打包共用）
