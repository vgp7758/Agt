# tools/builtin/ — 脚本工具目录（workspace 级）

这个目录里的 `.py` 会被 Agent 框架扫描并 **import 执行**（把 `agt_register()` 返回的描述符注册为工具）。**目录内脚本会被 import 执行——不要放一次性任务脚本**。

## 怎么写一个脚本工具（标准形态：函数 + 描述符）

工具的实现就是普通函数——**参数表（schema）从函数签名+类型注解自动推断，描述从 docstring 读取**：

```python
# tools/builtin/my_tools.py

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
        # …可一次返回多个
    ]
```

保存后 `/reload tools` 热生效（免重启）；同名工具后扫目录覆盖先扫（`.agent/tools/` > 本目录 > 内置）。

## 描述符字段一览（`src/script_tools.py` 的 `_tool_from_desc`）

| 字段 | 说明 |
|---|---|
| `func` | ★ 函数引用（inline 模式必需）——**没有 `run`/`parameters` 字段**，参数表来自函数签名 |
| `name` / `description` | 覆盖函数推断值（description 回退链：描述符 → docstring → 函数名） |
| `params` | `{参数名: "一句话描述"}` 注入 schema 的参数描述 |
| `group` / `hidden` / `outputs` | 分组 / 不投影 LLM / 显式输出 schema（编辑器补全用） |
| `mode: "subprocess"` | 子进程模式（脚本每次调用起子进程执行目标函数，params 用完整 schema dict、func 填函数名字符串）——重依赖隔离场景用 |

## 硬性规则

- **一次性任务脚本不要放这里**：模块顶层（无缩进）出现 `while` / `time.sleep` / `input()` / 网络调用的脚本，
  框架会用子进程做 8 秒 import 预检——超时直接拒载并记录（import 无法中断，卡死就是拖死整个启动）。
  跑一次就完的任务放 `jobs/` 之类自建目录，用 `run_python` 显式执行或 `add_schedule` 定时。
- `tools/` 根目录**不再被扫描**——只有 `tools/builtin/` 生效（防随手丢脚本被入库执行）。

## 真实范例

随包外置件全部用这套协议：`src/assets/tools_builtin/`（rag_tools / wiki_tools / ltm_tools / misc_tools…）——
照抄任何一个的结构即可。

## 深入

- 实现与描述符字段：`src/script_tools.py`（`_tool_from_desc`）
- 热重载：`/reload tools` 或 `reload_hot(scope="tools")`
