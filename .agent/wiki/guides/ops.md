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

**嵌套子画布轨迹（2026-08，commit 31d5ef3）**：复合节点（loop/batch）/子工作流不再是黑盒——`track_stack` 容器栈收集子节点事件，复合节点逐轮更新 `children`（最后一轮轨迹 + rounds + childmeta），观测页顶层行可点击展开子轨迹表（`▸ 循环 200001 ♻ 12 轮 · 5 子节点` / `🔗 sub_test · 9 节点`）。调试 wait_extract 等待循环 / 子工作流时用它（详见 [wf-monitor · 嵌套子画布](../features/wf-monitor.md#嵌套子画布轨迹复合节点--子工作流的子节点事件2026-08commit-31d5ef3)）。

覆盖三类执行路径：同步钩子（线程池）、async 钩子（后台线程）、wf_* 工具调用。注意：`_WF_RUNS` 是进程内存，/restart 清空；旧进程的「执行中」行不携带 run_id（不可点击），需 /restart 后生效。实现细节见 [工作流运行观测](../features/wf-monitor.md)。

### 工作流调试页 · 节点输出白框（2026-08-21，commit 6c804a2）

调试页画布「播放」工作流后，每个执行过的节点**下方直接挂白框**（foreignObject，白底黑字等宽）显示节点输出：顶部灰色头条「▾ 输出 · N 字段」点击折叠/展开，内容每字段一行截 240 字、超 16 行滚动。实现上 nodeH 拆为 `_baseH`（基础高度，含 type8 分支/type32 分组等动态高度）+ 输出框高——布局与连线避让自动生效；端口锚点一律用 `_baseH`（不含输出框），连线不随输出出现/折叠跳动。纯前端单文件（`src/static/workflow_debug.html`），Ctrl+F5 即生效。与 /wf/monitor 互补：观测页看「别人跑的过程」，画布白框适合「自己调试时盯着改」。详见 [工作流调试页](../features/workflow-debug.md)。

### /stats 页（WebUI 📊 统计按钮）

前端逻辑：`src/static/stats.html`

- **缓存命中率折线**：横轴=调用序列等间距（真实时间看 tooltip）；双端滑块选窗口（如 #650~#850），窗口内统计+图形缩放
- **默认显示范围**：最近 **200 条**调用记录（不足 200 则全量）——滑块初始窗口不再是全量，长会话打开即聚焦近期，拖双端滑块可回看更早（commit bd0d1ef）
- **端点聚合**：`provider/resp_model`（回包实际模型）相同=同端点——proxy 内部路由可见
- **拖拽扫描交互（commit af66c0f）**：按住左键在图形区域内拖动 → 鼠标位置出现一条沿 y 轴的**虚线吸附竖线**，x 锁定到鼠标当前位置**左侧最近折线点**的 x 坐标，竖线旁同步显示该点 tooltip；松开 / 移出图区即消失。解决 hover 小圆点需精确移到点附近才出 tooltip 的定位不便——横扫即可逐点查看
  - 实现：SVG 内联后立即执行 `bindScan()` IIFE——收集窗口内全部折线点（visible 各模型的 pts 中 gi∈[lo,hi]）按 gi 升序排序，拖拽时鼠标位置换算到 SVG viewBox 坐标做最近点吸附
  - 细节：tooltip 靠右缘自动左翻；拖拽中 `user-select:none` 防选中文字；SVG 外松手后回到图内自动清理；每次刷新重建 SVG 时重绑（无泄漏）
  - 原 hover 小圆点 tooltip **保留并存**（两种查看方式）；纯前端改动，Ctrl+F5 刷新即生效，不需 /restart
  - **tooltip 锁定数据点（v0.18.7，commit aae43b0 打包发布）**：吸附后 tooltip 不驻留鼠标附近，而是**锁定到所吸附的数据点上**、y 跟随曲线起伏——横扫多条折线时 tooltip 始终贴着当前数据点，读数与曲线视觉位置一致；纯前端，Ctrl+F5 生效
- tooltip：序号/时间（**精确到秒**，bd0d1ef 前为分钟级）/命中率/具体 cached/prompt tokens/**scene**（调用时机）/**turn/step 轮步标记**（commit 4aced81）
  - turn/step 标记格式：`· t{轮号} · s{步号}`（如 `· t206 · s6`）
  - 与 `projections/` 目录下投影转储文件名同源对齐：`t206_s6_*.json`
  - 老记录（/restart 前生成的）无 turn/step 字段，tooltip 自动省略该段
  - 使用场景：从 /stats 折线图发现异常点（如缓存单步深跌）→ 拖拽扫描（或 hover）tooltip 获取 t{N}·s{M} → 直接打开 `projections/t{N}_s{M}_*.json` 查看当时完整投影，快速定位升档/折叠等事件断点
  - ⚠️ 转储格式（2026-08，commit 2dc64f2）：`.json` pretty-print **负载本体**（`{"_meta": {...}, "messages": [...]}`，零构造零截断，`json.load` 即消费）；历史 `.txt` 自定义格式仅存旧存档。详见 [context-engine · 投影转储](context-engine.md#投影转储文件名与-ts-标记commit-4aced81)

### llm_calls.jsonl 每条记录

`ts / model / resp_model / scene / attempt / finish_reason / usage(归一化) / elapsed / outcome / content_len / reasoning_len / tool_calls / error / completer / turn / step`

- **turn**（commit 4aced81）：当前已完成轮数（值为 `len(turns)`），与投影文件名中的 `t{N}` 对应
- **step**（commit 4aced81）：当前轮已完成步数（值为 `len(_current.steps)`），与投影文件名中的 `s{M}` 对应
- 仅 scene=`react·{agent_id}`（主循环）的记录有 turn/step；其他场景（钩子/recap/debug 等）为 null
- **传递链**（三层贯通，与 scene 同款机制）：`src/agent.py` react 主循环 3 处调用点（主调用/DSML 重试/空回答重试）传 `turn=len(turns), step=len(_current.steps)`（与 `_dump_projection` 完全同源）→ `src/llm_client.py` `chat()` 设 `_turnstep_ctx`（chat() 进入设置、finally 清理，**不进 API 请求**）→ `_record_call` 落盘 → `src/server.py` `/api/stats` 透传 → `src/static/stats.html` tooltip 拼接
- 老记录无 turn/step 字段，新记录添加后向前兼容（读取侧可选）

scene 取值（2026-08-29 起携带发起者——与 [🐞 日志面板](#日志面板--场景标注2026-08) 行尾小括号同源）：

| 调用方 | scene |
|---|---|
| ReAct 主循环（主调用/DSML 重试/空回答重试 3 处） | `react·{agent_id}`，如 `react·_main_` |
| 钩子内工作流 LLM 调用（同步 + async） | `hook:{位置}·{工作流名}·{agent_id}`，如 `hook:before_turn·wiki_auto_query·_main_` |
| recap 生成 / wrap_up 收尾总结 | `recap·{agent_id}` / `wrap_up·{agent_id}` |
| 其余 | debug（/debug prompt）/ completer / llm.chat（默认，如 RAG 检索） |

### 其他

- `/debug prompt <提示词>`：按当前投影直调 LLM，**不落盘不执行**，打印完整回包（耗时/finish_reason/usage/含缓存命中/tool_calls）——与投影转储配套（进什么 vs 出什么）
- `/context`：投影分段统计 + 缓存概况——分段数据**三级读取**（2026-08-29，commit e703c67）：内存 **live 缓存 `_proj_stats`**（真实投影装配时顺手记录，commit 4212f65）→ **旁车 `session_dir/proj_stats.json`**（上次真实投影 + 档位边界快照的存档，跨重启有效）→ 现算兜底（新 session 无存档时）。零重算、口径=真实发给模型的那份，输出带三态来源标注（live「采自上次真实投影 t336·s2，4分钟前」/ sidecar「采自旁车——上次真实投影的存档，跨重启有效」/「现算估算——本进程尚未跑过投影，且无旁车存档」）；另结合 llm_calls 最近 react 回包实测 prompt_tokens 校准。见 [context-engine · 投影分段统计](../architecture/context-engine.md#投影分段统计-context-改读真实投影缓存commit-4212f65)、[段统计旁车持久化](../architecture/context-engine.md#段统计旁车持久化proj_statsjson--context-三级读取2026-08-29commit-e703c67)
- `/stats`（CLI）/ /logs：文本版统计与日志
- restart.log（~/.agt/）：/restart 看门狗全程时序（含新进程 stderr）
- **唤醒链路观测点日志（commit e0ae60b）**：`src/agent.py` 中 `_bg` 路由 `push_message`（answer 入 caller inbox）与 `inbox_thread` 搬运（inbox→work_q）两处已埋诊断日志——排"子 Agent 完成后主 Agent 未唤醒"时直接看实例日志定位断点（需 `/restart` 加载新代码）

### 🐞 日志面板 · 场景标注（2026-08）

WebUI 右下角 🐞 入口（v0.20.1 引入；最多留 200 条滚动、error 未读徽标）实时显示 LLM 运行告警——回退链切换 / 限流换 token / max_tokens 截断 / 空响应重试 / 回退链耗尽，此前这些只进 log 文件、前端无从感知。2026-08-29 起**每条行尾附场景小括号**，一眼看出这次限流/回退是谁发起的：

```
回退 glm→deepseek-chat 原因=RateLimitError 退避5s (hook:before_turn·wiki_auto_maintenance·_main_)
空响应(疑似限流) 重试 1/3 退避5s 耗时0.4s (react·_main_)
回退链耗尽 tried=['glm','deepseek-chat'] (hook:turn_end·recap_gen·_main_)
```

scene 格式与 [llm_calls.jsonl](#llm_callsjsonl-每条记录) 同源：react/recap/wrap_up 尾缀 `·{agent_id}`；钩子三段式 `hook:{位置}·{工作流名}·{agent_id}`；空场景省略括号。

**四层链路**：

1. `src/llm_client.py`：`chat()` 进入时把 scene 写入 **ContextVar `_SCENE_CTX`**（线程隔离）、finally reset；`_SinkHandler.emit` 捕获 WARNING+ 记录读取后，以三参 `(level, msg, scene)` 回调 sinks。**为什么用 ContextVar 而非 client 实例属性**：utility client 被主/子 Agent/工作流线程并发共用，实例属性会互相覆盖（A 场景的告警挂上 B 的场景）。
2. `src/agent.py` 六处调用点：react 主循环 3 处（主调用/DSML 重试/空回答重试）/ recap / wrap_up 带 `agent_id`；同步与 async 钩子执行前给 utility client 设 `_scene_override = f"hook:{位置}·{工作流名}·{agent_id}"`，finally 恢复原值。
3. `src/chat.py`：`set_log_sink` 回调把 scene 塞进 `llm_log` 事件（主 Agent `_emit` 广播）。
4. `src/static/index.html`：`pushLogEntry(level, text, scene)` 行尾拼 `(scene)`。

生效方式：`/restart`。

### 跨进程状态查询（/api/status）

**已实现并验证**（commit a922121，`src/server.py` POST `/api/status`）：返回实例运行时状态快照（18 个顶层字段 + 3 个嵌套数组），用于跨实例诊断。详见 [/api/status 端点](../features/api-status.md)。

**背景**：`AgentRegistry`（`src/registry.py`）是进程内全局对象，记录所有活 Agent 实例及其运行时状态。此前无跨进程 API 从外部查询 registry 内容，调试子 Agent 异步唤醒问题（如 [multi-agent registry 修复](../architecture/multi-agent.md#agentregistry-与-answer-路由修复2026-08) 所述场景）只能靠日志事后排查。现已补全。

**使用**：POST `/api/status` → JSON 快照（只读，registry 读取时已加锁/快照）。多实例部署可逐个采集做横向对比。改完源码需 `/restart` 生效。

**验证结论（2026-08-18，三阶段）**：
- **阶段一（通过）**：跨实例 POST `/api/status` 调用成功。结合 [三层消费机制](../architecture/multi-agent.md#三层消费机制当前代码消息不会丢前提进程存活) 确认：registry 正确注册 → answer 正常入队 → 三层消费无丢消息 → 主 Agent 被正确唤醒。原"子 Agent 完成后主 Agent 未唤醒"根因已定位为旧版 registry 为 None 导致消息未入队（非消费端丢失），代码已修复。
- **阶段二（根因已修正）**：9100 端口新实例反复退出（rc=0）的真正根因是**端口被旧实例（pid 22636）占用**，非此前推断的 entry point/端口探测问题——已 `taskkill` 清理，进程死亡导致 daemon 线程与 inbox 消息丢失的表象见 [三层消费机制](../architecture/multi-agent.md#三层消费机制当前代码消息不会丢前提进程存活)。
- **阶段三（进行中）**：端口清理后新实例稳定，stdin 通道端到端验证成功（`send_to_service` → busy=True）；唤醒链路两处核心观测点日志已埋（commit e0ae60b，见 [端到端验证状态](../architecture/multi-agent.md#端到端验证状态2026-08-18三阶段)）；新实例首轮因 proxy 响应极慢（单次 590+ 秒）未完成，观测点未触发，已挂定时巡检等待闭环。

**跨实例不止查询（2026-08）**：实例还可作为对方实例的 **WS 客户端**——收初始事件、只读 action、发消息驱动对方 agent 干活、斜杠命令（⚠️ 含 `/exit` 可远程关服）；服务无鉴权，跨电脑/公网使用需隧道。demo 见 `tools/remote_client_demo.py`，详见 [跨实例客户端](../features/remote-client.md)。

### 轮边界缓存观测（2026-08）

- **折叠事件（t206）**：/stats 单步深跌（98%+ miss），下一步即恢复 ~99.9% → 预期一次性成本，无需处置（折叠摘要 byte-stable）。见 [context-engine 折叠实证](../architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)
- **正常轮边界（t224）**：/stats 显示 98%+ 命中，仅 1~2% 结构性重算 → 验证轮边界平滑路径已生效（未超 75% 阈值）。见 [context-engine 正常轮边界路径](../architecture/context-engine.md#正常轮边界路径t224-实证2026-08)
- **折叠判阈口径修复（2026-08）**：`_estimate_tokens` 估算分子补齐 tools schema——修前估算「以为达标」（271,623 判 ≤300K → 折叠 0 轮）而实际 419,284 超 win=400K（估算 vs 实际系统性差 ~147K/请求）。**症状**：新一轮初始 prompt_tokens 远超 400K×0.75 目标却折叠 0 轮 / 未见升档折叠日志。需升级代码 + `/restart` 生效。见 [context-engine 估算与校准口径闭环](../architecture/context-engine.md#估算与校准口径闭环tools-schema-补齐2026-08)
- **DeepSeek 端低命中排查（2026-08 探针实证）**：DeepSeek 缓存按**原始消息序列位置敏感**、**不做 system 合并规范化**、`reasoning_content` 不参与缓存键——装配字节稳定即可命中（D 组实证：同内容 system 块原位命中 91.5%、挪到最前 0%），分层投影结构本身不是低命中原因。持续低命中优先查 **multi-token 轮换**（per-token 缓存隔离）与 **TTL**；判别手段 `tools/deepseek_cache_probe.py`（可复用其它 provider）。见 [context-engine 缓存行为实证](../architecture/context-engine.md#deepseek-缓存行为实证位置敏感不合并-system2026-08-探针)

### /restart 看门狗与 agt 入口（2026-08）

### /restart 看门狗：超时强杀兜底 + 日志按实例分离（2026-08，commit affdb09）

**背景（8000 实例用户报告）**：/restart 后浏览器一直等待，看门狗始终没拉起新服务，直到手动在 repo 启动一个新实例才触发旧实例恢复成功（用户纠正过机制：手动实例还在装配没起服务时旧链路就拉起了，退掉手动实例旧实例也不关——实际卡点是**新进程装配期**的共享资源交互，现场已失）。

**看门狗超时放弃（已修）**：旧逻辑父进程 300s 未退出 → **放弃重启**（服务已下线需手动启动）。修复：300s 未退 → `taskkill /F /T`（POSIX SIGKILL）→ **继续拉起流程**——`/restart` 是用户显式重启请求，卡死进程不应阻塞它；优雅退出留给正常退出，强杀留给明确要求重启的时刻。

**日志按实例分离（已修）**：多实例 stdout 此前**都追加写同一个 `~/.agt/restart.log`**——9000 与 8000 交错 16 万行，排障时搜不到彼此的段（追加写不互锁，但可观测性灾难）。修复：`~/.agt/restart-{mode}-{port}.log`（如 restart-web-8000.log / restart-web-9000.log），新进程 `-u` unbuffered——装配日志实时落盘（块缓冲会把输出困住几十分钟，事后无法判断新进程卡在哪个阶段）。

**下次复现时一眼定位**：`restart-web-8000.log` 里看门狗拉起实例的**最后输出行**就是卡点——停在「[MCP] 已连接 'python-lsp'」之后=卡下一个 MCP；停在「[rag] 加载 embedding 模型」=模型/HF 路径。候选：MCP stdio 双实例竞争（LSP 单实例锁）、HF 联网探测挂起、模型文件锁——若确认，下一步给装配阶段加超时保护（MCP 连接限时、`HF_HUB_OFFLINE` 兜底）。

### `agt --help` / `--version`（2026-08，commit 0e186c9）

**背景（用户观察）**：Agent 新环境探索时常用 `agt --help` 获取帮助——此前不支持，直接进交互。修复：`_early_argv()` 支持 `--help/-h/help`、`--version/-V`，打印能力概貌后退出（不进交互），`agt`/`agt-web` 两入口都有；无参数直通不变。与 README「Agent 上手指引」闭环（[multi-instance 边界](../architecture/multi-instance.md#边界与后续)）。

## 常见错误对照

| 症状 | 原因 → 处置 |
|------|------------|
| BadRequestError 400 "has no provider supported" | model id 写错（逐字符与 /v1/models 核对） |
| 400 "only 1 is allowed...temperature" | kimi 类模型限制 → 换模型或 provider 侧适配 |
| utility 通道连续 400（钩子/辅助 LLM 报错） | 进程内通道状态异常 → `/restart` 重启即恢复（调试 [wiki_auto_query](../features/wiki-auto-query.md) 时遇到） |
| 空响应连续 3 次 | 限流/服务波动 → 自动退避重试+回退；ModelScope 空壳 200 是已知病 |
| 回答是 XML 状 `<｜｜DSML｜｜invoke...` | 模型把工具调用泄进 content → llm_client 自动兜底解析；仍残留会提示重试 |
| tool_calls 与 content 同现 | 思考误放 content → 自动转移 content→reasoning（投影保 CoT） |
| /stats 命中率**单步深跌**（如 98%+ miss），下一步即恢复 ~99.9% | **折叠（fold）事件，预期一次性成本，无需处置**：轮边界 `_plan_fold` 计划触发全档折叠，历史段整段全价重算（t206_s7 实证，见 [context-engine 折叠实证](../architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)）。新模型下折叠只在**轮边界**统一计划（先升档到 75% 再折叠到 75%），不再轮内随机触发——单步深跌即轮边界重排的代价，之后轮内零调整、缓存整段命中。区别于：持续骤降且与 utility 交错=驱逐；恒 0=随机路由（见下行两条）。**排障速查**：/stats 折线图看到异常点 → 拖拽扫描（或 hover）tooltip 获取 t{N}·s{M} → 打开 `projections/t{N}_s_{M}*.json` 直接看当时完整投影（2026-08 起转储为 JSON pretty-print 负载本体，见 [turn/step 轮步标记](#stats-页webui-统计按钮)） |
| 新一轮初始 prompt_tokens 远超 win×0.75（如 win=400K 时 >300K）却**折叠 0 轮**、无升档/折叠日志 | **`_estimate_tokens` 漏算 tools schema（旧代码）**：估算≠实际（实际含 130+ 工具 schema 的 ~264K 字符），计划「以为达标」实超窗 → 升级 session.py/agent.py 修复 + `/restart`（见 [context-engine 估算与校准口径闭环](../architecture/context-engine.md#估算与校准口径闭环tools-schema-补齐2026-08)）。排障佐证：llm_calls.jsonl 实际 prompt_tokens 与 /stats 或估算口径相差一个量级（十万级） |
| 某端点缓存命中骤降 | per-token 驱逐：utility 与 react 共用 token → 分条目分 token |
| 某端点命中率恒 0 | 随机路由或 provider 不支持缓存 → 链路后置 |
| 中断轮"消失" | 已修复（start_turn 防御归档，answer=中断标注）；旧数据读档可見 |
| 工作流编辑后保存丢子画布 | 已修复（exitComposite 从栈顶帧父层写回）→ 强刷编辑器 |
| Windows 闪终端窗 | 已修复（子进程统一 CREATE_NO_WINDOW）→ agt ≥ 0.18.1 |
| 子 Agent 调用后主 Agent 不响应 | **先看下一行**：若伴随实例反复退出 rc=0 → 多为端口被旧实例占用（`netstat` 查 pid → `taskkill`） |
| answer 完成后插话不消费、滞留到用户发下条消息才注入 | 已修（commit fb115aa）：answer 后只查 inbox、漏 pending_messages（插话队列）→ 现 inbox 空时兜底消费插话队列，自动 `background_trigger·user_insert` 开新轮；旧进程需 `/restart`（见 [user-interaction](../features/user-interaction.md#插话全生命周期2026-08-19-修复闭环commit-fb115aa)） |
| 并行钩子（同 hook 挂多工作流）某行「执行中」永远闪烁不消失 | 已修（commit fb115aa）：前端单数 runningWf 被后启动的钩子覆盖引用 → 改 Map 按 hook::name 独立跟踪；`/restart` + 强刷生效（见 [user-interaction](../features/user-interaction.md#并行钩子执行中状态跟踪修复2026-08-19)） |
| 手机 `/restart` 后电脑无端多开一个浏览器 tab，新 tab 显示「(当前对话)」需手动刷新才见会话 | 已修（commit 7ca6cfc）：重启链路 `open_browser` 无条件开 tab + `/resume` 恢复慢于页面连接、完成后又无推送 → 重启场景跳过 open_browser（检测 `AGT_RESTART_*` env）+ 恢复即 `broadcast_session_state` 广播全端；旧进程需 `/restart`（见 [user-interaction](../features/user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc)） |
| 「⏳ 工作流执行中…」行不可点击、看不到节点进度 | 旧进程代码（事件不带 run_id）→ `/restart` 后新运行即可点击打开 [/wf/monitor 实时观测](#wfmonitor-工作流运行观测页2026-08-20新commit-8aeb21a) |
| 观测页节点预览无 📄、点不开全文 | 旧进程（无 full 存储）或**全量预算（20M 字符）耗尽**后只存预览；先 `/restart`，仍不行即预算耗尽属预期降级（见 [wf-monitor · 节点全文查看](../features/wf-monitor.md#节点全文查看2026-08-20commit-bb56a82)） |

## 相关页面

- [长期记忆](../features/longterm-memory.md) — memories/ 三类记忆、episodic 召回流水线、`/memory` 管理页
- [工作流运行观测](../features/wf-monitor.md) — /wf/monitor 实时节点轨迹（run registry）+ 节点全文纯文本路由
- [工作流调试页 · 节点输出白框](../features/workflow-debug.md) — 调试画布内联节点输出（与 /wf/monitor 互补：盯着改 vs 旁观跑的过程）
- [上下文引擎与缓存优化](../architecture/context-engine.md) — 投影转储、分档折叠、折叠实证、估算与校准口径闭环、/context live 分段统计
- [系统总览](../architecture/overview.md) — 模块地图、数据流
- [/api/status 端点](../features/api-status.md) — 跨进程状态查询
- [多 Agent 体系](../architecture/multi-agent.md) — 三层消费机制、唤醒链路验证
- [用户交互 · 插话机制与消息路由](../features/user-interaction.md) — 插话全生命周期、并行钩子 UI 状态
- [v0.18.7 发布记录](../releases/v0.18.7.md) — /stats tooltip 修复随该版发布