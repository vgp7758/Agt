# SCNet 异步生产流水线 · 画布转 API + 容器主动回调（2026-09-14）

> 把 [SCNet 算力网](../guides/scnet.md) 的「本地批量客户端」从纸面推到**真出片 + 异步收货**：容器侧 monitor 主动回调本机 agt，关电脑也能等产物。
> 本轮 commit `18d7e7f`（server.py 回调端点 + 3 个工具脚本），生效需 `/restart`。

## 职责与全链路

```
本地 agt（:9000）                          SCNet 容器（K100_AI 68.7GB）
  │                                          ┌─ ComfyUI :8190（跑批）
  │  enqueue 工作流（HTTP /prompt） ────────▶│
  │                                          ├─ monitor.py :8191（反代 + 监控二合一）
  │                                          │   ├ /monitor        → 状态 JSON
  │                                          │   ├ 其余路径        → 反代 127.0.0.1:8190
  │                                          │   ├ 自检回调：每 60s 重试（等本机 restart）
  │                                          │   └ 轮询 /history → 完成后回调
  │  ◀── POST /api/callback（cpolar 隧道）────┘
  │       token 鉴权 → message 入 inbox 唤醒 Agent
  │       file → 落盘 scnet_inbox/
```

关键点：**容器是主动方**。本机不需要常驻轮询线程，也不需要公网 IP 之外的任何东西——cpolar 隧道已验证通路，容器侧只要拿到回调 URL 就能推。

**⚠️ 平台级约束（本轮实测踩到）**：SCNet 同一实例**只有一个公网代理端口**（`…:58043`），后启动的自定义服务会**顶掉先前服务的入口**。启 monitor(8191) 后 ComfyUI 的公网入口即失效（58043 返回 monitor 状态页，`/history` 查不到）。因此 monitor 必须做成**反代**：对外一个入口，内部按路径分流到 8190。反代为纯 HTTP（ComfyUI API 足够）；前端 WebSocket 不经此代理，进度条降级。

## 本机端点：POST /api/callback（src/server.py）

| 项 | 值 |
|---|---|
| 路径 | `POST /api/callback` |
| 鉴权 | `token`（本轮值 `2bc435c5…`，已写入设置；不匹配即拒） |
| `message` 字段 | 注入 agent inbox → 触发唤醒轮（后台通知语义，见 [user-interaction](user-interaction.md)） |
| `file` 字段 | 落盘 `scnet_inbox/`，供 Agent 后续处理（下载/转存/汇报） |

- 本地模拟回调实测：**HTTP 404**——精确命中预期（当前进程还是旧代码，路由不存在）；`/restart` 后即 200。
- 未鉴权/错 token 行为未做额外容错：宁可 404/401 也不静默吞。

## 容器侧 monitor.py（:8191）· v2 反代 + 监控二合一

**v2 反代版（2026-09-14，`tools/scnet_monitor.py` 208 行）**——因上述单端口约束，monitor 从「独占状态页」升级为「反代 + 监控二合一」：

- **路由**：`StatusHandler`（`BaseHTTPRequestHandler`）——`/monitor*` → 状态 JSON（`STATE`，`_LOCK` 保护）；其余全部反代到 `COMFY = http://127.0.0.1:8190`。
- **反代实现**：`urllib.request` 转发 method + body + 请求头（剔除 `host/content-length/connection/accept-encoding`），回写响应头（剔除 `transfer-encoding/connection/content-length/content-encoding`）；`HTTPError` 原样透传状态码与 body，其他异常 → 502。`do_GET/POST/PUT/DELETE/HEAD` 全部绑到 `_handle`，`log_message` 静默。
- **实测**：`https://c-{id}.ksai.scnet.cn:58043/system_stats` 反代通（API 全通），`/monitor` 走状态页。
- **自检回调**：启动后每 60s 重试一次回调（等本机 `/restart` 激活端点），成功后转正常轮询。
- **监控循环**：轮询 ComfyUI `/history`，任务完成 → 回调本机（message + 产物文件）。
- **history 持久**：monitor 晚接入也能补推已完成任务（不会漏单）。
- **CLI**：`--ids <prompt_id 逗号分隔>` `--cb <回调 URL>` `--token` `--port 8191` `--interval 15` `--no-push-file`。
- **部署通道**：容器内用 **JupyterLab 开终端**重启（本轮实测比 SSH 更好用——该镜像未装 SSH）。

## 热态出片速度实测（2026-09-14）

| 任务 | 状态 | execution 时长 |
|---|---|---|
| e4bd2630（第一单·含首次权重加载） | ✅ | **607s**（10分07秒） |
| 5b4caea7（第二单·权重已热） | ✅ | **464s**（7分44秒） |
| 1b01d575（第三单） | 🔄 进行中 | — |

**结论修正**：权重加载只占约 2.4 分钟，**真正瓶颈是推理本身 ~7-8 分钟/单**（480p/5s，4 步 Turbo + EasyCache，K100_AI）。批量生产偏慢，后续调优杠杆：`low_vram` 开关、EasyCache 参数、分辨率降档。

**异常观察**：history 里另有两个非本机提交的 `error` 任务（20:19、20:30），疑似 ComfyUI 前端页面自动提交，待查。

**产物**：`scnet_outputs/MiniMax_H3_00002_.mp4`（0.74 MB，第二单已下载）。

## 画布格式 → API 格式转换器：tools/wf_canvas2api.py

ComfyUI 的「画布 JSON」（编辑器导出，含 nodes/links/widgets_values）与 API 提交用的「prompt JSON」（`{node_id: {class_type, inputs}}`）格式不同。本工具做转换，需 `object_info` 作类型元数据（`scnet_objinfo.json`）。

```bash
python -c "import sys; sys.path.insert(0,'tools'); from wf_canvas2api import convert; \
  convert('scnet_wf_duotu.json','scnet_objinfo.json','scnet_wf_duotu_api.json')"
```

**四个对位坑（全部实测踩过并修掉）**

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | 连接值写成输出名 | API 格式连接值是 `[源节点id(str), 源输出slot索引(int)]`（execution.py L935 `r[val[1]]`） |
| 2 | DYNAMICCOMBO 带点子参数 | `format.codec` 这类带点键要单独对位，不能只按 `format` 匹配 |
| 3 | widget 占位语义 | 画布 `inputs` 里 `link=null` 的端口顺序即 widget 顺序；**连接型类型不占位**、已连接残留占位丢弃 |
| 4 | 旧式 `COMBO` 类型名 | object_info 里类型名可能是旧式 `COMBO`，需归一 |

- 转换时 `SKIP_TYPES = {MarkdownNote, Note}` 跳过展示型节点（本轮 26 节点 → 23 执行节点）。
- 修完后 `POST /prompt` 校验通过、真出片。

## 第一单真实出片（2026-09-14）

| 项 | 值 |
|---|---|
| 产物 | `MiniMax_H3_00001_.mp4`（0.77 MB · 5 秒 · 480p 竖屏） |
| 场景 | 参考图人物对话（多图参考 minimax + easycache + 4 步 lora 工作流） |
| 落盘 | 本地 `scnet_outputs/` |
| 第二批 | 2 单已 enqueue（不同 seed 变体，权重热，每单约 1-2 分钟） |

## 固定打法（批量生产）

1. 本地 `convert` 出 API JSON（或直接用 `tools/scnet_comfy_client.py` 的 `--batch`）
2. enqueue N 单（不同 seed / 提示词 / 镜头）
3. 平台页一键启动 monitor（:8191，**反代版**——一个入口同时保住 ComfyUI API 与状态页）
4. **关电脑等收货**——回调唤醒 Agent，产物自动落 `scnet_inbox/`

## 注意事项

- `/api/callback` 是引擎层改动，**必须 `/restart` 才生效**；在此之前容器侧自检会一直 404 重试。
- **单端口约束**：同一实例的自定义服务入口唯一，后启动顶掉先启动——任何新服务上线前先想清楚是否要反代（本轮 monitor 已按此改造）。
- ComfyUI API 无鉴权，URL 即凭证（cpolar 隧道同理）——勿外泄。
- 实例按 ¥2.53/时计费，余额有限时记得收工关机；monitor 的「全部完成」通知会提示是否关机。
- 画布转换器依赖 `object_info` 快照（`scnet_objinfo.json`）——换镜像/换节点版本后要重新拉。
- 出片速度 ~7-8 分钟/单（热态），排产时按此估时。

## 相关页面

- [SCNet 算力网](../guides/scnet.md) — 平台通道、镜像、资源与价格
- [user-interaction](user-interaction.md) — 回调 message 注入 inbox 的唤醒语义
- [background-scheduler](background-scheduler.md) — 定时任务与后台服务机制
