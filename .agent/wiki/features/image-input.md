# 图片输入链路 · WebUI 贴图 → 视觉 API

> src/static/index.html（readAsDataURL + WS）+ src/server.py（解析 / 落盘 / 传参）+ src/chat.py（`_merge_batch` 第四返回值）+ src/session.py（`Turn.images` + 投影 `image_url` 块 + 格式规范化 + 插话 `<img>` 标签门控展开）。2026-09-29 两轮（commits `d13adca` v0.30.5 / `e2c0b24` v0.30.6）；2026-10-03 插话原生看图（commit `e5398f2`）。

## 全链路

```
[前端] 贴图/附件 → FileReader.readAsDataURL(file) → data URL 列表
   → ws.send({text, images:[dataURL…]})                        （index.html sendRaw）
[server] 解析 images
   ├─ 空闲（非 busy）→ _work_q.put(("user", text, images))      ← 三点元组
   │     → chat._worker: item[2] → _merge_batch 第四返回值 _batch_imgs
   │     → agent.run(user_msg, images=_batch_imgs)             【原生多模态通道】
   │     → session Turn.images → 投影 image_url 块
   └─ 忙碌（busy 插话）→ agent.queue_user_message(text + _materialize_user_images(images))
         data URL 落盘 repo images/，文本嵌 <img>文件名</img> 标签（2026-10-03 起）
         → 步边界注入〔用户中途补充〕user 消息
         → 投影时 _project_imgs 按 vision 门控展开（见下节「插话原生看图」）
[投影] session `_user_content` / `_project_imgs`
   → {"type":"image_url","image_url":{"url": _norm_img_data_url(data_url)}}
```

两条通道殊途同归（2026-10-03 起）：**空闲新轮走原生多模态**（`Turn.images` 直投 `image_url` 块）；**busy 插话经 `<img>` 标签 + 投影门控展开**——视觉模型同样直接看图，不再强制委托 vision 子 Agent 中转（历史形态曾是纯文本注入 + 固定「无视觉能力」文案，见下节）。

## 图片格式规范化 `_norm_img_data_url`（2026-09-29，commit `d13adca`，v0.30.5）

**现象**：VM 里贴图 → 智谱报 400 `image data 0 failed: Unsupported image format or invalid image data`。

**根因**：前端 `readAsDataURL` **原样保留文件 mime**（浏览器存下来的图常是 WebP，另有 GIF/BMP/HEIC），后端此前原样透传——而智谱等 provider 的视觉白名单只吃 PNG/JPEG。

**修复**（src/session.py 模块级函数，两处接入：`_user_content` 的 `[图片 路径]` 分支 + `_project_imgs` 的 `turn.images`）：

| 情形 | 处理 |
|---|---|
| `image/png`、`image/jpeg` 且两边 ≤2048 | **原样返回**（零开销） |
| 其它格式（webp / gif / bmp / heic…） | **PIL 重编码为 PNG**（GIF / 多帧取首帧） |
| 任一边 >2048 | 等比缩到限内（`Image.LANCZOS`） |
| PIL 不可用 / 解析失败 / 非 data URL | 原样返回（**尽力而为，不炸投影**） |

`_MAX_IMG_EDGE = 2048` 与 [real_tools](run-python.md) 同值（视觉 API 边长上限，超限同样 400）。九场景实测全绿（webp→PNG / bmp→PNG / gif→PNG / 小图 PNG·JPEG 原样 / 4000×3000→2048×1536 / http URL / 非法 base64 / None 均安全）。

## v0.30.6 三层修复（2026-09-29，commit `e2c0b24`）

| # | 文件 | bug | 症状 |
|---|---|---|---|
| ① | src/session.py | `_norm_img_data_url` 缩进错误**写在 Session 类体内**——类体内无 self 的函数不进模块命名空间 → 模块级调用 `NameError` | **重启后会话拉不起来**（用户报告的核心问题） |
| ② | src/multiagent.py | `_revive_subagent` 缺 `sub = SubAgent(...)`（`2cec328` 目录形态重构误删） | 复活必炸被静默吞 → 子 Agent 全部重建实例（见 [multi-agent · 复活路径复发二](../architecture/multi-agent.md)） |
| ③ | src/server.py + src/chat.py | server 解析出 images 后**从未传给 agent.run** | WebUI 上传的截图被**静默丢弃**（agent 看不到图） |

③ 的修复链：`_work_q` 三点元组 `("user", text, images)` → chat `_worker` 取 `item[2]`（二元组兼容）→ `_merge_batch` 第四返回值 `_batch_imgs` → `agent.run(images=…)`；插话路径新增 `_materialize_user_images()`（落盘 + `<img>` 标签指路 vision 子 agent）。

### 实战首验（2026-09-29）：用户截图贯通读取

### 实战首验（2026-09-29）：用户截图贯通读取

用户贴入 WebP 截图（VM 启动报错堆栈），完整读出内容——规范化链路（v0.30.5 `d13adca`）+ v0.30.6 三层修复后的**首次真实场景验证**（此前只有探针/单测）。同轮顺带产出 httpx 依赖修复（v0.30.8，pyproject 显式声明——VM 全新环境启动即崩 ModuleNotFoundError，见 [home](../home.md) 快速事实增补）。

## 端点拒图自动降级：历史轮图片 × 非视觉模型不再 400（2026-09-30，glm-5.3 实锤，commit `5c852fc`，随 v0.30.11）

**场景**：会话历史里已有图片（投影含 `image_url` 块），切到非视觉模型——或 vision 卡片配置错（glm-5.3 曾被误标 true）——端点 400 `1210 messages.content.type 参数非法，取值范围 ['text']`，每轮必炸（glm-5.3 的 chat completions 端点不收图，视觉是 glm-4v / glm-4.5v 系列的活）。

**机制链**（src/llm_client.py + src/agent.py，随 v0.30.11）：

```
端点拒图（_is_img_reject：1210+content.type / image_url not supported / invalid image+type…）
  → llm_client：vision_supported 自动降 False + 抛 ImageUnsupportedError（跳过回退链、不记冷却）
  → agent：invalidate_projection（历史图降为文字占位重投影）→ 同模型重试（上限 3 次防循环）
  → WebUI：⚠️ xx 端点不支持图片输入，已降级为文字占位（需要看图 → Agent 委托 vision 子 agent）
```

**语义**：图降级为 `[图片 文件名]` 文字占位，**模型不换**——用户选它要的是文本能力，不被一张历史图踢进回退链换模型。粘性路由 / 等网重试等既有韧性机制不受影响。机制细节与 llm_calls 定性时间线见 [LLM 客户端韧性 · 端点拒图自愈](llm-network-resilience.md)。

## 图片按视觉能力门控投影：非视觉模型投影时即降文字占位（2026-09-30，commit `3591683`，随 v0.30.11）

**动机**：与上节拒图自愈同根——历史轮投影含 `image_url` 块而当前模型不收图 → 端点 400。拒图自愈（`5c852fc`）是**事后自愈**（端点炸了 → 降 vision 位 → 重投影重试，每炸一次烧一次请求）；本修是**事前门控**（400 根因之一，`5c852fc` 之外的第二处源头）：投影构造时就不给非视觉模型发图，请求还没出去 400 就不会发生。

**机制**（src/session.py 投影链，`_project_imgs`）：`turn.images` 进投影时按当前模型视觉能力位分流——视觉模型照旧 `image_url` 块；非视觉 → 降 `[图片 文件名]` 文字占位（与拒图自愈降级后的形态一致）。与手动切模型全量重刷（`ce8ed57`，见 [context-engine](../architecture/context-engine.md)）配套：切回视觉模型重投影即恢复图块。

**顺带修复**：`_project_imgs` 嵌套占位递归放大 bug——已降级的占位文本被再次当图片处理、递归包装越投越长。

**双层分工**：门控挡在投影侧（事前，正常路径零请求浪费）；自愈兜在响应侧（事后，覆盖门控拦不住的形态——如卡片 vision 位置信、端点收图但格式拒）。两者同批随 v0.30.11 发布，自愈侧细节见 [LLM 客户端韧性 · 端点拒图自愈](llm-network-resilience.md)。

## 插话原生看图：`<img>` 标签按 vision 门控展开（2026-10-03，用户裁定，commit `e5398f2`）

**动机**：2026-09-29 插话丢图修复（`e2c0b24` ③）的形态是**纯文本注入**——图片落盘后插话文本追加固定文案「[图片 xxx，当前模型无视觉能力无法查看…请委托视觉子 agent]」。这条文案对**视觉模型是误导**：当前模型明明能看图（如 glm），却被声明"无法查看"，想看一张插话里的截图还得派 vision 子 Agent 中转。用户裁定（2026-10-03）：**插话原生看图**——按当前模型 vision 门控展开，与原生贴图（`agent.run(images=)`）同语义。

**机制**（最小改动，复用 `3591683` 的 vision 门控基建，四处接线）：

| 位置 | 改动 |
|---|---|
| src/server.py `_materialize_user_images`（L2713 起） | 固定误导文案 → append `<img>文件名</img>` 标签（纯标记，不带任何能力断言，能力判断交给投影层） |
| src/session.py 当前轮 steps 注入位（L1260） | `preceding_hint` → `self._project_imgs(_MIDTURN_TAG + hint)` |
| src/session.py pending hint 注入位（L1357） | `_pending_step_hint` 同款包裹 |
| src/session.py 历史档滚入（L2989） | 同款包裹——插话的图滚入历史后**依然按门控展开**，与 `Turn.images` 通道历史行为一致 |

**三态门控**（`_project_imgs` 对插话文本内的 `<img>` 标签）：

| 模型 | 展开形态 |
|---|---|
| 视觉模型 | `[text 块 + image_url 块]`——读盘转 data URL，模型当步直接看像素 |
| 非视觉模型 | 文字占位（提示可委托 vision 子 Agent） |
| 插话无 `<img>` 标签 | **原样 str，byte-stable**——不带图的插话投影逐字节不变，缓存前缀零扰动 |

**验证**（五通道全绿，真 1×1 png 探针）：`_project_imgs` 三态直测 ✓ / 当前轮 steps 通道 ✓ / pending hint 通道 ✓ / 历史档通道 ✓ / byte-stable ✓。插曲：探针图须落 `repo_images_dir()`（与 server `_materialize_user_images` 同目录），放 repo 根 `images/` 会 FAIL——**落盘位置是投影解析的契约**，两端必须同一 `repo_images_dir` 真源。

**生效方式**：引擎层两文件（server.py / session.py），需 `/restart`。

**同批顺带**：`_aid` 缩进断裂（三天潜伏 bug，用户实际使用撞出）修复一并推送。

**关联**：[vision 门控投影](#图片按视觉能力门控投影非视觉模型投影时即降文字占位2026-09-30commit-3591683随-v03011)（基建来源，`Turn.images` 通道）· [端点拒图自愈](llm-network-resilience.md)（响应侧兜底）· [用户交互 · 插话](user-interaction.md)（插话队列与步边界注入）

### 实战翻车：`<img>` 发 vision 子 Agent 三占位全「读取失败」（2026-10）

**现象**（2026-10 求职视频校验实战，机制上线后首次真用即翻车）：主 Agent 给 vision 子 Agent 的任务文本带三个 `<img>probe_qN.png</img>` 占位（视频抽帧图），对方展开时**全部报「读取失败」**、未看到像素，改用 YuNet 人脸检测 + 像素统计兜底完成校验（全过程见 [job-hunt · 视频校验管线](job-hunt.md)）。

**根因**未程序化实锤，但与上节验证插曲的契约完全吻合：**`<img>` 展开只认 `repo_images_dir()` 落盘位置**——抽帧产物当时落在 workspace 根（`D:\AI_Usings\Agt\probe_q1.png`），投影解析端按 `repo_images_dir` 真源找不到文件。教训：**给子 Agent 发 `<img>` 前，图必须先落 `repo_images_dir()`**；工具产物默认落 workspace 根 ≠ 契约位置。

## 排障速查

- `image data N failed: Unsupported image format` —— 第 N 张图格式不在 provider 白名单（< 0.30.5 未规范化，升级即愈）
- `image data N failed: invalid image data` —— base64 损坏或超尺寸上限
- `1210 messages.content.type 参数非法，取值范围 ['text']` —— **端点不收图**（glm-5.3 等 chat completions 无视觉通道，视觉是 glm-4v/4.5v 系的活）；< 0.30.10 且卡片 vision 卡错时每轮 400，升级后自愈（拒图自动降级：图降文字占位同模型重试，见上节）
- **图片发了但 Agent 说没看到** —— ① 版本 < 0.30.6（传参断链，静默丢图）② busy 插话路径 + 当前模型非视觉：图经 `<img>` 标签降文字占位（视觉模型 2026-10-03 起原生看图，见上上节；此前一律须委托 vision 子 Agent）
- 本地模型（llama-server）不走 provider 白名单校验，但同样受 2048 边长经验约束

## 相关页面


- [气泡交互](bubble-interaction.md) — 变更文件区的图片/音频内嵌渲染（**展示侧**，与输入侧相对）
- [多 Agent 体系](../architecture/multi-agent.md) — vision 子 Agent 委托看图 / 复活路径复发
- [工具执行审批](tool-approval.md) — 同批 2026-09-29 修复（审批默认关闭 + 刷新恢复）
- [运维与排障](../guides/ops.md) — 常见错误对照
- [生成图自动可视](image-autoview.md) — 反向通道（2026-10-07）：工具生成的新增图片自动挂投影（伪造 read_file 对，`vision_supported` 门控）——与本页共用 vision 门控与 image_url 块基建

