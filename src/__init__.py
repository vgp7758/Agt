"""Agt Agent 框架——src 包。确保包内绝对导入(import config 等)能找到同级模块。"""
from paths import VERSION as __version__   # 版本唯一真源在 paths.py（桌面平铺打包共用）
import sys, os
_pkg = os.path.dirname(os.path.abspath(__file__))
if _pkg not in sys.path:
    sys.path.insert(0, _pkg)
