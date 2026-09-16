# misc_tools · 杂项纯函数组 + sleep 等待工具（外置件 misc_tools.py）

> 源码：`tools/builtin/misc_tools.py`（开发份）+ `src/assets/tools_builtin/misc_tools.py`（播种份，双层 md5 同步，见[工具外置 · 双层对账](tool-externalization.md#双层一致性对账workspace-层-vs-assets-层2026-09-12commit-971535a用户提问触发)）
> 组内工具：add / divide / kw_score 等纯函数 + **sleep**（2026-09-16 起对 Agent 开放）
> 定位：早期纯函数批（v0.19.0，15 个纯函数的一部分），纯函数零状态——全组唯一有非平凡执行语义的是 sleep

## 职责

misc 组承载不方便归类的纯函数小工具：`add` / `divide` / `kw_score` 等一次性算子，以及 `sleep`（进程内等待）。注册走 `agt_register()` 描述符列表，`/reload tools` 热加载即生效（无需 `/restart`，见[工具外置](tool-externalization.md)）。

## sleep · 等待指定秒数（2026-09-16 对 Agent 开放，commit 30fe776）

```python
sleep(seconds: float) -> str   # 秒数，现上限 3600
```

用户实测场景驱动：Agent 有「等外部事件落地再继续」的需求（如 sleep(600) 后收货/巡检），此前 sleep `hidden=True` 不进 Agent 工具箱 schema（docstring 原定位「工作流 wait 节点：轮询间隔/限速等用」，上限 0~300）。

### 变更三件（两份同步）

| 变更 | 前 | 后 |
|---|---|---|
| `hidden` | True（不进 Agent schema） | **False**（Agent 可直接调用） |
| 上限 | 0~300 | **0~3600**（覆盖 sleep(600) 场景） |
| docstring | 只写 wait 节点用途 | 写明 inline 执行行为（见下） |

- 同步改两份：`tools/builtin/misc_tools.py` + 播种版 `src/assets/tools_builtin/misc_tools.py`（防双层漂移）
- `src/real_tools.py` 两处工具文案同步更新：「add/split/sleep 等」→「add/divide/kw_score 等（**sleep 已对 Agent 开放**）」
- `py_compile ×3` 全过；commit `30fe776`，`/reload tools` 生效

### 关键边界：inline 模式不受 TOOL_TIMEOUT 管（用户问句钉死）

用户问：「tool_timeout 比 sleep(t) 的 t 短时，sleep 会在 timeout 提前结束吗？」——**不会**。转后台/超时机制挂在 `_run_subprocess_streaming`，只覆盖 **subprocess 系**工具（run_python / run_shell / run_script / subprocess 模式外置工具）；sleep 是 **inline 模式**（进程内直调 `time.sleep`），没有 subprocess 超时包装——**真实睡满，不提前掐断**。

### 意外实证：sleep(600) 真调穿越超时

开发验证时为测「600 在新范围内」直接真调了 `sleep(600)`：宿主 run_python 在 180s 超时转后台（bg_task），而 **sleep 本身在后台继续睡满 600s**（不被掐）——恰好构成 inline 工具不超时的直接证据；睡满后走 bg_task 完成通知唤醒（见 [run-python · 转后台自动通知](run-python.md#超时转后台与完成自动通知2026-08-30commit-6460ad1)），无害。

### 代价与适用场景

- **阻塞当前 react 轮**：inline 睡眠期间 UI 显示工具运行中，用户插话排队等睡醒——语义上恰好符合「等外部事件」，但若 Agent 睡满 3600s，这一整轮期间都不可用
- 适用：等远程任务出片/巡检到点/限速重试等「睡醒即续」场景；需中途可被打断的等待用 `add_schedule` 定时任务替代（见 [background-scheduler](background-scheduler.md)）
- docstring 已把上述行为写明——Agent 选工具时即能看到

## 与其他模块的关系

- [工具外置体系](tool-externalization.md)：misc_tools 是 15 外置件之一（早期纯函数批）
- [run_python](run-python.md)：subprocess 系超时转后台机制的本体；sleep 的 inline 形态是该机制的对照边界
- [background-scheduler](background-scheduler.md)：可打断/到点唤醒的等待，走 add_schedule 而非长 sleep

## 相关页面

- [run-python · TOOL_TIMEOUT 边界](run-python.md#tool_timeout-边界只管-subprocess-系inline-工具不受管2026-09-16commit-30fe776)
- [工具外置](tool-externalization.md) / [工具外置判别标准](../architecture/tool-externalization-criteria.md)
