# 跨实例客户端 · agt 实例作为另一个 agt 实例的 WS 客户端

> 演示脚本：`tools/remote_client_demo.py`（2026-08 实测验证）。回答"实例 A 能否自己开客户端与 url 上的实例 B 交互、调用 B 上的工具"——**能，且已实测**；本页记录四条通道、语义边界与封装方向。

## 职责

一个 agt 实例（经 `run_python` + websocket 库）作为**另一个 agt 实例的 WS 客户端**——浏览器 index.html 就是这样一个客户端，浏览器能干的，另一个 agt 实例都能干：连接收事件、发消息驱动对方 agent、发斜杠命令、REST 读写。对方可在另一台电脑上（url 参数化，agt-web 监听 0.0.0.0）。

与 [api-status](api-status.md) 的关系：那条是 REST 只读快照（跨实例**诊断**）；本页是完整客户端能力（跨实例**驱动**）。

## 能力矩阵（四条通道）

| 通道 | 能做什么 | 门槛/依赖 |
|------|---------|----------|
| REST | `/api/status` 诊断、`/api/tools`、读写 models/agents/workflows/memory/stats | HTTP 请求即可，已在用 |
| WS 文本消息 | 驱动对方 agent 跑任务（对方消耗**它自己的** token、带着**它自己** session 的上下文）——即"远程指挥干活" | `run_python` + websocket 库 |
| WS 斜杠命令 | 即时处理不进对方 LLM：/model、/reload、/restart 甚至 **/exit（可远程关服）** | 同上 |
| 跨电脑 | 换 url 即可 `ws://192.168.x.x:8000/ws` | 网络可达；⚠️ 服务无鉴权，公网需隧道 |

## 演示脚本（tools/remote_client_demo.py）

最小形态三步，`websocket.create_connection(url, timeout=6)` 开场：

1. **连接即推的初始事件**（不发自定义消息就有）：`system` 欢迎（含 models 列表 + current_model）/ `sessions` / `workflows` 三个事件
2. **只读 action**：`ws.send({"action": "list_sessions"})` → 单发响应（服务端不进 work_q、不碰 LLM）
3. **REST 对照**：urllib POST `/api/status` 顺手取跨实例状态快照

实测输出（连本机 8000 端口的另一实例，其内有 ComfyUI 相关会话）：

```
✅ 已连接 ws://127.0.0.1:8000/ws
[system]    模型选项 11 个，当前 glm-official
[sessions]  1 个会话: [在CNB上调用ComfyUI]
[workflows] 17 个工作流
[action→list_sessions] 请求-响应通路 ✓
[REST /api/status] 工具 135 个 | registry 2 Agent | busy=False
✅ 已断开（全程零打扰：未触发对方 LLM）
```

## 语义辨析：没有"直接调远程工具"的端点

严格说**不存在** `POST /api/tool/edit` 这类直接工具执行端点——工具执行绑定在对方 agent 的上下文里，驱动方式是**发消息让它调**（它带着自己 session 的记忆决策）。这是正确架构而非缺陷：

- 要改对方仓库的文件，正确姿势是让对方自己 edit——它有 file_version 锁、py_auto_diag 钩子、改动进它的 session 记录
- 绕过它的上下文直接执行 = 丢失对方侧的全部保护与留痕

## 缺的一层封装（方向，未实施）

现在是"每次写段脚本"的形态，没有第一等工具。若成为常用场景，值得封装约 60 行：

```python
remote_agt(url, message, timeout=120)
  # 连接 → 发消息 → 挂流收事件直到 _done → 聚合 answer + 工具调用摘要返回
remote_agt(url, action="list_team")   # 只读 action 快捷通道
```

长等待需配合 `set_tool_timeout` 或转后台轮询。附带愿景：**多实例组网**——A 机器实例派 B 机器的 vision 看图（B 有 GPU 跑本地模型），答案回流入 A 的 inbox。

## 注意事项

- **无鉴权**：服务监听 0.0.0.0 且无认证——公网暴露必须走隧道（frp 等），局域网内使用无碍
- **/exit 可远程关服**：斜杠命令威力大，误发即停对方服务
- **token 归属**：WS 消息驱动下，对方 agent 消耗的是它自己配置的模型/额度，不是调用方
- **demo 不是外置工具**：`tools/remote_client_demo.py` 无 `agt_register`、不进工具箱 schema，只是演示脚本（外置件形态见 [tool-externalization](tool-externalization.md)；若封装 remote_agt 应落外置件体系）

## 相关页面

- [api-status](api-status.md) — REST 通道的跨实例诊断（18+3 字段快照）
- [运维与排障 · 跨进程状态查询](../guides/ops.md) — 多实例部署的运维消费场景
- [run-python](run-python.md) — 客户端脚本的执行载体
- [tool-externalization](tool-externalization.md) — remote_agt 封装若实施，落位外置工具体系
