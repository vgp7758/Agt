# WebIDE · 📝 IDE 按钮 → VS Code serve-web 新页签打开工作区

## 职责

控件栏一个按钮把整个工作区用浏览器版 VS Code 打开（`code serve-web`，VS Code 1.134+ **自带**，非 code-server、零额外安装）——代码即看即改，手机/局域网设备也可用（serve-web 监听 0.0.0.0）。用户提案 2026-09-06「点一个按钮用 WebIDE 打开工作区」。

## 端点：POST /api/ide/open（src/server.py）

拉起/复用 WebIDE，语义按序：

1. **复用优先**：探测 8443 已活（HTTP 可达且 body > 500 字符——VS Code 组件下载占位页仅 146 字符，就绪工作台 4.5KB）→ 直接返回 payload，不重复拉起。
2. **未活则拉起**：`code serve-web --host 0.0.0.0 --port 8443 --without-connection-token --accept-server-license-terms`
   - 有 agent：`_agent.services.start("webide", cmd)` **纳管**——看板可见、可停止、退出码可观测；同名条目在但探测不活（僵死/未起完）→ 先 `stop` 再 `start`；
   - 无 agent：独立 `subprocess.Popen`（`CREATE_NO_WINDOW`）兜底。
3. **就绪等待**：150s 轮询（3s 间隔）探测，就绪 → `ready=true`；超时 → `ready=false` + hint（首次启动 VS Code 下载 server 组件，一次性 ~1 分钟，磁盘缓存后重启秒开；serve-web 下载页**自带自动刷新**，页签开着即可）。

## 响应 payload（_ide_payload）

```json
{"ok": true, "port": 8443, "ready": true,
 "folder_uri": "file:///D:/AI_Usings/Agt"}
```

- `folder_uri` 用 `file:///` 形态（serve-web `?folder=` 参数约定）；
- 未就绪时附加 `hint` 字段（前端 toast 直接展示）。

## 前端：📝 IDE 按钮（src/static/index.html）

- 控件栏 `🤖 Agent` 按钮旁新增 `📝 IDE`（`id=btnIde`，title「用 WebIDE (VS Code·serve-web) 新页签打开工作区」）。
- `openWebIde()`：禁用按钮 → `⏳ IDE…` → `fetch POST /api/ide/open` → 用 **`location.hostname`** 拼 `http://{host}:{port}/?folder={folder_uri}` → `window.open` 新页签 → toast（`ready` 成功 / 未就绪展示后端 `hint`）→ 恢复按钮。
- host 用 `location.hostname` 而非 `127.0.0.1`：手机/其它设备访问时 serve-web 监听 0.0.0.0 局域网可达。

## 与其他模块的关系

- 后端 `src/server.py`（端点 + `_ide_payload`）+ 前端 `src/static/index.html`（按钮 + `openWebIde()`）两处改动。
- `agent.services` 后台服务纳管——与 [background-scheduler](background-scheduler.md) 的后台服务管理同族（看板可见/可停止）。
- 无框架耦合：纯 HTTP 探测 + 子进程拉起。

## 注意事项

- 首次启动要下载 server 组件（一次性 ~1 分钟，磁盘缓存后重启秒开）；150s 超时返回 `ready=false` **不是失败**——下载页自动刷新。
- 端口固定 8443、命令固定 `code serve-web`，**未做成 settings 可配**（可选增强：端口/命令可配、支持 code-server）。
- 手机竖屏触屏体验受 VS Code Web 本身限制（VS Code 侧的事）。

## 相关页面

- [background-scheduler](background-scheduler.md)（agent.services 服务纳管同族）
- [ops · /restart 看门狗](../guides/ops.md)（服务生命周期）
