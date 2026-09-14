# SCNet 异步生产流水线 · 画布转 API + 容器主动回调（2026-09-14）

> 把 [SCNet 算力网](../guides/scnet.md) 的「本地批量客户端」从纸面推到**真出片 + 异步收货**：容器侧 monitor 主动回调本机 agt，关电脑也能等产物。
> 本轮 commit `18d7e7f`（server.py 回调端点 + 3 个工具脚本），生效需 `/restart`。

## 职责与全链路

```
本地 agt（:9000）                          SCNet 容器（K100_AI 68.7GB）
  │                                          ┌─ ComfyUI :8190（跑批）
  │  enqueue 工作流（HTTP /prompt） ────────▶│
  │                                          ├─ monitor.py :8191（状态页 + 监控）
  │                                          │   ├ 自检回调：每 60s 重试（等本机 restart）
  │                                          │   └ 轮询 /history → 完成后回调
  │  ◀── POST /api/callback（cpolar 隧道）────┘
  │       token 鉴权 → message 入 inbox 唤醒 Agent
  │       file → 落盘 scnet_inbox/
```

关键点：**容器是主动方**。本机不需要常驻轮询线程，也不需要公网 IP 之外的任何东西——cpolar 隧道已验证通路，容器侧只要拿到回调 URL 就能推。

## 本机端点：POST /api/callback（src/server.py）

| 项 | 值 |
|---|---|
| 路径 | `POST /api/callback` |
| 鉴权 | `token`（本轮值 `2bc435c5…`，已写入设置；不匹配即拒） |
| `message` 字段 | 注入 agent inbox → 触发唤醒轮（后台通知语义，见 [user-interaction](user-interaction.md)） |
| `file` 字段 | 落盘 `scnet_inbox/`，供 Agent 后续处理（下载/转存/汇报） |

- 本地模拟回调实测：**HTTP 404**——精确命中预期（当前进程还是旧代码，路由不存在）；`/restart` 后即 200。
- 未鉴权/错 token 行为未做额外容错：宁可 404/401 也不静默吞。

## 容器侧 monitor.py（:8191）

- **状态页**：浏览器直开 `https://c-{id}.<region>.scnet.cn:58043/` 之外的 8191 端口（经「访问自定义服务」代理）看任务队列与进度。
- **自检回调**：启动后每 60s 重试一次回调（等本机 `/restart` 激活端点），成功后转正常轮询。
- **监控循环**：轮询 ComfyUI `/history`，任务完成 → 回调本机（message + 产物文件）。
- **history 持久**：monitor 晚接入也能补推已完成任务（不会漏单）。

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
3. 平台页一键启动 monitor（:8191）
4. **关电脑等收货**——回调唤醒 Agent，产物自动落 `scnet_inbox/`

## 注意事项

- `/api/callback` 是引擎层改动，**必须 `/restart` 才生效**；在此之前容器侧自检会一直 404 重试。
- ComfyUI API 无鉴权，URL 即凭证（cpolar 隧道同理）——勿外泄。
- 实例按 ¥2.53/时计费，余额有限时记得收工关机；monitor 的「全部完成」通知会提示是否关机。
- 画布转换器依赖 `object_info` 快照（`scnet_objinfo.json`）——换镜像/换节点版本后要重新拉。

## 相关页面

- [SCNet 算力网](../guides/scnet.md) — 平台通道、镜像、资源与价格
- [user-interaction](user-interaction.md) — 回调 message 注入 inbox 的唤醒语义
- [background-scheduler](background-scheduler.md) — 定时任务与后台服务机制
