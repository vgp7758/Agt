# image_feed · 实时画面装配段（每步最新帧挂投影末尾，2026-10-06，用户提案）

## 职责

让 Agent（典型：子 Agent desktop-operator）**每步实时看到画面源**：assembly 里声明 `image_feed:` 动作项，每次装配都从画面服务取**最新帧** → base64 挂投影末尾。与 [services 依赖声明](../architecture/multi-agent.md)（yml 声明依赖服务、实例化幂等拉起）配合 = 声明自包含：拉起 Agent 即自带眼睛。

## 声明形态（首个实例 desktop-operator）

```yaml
# .agent/agents/desktop-operator/desktop-operator.yml（节选）
assembly:
  - system
  - file: .agent/agents/desktop-operator/desktop-operator.md
  - user_message
  - steps
  - image_feed: http://127.0.0.1:8765/frame   # 必须在 steps 之后（区3 尾部）
services:
  - desktop-frame: python tools/image_feed_poc.py 8765
```

## 求值语义（src/session.py，`kind == "image_feed"`）

| 维度 | 行为 |
|---|---|
| src | `http(s)://`（内存流零落盘，推荐）或文件路径（workspace 相对） |
| 视觉门控 | 非视觉模型（`vision_supported=False`）整段**静默跳过** |
| 失联/空帧 | 降级一行文字（`[image_feed 不可用：...]` / `[image_feed 空帧：...]`），**不炸轮** |
| 哨兵 | 命中返回 `@@IMGFEED@@data:<mime>;base64,<b64>@@`——桶收集处抽走，不进文本桶 |

## 位置约束与图片通道（tail_images，src/session.py 装配）

- **必须声明在 steps 段之后**（区3 尾部）：段产出的哨兵由 merge 桶抽取进 `tail_images`；写进 system run 的会被 warning 跳过（「image_feed 项应声明在 steps 段之后」）
- base64 **不进统计文本桶**（token 统计不含图）
- 组装层把 data URL 变 `image_url` 块：末条 user → content **数组化追加**；末条 assistant → **独立 user 消息承载**（部分端点不允许 assistant 挂图）
- **瞬态语义**：组装层注入，不落 events.jsonl / step 存档；每步求值 = 每步最新帧

## 画面服务：tools/image_feed_poc.py

- `python tools/image_feed_poc.py [port]`（默认 8765，第二参 = 最大宽度）
- PIL.ImageGrab 抓全屏 → 缩放（默认宽 **1280** 控 token）→ JPEG q70 → 内存缓存；`GET /frame` 返回缓存帧（**0.5s 节流**防高频抓屏）；抓屏失败转 503
- 零落盘：帧只驻内存；依赖 Pillow

## 首个实战：desktop-operator（2026-10-06 深夜四连发）

- 三件套实测全过：看帧（image_feed 注入）/ 工具（键鼠模拟/剪切板/窗口切换）/ 剪切板
- **崩溃治理闭环**：desktop-frame 曾 rc=1 挂掉（无输出，疑似抓屏偶发异常）→ 服务化前忘了手工 start 就「睁眼瞎」；services 声明后派活自动拉起（幂等），挂了有 on_exit_wake 通知兜底
- model glm-official-flash（vision）；`tools: ""`——只带目录内专属工具

## 进包播种：新 repo 开箱即得（2026-10-06 · 二，commit 789d88e）

desktop-operator 四件（yml + md 人设 + desktop_tools.py + image_feed_poc.py）进 bundled 播种源 `src/agents/desktop-operator/`，随 wheel 分发；seed 改递归拷贝让 `tools/` 随行，services 命令同步自包含化（指向 `.agent/agents/desktop-operator/tools/`）。通用环境两个前提：**视觉模型**（非 vision 则本段门控静默跳过——agent 可起但看不到桌面）+ `pip install pyautogui pyperclip`（缺依赖仅 warning 空工具箱，不炸实例化）。详见 [workspace 播种](workspace-seeding.md)。

## 编辑器支持

agents 管理页类型下拉 `ACT_TYPES` 增 `image_feed`（src/static/agents.html）——装配动作可直接选型，值 = 画面服务 URL。见 [Agent 管理页](agents-admin.md)。

## 姊妹特性：bg_services watch_tail——服务日志尾部每步投影（2026-10-07，commit 9e1d523）

bg_services 装配段同日增 `start_service(watch_tail=N)`：服务 stdout 日志尾部 N 行随每步投影注入——与本段同属「每步实况注入」家族。分工：本段走**画面**通道（tail_images，vision 投影，看帧服务的画面）；watch_tail 走**文本**通道（零协议，stdout 日志即状态，看服务运行实况）——帧服务两者可同用。详见 [background-scheduler · watch_tail](background-scheduler.md)。

## 注意事项

- 每步一帧有 token 代价：服务端 1280 宽 + JPEG q70 是经济档，更高清改启动第二参（MAX_W）
- 端点挂了不炸轮但「看不见」——服务起停交给 [services 声明](../architecture/multi-agent.md) 管
- 非 vision 模型声明了也白声明（静默跳过）——选视觉模型（desktop-operator 用 glm-official-flash）

## 相关页面

- [services 依赖声明](../architecture/multi-agent.md) —— 服务生命周期随声明走
- [Agent 管理页](agents-admin.md) —— ACT_TYPES 编辑器支持
