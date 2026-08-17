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

- **缓存命中率折线**：横轴=调用序列等间距（真实时间看 tooltip）；双端滑块选窗口（如 #650~#850），窗口内统计+图形缩放
- **端点聚合**：`provider/resp_model`（回包实际模型）相同=同端点——proxy 内部路由可见
- tooltip：序号/时间/命中率/具体 cached/prompt tokens/**scene**（调用时机）

### llm_calls.jsonl 每条记录

`ts / model / resp_model / scene / attempt / finish_reason / usage(归一化) / elapsed / outcome / content_len / reasoning_len / tool_calls / error / completer`

scene 取值：react（主循环）/ hook:before_turn 等钩子 / recap / debug（/debug prompt）/ wrap_up / completer / llm.chat（默认，如 RAG 检索）

### 其他

- `/debug prompt <提示词>`：按当前投影直调 LLM，**不落盘不执行**，打印完整回包（耗时/finish_reason/usage/含缓存命中/tool_calls）——与投影转储配套（进什么 vs 出什么）
- `/stats`（CLI）/ /logs：文本版统计与日志
- restart.log（~/.agt/）：/restart 看门狗全程时序（含新进程 stderr）

### 跨进程状态查询缺失

**现状**：`AgentRegistry`（`src/registry.py`）是进程内全局对象，记录所有活 Agent 实例及其运行时状态（agent_id/status/caller_id 等）。但当前 **无跨进程 API** 从外部查询 registry 内容——即无法通过 HTTP 端点获知"当前有哪些子 Agent 在跑、各处于什么状态"。

**影响**：调试子 Agent 异步唤醒问题时（如 [multi-agent registry 修复](../architecture/multi-agent.md#agentregistry-与-answer-路由修复2026-08) 所述场景），只能靠日志/events.jsonl 事后排查，缺少实时运行时观测手段。

**建议**：在 `server.py` 添加 `/api/status` 端点，序列化 registry 中各 Agent 的 id/name/status/caller_id 等只读字段，供外部监控或调试工具消费。需注意 registry 非线程安全，读取时需加锁或快照。

## 常见错误对照

| 症状 | 原因 → 处置 |
|------|------------|
| BadRequestError 400 "has no provider supported" | model id 写错（逐字符与 /v1/models 核对） |
| 400 "only 1 is allowed...temperature" | kimi 类模型限制 → 换模型或 provider 侧适配 |
| utility 通道连续 400（钩子/辅助 LLM 报错） | 进程内通道状态异常 → `/restart` 重启即恢复（调试 [wiki_auto_query](../features/wiki-auto-query.md) 时遇到） |
| 空响应连续 3 次 | 限流/服务波动 → 自动退避重试+回退；ModelScope 空壳 200 是已知病 |
| 回答是 XML 状 `<｜｜DSML｜｜invoke...` | 模型把工具调用泄进 content → llm_client 自动兜底解析；仍残留会提示重试 |
| tool_calls 与 content 同现 | 思考误放 content → 自动转移 content→reasoning（投影保 CoT） |
| 某端点缓存命中骤降 | per-token 驱逐：utility 与 react 共用 token → 分条目分 token |
| 某端点命中率恒 0 | 随机路由或 provider 不支持缓存 → 链路后置 |
| 中断轮"消失" | 已修复（start_turn 防御归档，answer=中断标注）；旧数据读档可見 |
| 工作流编辑后保存丢子画布 | 已修复（exitComposite 从栈顶帧父层写回）→ 强刷编辑器 |
| Windows 闪终端窗 | 已修复（子进程统一 CREATE_NO_WINDOW）→ agt ≥ 0.18.1 |
| 子 Agent 调用后主 Agent 不响应 | 旧版 registry 为 None → push_message 跳过 → answer 未入 inbox；已修复（见 [multi-agent](../architecture/multi-agent.md#agentregistry-与-answer-路由修复2026-08)） |

## 生命周期命令

- `/restart [消息]`：看门狗重启（恢复 session/端口/首条消息；改完源码生效；utility 通道 400 也靠它恢复）；**发出后不要再手动启动**
- `restart_agent(message)`：Agent 工具版（改完自身代码后自举）
- `/agent <id>`：切换与子 Agent 直接交互（历史 Agent lazy load 历史）
- snapshot/rewind：每轮工作区快照，可回溯检查点
