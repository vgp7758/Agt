"""Agt Agent 框架——src 包。确保包内绝对导入(import config 等)能找到同级模块。"""
__version__ = "0.3.5"
import sys, os
_pkg = os.path.dirname(os.path.abspath(__file__))
if _pkg not in sys.path:
    sys.path.insert(0, _pkg)
