# .agent/tools/ — 脚本工具目录（本地自定义统一入口）

这个目录里的 `.py` 会被 Agent 框架扫描并 **import 执行**（把 `agt_register()` 返回的描述符注册为工具）。
**目录内脚本会被 import 执行——不要放一次性任务脚本。**

扫描顺序：随包 `src/assets/tools_builtin/` → 本目录（同名后扫胜出——可覆盖随包同名工具，
即用户定制覆写机制）。旧 `tools/builtin/` 仍兼容扫描（存量 repo 不炸），建议逐步迁入本目录；
`tools/` 根永不扫描。

## 怎么写一个脚本工具（标准形态：函数 + 描述符）

工具的实现就是普通函数——**参数表（schema）从函数签名+类型注解自动推断，描述从 docstring 读取**：

```python
# .agent/tools/my_tools.py

def my_tool(path: str, count: int = 1) -> str:
    """一句话说明这个工具做什么（模型靠它决定要不要调用）。"""
    return f"处理了 {path} × {count}"

def agt_register():
    return [
        {"name": "my_tool",          # 可选；缺省用函数名
         "func": my_tool,            # ★ 函数引用（必需）——schema 由签名推断
         "group": "my",              # 可选：分组（工具箱归类）
         "description": "……",        # 可选；缺省读函数 docstring
         # "params": {"path": "要处理的文件路径"},   # 可选：参数级描述（{参数名: 一句话}）
         # "hidden": True,           # 可选：不投影给 LLM（工作流 plugin 仍可用）
        },
        # 需要引擎态的工具（explore 等）：def agt_register(ctx=None) —— ctx 含 {"agent", "cwd"}，
        # agent=None（纯工具箱场景）时自行降级不注册。
    ]
```

保存后 `/reload tools` 秒级热生效（不需要重启）。改描述/参数也走同一路径。

## 防御与信任

- **一次性任务脚本不要放这里**：模块顶层（无缩进）出现 `while` / `time.sleep` / `input()` / 网络调用的脚本，
  框架会用子进程做 8 秒 import 预检——超时直接拒载并记录（import 无法中断，卡死就是拖死整个启动）。
  跑一次就完的任务放 `jobs/` 之类自建目录，用 `run_python` 显式执行或 `add_schedule` 定时。
- 信任模型：目录内代码在引擎进程内执行（同 `.agent/workflows/`），勿放不受信脚本。

## 真实范例

随包外置件全部用这套协议：`src/assets/tools_builtin/`（rag_tools / wiki_tools / ltm_tools / misc_tools…）——
照抄任何一个的结构即可。

## 深入

- 完整协议（subprocess 隔离模式 / mtime 缓存 / 热加载链）：`src/script_tools.py` 模块 docstring
