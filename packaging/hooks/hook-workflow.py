# 空操作 hook：覆盖 pyinstaller-hooks-contrib 的 hook-workflow.py（它针对同名 PyPI 包
# workflow——与本项目顶层模块 src/workflow.py 平铺收集后同名冲突，其 import 会失败）。
# 我们自己的 workflow.py 无需任何 hook 处理。
hiddenimports = []
