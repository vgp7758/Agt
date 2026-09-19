# 外部事件注入（脚本 / 服务 / 其它机器 → Agt 实例）

> **给 Agent 的一段话**：当你需要「让一个脚本、后台服务、定时任务或另一台机器，把事件推送给某个正在运行的 Agt 实例（可能是你自己、也可能是组网中的队友）」时，用本文档的 **HTTP 回调通道**。
> 核心一句话：`POST http://<实例地址>/api/callback` + header `X-Cb-Token: <callback_token>` + JSON body `{"text": "...", "source": "..."}` → 消息进入该实例的 inbox 并**唤醒它跑一轮**。

---

## 一、HTTP 回调通道（推荐：任何进程都能用）

### 1.1 鉴权

| 项 | 说明 |
|---|---|
| 凭据 | 目标实例 `settings.json` 里的 `callback_token`（32 位 hex） |
| 传递位置 | **HTTP header `X-Cb-Token`（首选）** > query `?token=`（兜底） |
| 为什么优先 header | cpolar 等内网穿透隧道会**丢弃 query string**（2026-09-14 实测），header 不受影响 |
| 未配置时 | 实例拒绝**所有**回调（返回 `{"ok":false,"error":"未配置 callback_token…"}`） |
| 配置位置 | `<实例工作目录>/.agent/settings.json` 或 `$AGT_HOME/settings.json`（AGT_HOME 默认 `~/.agt`） |

### 1.2 形态 A：JSON 消息（文本事件）

```bash
curl -X POST http://127.0.0.1:9000/api/callback \
  -H "Content-Type: application/json" \
  -H "X-Cb-Token: <callback_token>" \
  -d '{"text": "[构建完成] 制品已产出：dist/app.exe", "source": "ci"}'
# → {"ok": true, "queued": true, "inbox_size": 1}
```

```python
import json, urllib.request
def push_event(base_url: str, token: str, text: str, source: str = "script") -> dict:
    """向 Agt 实例推一条事件（脚本/服务通用）。等价于 agent 的 push_message(wake=True)。"""
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/callback",
        data=json.dumps({"text": text, "source": source}, ensure_ascii=False).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Cb-Token", token)          # ★ 鉴权走 header
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())
```

**语义**：
- 消息进入目标实例的 **inbox**（持久化到 `inbox.jsonl`，重启不丢）
- `push_message(..., wake=True)`：**实例空闲时会立刻开一轮** ReAct 处理这条消息；忙碌时排队
- 处理时消息以 `source` 标注（如 `callback:probe`），在 WebUI 里渲染为系统气泡

### 1.3 形态 B：文件推送（二进制 / 大文件）

```bash
curl -X POST http://127.0.0.1:9000/api/callback \
  -H "X-Cb-Token: <callback_token>" \
  -H "X-Cb-Type: file" \
  -H "X-Cb-Filename: render_0007.mp4" \
  --data-binary @render_0007.mp4
# → {"ok": true, "saved": "<workspace>/scnet_inbox/153012_render_0007.mp4", "size": 812345}
```

- 文件落到目标实例的 `<workspace>/scnet_inbox/{HHMMSS}_{filename}`
- 同时自动推一条通知消息（`📥〔外部回调·文件〕…`）唤醒 Agent

---

## 二、Agent 侧通道（实例之间，工具层）

Agent 自己（而非外部脚本）联动其它实例时，用工具并带 `remote_instance_id` 路由：

| 工具 | 语义 | 是否阻塞 |
|---|---|---|
| `remote_message(id, message, expect_reply=…)` | 异步投递一条消息（默认触发对方一轮） | 否 |
| `remote_ask(id, question)` | 提问并等对方回答 | 是 |
| `remote_call_tool(id, name, arguments)` | 远程直执行某工具（纯工具调用，不产生对方上下文） | 是 |
| `agent_prompt(name, task)` | 派活给本实例的子 Agent | 可异步可等待 |

> 外部脚本无法直接调这些工具——用第一节的 HTTP 通道即可（`remote_call_tool` 的底层就是 `POST /api/tool/exec`）。

---

## 三、只读 / 其它常用端点

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/status` | POST | 实例状态快照（就绪/模型/工具数/session/busy/work_q/inbox 深度/子进程树） |
| `/api/tool/exec` | POST | `{name, arguments}` → 在本实例执行工具，返回结果（跨实例工具路由的落地端） |
| `/api/dash` | GET | 团队 + 后台服务看板数据 |
| `/api/stats` | GET | LLM 调用统计（缓存命中/模型/耗时/cache_id） |
| `/api/wf/runs` | GET | 工作流运行列表（观测页数据源） |

---

## 四、实战清单（踩过的坑）

1. **隧道场景必须用 header**：cpolar 免费版丢 query string；调用方还要**解析响应体确认 `ok==true`**（只看 HTTP 200 会误判成功——曾出现"假成功"）。
2. **AGT_HOME 非默认的环境**：`/api/callback` 曾硬编码读 `~/.agt/settings.json`，在 `AGT_HOME=/workspace/.agent-data/agt`（CNB 容器）下读不到 token → 所有回调被拒。已在 `src/server.py` 改为走 `config.load_runtime_settings()`（2026-09-19 修复）；旧版本环境可加软链 `ln -s $AGT_HOME ~/.agt` 兜底。
3. **消息驱动 ≠ 工具路由**：`/api/callback` 推的消息会进入对方**上下文**并触发它自己决策（适合"派活/通知"）；`/api/tool/exec` 只是借用对方的手脚执行（**不进对方上下文**，适合"取数/操作"）。
4. **别把 token 写进 query 或日志**：token 等同于该实例的远程控制权（可推消息、可执行工具）。
5. **幂等与去重**：回调没有内置去重——需要幂等时请在 `source`/`text` 里带业务幂等键，或让处理方查台账。

---

*最后更新：2026-09-19（含 CNB 容器 AGT_HOME 修复）。相关代码：`src/server.py` 的 `api_callback` / `api_tool_exec`；`src/agent.py` 的 `push_message`。*
