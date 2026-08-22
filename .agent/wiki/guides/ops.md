# 运维、可观测性与排障

## 存档布局（~/.agt/repos/）

```
<fixed-cwd>/            # cwd 斜线替换为'-'（D:\A\Agt → D--A-Agt；旧 hash 目录启动自动迁移）
  sessions/<ts>/        # events.jsonl / toollog.jsonl / llm_calls.jsonl / meta.json
    agents/<子id>/      # 子 Agent 嵌套 session（meta.json 含 _agent_meta）
    projections/        # 投影转储（/config dump_projections true 时）
  memories/             # 长期记忆三类（semantic 常驻 / episodic 按召回 / procedural 标题+按需）
  plans/  specs/  images/  rag/
```

memories/ 三类记忆、episodic 召回流水线与 `/memory` 管理页见 [长期记忆](../features/longterm-memory.md)。

## 可观测性

### /wf/monitor 工作流运行观测页（2026-08-20 新，commit 8aeb21a）

**实时**节点级观测（区别于 /stats 的事后统计）：对话中「⏳ 工作流『xxx』执行中…」行带 run_id 时**可点击**（虚线下划线）→ 新标签打开 `/wf/monitor?run=<id>`——节点时间线表格（标题/类型/状态/耗时/**甘特时间条**/输出预览 200 字），2s 轮询增量渲染，running 节点橙色脉冲，完成全绿停轮询。无参访问 = 最近 50 次运行列表。

**节点全文查看（commit bb56a82）**：预览 200 字不够排障时，`has_full` 节点的预览单元格**可点击**（📄 标记 + 虚线下划线）→ 新标签 `GET /api/wf/runs/<id>/node/<nid>` 打开 **text/plain 纯文本页**——页面文本直接就是节点完整输出（非 HTML、无样式，浏览器原生渲染）。单节点 200K 字符内原文，超限截断并标注总长；全量总预算 20M 字符耗尽后节点只存预览（不可点击）。看 LLM 节点完整回包、检索节点全量结果用它。

覆盖三类执行路径：同步钩子（线程池）、async 钩子（后台线程）、wf_* 工具调用。注意：`_WF_RUNS` 是进程内存，/restart 清空；旧进程的「执行中」行不携带 run_id（不可点击），需 /restart 后生效。实现细节见 [工作流运行观测](../features/wf-monitor.md)。

### 工作流调试页 · 节点输出白框（2026-08-21，commit 6c804a2）

调试页画布「播放」工作流后，每个执行过的节点**下方直接挂白框**（foreignObject，白底黑字等宽）显示节点输出：顶部灰色头条「▾ 输出 · N 字段」点击折叠/展开，内容每字段一行截 240 字、超 16 行滚动。实现上 nodeH 拆为 `_baseH`（基础高度，含 type8 分支/type32 分组等动态高度）+ 输出框高——布局与连线避让自动生效；端口锚点一律用 `_baseH`（不含输出框），连线不随输出出现/折叠跳动。纯前端单文件（`src/static/workflow_debug.html`），Ctrl+F5 即生效。与 /wf/monitor 互补：观测页看「别人跑的过程」，画布白框适合「自己调试时盯着改」。详见 [工作流调试页](../features/workflow-debug.md)。

### /stats 页（WebUI 📊 统计按钮）

前端逻辑：`src/static/stats.html`

- **缓存命中率折线**：横轴=调用序列等间距（真实时间看 tooltip）；双端滑块选窗口（如 #650~#850），窗口内统计+图形缩放
- **默认显示范围**：最近 **200 条**调用记录（不足 200 则全量）——滑块初始窗口不再是全量，长会话打开即聚焦近期，拖双端滑块可回看更早（commit bd0d1ef）
- **端点聚合**：`provider/resp_model`（回包实际模型）相同=同端点——proxy 内部路由可见
- tooltip：序号/时间（**精确到秒**，bd0d1ef 前为分钟级）/命中率/具体 cached/prompt tokens/**scene**（调用时机）/**turn/step 轮步标记**（commit 4aced81）
  - turn/step 标记格式：`· t{轮号} · s{步号}`（如 `· t206 · s6`）
  - 与 `projections/` 目录下投影转储文件名同源对齐：`t206_s6_*.txt`
  - 老记录（/restart 前生成的）无 turn/step 字段，tooltip 自动省略该段
  - 使用场景：从 /stats 折线图发现异常点（如缓存单步深跌）→ hover tooltip 获取 t{N}·s{M} → 直接打开 `projections/t{N}_s{M}_*.txt` 查看当时完整投影，快速定位升档/折叠等事件断点

### llm_calls.jsonl 每条记录

`ts / model / resp_model / scene / attempt / finish_reason / usage(归一化) / elapsed / outcome / content_len / reasoning_len / tool_calls / error / completer / turn / step`

- **turn**（commit 4aced81）：当前已完成轮数（值为 `len(turns)`），与投影文件名中的 `t{N}` 对应
- **step**（commit 4aced81）：当前轮已完成步数（值为 `len(_current.steps)`），与投影文件名中的 `s{M}` 对应
- 仅 `scene=react`（主循环）的记录有 turn/step；其他场景（钩子/recap/debug 等）为 null
- **传递链**（三层贯通，与 scene 同款机制）：`src/agent.py` react 主循环 3 处调用点（主调用/DSML 重试/空回答重试）传 `turn=len(turns), step=len(_current.steps)`（与 `_dump_projection` 完全同源）→ `src/llm_client.py` `chat()` 设 `_turnstep_ctx`（与 `_scene_ctx` 同构 contextvar，finally 清理，**不进 API 请求**）→ `_record_call` 落盘 → `src/server.py` `/api/stats` 透传 → `src/static/stats.html` tooltip 拼接
- 老记录无 turn/step 字段，新记录添加后向前兼容（读取侧可选）

scene 取值：react（主循环）/ hook:before_turn 等钩子 / recap / debug（/debug prompt）/ wrap_up / completer / llm.chat（默认，如 RAG 检索）

### 其他

- `/debug prompt <提示词>`：按当前投影直调 LLM，**不落盘不执行**，打印完整回包（耗时/finish_reason/usage/含缓存命中/tool_calls）——与投影转储配套（进什么 vs 出什么）
- `/stats`（CLI）/ /logs：文本版统计与日志
- restart.log（~/.agt/）：/restart 看门狗全程时序（含新进程 stderr）
- **唤醒链路观测点日志（commit e0ae60b）**：`src/agent.py` 中 `_bg` 路由 `push_message`（answer 入 caller inbox）与 `inbox_thread` 搬运（inbox→work_q）两处已埋诊断日志——排"子 Agent 完成后主 Agent 未唤醒"时直接看实例日志定位断点（需 `/restart` 加载新代码）

### 跨进程状态查询（/api/status）

**已实现并验证**（commit a922121，`src/server.py` POST `/api/status`）：返回实例运行时状态快照（18 个顶层字段 + 3 个嵌套数组），用于跨实例诊断。详见 [/api/status 端点](../features/api-status.md)。

**背景**：`AgentRegistry`（`src/registry.py`）是进程内全局对象，记录所有活 Agent 实例及其运行时状态。此前无跨进程 API 从外部查询 registry 内容，调试子 Agent 异步唤醒问题（如 [multi-agent registry 修复](../architecture/multi-agent.md#agentregistry-与-answer-路由修复2026-08) 所述场景）只能靠日志事后排查。现已补全。

**使用**：POST `/api/status` → JSON 快照（只读，registry 读取时已加锁/快照）。多实例部署可逐个采集做横向对比。改完源码需 `/restart` 生效。

**验证结论（2026-08-18，三阶段）**：
- **阶段一（通过）**：跨实例 POST `/api/status` 调用成功。结合 [三层消费机制](../architecture/multi-agent.md#三层消费机制当前代码消息不会丢前提进程存活) 确认：registry 正确注册 → answer 正常入队 → 三层消费无丢消息 → 主 Agent 被正确唤醒。原"子 Agent 完成后主 Agent 未唤醒"根因已定位为旧版 registry 为 None 导致消息未入队（非消费端丢失），代码已修复。
- **阶段二（根因已修正）**：9100 端口新实例反复退出（rc=0）的真正根因是**端口被旧实例（pid 22636）占用**，非此前推断的 entry point/端口探测问题——已 `taskkill` 清理，进程死亡导致 daemon 线程与 inbox 消息丢失的表象见 [三层消费机制](../architecture/multi-agent.md#三层消费机制当前代码消息不会丢前提进程存活)。
- **阶段三（进行中）**：端口清理后新实例稳定，stdin 通道端到端验证成功（`send_to_service` → busy=True）；唤醒链路两处核心观测点日志已埋（commit e0ae60b，见 [端到端验证状态](../architecture/multi-agent.md#端到端验证状态2026-08-18三阶段)）；新实例首轮因 proxy 响应极慢（单次 590+ 秒）未完成，观测点未触发，已挂定时巡检等待闭环。

### 轮边界缓存观测（2026-08）

- **折叠事件（t206）**：/stats 单步深跌（98%+ miss），下一步即恢复 ~99.9% → 预期一次性成本，无需处置（折叠摘要 byte-stable）。见 [context-engine 折叠实证](../architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)
- **正常轮边界（t224）**：/stats 显示 98%+ 命中，仅 1~2% 结构性重算 → 验证轮边界平滑路径已生效（未超 75% 阈值）。见 [context-engine 正常轮边界路径](../architecture/context-engine.md#正常轮边界路径t224-实证2026-08)

## 常见错误对照

| 症状 | 原因 → 处置 |
|------|------------|
| BadRequestError 400 "has no provider supported" | model id 写错（逐字符与 /v1/models 核对） |
| 400 "only 1 is allowed...temperature" | kimi 类模型限制 → 换模型或 provider 侧适配 |
| utility 通道连续 400（钩子/辅助 LLM 报错） | 进程内通道状态异常 → `/restart` 重启即恢复（调试 [wiki_auto_query](../features/wiki-auto-query.md) 时遇到） |
| 空响应连续 3 次 | 限流/服务波动 → 自动退避重试+回退；ModelScope 空壳 200 是已知病 |
| 回答是 XML 状 `<｜｜DSML｜｜invoke...` | 模型把工具调用泄进 content → llm_client 自动兜底解析；仍残留会提示重试 |
| tool_calls 与 content 同现 | 思考误放 content → 自动转移 content→reasoning（投影保 CoT） |
| /stats 命中率**单步深跌**（如 98%+ miss），下一步即恢复 ~99.9% | **折叠（fold）事件，预期一次性成本，无需处置**：轮边界 `_plan_fold` 计划触发全档折叠，历史段整段全价重算（t206_s7 实证，见 [context-engine 折叠实证](../architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)）。新模型下折叠只在**轮边界**统一计划（先升档到 75% 再折叠到 75%），不再轮内随机触发——单步深跌即轮边界重排的代价，之后轮内零调整、缓存整段命中。区别于：持续骤降且与 utility 交错=驱逐；恒 0=随机路由（见下行两条）。**排障速查**：/stats 折线图看到异常点 → hover tooltip 获取 t{N}·s{M} → 打开 `projections/t{N}_s_{M}*.txt` 直接看当时完整投影（见 [turn/step 轮步标记](#stats-页webui-统计按钮)） |
| 某端点缓存命中骤降 | per-token 驱逐：utility 与 react 共用 token → 分条目分 token |
| 某端点命中率恒 0 | 随机路由或 provider 不支持缓存 → 链路后置 |
| 中断轮"消失" | 已修复（start_turn 防御归档，answer=中断标注）；旧数据读档可見 |
| 工作流编辑后保存丢子画布 | 已修复（exitComposite 从栈顶帧父层写回）→ 强刷编辑器 |
| Windows 闪终端窗 | 已修复（子进程统一 CREATE_NO_WINDOW）→ agt ≥ 0.18.1 |
| 子 Agent 调用后主 Agent 不响应 | **先看下一行**：若伴随实例反复退出 rc=0 → 多为端口被旧实例占用（`netstat` 查 pid → `taskkill`） |
| answer 完成后插话不消费、滞留到用户发下条消息才注入 | 已修（commit fb115aa）：answer 后只查 inbox、漏 pending_messages（插话队列）→ 现 inbox 空时兜底消费插话队列，自动 `background_trigger·user_insert` 开新轮；旧进程需 `/restart`（见 [user-interaction](../features/user-interaction.md#插话全生命周期2026-08-19-修复闭环commit-fb115aa)） |
| 并行钩子（同 hook 挂多工作流）某行「执行中」永远闪烁不消失 | 已修（commit fb115aa）：前端单数 runningWf 被后启动的钩子覆盖引用 → 改 Map 按 hook::name 独立跟踪；`/restart` + 强刷生效（见 [user-interaction](../features/user-interaction.md#并行钩子执行中状态跟踪修复2026-08-19)） |
| 「⏳ 工作流执行中…」行不可点击、看不到节点进度 | 旧进程代码（事件不带 run_id）→ `/restart` 后新运行即可点击打开 [/wf/monitor 实时观测](#wfmonitor-工作流运行观测页2026-08-20新commit-8aeb21a) |
| 观测页节点预览无 📄、点不开全文 | 旧进程（无 full 存储）或**全量预算（20M 字符）耗尽**后只存预览；先 `/restart`，仍不行即预算耗尽属预期降级（见 [wf-monitor · 节点全文查看](../features/wf-monitor.md#节点全文查看2026-08-20commit-bb56a82)） |

## 相关页面

- [长期记忆](../features/longterm-memory.md) — memories/ 三类记忆、episodic 召回流水线、`/memory` 管理页
- [工作流运行观测](../features/wf-monitor.md) — /wf/monitor 实时节点轨迹（run registry）+ 节点全文纯文本路由
- [工作流调试页 · 节点输出白框](../features/workflow-debug.md) — 调试画布内联节点输出（与 /wf/monitor 互补：盯着改 vs 旁观跑的过程）
- [上下文引擎与缓存优化](../architecture/context-engine.md) — 投影转储、分档折叠、折叠实证
- [系统总览](../architecture/overview.md) — 模块地图、数据流
- [/api/status 端点](../features/api-status.md) — 跨进程状态查询
- [多 Agent 体系](../architecture/multi-agent.md) — 三层消费机制、唤醒链路验证
- [用户交互 · 插话机制与消息路由](../features/user-interaction.md) — 插话全生命周期、并行钩子 UI 状态
