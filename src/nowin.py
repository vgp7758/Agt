"""nowin.py —— Windows 全进程树禁弹控制台窗（2026-10-23 用户报障）。

现象：自动转后台的 run_shell / 每 10 分钟一跳的 schedule code（push-retry 的 git push）
等，桌面上周期性闪出黑色终端窗（"过一会儿就出个"）。

根因：agt 实例经看门狗 /restart 以 detached 方式拉起时【没有控制台】——此后进程内任何
subprocess 拉起的控制台程序（git / taskkill / netstat / cmd / pip / 用户 schedule code
与第三方库内部调用…）默认各自新建一个【可见】控制台 → 桌面上闪黑窗。逐调用点补
creationflags 无法穷举（real_tools/background/code 节点等已补，但 schedule code 里用户
写的命令、第三方库调用不在掌控），故在入口统一 patch subprocess.Popen。

放行规则：调用方显式要新窗口 / 分离的（CREATE_NEW_CONSOLE / DETACHED_PROCESS）不干预
——如 server.py 的「start cmd /k」（给用户看日志的终端）、intent_nano 的 DETACHED 常驻。
注意：CREATE_NO_WINDOW 与 CREATE_NEW_PROCESS_GROUP 可共存（background.py 已组合使用）；
shell=True 同样生效，且子进程树继承隐藏控制台（孙进程不再弹）。
"""
import subprocess
import sys


def install() -> bool:
    """幂等安装 Popen patch；非 Windows 直接返回 False。"""
    if sys.platform != "win32":
        return False
    _P = subprocess.Popen
    if getattr(_P.__init__, "_agt_nowin", False):
        return True                      # 已装过（多入口 import 安全）
    _orig = _P.__init__
    _keep = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
             | getattr(subprocess, "DETACHED_PROCESS", 0))

    def _init(self, *a, **kw):
        cf = kw.get("creationflags", 0) or 0
        if not (cf & _keep):
            kw["creationflags"] = cf | subprocess.CREATE_NO_WINDOW
        return _orig(self, *a, **kw)

    _init._agt_nowin = True
    _P.__init__ = _init
    return True


install()