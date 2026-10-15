# /record · 页面录屏插件（随包种源 src/assets/plugins/record/，2026-10-16，用户提案，commit 6443993）

> 用户提案（2026-10-16）：「添加一个复杂一点的示例插件 /record 吧」——命令面按提案原文实现：start / pause / resume / stop / save `<路径>` / flush（无参=状态查询）。定位是[插件 SDK](../architecture/plugin-sdk.md) 的**「三桥进阶」复杂示例**：比 screenshot（三桥范式档）再高一个复杂度档位，演示插件体系如何承载「前端长生命周期对象 + 浏览器平台限制 + 大数据回传」这类真实功能。

## 职责与用法

| 命令 | 行为 |
|---|---|
| `/record start` | 前端弹「🎬 点击开始录制」确认浮条 →（点击=拿到浏览器激活）→ 选屏授权 → MediaRecorder 开录 |
| `/record pause` / `resume` | 原生暂停/恢复（toast 反馈） |
| `/record stop` | 结束录制，**blob 暂存内存**（toast 报时长/体积/容器） |
| `/record save tmp/myRecord.mp4` | 上传落盘——**扩展名与实际容器自动对齐**（支持 mp4/avc1 就真 mp4，否则 webm），保真实可播放 |
| `/record flush` | 丢弃暂存 |
| `/record`（无参） | 状态查询（toast） |
| dock 🔴 按钮 | 免命令一键起/停 |

录的是**敲命令的那个浏览器标签页/屏幕**（getDisplayMedia 授权范围）。

## 三桥范式 + 四个复杂度维度

三桥同 screenshot 范式：① 命令桥 `register_command("/record")` 后端发令 → ② 前端 JS 干活（MediaRecorder）→ ③ 后端路由 `/api/record/save` 落盘。进阶在教学点比 screenshot 多四个维度：

1. **子命令状态机**：`idle → recording ⇄ paused → blobReady`（save 可多次存不同路径）→ flush 清——单条 `/record` 命令内含六子命令的解析与非法状态拒绝
2. **前端长生命周期对象**：MediaRecorder 跨命令存活——命令只发令，对象与 blob 都在前端状态里（`S = {rec, stream, chunks, state, fmt, blob, t0}`）；`timeslice=1s` 分片收集防长录内存尖峰
3. **浏览器激活限制**（见下节）
4. **大 blob 回传 + 动态容器**：MB 级 POST；容器按浏览器能力自适应

## user activation：WS 事件回调不算激活（关键平台坑）

`getDisplayMedia` 要求 **user activation**——而插件的命令桥走 WS 事件回调，浏览器**不视为用户手势**，直接 `getDisplayMedia()` 会被拒。标准 workaround：`/record start` 时前端先弹一个真按钮浮条，用户点击该按钮即为激活，在 click 处理器内再唤起选屏授权。代码注释（frontend.js）讲清了这条链。

## 动态容器与 save 扩展名对齐

- 录制容器按浏览器能力定：`mp4/avc1` 优先，回退 `webm`（`S.fmt` 记实际格式）
- `/record save` 时扩展名与实际格式**自动对齐**——用户写 `.mp4` 但实际录的是 webm 时落盘为真 webm（反之亦然），避免「能播但后缀骗人」
- 后端落盘响应：`{"ok":true,"path":…,"size":…,"abs":…}`

## 测试模式 `?record-test=1`（无头/自动化验证）

URL 加 `?record-test=1` 用 **canvas 动画流**替代选屏——不弹授权框，Playwright/自动化可全程无人值守验证。本轮 7 步实测即走此模式。

## 自绘 toast（SDK 未暴露 toast 的顺带演示）

SDK 当前未暴露 toast 工具件，前端自绘 toast 实现——顺带演示插件前端的自由度：不依赖聊天流内部结构，独立 DOM 即可。

## 验证（2026-10-16 开发轮，playwright 测试流 7 步全绿）

start → pause → resume → stop（2.5s / 0.09MB / **mp4** 容器）→ save（落盘 **92,042 字节**）→「🎥 录制已保存」聊天卡片 → flush。commit `6443993` 已推送。

## 注意事项

- **save 路由未防路径穿越**：实测 `path=tmp/../evil.webm` 成功写到 workspace 根（`{"ok":true,"abs":"D:\...\evil.webm"}`，测试产物已删）。与[静态资产路由](../architecture/plugin-sdk.md)的防 `../` 穿越**不是一回事**——落盘类路由本来就持有全 workspace 写权，但 HTTP 端点无鉴权直达，内网本机信任模型下暂未收窄；若日后暴露公网需补 resolve-in-workspace 校验
- `register_route` 挂载一次永久：改 backend.py 后 `/reload plugins` 热生效，但路由不随 reload 摘挂（引擎既有设计）
- 改 frontend.js 刷新页面即生效（静态路由 no-cache）

## 相关页面

- [插件 SDK](../architecture/plugin-sdk.md) —— record 是随包六实用例之一（三桥进阶档）；seed_default_plugins 播种与种源机制
- [/cmd](cmd-command.md) —— `register_command` 最小示例；record 是其进阶（单命令多子命令解析）
- [fabDock](fab-dock.md) —— 🔴 dock 按钮席位
