# tools/builtin/ — 脚本工具目录（workspace 级）

这个目录里的 `.py` 会被 Agent 框架扫描并 **import 执行**（把 `agt_register()` 返回的描述符注册为工具）。**目录内脚本会被 import 执行——不要放一次性任务脚本**。

## 怎么写一个脚本工具

```python
# tools/builtin/my_tools.py
def agt_register():
    return [
        {
            "name": "my_tool",
            "description": "一句话说明这个工具做什么（模型靠它决定要不要调用）",
            "parameters": [
                {"name": "path",  "type": "string", "required": True, "description": "文件路径"},
                {"name": "count", "type": "int",    "required": False, "description": "数量"},
            ],
            "run": lambda path, count=1: f"处理了 {path} × {count}",
        },
        # ...可返回多个工具
    ]
```

保存后 `/reload tools` 热生效（免重启）；工具名冲突时后扫目录覆盖先扫。

## 硬性规则

- **一次性任务脚本不要放这里**：模块顶层（无缩进）出现 `while` / `time.sleep` / `input()` / 网络调用的脚本，
  框架会用子进程做 8 秒 import 预检——超时直接拒载并记录（import 无法中断，卡死就是拖死整个启动）。
  跑一次就完的任务放 `jobs/` 之类自建目录，用 `run_python` 显式执行或 `add_schedule` 定时。
- `tools/` 根目录**不再被扫描**——只有 `tools/builtin/` 生效（防随手丢脚本被入库执行）。
- 想临时覆盖内置工具：同名 `agt_register` 即可（后注册胜出）。

## 深入

- 实现与描述符字段：`src/script_tools.py`（`_tool_from_desc`）
- 热重载：`/reload tools` 或 `reload_hot(scope="tools")`
