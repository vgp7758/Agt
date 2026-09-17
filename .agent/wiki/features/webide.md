# WebIDE · VS Code logo 按钮 → serve-web 新页签打开工作区

## 职责

控件栏一个按钮把整个工作区用浏览器版 VS Code 打开（`code serve-web`，VS Code 1.134+ **自带**，非 code-server、零额外安装）——代码即看即改，手机/局域网设备也可用（serve-web 监听 0.0.0.0）。用户提案 2026-09-06「点一个按钮用 WebIDE 打开工作区」。

## 端点：POST /api/ide/open（src/server.py）

拉起/复用 WebIDE，语义按序：

1. **端口潜规则（用户 2026-09-06）**：WebIDE 端口 = **当前 WebUI 端口 + 30000**——从 `request` 的 Host 头解析（`agt-web --port` 直接决定），多实例各跑各的 serve-web、互不抢固定口。Host 头无端口（异常兜底）→ 38000。
2. **复用优先（`_alive` 判定，2026-09-06 二轮拆分）**：探测动态端口**能建立 HTTP 连接**（`_alive`：无论 202 下载页还是 200 工作台都算活）→ 直接复用该口返回 payload，**不重复拉起**——页签自己等 serve-web 下载页自动刷新。`_ready`（body > 500 字符——下载占位页仅 146 字符）**不再参与复用判定**，只决定 toast 文案（「组件还在初始化」/「已就绪」）。
3. **未活则拉起**：`code serve-web --host 0.0.0.0 --port {webui_port+30000} --without-connection-token --accept-server-license-terms --default-folder "{_workspace}"`——`--default-folder` 由**服务端直开工作区**（**勿用 `?folder=` URL 参数**：serve-web 1.134 会误路由成「远程代理」会话，用户实锤，见注意事项）
   - **端口 bind 实测 + 向后扫描（2026-09-06 轮次一，治幽灵占位）**：潜规则口被占（WinNAT 隐形保留 / serve-web 孤儿进程）→ 向后扫 **+1..+5** 选第一个可 bind 的口，不再死磕固定口、不依赖人工清理；
   - 有 agent：`_agent.services.start("webide", cmd)` **纳管**（**独立进程组，stop 整树杀**——治孤儿模式：code-tunnel 启动器被杀后 node 子进程继承 socket 成幽灵 LISTENING）——看板可见、可停止、退出码可观测；同名条目在但探测不活（僵死/未起完）→ 先 `stop` 再 `start`；
   - 无 agent：独立 `subprocess.Popen`（`CREATE_NO_WINDOW`）兜底。
   - **`--commit-id` 钉缓存版本（2026-09-17，用户提案，commit `b17dc25`）**：拉起前扫缓存目录——有缓存 → 命令追加 `--commit-id <hash>`（不再检查更新、纯秒起）；无缓存（首次）→ 不加参数拉最新、下次命中。治「VS Code 更新即重下全套 689MB」，详见下文专节。
4. **等待上限 12s（2026-09-06，150s→12s）**：就绪 → `ready=true`；未就绪 → `ready=false` + hint（首次启动 VS Code 下载 server 组件，一次性 ~1 分钟；钉缓存版本后正常情况不再重下，见下文专节；**不再长轮询阻塞交互**——此前 150s 长轮询导致按钮卡 ⌛ 两分半、页签迟迟不弹，用户实锤）。

## `--commit-id` 钉缓存版本：VS Code 更新/重启不再重下 689MB（2026-09-17，用户提案，commit b17dc25）

用户观察：「打开 WebIDE 下载时间挺长，重启后似乎要重新下载——不能第一次下载后缓存一下吗？」缓存目录数据实锤 + 根因定位 + 一轮修复闭环。

**实锤：十一天重下三次**——`%USERPROFILE%\.vscode\cli\serve-web\<hash>\`（组件按 commit-hash 绑目录）：`a44adf7f…` 09-06 03:14（714MB）、`645f29cc…` 09-10 07:49（685MB）、`7debcd0e…` 09-17 19:57（689MB，当晚点按钮触发）。

**根因**：`code serve-web` **默认拉「最新 stable commit」**——VS Code 客户端几天一自动更新 → commit 变 → hash 目录判不匹配 → **重新下载全套**（node.exe 92MB + out/extensions ≈ 689MB）。用户看到的「重启后重下」实为「**VS Code 更新后重下**」——版本没变时缓存本来命中、秒起，只是每次启动都去问一遍最新版。

**修复（src/server.py 端点命令构造，commit `b17dc25`）**：拉起前扫缓存目录，**选 mtime 最新且含 node.exe 的 hash 目录**——

- **有缓存** → 命令追加 `--commit-id <hash>`（`code serve-web --help` 原生参数："Use a specific commit SHA for the client"）——**不再检查更新，纯秒起**；除非缓存被清，永远不会再下载；
- **无缓存（首次）** → 不加参数拉最新 stable，下载完下次自然命中（从此钉住）。

本机验证：扫描命中 `7debcd0e…`（09-17 版）——之后点 IDE 按钮纯秒起。

**代价与更新方式**：钉住后 WebIDE 客户端版本**不再跟随 VS Code 自动更新**（停在当前 commit）；想升级时删掉缓存目录里的旧 hash 文件夹 → 回落「无缓存」分支重下最新，随后再次钉住。生效：`/restart`（server.py 改动）。

### 滚动预热：用旧版秒起 + 后台预下新版 + 下次切换（2026-09-17 同日·v2，commit 7787f55，用户提案）

v1 钉住后版本停在当前 commit，想升级得手动删缓存目录回落重下（上文「代价与更新方式」）——用户同日追加提案（思路即浏览器双通道更新）：**打开用旧版秒起，同时后台预下最新版，下次打开自然切到新版，旧版清理**。实现比「临时位置再移动」还省一步：serve-web 组件天然按 hash 下到缓存目录，预热进程下完 kill 即可，组件已留在缓存。

**实现（src/server.py，commit `7787f55`）**：命令构造扫缓存时记下 `_cached_now`（现有 hash 目录集合）→ fire-and-forget 起后台任务 `_webide_prefetch(cached_hashes)`：

1. **打开**：v1 同款——扫缓存选 mtime 最新且含 node.exe 的 hash 目录，`--commit-id <hash>` 秒起（不检查更新）；
2. **后台预热**：起一个**默认行为的 serve-web**（它自己拉最新 stable 下到缓存目录）→ 轮询缓存**出现新 hash 目录**（node.exe 就绪）→ kill 预热进程（组件已留在缓存）；
   - 预热端口 **39990–39996** 逐个试 bind 选可用的（高位，不撞工作台潜规则口 +30000 区段）；
   - 轮询 **15s 间隔 / 10 分钟上限**——慢网到点放弃（下次再试），**打开始终不受影响**（预热 fire-and-forget，不阻塞按钮）；
3. **清理**：保留**最近 2 个**版本（当前在用 + 新预热），更旧的删——顺手治掉「11 天 3 版 ≈ 2GB」的缓存累积老账（v1 手动升级路径就此淘汰）；
4. **下次打开**：扫缓存 mtime 最新 = 上次预热的**新版** → 秒起 → 又触发下一轮预热 → **无限滚动循环**。

**边界**：

- **首次无缓存**：不预热——走正常拉最新，下载完下次进循环；
- **版本节奏**：WebIDE 客户端永远比 stable 最新**晚一拍**（本次用上次预热的）——滚动更新的固有代价，换来打开永远秒起。

**验证（四步模拟，全过）**：① 本次启动用 old2（最新缓存）→ ② 预热完成检测 True（new1 落地）→ ③ 清理后缓存 `[old2, new1]`（old1 已删）→ ④ 下次启动用 new1（滚动切新版 ✓）。

## 响应 payload（_ide_payload）

```json
{"ok": true, "port": 39000, "ready": true}
```

- `port` **动态**：当前 WebUI 端口 + 30000（例：WebUI 9000 → 39000、8000 → 38000、9100 → 39100；Host 头无端口兜底 38000）；
- **不再携带 `folder_uri`**（2026-09-06 随 `--default-folder` 直开移除）：工作区由 serve-web 服务端直开，URL 零参数、payload 无需文件夹路径；
- 未就绪时附加 `hint` 字段（前端 toast 直接展示）。

## 前端：VS Code logo 按钮（src/static/index.html）

- 按钮（2026-09-06 图标换代）：**白底圆形按钮**（`background:#fff`）+ **VS Code logo 图片** `<img src="/icons/vscode.png" alt="VS Code" style="width:26px;height:26px;object-fit:contain">`（`id=btnIde`）——logo 已下载到 `src/static/icons/vscode.png`（170×170 透明底，经 `/icons/{name}` 静态端点服务）；`data-label="WebIDE (VS Code)"`（dock hover 标签）+ `aria-label` 完整语义（原 `title` 移除，见 [fab-dock](fab-dock.md)）。
- **2026-09-06 起收进右侧工具 dock**（与其它管理按钮一起折叠进 🧰，见 [fab-dock](fab-dock.md)）——控件栏不再直接摆放，展开 dock 后点击。
- `openWebIde()`：禁用按钮 + **`.loading` class**（`opacity:.55; cursor:progress`——图标已是 `<img>`，不再用 `textContent='⏳'` 覆盖，2026-09-06 修复）→ `fetch POST /api/ide/open` → 用 **`location.hostname`** 拼 `http://{host}:{port}/`（**零参数**——工作区由 serve-web `--default-folder` 直开；勿拼 `?folder=`，1.134 会误路由成远程代理会话）→ `window.open` 新页签 → toast（`ready` 成功附语言 workaround 提示 / 未就绪展示后端 `hint`）→ 恢复按钮。
- **fetch 12s 超时 + 乐观开页签（2026-09-06，用户实锤按钮卡沙漏）**：`AbortController` + `setTimeout(…, 12000)`——12s 超时或异常时**不再干等**：退回前端同款潜规则口 `http://{host}:{webui_port+30000}/` **乐观开页签** + toast「后台启动中，稍后刷新」；按钮最迟 12s 恢复。此前无超时随后端 150s 长轮询走 → 按钮 ⌛ 两分半、页签不弹。
- host 用 `location.hostname` 而非 `127.0.0.1`：手机/其它设备访问时 serve-web 监听 0.0.0.0 局域网可达。

## 与其他模块的关系

- 后端 `src/server.py`（端点 + `_ide_payload`）+ 前端 `src/static/index.html`（按钮 + `openWebIde()`）两处改动。
- `agent.services` 后台服务纳管——与 [background-scheduler](background-scheduler.md) 的后台服务管理同族（看板可见/可停止）。
- 无框架耦合：纯 HTTP 探测 + 子进程拉起。

## 注意事项

- 首次启动要下载 server 组件（一次性 ~1 分钟）；此后由 `--commit-id` **钉住缓存版本**——重启、VS Code 更新都不再重下（2026-09-17 前「缓存后秒开」只在 VS Code 版本未变时成立，几天一更新就重下全套 689MB，见下方专节）；12s 等待上限返回 `ready=false` **不是失败**——下载页自带自动刷新，页签开着等即可。
- **幽灵端口占位 / 孤儿进程教训（2026-09-06 用户实锤「点 IDE 变沙漏两分半不弹页签」）**：`code serve-web` 的启动器（code-tunnel）被杀后，其 spawn 的 node 子进程**继承 LISTEN socket 存活**成孤儿——`netstat` 显示 `39000 LISTENING owner=pid 21512`（该 pid 已不存在，`bind` 报 10048、连接报 10061）。旧端点 150s 长轮询 + 单 `_probe` 判定（body>500）→ 误判「无服务」→ 可能再起一个实例。治本三件：① `_agent.services.start` 绑**独立进程组**、stop 整树杀（杀启动器不漏子进程）；② 端口 **bind 实测 + 向后扫 +1..+5**（被占自动滚到可用口，不依赖人工清理）；③ 前端 fetch **12s 超时 + 乐观开页签**（不再干等）。清理命令参考：`netstat -ano | findstr 3900` 定位 owner 为已退 pid 的 LISTENING 后按 pid 清 node/cmd。
- **端口是潜规则（WebUI 端口 + 30000），不是 settings 可配**；命令固定 `code serve-web`（可选增强：命令可配、支持 code-server）。**端口可能偏移 +1..+5**（被幽灵占用时）——正常路径前端用后端返回的实际口；乐观开页签用了潜规则口而实际口偏移时，页签内刷新一下即落到实际口。
- **手机现状维持**（用户 2026-09-06 裁定「手机就忍了吧，能打开能看就不错了」）：serve-web 监听 0.0.0.0 局域网可达，但竖屏触屏体验受 VS Code Web 本身限制（VS Code 侧的事，不做额外适配）。
- 端口从 Host 头解析：本实例 9000 → WebIDE 39000；改 `agt-web --port` 后自动跟随，多实例互不抢口。
- **勿用 `?folder=` URL 参数（serve-web 1.134 的坑，2026-09-06 用户实锤「不受支持的断点图标 + 怪弹窗」）**：`?folder=` 会被误路由成「远程代理」会话——弹「选择要管理的远程代理」面板、标签页名变成 l10n 字面量（如「不受支持的断点的图标。」）、编辑区渲染出翻译串拼的假帮助文档、文件树图标显示异常。修复：服务端 `--default-folder` 直开工作区，URL 零参数；旧进程需 `/restart` 生效。
- **中文语言包 403 → l10n 字面量泄漏（同一个用户报告的深层诱因）**：vscode-language-pack-zh-hans 的 web 资源经 `vscode-unpkg.net` 代理 **403**（console 多个 403）时 l10n 机制崩坏，UI 泄漏翻译字面量（标签名/图标描述串）。治法：serve-web 内 `Ctrl+Shift+P → Configure Display Language → English` **一次即治**（serve-web 自己的 profile 记住，**不影响桌面版中文**）；serve-web 不支持 `--locale` 参数、`argv.json` 与桌面版共享不能动。若英文切换后文件图标仍是圆点，是 serve-web minimal 版未带文件图标主题（属预期，可装 Seti Icons 扩展美化）。

## 相关页面

- [background-scheduler](background-scheduler.md)（agent.services 服务纳管同族）
- [ops · /restart 看门狗](../guides/ops.md)（服务生命周期）
