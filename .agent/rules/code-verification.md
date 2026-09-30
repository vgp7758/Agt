# 代码改动的回归验证（重启 agt 实例前必做）

## 为什么这是一条硬规则

当前正在服务的 agt 实例跑的就是本 repo 的代码。一旦改动有缺陷，`/restart` 后新进程可能
**直接阻塞 / 每轮秒死**——与用户的交互通道立刻断开，只能靠外部 coding agent 救场。
所以：**改完代码，先验证到"可放心重启"，才允许 `/restart`。**

## 三类必验风险（2026-09-30 一天内全部真实发生）

1. **漏名字（import / 定义）**——语法检查抓不到，只在特定路径触发：
   - `except ImageUnsupportedError`（漏 import）：任何异常传播到该 handler 才炸，
     且**原始异常被 NameError 吞掉**，排查难度翻倍；
   - `Path(_AD)`（agent.py 全文 `Path` 均为**函数内局部导入**）：`_wf_canvas_index` 每轮
     开头必跑 → 每个 turn 启动即 NameError 秒死。
2. **重算 / 计划类改动的性能螺旋**：tier_boundaries 不持久化 + 重启重算 → `_plan_fold`
   逐步碎刀 + 每刀全量重渲染 1200 轮 → **数分钟无响应**，且 fold_count 被吃到 len(turns)
   （近期上下文全进摘要，细节丢失）。
3. **启动期 / 每轮路径**的改动风险最高：`agent.run` 主循环、`_hook_tasks` / `_wf_canvas_index`、
   `build_agent`、`Session.load` / `_plan_fold`、`messages_for_llm`、`real_tools` 工具注册。

## 验证阶梯（L0+L1 永远必做；改到每轮路径必须加 L2）

### L0 语法
- `.py`：对每个改过的文件跑 `py_diag`（python-lsp MCP）；
- 内联 JS（index.html / workflow_editor.html）：抽 `<script>` 块 `node --check`。

### L1 名字自检（今日两次踩坑的专项）
逐行核对新增/修改代码引用的**每个名字在当前作用域已定义**：
- 本 repo 风格：`agent.py` 等文件的 `Path` / `re` / `os` 多为**函数内局部导入**——
  新代码就地带 `from pathlib import Path` 这类局部 import，不要假设模块级有；
- 新增 `except X` 前先确认 `X` 已 import；新增类型注解、默认值、f-string 里的名字同理。

### L2 隔离实跑（改到每轮路径时必做，约 90 秒）
```
不能用生产 workspace（会撞 session 文件）——用隔离工作区：

1) 建空工作区：C:\Users\vgp77\.agt\_regr_ws\（含 .agent\workflows\ 子目录）
2) 旁路起实例（直跑=用本地最新源码；不影响生产实例）：
   start_service(name="regr-96xx", command="python D:\\AI_Usings\\Agt\\src\\chat.py 96xx",
                 cwd="C:\\Users\\vgp77\\.agt\\_regr_ws")
3) 等约 40s 就绪 → 走真实每轮路径：
   send_to_service("regr-96xx", "回复一个字：pong")
4) 看 service_logs：钩子应正常执行（🔍 ... 执行中）、消息进入处理、**无 NameError/traceback**
5) stop_service("regr-96xx")
```
L2 能抓住：每轮路径 NameError、启动阻塞、钩子解析失败、全局/本地工作流加载问题。

### L3 关键逻辑单测（改核心函数时）
- mock 最小对象直接调函数（不必起完整实例），跑 5 类场景：正常 / 边界 / 空 / 异常 / 幂等；
- 改 session / context 的，必须验证 `fold_count`、`tier_boundaries`、投影段序未被意外改写。

### L4 长会话安全性（改 tier / fold / plan / 投影时）
- 在真实长会话上**实算一次**：加载耗时、`_plan_fold` 耗时、投影形态（段序 + 总量）与改前同量级；
- 关键不变量：fold_count 不被重启重置；tier_boundaries 存档往返一致（否则会重演折叠螺旋）。

## 重启纪律
- 生产实例**最后重启**——先用 L2 旁路实例证明新代码能跑完整一轮；
- 改动保持**小步 commit**（回滚 = `git checkout <file>`，一行命令）；重启前 `git status` 干净；
- 别把 `py_auto_diag` 当全部：它只等于 L0。

## 备案：改坏了怎么救（外部 coding agent 视角）
- 症状「重启后实例无响应 / 秒死」→ 看 `sessions/<xxx>/events.jsonl` 尾部：
  `turn_start` 之后无事件 = 轮启动即炸；再看实例日志 traceback，**最后一个改动文件最可疑**；
- 救法：`git log --oneline -5` → `git checkout <上一个好 commit> -- <可疑文件>` → 重启；
- 所以改动越碎、commit 越勤，救援越快。
