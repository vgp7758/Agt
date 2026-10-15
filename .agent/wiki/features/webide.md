# WebIDE · VS Code logo 按钮 → serve-web 新页签打开工作区

## 职责

控件栏一个按钮把整个工作区用浏览器版 VS Code 打开（`code serve-web`，VS Code 1.134+ **自带**，非 code-server、零额外安装）——代码即看即改，手机/局域网设备也可用（serve-web 监听 0.0.0.0）。用户提案 2026-09-06「点一个按钮用 WebIDE 打开工作区」。

**2026-10-16 起实现形态为 webide 插件**（随包播种四例之一）：按钮与端点全在插件内，`src/server.py` 内置端点 + index.html 内置按钮已退役——见下文[插件化搬迁](#插件化搬迁按钮--端点全链路迁入-webide-插件2026-10-16用户提案播种示例扩散)章节；以下各节描述的端口潜规则 / `--commit-id` 钉缓存 / 滚动预热 / 12s 上限等逻辑**全部原样保留**在插件 backend.py，语义零变化。

## 插件化搬迁：按钮 + 端点全链路迁入 webide 插件（2026-10-16，用户提案「播种示例」扩散）

随「把实用功能作为插件 SDK 示例播种」提案扩散（2026-10-16，随包播种四例之一，见 [插件 SDK](../architecture/plugin-sdk.md)），WebIDE 成为第三个插件化收官的内置功能：

- **backend.py**：原 server.py 端点逻辑**原样照搬**——端口潜规则（WebUI+30000，Host 头解析）/ 复用优先（`_alive` 判定）/ 端口 bind 实测向后扫 +1..+5 / `--commit-id` 钉缓存 + 滚动预热（预热口 39990–39996、保留最近 2 版）/ `agent.services` 纳管独立进程组 / 12s 等待上限。插件路由替代 `POST /api/ide/open`
- **frontend.js**：`addButton` 挂 dock 席位，`icon: '<img src="/plugins-assets/webide/vscode.png">'`——**图标是插件本地资产**（9511B，随播种分发，不再放 `src/static/icons/`）；`onClick` 事件透传拿按钮引用维持 `.loading` class 加载态；12s 超时乐观退回潜规则页签逻辑随迁
- **内置退役**：server.py 端点 + `_ide_payload`、index.html `btnIde` + `openWebIde()` 全删
- 语义零变化：老用户无感，只是实现搬家；`/plugins disable webide` 可关

## 首个实战报错：too many values to unpack——裸 dict 返回漏包路由元组（2026-10-16，用户实锤）

> 搬迁后的**首个实战报错**（用户点 WebIDE 图标即触发）：`too many values to unpack (expected 2)`——「内置端点写法直接照搬进插件」的第二例风格错配（第一例是 422 注解坑，见 [插件 SDK · 插件路由 422 根治](../architecture/plugin-sdk.md)）。

**根因**：原 server.py 端点是 FastAPI 路由——**裸 `return dict` 合法**（FastAPI 自动包 JSONResponse）；而插件路由约定返回 **`(status, body)` 元组**（SDK 包装层 `_handler` 做 `status, body = fn(...)` 后自构响应）。搬迁时照搬了裸 dict 风格 → `_handler` 把 dict 当可迭代解包成**键流**（ok/port/ready/hint 共 4 个 > 2）→ `too many values to unpack`。

**修复（src/assets/plugins/webide/backend.py，两处）**：

- `_payload(ready, port)`：改 `return 200, {...}`（docstring 注明本次教训：插件路由约定 (status, body)，裸 dict 会炸解包）
- 端口全占分支：`return 200, {"ok": False, "error": ...}`

**SDK 治本（同轮，src/plugins.py `_handler`）**：返回值**宽容解析**——仅「首元素是 int 的二元组」才按 `(status, body)` 解；裸 dict/list/str 一律当 body（status=200）。后续任何插件作者再犯同款错不再炸。详见 [插件 SDK · 插件路由返回值宽容解析](../architecture/plugin-sdk.md)。

**生效**：插件侧改动 `/reload plugins` 热生效即修好（免重启）；SDK 宽容解析属引擎侧，随下次 `/restart` 生效。

## serve-web 缓存被删致「无限 panic」：三层根因 + 插件四修 + 救场脚本（2026-10-15，commit abcc03c）

> 用户实锤症状：WebIDE 页签无限刷 panic 日志 / `RecvError`，缓存目录 `~/.vscode/cli/serve-web/<commit>/` 被删成只剩 `node.exe + node_modules`。这是**插件搬迁之后、SDK 之上**的一起运行期事故——代码全在 `src/assets/plugins/webide/backend.py`（`_prefetch` / `_dir_ok` / `_drop_dir` / `_cli_exe` / `_probe`），与 SDK 层无关。

### 根因链（三层，逐层实测）

1. **孤儿进程层**：`_prefetch` 用 `subprocess.Popen(cmd, shell=True)` 起预热，结束时 `pre.kill()` —— 只杀掉 `cmd.exe` 本身，它拉起的 **`code-tunnel.exe` 变孤儿**（实测堆了 **6 个**，各自都在下载组件、抢占**同一个缓存目录**）。
2. **并发安装层**：多进程同时往同一 commit 目录里装/清组件 → 「保留最近 2 个版本」的清理 `rmtree` **删到一个正在使用中的版本** → 目录被删剩 `node.exe + node_modules`（`node.exe` 被占用删不掉，活下来当"残骸"）。
3. **死循环层**：下一次 CLI 扫缓存时把这个半残目录当成可用版本，拿它当 `--commit-id` → spawn `bin\..\out\server-main.js` 报 **"The system cannot find the path specified."** → 无限重试 → 用户看到的就是循环 panic + `RecvError`。

併发诱因（同轮用户实锤）：**Unity Hub 固定占 39000**（正是「WebUI + 30000」潜规则口），回 401 `Unauthorized access` —— 旧 `_probe` 把 4xx/5xx 一律当"别人的服务"，而 serve-web **自己在启动中**也回 503，于是误报"被 Unity Hub 占用"。

### 插件修复（`src/assets/plugins/webide/backend.py`，四处）

| 位置 | 旧行为 | 修复 |
|---|---|---|
| `_dir_ok(d)` | 「完整安装」只判 `node.exe` 存在 | 判据升级为 **`node.exe` 且 `out/server-main.js`** ——半残目录不再被当完整版本（预热完成检测、候选挑选、清理三处全用它） |
| `_drop_dir(d)` | 直接 `rmtree` 硬删 | 先 `rename` 成 `.名.deleting`（被占用即失败）再 rmtree，失败就跳过留给下一轮——**绝不硬删正在使用的版本**；`.` 前缀同时让残留不会被 `_dirs` 当候选 |
| `_prefetch` | 无锁 + `shell=True` + 固定口 39990–39996 | ① 模块级**单例锁 `_PREFETCHING`**（同时只允许一路预热，防并发互踩）；② `_cli_exe()` 取全路径**直起、不经 shell**（kill/taskkill 才杀得干净，孤儿根除）；③ 预热口改 **`--port 0`**（随机口，不再扫 39990+ 固定段）；④ 清理只保留最近 2 个**完整**版本 + 顺手清半残目录 |
| `_probe` 的 4xx/5xx 分支 | 一律判"不是我们的服务" | **读 body 判特征**：serve-web 启动中的 `503 "Error connecting to server: hyper::Error(IncompleteMessage)"` → `ours=True`（不再误报"被 Unity Hub 占用"）；Unity Hub 的 `401 Unauthorized access` 仍判别人的服务 → 走扫口逻辑 |

### 救场工具 `tools/_fix_serveweb_cache.py`（一次性、幂等）

事故当场缓存已毁，光改代码救不回——脚本直接把损坏目录补回完整版：

- 从微软 CDN 拉 `vscode-server-win32-x64-web.zip`（**213MB**），URL 形如
  `https://vscode.download.prss.microsoft.com/dbazure/download/stable/<commit>/vscode-server-win32-x64-web.zip`
- **剥顶层壳目录** `vscode-server-win32-x64-web/`（判定"namelist 顶层只有唯一目录前缀"才剥）→ **逐文件覆盖**解压到 `~/.vscode/cli/serve-web/<commit>/`；zip 已存在且 >200MB 则跳过下载（可重复跑）
- **踩过的坑（必看）**：首版只做"一层目录合并"，漏补 `node_modules/@vscode/*/index.js` → 启动报 `ERR_MODULE_NOT_FOUND` ——**必须逐文件覆盖**，不能只并目录

### 实测闭环（2026-10-15）

- `POST /api/webide/open` → `{"ok":true,"port":39000,"ready":true}`（**3.0s**）
- `GET :39000/` → **HTTP 200**（6304B 工作台页）

### 教训

- **杀进程要杀干净**：`shell=True` + `kill()` 在 Windows 上只杀 shell 壳，真正的 `.exe` 会变孤儿——凡"起进程并要能停掉"的地方（预热/后台任务）都要全路径直起 + 独立进程组（同一课在 [background-scheduler](background-scheduler.md) 的服务纳管里也出现过）。
- **"删缓存"必须先证明不是正在用的**：rename 探测占用是零成本、跨平台可用的"能不能删"判据；硬 rmtree 换来的是一条难查的 spawn 死循环。
- **"安装完成"判据要覆盖真正的入口文件**：只判一个占位符（`node.exe`）会被半残状态骗过。
- **判"是不是我们的服务"不能只看状态码**：自己的服务启动中也会 5xx，要读 body 特征（见上表 `_probe`）。

### 关联

- [插件 SDK](../architecture/plugin-sdk.md)：webide 插件（本事故代码所在，插件路由/资产/启停）
- [background-scheduler](background-scheduler.md)：后台服务纳管与进程组/孤儿同类教训

## 「Unauthorized access」撞口误复用：Unity Hub 固定占潜规则口 + _probe 三态判据（2026-10-15，用户实锤）

> 用户实锤（2026-10-15 第一轮）：点 WebIDE 图标，页签打开后显示 **`Unauthorized access`**（不是 VS Code 工作台）。与同日[缓存事故](#serve-web-缓存被删致无限-panic三层根因--插件四修--救场脚本2026-10-15commit-abcc03c)是**两个独立病灶**，共用同一天的一次 `_probe` 判据升级。

### 实测证据：撞口 + 误复用

| 检查 | 结果 |
|---|---|
| `netstat` 查 39000 | **Unity Hub.exe（pid 18808）** 在监听 |
| `curl :39000` | **HTTP 401** + body `Unauthorized access`（19 字节——与用户看到的文案逐字一致） |
| 端口来源 | WebUI 9000 → 潜规则口 9000+30000 = **39000** ← 与 Unity Hub 的**固定口正面冲突** |

**链路**：点图标 → 后端判定 39000「可用」→ 把 `port: 39000` 返给前端 → 浏览器打开 39000 → 实际是 Unity Hub 的 401 页面。

### 根因：复用判据只看「连得上」

旧代码 `if _alive(base_port): return _payload(_ready(base_port), base_port)`——`_alive` 语义仅为「口上能建立 HTTP 连接」，**不区分是不是我们的 serve-web**。Unity Hub 的 401 口同样「连得上」（192.x 上 401 在 urllib 侧还曾表现为断连）→ 直接复用别人的口。

### 修复三处（src/assets/plugins/webide/backend.py）

| 位置 | 旧 | 新 |
|---|---|---|
| `_probe(port)` | 返回布尔「活着吗」 | **三态 `(alive, ready, ours)`**——`ours` = body 含 serve-web 特征（`_SW_MARKS`：`visual studio code` / `serve-web` / `vscode` / `code-server`）；401/403/无特征页一律**不算 ours** |
| 复用分支（①） | `_alive(base_port)` 即复用 | `_a and _ours` 才复用 → 别家程序占口自动落到 ② 扫口（`_bindable` +1..+5） |
| 12s 超时兜底 | 未就绪一律返回 `ready=false` | 再探一次：`_a2 and not _ours2` → `{"ok": false, "error": "端口 X 上的服务不是 VS Code（被其他程序占用）——常见于 Unity Hub 固定占用 39000，请关闭该程序或更换 WebUI 端口后重试"}`——**不再把别人的页面丢给浏览器** |

`ready` 判据同时排除 body 含 `unauthorized`（401 页长 > 500 字符，不能被当就绪）。

### 验证（复现用户现场）

- (a) 用 401 诱饵页占 39020（Unity Hub 同款行为）→ 返回 `port=39021` ✅ **跳口、未复用**
- (b) 真 serve-web 在 39003 → 正常复用返回 39003 ✅

生效：插件侧 `/reload plugins`（免重启）。

### 教训

- **「口上有服务」≠「是我们的服务」**：复用判定必须带**身份特征**，否则会把别人的 401/404 页面当自己的工作台返给浏览器。
- **端口潜规则会撞固定口**：`WebUI + 30000` 是约定、不是保留段——撞上 Unity Hub（固定 39000）这类程序是迟早的事；探测到「不是自家的」要**自动跳口 + 明确报错**，不能静默复用，也不能静默空白。

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

- **2026-10-16 起实现收敛到 webide 插件**（backend.py 端点逻辑 + frontend.js 按钮一处承载，见上文「插件化搬迁」；原 server.py / index.html 两处内置实现已删）——插件路由/资产/启停见 [插件 SDK](../architecture/plugin-sdk.md)。
- `agent.services` 后台服务纳管保留在插件 backend.py 内——与 [background-scheduler](background-scheduler.md) 的后台服务管理同族（看板可见/可停止）。
- 无框架耦合：纯 HTTP 探测 + 子进程拉起。

## 注意事项

- 首次启动要下载 server 组件（一次性 ~1 分钟）；此后 **`--commit-id` 用缓存秒起 + 滚动预热自动跟进新版**（2026-09-17 v1 钉版本 + 同日 v2 滚动预热：打开永远秒起、旧版自动清理、版本比 stable 最新晚一拍跟进，见下方专节及「滚动预热」小节）；12s 等待上限返回 `ready=false` **不是失败**——下载页自带自动刷新，页签开着等即可。
- **幽灵端口占位 / 孤儿进程教训（2026-09-06 用户实锤「点 IDE 变沙漏两分半不弹页签」）**：`code serve-web` 的启动器（code-tunnel）被杀后，其 spawn 的 node 子进程**继承 LISTEN socket 存活**成孤儿——`netstat` 显示 `39000 LISTENING owner=pid 21512`（该 pid 已不存在，`bind` 报 10048、连接报 10061）。旧端点 150s 长轮询 + 单 `_probe` 判定（body>500）→ 误判「无服务」→ 可能再起一个实例。治本三件：① `_agent.services.start` 绑**独立进程组**、stop 整树杀（杀启动器不漏子进程）；② 端口 **bind 实测 + 向后扫 +1..+5**（被占自动滚到可用口，不依赖人工清理）；③ 前端 fetch **12s 超时 + 乐观开页签**（不再干等）。清理命令参考：`netstat -ano | findstr 3900` 定位 owner 为已退 pid 的 LISTENING 后按 pid 清 node/cmd。
- **缓存目录 `~/.vscode/cli/serve-web/<commit>/` 是共享资源，也是事故高发点**：2026-10-15 曾因孤儿 `code-tunnel.exe` 并发安装 + `rmtree` 删到**正在使用**的版本 → 目录只剩 `node.exe` → 之后 CLI spawn 报 `The system cannot find the path specified.` 无限重试（页签循环 panic）。排障第一动作：列该缓存目录，找「有 `node.exe` 却无 `out/server-main.js`」的半残目录（删除或跑 `tools/_fix_serveweb_cache.py` 补全），见上文[事故专节](#serve-web-缓存被删致无限-panic三层根因--插件四修--救场脚本2026-10-15commit-abcc03c)。
- **端口是潜规则（WebUI 端口 + 30000），不是 settings 可配**；命令固定 `code serve-web`（可选增强：命令可配、支持 code-server）。**端口可能偏移 +1..+5**（被幽灵占用时）——正常路径前端用后端返回的实际口；乐观开页签用了潜规则口而实际口偏移时，页签内刷新一下即落到实际口。**潜规则口也可能被别家程序固定占用**（实锤：Unity Hub 固定占 39000 → 回 401 `Unauthorized access`，用户点图标看到的就是这句）——`_probe` 三态判据下不复用别人的口、自动向后扫口；若拉起后口仍被抢，端点返回 `ok:false` + 明确报错，不再打开别人的页面（见上文「Unauthorized access」专节）。
- **手机现状维持**（用户 2026-09-06 裁定「手机就忍了吧，能打开能看就不错了」）：serve-web 监听 0.0.0.0 局域网可达，但竖屏触屏体验受 VS Code Web 本身限制（VS Code 侧的事，不做额外适配）。
- 端口从 Host 头解析：本实例 9000 → WebIDE 39000；改 `agt-web --port` 后自动跟随，多实例互不抢口。
- **勿用 `?folder=` URL 参数（serve-web 1.134 的坑，2026-09-06 用户实锤「不受支持的断点图标 + 怪弹窗」）**：`?folder=` 会被误路由成「远程代理」会话——弹「选择要管理的远程代理」面板、标签页名变成 l10n 字面量（如「不受支持的断点的图标。」）、编辑区渲染出翻译串拼的假帮助文档、文件树图标显示异常。修复：服务端 `--default-folder` 直开工作区，URL 零参数；旧进程需 `/restart` 生效。
- **中文语言包 403 → l10n 字面量泄漏（同一个用户报告的深层诱因）**：vscode-language-pack-zh-hans 的 web 资源经 `vscode-unpkg.net` 代理 **403**（console 多个 403）时 l10n 机制崩坏，UI 泄漏翻译字面量（标签名/图标描述串）。治法：serve-web 内 `Ctrl+Shift+P → Configure Display Language → English` **一次即治**（serve-web 自己的 profile 记住，**不影响桌面版中文**）；serve-web 不支持 `--locale` 参数、`argv.json` 与桌面版共享不能动。若英文切换后文件图标仍是圆点，是 serve-web minimal 版未带文件图标主题（属预期，可装 Seti Icons 扩展美化）。

## 相关页面

- [插件 SDK](../architecture/plugin-sdk.md)：随包播种四例之一 + 插件路由/本地静态资产（vscode.png）/ addButton onClick 事件透传的实战范例
- [fab-dock](fab-dock.md)：VS Code logo 按钮所在 dock（2026-10-16 起该按钮由本插件注入）
- [background-scheduler](background-scheduler.md)（agent.services 服务纳管同族）
- [ops · /restart 看门狗](../guides/ops.md)（服务生命周期）

