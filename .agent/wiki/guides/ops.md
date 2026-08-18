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

## 可观测性

### /stats 页（WebUI 📊 统计按钮）

前端逻辑：`src/static/stats.html`

- **缓存命中率折线**：横轴=调用序列等间距（真实时间看 tooltip）；双端滑块选窗口（如 #650~#850），窗口内统计+图形缩放
- **默认显示范围**：最近 **200 条**调用记录（不足 200 则全量）——滑块初始窗口不再是全量，长会话打开即聚焦近期，拖双端滑块可回看更早（commit bd0d1ef）
- **端点聚合**：`provider/resp_model`（回包实际模型）相同=同端点——proxy 内部路由可见
- tooltip：序号/时间（**精确到秒**，bd0d1ef 前为分钟级）/命中率/具体 cached/prompt tokens/**scene**（调用时机）

### llm_calls.jsonl 每条记录

`ts / model / resp_model / scene / attempt / finish_reason / usage(归一化) / elapsed / outcome / content_len / reasoning_len / tool_calls / error / completer`

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
- **阶段三（进行中）**：端口清理后新实例稳定，stdin 通道端到端验证成功（`send_to_service` → busy=True）；唤醒链路两处核心观测点已埋诊断日志（commit e0ae60b，见 [端到端验证状态](../architecture/multi-agent.md#端到端验证状态2026-08-18三阶段)）；新实例首轮因 proxy 响应极慢（单次 590+ 秒）未完成，观测点未触发，已挂定时巡检等待闭环。

## 常见错误对照

| 症状 | 原因 → 处置 |
|------|------------|
| BadRequestError 400 "has no provider supported" | model id 写错（逐字符与 /v1/models 核对） |
| 400 "only 1 is allowed...temperature" | kimi 类模型限制 → 换模型或 provider 侧适配 |
| utility 通道连续 400（钩子/辅助 LLM 报错） | 进程内通道状态异常 → `/restart` 重启即恢复（调试 [wiki_auto_query](../features/wiki-auto-query.md) 时遇到） |
| 空响应连续 3 次 | 限流/服务波动 → 自动退避重试+回退；ModelScope 空壳 200 是已知病 |
| 回答是 XML 状 `<｜｜DSML｜｜invoke...` | 模型把工具调用泄进 content → llm_client 自动兜底解析；仍残留会提示重试 |
| tool_calls 与 content 同现 | 思考误放 content → 自动转移 content→reasoning（投影保 CoT） |
| /stats 命中率**单步深跌**（如 98%+ miss），下一步即恢复 ~99.9% | **折叠（fold）事件，预期一次性成本，无需处置**：轮边界 `_plan_fold` 计划触发全档折叠，历史段整段全价重算（t206_s7 实证，见 [context-engine 折叠实证](../architecture/context-engine.md#折叠事件与缓存命中t206-实证2026-08)）。新模型下折叠只在**轮边界**统一计划（先升档到 75% 再折叠到 75%），不再轮内随机触发——单步深跌即轮边界重排的代价，之后轮内零调整、缓存整段命中。区别于：持续骤降且与 utility 交错=驱逐；恒 0=随机路由（见下行两条） |
| 某端点缓存命中骤降 | per-token 驱逐：utility 与 react 共用 token → 分条目分 token |
| 某端点命中率恒 0 | 随机路由或 provider 不支持缓存 → 链路后置 |
| 中断轮"消失" | 已修复（start_turn 防御归档，answer=中断标注）；旧数据读档可見 |
| 工作流编辑后保存丢子画布 | 已修复（exitComposite 从栈顶帧父层写回）→ 强刷编辑器 |
| Windows 闪终端窗 | 已修复（子进程统一 CREATE_NO_WINDOW）→ agt ≥ 0.18.1 |
| 子 Agent 调用后主 Agent 不响应 | **先看下一行**：若伴随实例反复退出 rc=0 → 多为端口被旧实例占用（`netstat` 查 pid → `taskkill`，9100 案例即此）；否则查旧版 registry 为 None → push_message 跳过 → answer 未入 inbox（已修复，见 [multi-agent](../architecture/multi-agent.md#agentregistry-与-answer-路由修复2026-08)）。排障首选 POST `/api/status` 查 registry 字段 + 看观测点日志（e0ae60b） |
| agt-web 新端口实例反复退出 rc=0（9100 案例），伴 daemon 线程死亡、inbox 消息丢失、子 Agent 完成后主 Agent 不唤醒 | **端口被旧实例占用（根因已修正，非 entry point 问题）**：9100 被旧实例 pid 22636 占用 → 新实例起不来反复自退。处置：`netstat -ano \| findstr <端口>` 定位 pid → `taskkill /PID <pid> /F` 清理（已执行，新实例随后稳定）。进程死亡时 daemon 线程/inbox 消息随之丢失，表象同链路 bug，勿据此怀疑 [multi-agent 唤醒链路代码](../architecture/multi-agent.md#端到端验证状态2026-08-18三阶段) |
| proxy 响应极慢（单次 590+ 秒），实例看似无反应 | proxy 侧慢而非 agt 假死（2026-08-18 唤醒链路复测首轮实测）。处置：看 llm_calls.jsonl 的 elapsed 区分"慢/死"，耐心等首轮完成，勿中途重启实例丢观测点 |

## 生命周期命令

- `/restart [消息]`：看门狗重启（恢复 session/端口/首条消息；改完源码生效；utility 通道 400 也靠它恢复）；**发出后不要再手动启动**
- `restart_agent(message)`：Agent 工具版（改完自身代码后自举）
- `/agent <id>`：切换与子 Agent 直接交互（历史 Agent lazy load 历史）
- snapshot/rewind：每轮工作区快照，可回溯检查点
