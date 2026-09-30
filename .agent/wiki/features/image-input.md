# 图片输入链路 · WebUI 贴图 → 视觉 API

> src/static/index.html（readAsDataURL + WS）+ src/server.py（解析 / 落盘 / 传参）+ src/chat.py（`_merge_batch` 第四返回值）+ src/session.py（`Turn.images` + 投影 `image_url` 块 + 格式规范化）。2026-09-29 两轮（commits `d13adca` v0.30.5 / `e2c0b24` v0.30.6）。

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
         data URL 落盘 repo `images/`，文本追加
         「 [图片 <文件名>，你无法直接查看；如需理解其内容请委托视觉子 agent：
            agent_prompt("vision", "请描述 <img>文件名</img> 的内容")]」
[投影] session `_user_content` / `_project_imgs`
   → {"type":"image_url","image_url":{"url": _norm_img_data_url(data_url)}}
```

两条通道的分工是关键：**空闲新轮走原生多模态**（投影构造 `image_url` 块）；**插话走纯文本注入**（图片只能落盘 + `<img>` 引用，理解交给 vision 子 Agent）。

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

## 排障速查

- `image data N failed: Unsupported image format` —— 第 N 张图格式不在 provider 白名单（< 0.30.5 未规范化，升级即愈）
- `image data N failed: invalid image data` —— base64 损坏或超尺寸上限
- **图片发了但 Agent 说没看到** —— ① 版本 < 0.30.6（传参断链，静默丢图）② 走的是 busy 插话路径（图变 `<img>` 引用，须委托 vision 子 Agent 看）
- 本地模型（llama-server）不走 provider 白名单校验，但同样受 2048 边长经验约束

## 相关页面

- [气泡交互](bubble-interaction.md) — 变更文件区的图片/音频内嵌渲染（**展示侧**，与输入侧相对）
- [多 Agent 体系](../architecture/multi-agent.md) — vision 子 Agent 委托看图 / 复活路径复发
- [工具执行审批](tool-approval.md) — 同批 2026-09-29 修复（审批默认关闭 + 刷新恢复）
- [运维与排障](../guides/ops.md) — 常见错误对照
