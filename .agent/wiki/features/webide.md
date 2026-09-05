# WebIDE · 📝 IDE 按钮 → VS Code serve-web 新页签打开工作区

## 职责

控件栏一个按钮把整个工作区用浏览器版 VS Code 打开（`code serve-web`，VS Code 1.134+ **自带**，非 code-server、零额外安装）——代码即看即改，手机/局域网设备也可用（serve-web 监听 0.0.0.0）。用户提案 2026-09-06「点一个按钮用 WebIDE 打开工作区」。

## 端点：POST /api/ide/open（src/server.py）

拉起/复用 WebIDE，语义按序：

1. **端口潜规则（用户 2026-09-06）**：WebIDE 端口 = **当前 WebUI 端口 + 30000**——从 `request` 的 Host 头解析（`agt-web --port` 直接决定），多实例各跑各的 serve-web、互不抢固定口。Host 头无端口（异常兜底）→ 38000。
2. **复用优先**：探测动态端口已活（HTTP 可达且 body > 500 字符——VS Code 组件下载占位页仅 146 字符，就绪工作台 4.5KB）→ 直接返回 payload，不重复拉起。
3. **未活则拉起**：`code serve-web --host 0.0.0.0 --port {webui_port+30000} --without-connection-token --accept-server-license-terms --default-folder "{_workspace}"`——`--default-folder` 由**服务端直开工作区**（**勿用 `?folder=` URL 参数**：serve-web 1.134 会误路由成「远程代理」会话，用户实锤，见注意事项）
   - 有 agent：`_agent.services.start("webide", cmd)` **纳管**——看板可见、可停止、退出码可观测；同名条目在但探测不活（僵死/未起完）→ 先 `stop` 再 `start`；
   - 无 agent：独立 `subprocess.Popen`（`CREATE_NO_WINDOW`）兜底。
4. **就绪等待**：150s 轮询（3s 间隔）探测，就绪 → `ready=true`；超时 → `ready=false` + hint（首次启动 VS Code 下载 server 组件，一次性 ~1 分钟，磁盘缓存后重启秒开；serve-web 下载页**自带自动刷新**，页签开着即可）。

## 响应 payload（_ide_payload）

```json
{"ok": true, "port": 39000, "ready": true}
```

- `port` **动态**：当前 WebUI 端口 + 30000（例：WebUI 9000 → 39000、8000 → 38000、9100 → 39100；Host 头无端口兜底 38000）；
- **不再携带 `folder_uri`**（2026-09-06 随 `--default-folder` 直开移除）：工作区由 serve-web 服务端直开，URL 零参数、payload 无需文件夹路径；
- 未就绪时附加 `hint` 字段（前端 toast 直接展示）。

## 前端：📝 IDE 按钮（src/static/index.html）

- 控件栏 `🤖 Agent` 按钮旁新增 `📝 IDE`（`id=btnIde`，title「用 WebIDE (VS Code·serve-web) 新页签打开工作区」）。
- `openWebIde()`：禁用按钮 → `⏳ IDE…` → `fetch POST /api/ide/open` → 用 **`location.hostname`** 拼 `http://{host}:{port}/`（**零参数**——工作区由 serve-web `--default-folder` 直开；勿拼 `?folder=`，1.134 会误路由成远程代理会话）→ `window.open` 新页签 → toast（`ready` 成功附语言 workaround 提示 / 未就绪展示后端 `hint`）→ 恢复按钮。
- host 用 `location.hostname` 而非 `127.0.0.1`：手机/其它设备访问时 serve-web 监听 0.0.0.0 局域网可达。

## 与其他模块的关系

- 后端 `src/server.py`（端点 + `_ide_payload`）+ 前端 `src/static/index.html`（按钮 + `openWebIde()`）两处改动。
- `agent.services` 后台服务纳管——与 [background-scheduler](background-scheduler.md) 的后台服务管理同族（看板可见/可停止）。
- 无框架耦合：纯 HTTP 探测 + 子进程拉起。

## 注意事项

- 首次启动要下载 server 组件（一次性 ~1 分钟，磁盘缓存后重启秒开）；150s 超时返回 `ready=false` **不是失败**——下载页自动刷新。
- **端口是潜规则（WebUI 端口 + 30000），不是 settings 可配**；命令固定 `code serve-web`（可选增强：命令可配、支持 code-server）。
- **手机现状维持**（用户 2026-09-06 裁定「手机就忍了吧，能打开能看就不错了」）：serve-web 监听 0.0.0.0 局域网可达，但竖屏触屏体验受 VS Code Web 本身限制（VS Code 侧的事，不做额外适配）。
- 端口从 Host 头解析：本实例 9000 → WebIDE 39000；改 `agt-web --port` 后自动跟随，多实例互不抢口。
- **勿用 `?folder=` URL 参数（serve-web 1.134 的坑，2026-09-06 用户实锤「不受支持的断点图标 + 怪弹窗」）**：`?folder=` 会被误路由成「远程代理」会话——弹「选择要管理的远程代理」面板、标签页名变成 l10n 字面量（如「不受支持的断点的图标。」）、编辑区渲染出翻译串拼的假帮助文档、文件树图标显示异常。修复：服务端 `--default-folder` 直开工作区，URL 零参数；旧进程需 `/restart` 生效。
- **中文语言包 403 → l10n 字面量泄漏（同一个用户报告的深层诱因）**：vscode-language-pack-zh-hans 的 web 资源经 `vscode-unpkg.net` 代理 **403**（console 多个 403）时 l10n 机制崩坏，UI 泄漏翻译字面量（标签名/图标描述串）。治法：serve-web 内 `Ctrl+Shift+P → Configure Display Language → English` **一次即治**（serve-web 自己的 profile 记住，**不影响桌面版中文**）；serve-web 不支持 `--locale` 参数、`argv.json` 与桌面版共享不能动。若英文切换后文件图标仍是圆点，是 serve-web minimal 版未带文件图标主题（属预期，可装 Seti Icons 扩展美化）。

## 相关页面

- [background-scheduler](background-scheduler.md)（agent.services 服务纳管同族）
- [ops · /restart 看门狗](../guides/ops.md)（服务生命周期）
