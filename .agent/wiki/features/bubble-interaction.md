# 气泡交互 · 展开折叠 + 气泡级复制 + answer 分页 + 行内资源渲染

> 前端两处：`static/editor.html`（工作流编辑器气泡面板，展开/折叠）、`static/index.html`（WebUI 聊天面板，气泡级复制 + answer 多 Agent 分页）。后端关联 `src/agent.py` 事件流 `_emit`（统一打 `agent_id` 标）。

## 职责

气泡交互目前有七个独立特性：

| 特性 | 前端文件 | 上线 |
|------|---------|------|
| **系统消息展开/折叠**：系统气泡默认折叠、用户气泡默认展开，点击切换 | `static/editor.html` | v0.18.2 |
| **气泡级复制按钮**：user/answer 气泡 hover 浮现「📋 复制」，一键复制整个气泡内容 | `static/index.html` | 2026-08-19，commit 3a7e9de |
| **answer 多 Agent 分页**：子 Agent 回应与主 answer 同轮时，气泡顶部小 tag 按钮翻页 | `static/index.html` + `src/agent.py` | 2026-08-21，commit ba0940b |
| **answer 行内富文本与资源渲染**：autolink 可点、`[!标题](路径)` 图框/音频框内嵌、**标准 markdown 图片 `![alt](路径)` 同链渲染**（2026-09-14，commit 6215ed1）、**无感叹号 `[文字](本地路径)` 渲染为 asset 链接**（2026-09-17，commit 4ad7742，四种引用形态齐）、**文本文件 → 点击开预览抽屉**（抽屉内 hlCode 语法高亮；后端 `/api/asset` 供文件）、**资产框 error 降级**：文件已被删 → 图/音/视频框 onerror → `.err` +「⚠️ 文件已不存在」（2026-09-18，commit deded59） | `static/index.html` + `src/server.py` | 2026-09-04，commits 4baa66a + fe44b5a + cb01d70 + 6215ed1 + 4ad7742 + deded59 |
| **bash 代码块执行按钮**：` ```bash ` 块下 ▶ Agent 执行 / 💻 终端执行双按钮；2026-09-17 起 Agent 执行改**逐条指令**（逐行去 shebang/行尾注释/纯注释/空行后逐条发 `/call run_shell` 串行按序），终端执行保持整块 | `static/index.html` | 按钮早期上线；逐条化 2026-09-17，commit 60f3c6d |
| **📎 本轮变更文件补充区**：answer 尾部自动补渲染「回答中未交代」的变更文件（快照 diff 直供）；modified/new 图片/音频/视频**直接内嵌渲染**（视频 2026-09-17 补）、文本/代码走预览抽屉，deleted 灰框只读；「已引用」剔除走路径归一化口径（`\`→`/` + basename 小写，2026-09-13 修复反斜杠/大小写漏判；2026-09-14 起同步认 `[!名]`、标准图片双语法，2026-09-17 起再认 `[文字](本地路径)` 链接——三语法 cited）；**容器限高 240px + 标题点击折叠**（2026-09-22，见[专节](#本轮变更文件容器限高--可折叠2026-09-22用户提案)） | `static/index.html` | 2026-09-04 引入；2026-09-06 图片/音频内嵌化；2026-09-13 路径归一化；2026-09-14 双语法 cited；2026-09-17 视频内嵌播放器 + 三语法 cited；2026-09-22 容器限高 + 折叠 |
| **轮收藏与收藏视角**：每轮 `.turn` 左上 ⭐ 收藏/取消（已收藏恒显/未收藏 hover 浮现）；控件栏「☆ 收藏」切换收藏视角只回看收藏轮（成对显隐 + 蓝字提示条）；`POST /api/favorite` 持久化 `extra_state.favorites` | `static/index.html` + `src/server.py` | 2026-09-22，commit fe1b2ac（v0.29.8 读侧预备先行） |

## 系统消息展开/折叠（editor.html）

气泡分两类，默认展开/折叠状态不同，支持点击切换：

| 气泡来源 | 默认状态 | 典型内容 |
|----------|---------|---------|
| **系统自动触发**（钩子注入、工具副作用通知、auto_diag 结果等） | **折叠** | 较长、辅助性信息，不打扰主对话流 |
| **用户指令**（用户消息、用户主动触发的工具输出） | **展开** | 核心对话内容，需即时可见 |

```
气泡渲染（editor.html）
  ├─ 系统气泡：collapsed=true（初始）→ 显示标题/摘要行
  │    └─ 点击气泡头部 → toggle expanded → 展开完整内容
  └─ 用户气泡：collapsed=false（初始）→ 完整可见
       └─ 点击气泡头部 → toggle collapsed → 折叠为摘要
```

- **点击区域**：气泡头部（标题行），带 `▶`/`▼` 方向指示符
- **折叠态**：仅显示标题 + 首行摘要（CSS `max-height` 截断 + 渐变遮罩）
- **展开态**：完整内容，长内容区可滚动
- **状态持久化**：当前会话内保持（刷新页面重置为默认态）

**设计意图**：系统消息（py_auto_diag 注入、async 钩子日志、wiki_auto_query inject 等）篇幅长但非当前关注焦点 → 默认折叠降噪；用户气泡是对话主线 → 默认展开。点击切换让用户按需深入，不强制滚动跳过。

## 系统气泡 markdown 渲染（index.html，2026-08-31，commit fdfc28a）

用户报告「/context 紫色系统信息气泡中的表格渲染还是纯文本」——WebUI 聊天面板的系统气泡（`index.html`，与 editor.html 的编辑器面板是两套实现）此前从未接上 markdown 管线。

**根因**：`case 'system'` → `addRow('sys', text)` 用 `textContent` 纯文本渲染；markdown 渲染（`renderAnswer` 的表格 `tryTable` / 代码块 / 段落）只服务 answer 气泡。上下文引擎侧早把 /context 段落表输出成 markdown 表格（[context-engine · /context 展示侧两修复](../architecture/context-engine.md#context-展示侧两修复scene-精确匹配失配--markdown-段落表2026-08-31commit-3ae7a76)），但系统气泡渲染成纯文本 → 用户看到的还是 `|` 分隔的裸表格——后端输出与前端渲染两段没打通。

**修复两处**（`src/static/index.html`）：

| 位置 | 改动 |
|---|---|
| `addRow` sys 分支 | `b.innerHTML = renderAnswer(text)`——表格/代码块正常渲染，文本仍经 `esc()` 转义（与 answer 同安全模型）；auto 折叠态点击展开时同样走 markdown（`b.innerHTML = renderAnswer(b.dataset.full)`） |
| `renderNotifyBubble`（后台通知气泡） | 展开态同款：`b.innerHTML = renderAnswer(b.dataset.full)` |

**效果**：/context 段落构成表在系统气泡里渲染成真表格（`.bubble table` 边框样式早已存在，此前无消费方）；通知气泡（user 语义标签体系，见 [用户交互 · user 消息语义标签](user-interaction.md#user-消息语义标签--后台通知轮-vs-用户轮2026-08-30用户提案批首归属-commit-803b3a5)）展开同样受益。Ctrl+F5 刷新即生效，纯前端改动无需 /restart。

**配套关系**：/context 展示侧输出 markdown 表格（3ae7a76，引擎侧）+ 系统气泡渲染 markdown（fdfc28a，前端侧）——两段合起来才让「段落构成表」真正以表格呈现；若只改后端不接前端，看到的仍是纯文本（本 bug 即此断点）。

## answer 气泡行内富文本与资源渲染（2026-09-04，用户提案，commit 4baa66a）

用户提案：agent 回答时在 answer 气泡中解析渲染 markdown 引用——链接可点、图片成框、音频成播放控件。此前 answer 的行内文本只有 `` `code` `` 高亮（`inlineCode`），其余全裸文本；这是 answer 气泡从「纯文本+表格+代码块」走向多模态呈现的一步。

### 三种语法 → 三种渲染

用户提案：agent 回答时在 answer 气泡中解析渲染 markdown 引用——链接可点、图片成框、音频成播放控件。此前 answer 的行内文本只有 `` `code` `` 高亮（`inlineCode`），其余全裸文本；这是 answer 气泡从「纯文本+表格+代码块」走向多模态呈现的一步。（标题的「三种」为初版定数，此后逐轮扩充——下表为现行全量。）

| 气泡里写 | 渲染成 |
|---|---|
| `<https://xxx.xxx.cn/>` | autolink 蓝色链接（`a.md-link`，新标签打开） |
| `[!架构示意图](assets/images/arch.png)` | **图框** `.asset-box`：标题 `.asset-cap` + 内嵌 `<img>`（外层 `<a target=_blank>` 点击看原图；加载失败降级为链接） |
| `[!主题曲](assets/audios/theme.wav)` | **音频框**：标题 + `<audio controls>` 播放器 |
| `[!演示视频](assets/videos/demo.mp4)` | **视频框**（2026-09-17）：内嵌 `<video controls>` 播放器 + 右上角 ↗ 新页签看原片（见[专节](#视频资产内嵌播放器渲染2026-09-17用户问诊)） |
| `[!设计文档](docs/gdd.md)` | **文本资产框**（2026-09-04 · 二，commit fe44b5a）：📄 标题框可点击 → 打开[文件预览抽屉](#文本文件--预览抽屉2026-09-04--二commit-fe44b5a用户提案)；覆盖 40 种文本扩展名（见下节） |
| `[文字](https://...)` | 普通外链（顺带支持的标准 markdown 语法） |
| `[文字](本地路径)` | **本地路径链接**（2026-09-17，commit 4ad7742）：href 走 `/api/asset`，新页签由浏览器按 Content-Type 直接播/显示——与带 `!` 姿态构成「嵌入 vs 链接」互补，见[专节](#文字本地路径-无感叹号链接渲染为-asset-链接2026-09-17用户问诊commit-4ad7742) |
| `` `![alt](相对路径)` `` | **标准 markdown 图片**（2026-09-14，commit 6215ed1）：本地路径**复用 `assetBoxHtml`**，与 `[!名](路径)` 同一条资产链（图框/音频/文本预览/未知后缀嗅探同款分流，alt 作 caption）；http(s)/ftp 外链直连 `<img>`——见[专节](#标准-markdown-图片语法渲染支持2026-09-14用户问诊commit-6215ed1) |
| `` `https://a.b` `` | code span **URL 整串 → 链接**（`a.md-link` 包 code，新标签打开） |
| `` `pip install -U agt-agent` `` | code span 非 URL → **可点击追加**：点一下把内容追加到消息输入框末尾并聚焦（2026-09-04 · 三，commit fd3d465，见[下节](#code-span-可点击化url-链接--点击追加输入框2026-09-04--三commit-fd3d465用户提案)） |
| `` `<https://a.b>` `` | code span 优先保护——内部 autolink / `[!…]` 资源语法不解析（防误伤）；仅按「URL 整串 / 其余」二分决定链接态还是可点击追加态 |

> 资产引用按扩展名分流在 `assetBoxHtml(title, path)`：初版（4baa66a）图 / 音 / 其它→普通链接三分支；二阶段（fe44b5a）把文本扩展名单列成**可点击资产框**（初版里文本文件只会渲染成普通链接）。标准 markdown 图片语法（6215ed1）不新建渲染器——本地路径直接投喂同一条 `assetBoxHtml` 资产链。无感叹号的 `[文字](本地路径)`（4ad7742）**不进资产链**——语义是链接（跳转），非嵌入（见[专节](#文字本地路径-无感叹号链接渲染为-asset-链接2026-09-17用户问诊commit-4ad7742)）。

### 前端管线：inlineRich + \x02 占位符（src/static/index.html）

- `inlineRich(t)` 替代原 `inlineCode(esc(...))`——段落 `flush()` 与表格 `cell()` 两个消费点全部换上
- **占位符保护**：先用 `\x02N\x02` 把特殊片段摘进 hold 数组，全文 esc 后统一还原。否则两个病：esc 把 `<` `>` 转义成实体后正则匹配不到 autolink/资源引用；已生成的 HTML 会被二次转义显示成源码
- 处理顺序：① code span 优先（内容 **esc——LLM 输出不可信**，初版漏了 esc，验证轮补上；2026-09-04 · 三起按 `_URL_RE` 分流：URL 整串 → `codeUrlHtml` 链接 / 其余 → `_codeSpanHtml` 可点击追加）→ ② autolink → ③ `[!标题](相对路径)` 资源引用 → ④ 普通外链
- `assetBoxHtml(title, path)` 按扩展名分流：图（png/jpg/jpeg/gif/webp/svg/bmp/avif）→ 图框；音（wav/mp3/ogg/oga/m4a/aac/flac/opus）→ 音频框；**视频（mp4/webm/mov/mkv/avi/m4v/flv/ts/3gp/wmv/mpg/mpeg）→ 视频框（2026-09-17）**；40 种文本扩展 → 📄 可点击资产框（·二）；其它扩展 → 普通链接。src 统一 `/api/asset?path=<encodeURIComponent(path)>`
- **code span 三件套单源（·三，commit fd3d465）**：`_URL_RE` / `codeUrlHtml` / `_codeSpanHtml` 由 `inlineRich` 与独立 `inlineCode`（普通外链文本等非 rich 场景）双管线共享——URL→链接与点击追加两种行为全场景一致

### 后端：GET /api/asset（src/server.py）

workspace 内资产文件服务——图框/音频控件的 src 都指这里：

- **安全约束**：路径按相对 workspace 根解析；拒绝绝对路径 / `/`、`\` 开头 / 含 `..` 段；`(base/path).resolve()` 后 `relative_to(base)` 不在 workspace 内 → 404（防路径穿越 / 任意文件泄露）
- media_type 按 `mimetypes` 扩展名判定；不存在 / 越界统一 404

**意义**：Agent 把产物（示意图 / 生成音频 / 截图等）写进 workspace 后，即可在回答里用 `[!标题](相对路径)` 引用——多模态产出有了呈现通道（回答文字 + 内嵌资产一体交付）。

### 验证链（全绿）

- node 语法 + 六场景纯逻辑（图 / 音 / 链 / 代码保护 / XSS 转义）
- 后端 uvicorn 冒烟（9392 临时实例）：正常 200 + 穿越 `../../` 404 + 缺失 404 + 绝对路径 404 + png mimetype 正确
- playwright 真页面：三种语法 DOM 齐备；真实 png 全链路 `img.onload naturalWidth=128` ✅

⚠️ `server.py` 新路由 + 静态页改动——需 `/restart` 生效（与纯前端的[系统气泡 markdown 渲染](#系统气泡-markdown-渲染indexhtml2026-08-31commit-fdfc28a)不同，那个 Ctrl+F5 即可）。

### 文本文件 → 预览抽屉（2026-09-04 · 二，commit fe44b5a，用户提案）

用户提案（同日二阶段，commit fe44b5a）：除了图片/音频，**一般文本文件也应可引用**——点击气泡里的文本资产框，直接在抽屉里读内容，不用去文件系统翻。

**渲染扩展**（`assetBoxHtml`，src/static/index.html）：扩展名分流从三分支变四路——

| 扩展名 | 渲染 |
|---|---|
| png/jpg/jpeg/gif/webp/svg/bmp/avif | 图框内嵌 |
| wav/mp3/ogg/oga/m4a/aac/flac/opus | 音频控件 |
| **40 种文本扩展**（txt/md/log/json/py/js/ts/tsx/html/css/scss/xml/yaml/yml/toml/ini/cfg/csv/sh/bat/ps1/c/cpp/java/go/rs/rb/php/lua/vue/svelte/sql/gradle/dockerfile/…） | **📄 文本资产框**（cursor:pointer，点击 `openFilePreview(path)`） |
| 其它 | 普通链接（初版行为） |

**抽屉形态**（用户指定）：右侧 40% 固定抽屉——

```
┌─ 📄 docs/gdd.md ────────────────────── ✕ ─┐  ← 顶栏固定：文件路径 + 关闭（不随内容滚动）
│                                            │
│   （hlCode 语法高亮渲染 · 滚动区）             │
│                                            │
└────────────────────────────────────────────┘
```

- **无 fab 图标按钮**——与 spec/🐞日志/团队/后台四个抽屉不同，它**纯由气泡 markdown 资产点击触发**（低频入口不占图标位）
- **渲染演进**：初版 `textContent` 纯文本（防 HTML 注入，与 answer 转义同安全模型）→ 同日·四升级为 [hlCode 轻量语法高亮](#预览抽屉轻量语法高亮组合交替正则单遍扫描2026-09-04--四commit-cb01d70用户提问)（token 全 esc 后拼 span，安全模型等价）；>300k 字符截断提示 + >200k 降级纯文本双护栏（防超大文件卡渲染）
- 文件内容经既有 `GET /api/asset`（workspace 沙箱）拉取——**后端零改动**，纯前端
- **双向互斥**：打开它时关掉 spec/🐞/团队/后台四抽屉（drawer-push 40% 只让一份）；反向四个抽屉打开时也 `closeFilePreview()`；✕ / ESC 关闭，竖屏全屏

**验证（playwright 真页面全链路）**：写 demo md → 回答里引用它 → 📄 资产框出现 → 点击 → 抽屉打开、标题=路径、内容经 /api/asset 加载显示 → ✕/ESC 均可关 → drawer-push 正确清除 → 与其它抽屉互斥正常。

⚠️ 生效：本节为纯前端改动，Ctrl+F5 即生效（/api/asset 路由 4baa66a 已带）；同 commit 的[回答风格提示](../architecture/context-engine.md#回答风格提示system-尾部写死追加2026-09-04用户提案commit-fe44b5a)属装配层，需 `/restart`。

### code span 可点击化：URL 链接 + 点击追加输入框（2026-09-04 · 三，commit fd3d465，用户提案）

用户提案：回答里反引号包裹的文本（命令 / 文件名 / 参数）常需复用到下一条消息——**点一下直接追加到消息输入框末尾并聚焦**，免手抄（与上一条「发送即退表单」同 commit）。

**code span 分流**（`_URL_RE` + `codeUrlHtml` + `_codeSpanHtml` 三件套，src/static/index.html）：

| code span 内容 | 渲染 | 点击行为 |
|---|---|---|
| URL 整串（http/https/ftp，`_URL_RE`） | `<a class="md-link" target=_blank>` 包 `<code>`（链接态） | 新标签打开 |
| 其余（命令 / 文件名 / 参数…） | `<code class="code-append" data-v="…" title="点击追加到输入框">` | **追加输入框末尾 + 聚焦** |

- **追加语义**：输入框已有内容 → 先去尾白再**空格分隔**追加（不打断已写的话）；为空 → 直接填入。追加后 `focus()` + 派发 `input` 事件（触发 textarea 自适应高度与发送按钮态）+ toast「已追加到输入框」
- **事件委托**：document 级监听 `closest('code.code-append')`——流式渲染 / 历史渲染 / 子 Agent 分页里的 code span 一处全覆盖，无需逐个绑
- **esc 往返**：`data-v` 存**已 esc 原文**（属性注入安全），点击时 `dataset.v` 由 innerHTML 读取自动解码回原文——安全与正确性兼得（「LLM 输出不可信须转义」纪律不变）
- **双管线收敛**：URL→链接是同日早前迭代先行（inlineRich 里）；本轮 `inlineCode`（独立路径：外链文本等非 rich 场景）也改走同一三件套——两种 code span 行为全场景单源一致
- **可交互暗示**：CSS `cursor:copy` + hover 高亮（紫底 `#e0e7ff` / 深紫字 `#3730a3`）——与普通 code 的静态灰底区分开

**验证（playwright 真页面全绿）**：两个 code span 渲染 + `data-v` 原文正确；URL code 仍是链接；点击后输入框 = 前文 + 空格 + 追加内容且聚焦 ✓；[工具表单发送即退](tool-form.md#发送即退表单模式2026-09-04commit-fd3d465用户提案)同轮验证。纯前端改动，Ctrl+F5 生效。

### 预览抽屉轻量语法高亮：组合交替正则单遍扫描（2026-09-04 · 四，commit cb01d70，用户提问）

用户提问：抽屉里纯文本渲染读代码不爽——能不能给关键字/注释/标签对着色？并担心「是不是要手写状态机、逻辑会不会重」。

**裁决：不需要手写状态机**——正则引擎本身就是状态机。把全部 token 类型拼成**一个组合交替正则**（分支顺序：docstring→注释→字符串→数字→关键字→标签），一次 `exec` 循环单遍扫完（O(n)）；最左优先 + 分支顺序天然保优先级（字符串先于其内部的 `#` 被整体吃掉，不会截断错着色）。~80 行零依赖，commit `cb01d70`。

**实现**（`_HL_LANGS` 惰性初始化 + `hlCode(txt, ext)`，src/static/index.html；`openFilePreview` 渲染从 `body.textContent=txt` 改为 `body.innerHTML=hlCode(txt, ext)`）：

| token 类 | CSS 类 / 色（VS Code Light） | 覆盖 |
|---|---|---|
| 注释 | `.hl-c` `#6a9955` | `#`（py/sh/yaml…）、`//`+`/* */`（js/ts/cs/java/c/go…）、`--`（sql）、`<!-- -->`（html/xml） |
| 字符串 | `.hl-s` `#a31515` | `'…'`/`"…"`/`` `…` `` + 三引号 docstring **整体着色** |
| 数字 | `.hl-n` `#098658` | 十进制 / 小数 / `0x` 十六进制 |
| 关键字 | `.hl-k` `#0033b3` | 按语言族分表（C 系 91 词 / SQL 43 词） |
| 标签 | `.hl-t` `#0e7490`（青；·五 由 `#800000` 调整，见[下节](#语法高亮配色调整--语言分组规则澄清2026-09-04--五commit-96873e8用户观察)） | `<tag` / `>` / 自闭合对（html/xml/vue/svg） |

- **安全模型不变**：token 与普通文本全部 esc 后再拼 span——innerHTML 的注入面与原 textContent 等价（「文件内容不可信须转义」纪律不变，见[预览抽屉安全模型](#文本文件--预览抽屉2026-09-04--二commit-fe44b5a用户提案)）
- **双护栏**：>200k 字符降级纯文本；>300k 截断提示（fe44b5a 原有）；md/txt/log 无 token 本就纯文本不受影响
- json/yaml 配置文件：键值字符串 + 数字着色
- **按后缀选规则**：`_HL_LANGS` 按扩展名分组，每组只拼自己的正则分支——详见[下节](#语法高亮配色调整--语言分组规则澄清2026-09-04--五commit-96873e8用户观察)五组表

**调试实锤两 bug（都修）**：
1. `openFilePreview` 里 `ext` 未定义——变量原本只在 `assetBoxHtml` 里提取，预览入口拿不到；改从 path 现场提取扩展名
2. **alternation 分支不包捕获组 → `m[i]` 全 undefined**：token 明明命中但组判定全空、cls 全落默认色（playwright 截图肉眼发现）；修复 = 每分支整体包一层 `(...)`，组号 = 分支顺序 = 左括号顺序（`groups[i]` 同步登记组号→类映射，不依赖固定序号）

**验证（playwright 真页面）**：py 样本 24 关键字 / 8 字符串（docstring 整体 ✓）/ 9 数字；html 8 标签 + 注释；json 键值；js 行/块注释——全绿后清理测试残留（`_hl_demo.py`）。实测 140k 字符高亮 30ms。纯前端改动，Ctrl+F5 生效。

### 语法高亮配色调整 + 语言分组规则澄清（2026-09-04 · 五，commit 96873e8，用户观察）

用户观察：窗口里标签色和字符串色看起来很接近——属实：`.hl-t` 原 `#800000`（褐红）与 `.hl-s` 字符串 `#a31515`（暗红）同属暗红系，同屏挤在一起难分辨。用户提议标签改冷色，采纳（commit `96873e8`）。

**配色调整**：`.hl-t` → **青色 `#0e7490`**。playwright 计算样式实测四色分离：

| token 类 | 计算样式 | 色系 |
|---|---|---|
| 标签 `.hl-t` | `rgb(14,116,144)` 青 | 冷 |
| 字符串 `.hl-s` | `rgb(163,21,21)` 暗红 | 暖 |
| 注释 `.hl-c` | `rgb(106,153,85)` 绿 | 冷 |
| 关键字 `.hl-k` | `rgb(0,51,179)` 蓝 | 冷 |

**语言分组规则澄清**（用户问「是根据文件后缀应用不同的 match 规则处理的是吗？」——是）：`_HL_LANGS` 按扩展名分 **5 组语言配置**，每组独立 `{line 行注释风格, blockC 块注释, tag 标签支持, kw 关键字表}`，构建组合交替正则时**只拼该组的分支**——同一套 `hlCode`，规则随扩展名变化：

| 组 | 后缀 | 注释 | 关键字表 |
|---|---|---|---|
| `#` 系 | py, sh, yaml, toml, ini, rb, gitignore, dockerfile… | `#` | C 系 91 词（含 py 的 def/lambda/self…） |
| `//` 系 | js, ts, cs, java, c, cpp, go, rs, swift, php, css, json… | `//` + `/* */` | C 系 91 词 |
| `--` 系 | sql | `--` + `/* */` | SQL 43 词（select/join/having…） |
| 标签系 | html, xml, vue, svelte, svg | `<!-- -->` | 无关键字，但启用标签分支 |
| 无高亮 | md, txt, log, csv | — | 纯文本（无 token 分支） |

**跨语言不误伤**：同一个 `#` 符号，py 文件里高亮成注释、js 文件里（不在该组规则中）不匹配——js 组行注释是 `//`，所以 C 的 `#include` 不会被 js 规则误吃；`.gitignore`/`env` 这类配置文件只有注释着色也够用。

**验证**：临时写 `_hl_colors.html`（标签/字符串/注释/关键字四类样本同屏）→ playwright 截图 + 计算样式核对四色 → 验证后清理临时文件（`_hl_colors.html` + `_hl_colors.png`）。纯前端改动，Ctrl+F5 生效。

### 📎 本轮变更文件补充区：图片/音频直接内嵌渲染（2026-09-06，用户提案）

**背景**：answer 尾部「📎 本轮变更文件」补充区（`unmentionedChangesHtml`，2026-09-04 用户提案「快照 diff 补渲染」引入）此前对 modified/new 的**所有文件一律渲染成文本资产框**（点击开预览抽屉）——图片/音频也按文本逻辑显示。用户报告：变更文件列表里的 `src/static/icons/vscode.png` 没有直接渲染图片，只是一行文本。

**修复**（`unmentionedChangesHtml`，src/static/index.html）：对 modified/new 按扩展名分流，**复用 `assetBoxHtml`**（就是 `[!名](路径)` 引用同款渲染逻辑）：

| 变更文件类型 | 之前 | 现在 |
|---|---|---|
| 图片（png/jpg/jpeg/gif/webp/svg/bmp/avif） | 文本框（点击开预览抽屉） | **直接内嵌图框**，caption 带 ✏️/➕ 标记，点击看原图 |
| 音频（wav/mp3/ogg/oga/m4a/aac/flac/opus） | 文本框 | **audio 播放条** 🔊（同一分支天然覆盖） |
| 视频（mp4/webm/mov/mkv/avi/m4v/flv/ts/3gp/wmv/mpg/mpeg） | 文本框 | **内嵌 video 播放器** 🎬（2026-09-17 补——此前落 else 走文本预览抽屉，8000 实测，见[专节](#视频资产内嵌播放器渲染2026-09-17用户问诊)） |
| 文本/代码类 | 点击预览抽屉 | 不变 ✅ |
| deleted | 灰框只读 | 不变 ✅ |

- **caption 带变更标记**：`assetBoxHtml(ic+' '+f, f)`——icon（✏️ modified / ➕ new）拼进标题，与文本资产框视觉一致
- **实时渲染与历史读档走同一函数**（`renderAnswerPages` 调 `unmentionedChangesHtml`）——一处改动两处生效
- 差集逻辑不变：answer 已用 `[!名](路径)` 引用过的不再重复列出（全等或 basename 相等），差集空 → 零噪声

**验证（playwright 实测 + 浏览器内单测）**：
- 读档实测（上一轮变更列表）：`vscode.png` → 图框，`<img>` loaded=true、naturalWidth=170；`index.html` → 仍文本框 ✅
- 四场景单测（evaluate 直接调 `unmentionedChangesHtml`）：`mp3_is_audio_player` / `png_is_img` / `py_is_text_preview` / `wav_deleted_grey` 全 true

纯前端改动，Ctrl+F5 即生效（无后端路由变更）。

### 变更文件补充区路径归一化：反斜杠/大小写引用漏判修复（2026-09-13，用户问诊，commit b1ef5e4）

**用户问诊（2026-09-13）**：answer 里引用变更文件的形态有四种——完整路径 / 相对路径 × `/` 或 `\` 分隔。「📎 本轮变更文件」补充区的「已引用剔除」逻辑到底覆盖哪几种？**答案：原版只覆盖 `/` 分隔的两种，`\` 分隔的两种漏判**（明明引用了仍被重复补充）。

**原版覆盖矩阵**（剔除判定两层：引用路径全等 + basename 兜底比对）：

| answer 引用形态 | 全等 | basename 兜底 | 结果 |
|---|---|---|---|
| `src/x/y.py`（相对，`/`） | ✅ 与 changed file 全等 | — | 剔除 ✓ |
| `D:/proj/src/x/y.py`（绝对，`/`） | ❌（前缀多） | ✅ `split('/')` 切出 `y.py` | 剔除 ✓ |
| `src\x\y.py`（相对，`\`） | ❌ | ❌ **`split('/')` 切不开反斜杠**——pop 回来还是整串 `src\x\y.py`，与 `y.py` 不等 | **误判「未交代」→ 重复补充** ✗ |
| `D:\proj\src\x\y.py`（绝对，`\`） | ❌ | ❌ 同上 | 同上 ✗ |

**根因一句话**：basename 切分只认 `/`——纯 `\` 路径切不出文件名，两层判定全 miss。

**修复**（`unmentionedChangesHtml`，src/static/index.html，commit `b1ef5e4`）：比对前先归一化——

```javascript
const _norm = s => String(s||'').replace(/\\/g, '/');       // \ → / 统一
const _base = s => _norm(s).split('/').pop().toLowerCase(); // 两种分隔符都切 + 大小写不敏感
```

全等与 basename 判定都走归一化后比对；顺带覆盖**大小写变体**（模型写 `Src/X/Y.PY` 引用 `src/x/y.py`——Windows 大小写不敏感，同样判「已引用」剔除）。

**验证（node 7/7）**：相对`/` / 绝对`/` / 相对`\` / 绝对`\` / 大小写变体 → 全部正确判定为「已引用」；未引用 / 引用其它文件 → 正确判定为「需补充」。纯前端改动，Ctrl+F5 生效。

### 未知后缀引用按内容嗅探渲染：/api/file-kind + 编码感知解码（2026-09-09，用户提案）

**用户提案**：answer 中引用的文件**后缀未识别**（无扩展名 / 冷门扩展 / 伪装后缀）时，先探测文件内容与编码，再决定渲染方式——文本（utf/gbk/ascii 等）点击渲染在文本抽屉里；图片渲染为图片；音频渲染为播放条；视频或其它二进制以文件完整路径从浏览器新页签打开（由浏览器按嗅探出的 Content-Type 渲染，而非触发下载）。

**后端（src/server.py）**：

- `_sniff_kind(header)` —— 读文件头 magic bytes 的嗅探矩阵（输入=头 4KB）：

| 判定 kind | 识别依据（magic bytes） |
|---|---|
| `image` | `\x89PNG` / `\xff\xd8\xff`(jpg) / `GIF8` / `BM`(bmp) / `RIFF..WEBP` |
| `audio` | `ID3`、mp3 帧头 / `RIFF....WAVE` / `OggS` / `fLaC` |
| `video` | `ftyp`(mp4/mov) / `\x1aE\xdf\xa3`(mkv/webm) / `RIFF....AVI` |
| `text` | BOM → `utf-8-sig` / `utf-16`；utf-8 试解码 → gbk 试解码；NUL 或控制字符 >10% → `binary` |

- **`GET /api/file-kind?path=`**（新端点）：workspace 沙箱取文件头 → `_sniff_kind` → `{kind, encoding, media_type}`（与 /api/asset 同款路径穿越防护）
- **`GET /api/asset` 增强**：后缀未识别或只猜出 `application/octet-stream` 时读文件头嗅探修正 Content-Type——`.bin` 后缀的 mp4 也能被浏览器直接渲染而非触发下载（端到端实测）

**前端（src/static/index.html）**：

- `probeUnknownAssets()`：渲染后扫描 `.asset-box[data-probe]`（扩展名表全 miss 的引用占位框），逐个异步 `GET /api/file-kind`，按结果**原位升级重建**为对应形态：`text` → 📄 可点击资产框（开预览抽屉）；`image` → 图框内嵌；`audio` → 播放条；`video`/`binary` → 新页签链接（`/api/asset` 的 Content-Type 已被后端修正，浏览器直接渲染）
- **探测缓存 `_assetKindCache[path] = {kind, encoding, media_type}`**：重绘（多 Agent 分页切换 / 读档 / 展开更早轮次）同步命中不再闪占位框——`renderAnswerPages` / `renderHistory` / `prependHistory` 三处渲染入口统一调 `probeUnknownAssets()`
- **编码感知解码**：`openFilePreview` 从 `r.text()`（恒 utf-8）改为 `arrayBuffer()` + `new TextDecoder(嗅探的 encoding)`——gbk / utf-16 文本文件在预览抽屉里正确显示不乱码（已知文本后缀默认 utf-8，未知后缀用 /api/file-kind 嗅探的 encoding）

**验证**：`test/test_file_kind.py`（新建）——`_sniff_kind` 矩阵 14 例全过 + 真实文件端到端（无扩展名 jpg / gbk `.dat` / `.bin` 内 mp4 / `.weird` ascii）+ 路径穿越拒绝 + node 两阶段行为模拟（占位 → 缓存命中同步渲染四形态）全绿。

**生效方式**：后端新端点 + 前端——需 `/restart`。

### 标准 markdown 图片语法渲染支持（2026-09-14，用户问诊，commit 6215ed1）

**用户问诊（2026-09-14）**：markdown 里的文件引用是不是还有 `![](comfy_out/edit_api_test/stormstreet_red_umbrella.png)` 这种写法？——**答案：此前不认**。`inlineRich` 管线只有 `[!名](path)` 自定义资源语法，标准 markdown 图片 `![alt](path)` 无规则命中 → 整段经 esc 后原样显示为字面文本（commit `6215ed1` 补上）。

**两大高频来源**：

1. **zai_file_parser 图片转 Markdown 的输出格式**——vision 解析产出的结构化 md 里图示就是 `![](images/xxx-image.png)` 引用（见 [zai-tools · 图片转 Markdown](zai-tools.md#zai_file_parser-27-种类型白名单--别名归一--图片转-markdown2026-09用户请求)），Agent 摘引该产物时原样进 answer；
2. **LLM 天然惯用写法**——训练语料里标准图片语法是主流（comfy 生图产物引用是典型场景），模型自发输出概率高。

**渲染规则**（`inlineRich` 管线 ③ `[!…]` 资源引用之后新增一条，src/static/index.html）：

| 写法 | 渲染 |
|---|---|
| `![alt](本地相对路径)` | **复用 `assetBoxHtml(alt, path)`**——与 `[!名](path)` 完全同一条资产链：图框 / 音频播放条 / 📄 文本预览抽屉 / 未知后缀嗅探（`/api/file-kind`）全同款分流，alt 作 caption |
| `![alt](https://… / ftp://…)` | **外链直连 `<img src>`**（src/alt 均 esc；`referrerpolicy="no-referrer"`） |

- 正则 `!\[([^\]]*)\]\(([^)\s]+)\)`——与 `[!…]` 同款形态（path 不含空白/右括号）；
- **插入位置在 ③ 资源引用之后**：自定义 `[!名](path)` 优先命中，标准图片语法兜底——两者不互抢。

**cited 判定同步（变更文件补充区误补防御）**：「📎 本轮变更文件」的「已引用剔除」集合（归一化口径见[路径归一化修复](#变更文件补充区路径归一化反斜杠大小写引用漏判修复2026-09-13用户问诊commit-b1ef5e4)）此前只收集 `[!名](path)` 形态——answer 用标准图片语法引用过的文件仍会被误判「未交代」而重复补充。现 `_collect` 回调同时喂两种语法的正则，`cited` 收 `_norm`（`\`→`/`）+ `_base`（basename 小写）双份——与本页 2026-09-13 归一化修复同层防御。

**生效方式**：纯前端，Ctrl+F5。验证方式：刷新后取一张真实产物图（如 comfy 的 `stormstreet_red_umbrella.png`）让 Agent 以 `![](路径)` 形态引用，应直接出图框而非字面文本。

### 🎬 视频资产内嵌播放器渲染（2026-09-17，用户问诊）

**用户问诊（2026-09-17）**：answer 气泡里引用的 `.mp4` 文件点开没走浏览器新页签，反而被当文本开了右侧预览抽屉；顺带问「视频能不能直接渲染成播放器」。**根因确认：两个入口都没把视频当视频**——

- `assetBoxHtml` 的分流表只有图 / 音 / 40 种文本三张后缀表——mp4 落「其它 → 普通链接」；
- 📎 变更文件补充区 `unmentionedChangesHtml` 的 inline 条件只认图片/音频——mp4 落 else → `openFilePreview()` 文本预览抽屉（8000 实例实测正主）。

**修复（src/static/index.html）**：

| 位置 | 改动 |
|---|---|
| `assetBoxHtml` | 新增 `isVid` 后缀表（mp4/webm/mov/mkv/avi/m4v/flv/ts/3gp/wmv/mpg/mpeg）→ **内嵌 `<video controls preload="metadata">` 播放器**（`src=/api/asset`，metadata 只取首帧不预载全片）+ 右上角 `↗` 新页签链接（`.vid-open`，浏览器按 /api/asset 的 Content-Type 直接播原片，不触发下载） |
| `unmentionedChangesHtml` | 条件补视频类 → 走 `assetBoxHtml` 内嵌播放器（不再进文本抽屉） |
| 嗅探前置 | 未知后缀探嗅条件补 `&& !isVid`——已知视频后缀短路，不再进 probe 链路 |
| CSS | `.asset-box.video`（max-width 560px，relative）+ `.vid-open` 右上角定位 |

- **caption 带变更标记**：`assetBoxHtml(ic+' '+f, f)` 已有——视频框自动继承 ✏️/➕ 标记
- **与未知后缀嗅探（/api/file-kind，2026-09-09）共存**：已知视频后缀 → 直接内嵌播放器；未知后缀嗅探出 `video` kind 仍走新页签链接（原设计）——两条路各归其位

**验证（run_python 断言）**：isVid 判定 ✓ / video 播放器分支 ✓ / 变更列表含视频 ✓ / CSS 写入 ✓。

**生效**：纯前端，Ctrl+F5 刷新即生效（8000 实例需 /update-assets 或升级后重启取新静态资源）。

### [文字](本地路径) 无感叹号链接：渲染为 asset 链接（2026-09-17，用户问诊，commit 4ad7742）

**用户问诊（2026-09-17）**：markdown 引用文件的 `[]()` 语法，`[]` 里是不是也可以没有感叹号 `!`？——**此前不行**：`inlineRich` 管线里 `[!]` 资源引用、`![alt]` 标准图片、`[](http)` 普通外链三条正则都吃不下本地路径的 `[文字](路径)`，整段经 esc 后原样显示字面文本。commit `4ad7742` 补上**第四种引用形态**，至此与标准 markdown 语义完全对齐。

| 写法 | 渲染 |
|---|---|
| `[!名](path)` | 资产框——图框 / 音频条 / 视频播放器 / 文本抽屉按扩展名分流 |
| `![alt](path)` | 同上（标准 markdown 图片，复用同一资产链） |
| `[文字](https://…)` | 普通外链（新页签打开） |
| **`[文字](本地路径)`** | **可点击链接**（本节新增）：href 走 `/api/asset`，新页签由浏览器按 Content-Type 直接播/显示——mp4 直接播、png 直接显示，不触发下载 |

**语义即 markdown 惯例**：带 `!` = **嵌入**（资产框内嵌在气泡里），不带 `!` = **链接**（跳转打开）。同一产物两种姿态并存：`[!演示视频](demo.mp4)` 出[内嵌播放器](#视频资产内嵌播放器渲染2026-09-17用户问诊)，`[演示视频](demo.mp4)` 出链接点开全页播放。

**实现要点**（`inlineRich`，src/static/index.html）：

- 新正则紧随 ④ 普通外链分支之后：`\[([^\]]+)\]\(((?!https?:\/\/)[^)\s]+)\)`——http(s) 先被外链分支吃掉，剩余本地路径落本条；href = `/api/asset?path=<encodeURIComponent(path)>`，`target=_blank`
- **安全边界**：绝对路径（`/`、`\` 开头）与含 `..` 段的不处理（保持字面文本）——不给相对路径注入留口（`/api/asset` 后端沙箱另有穿越防护，双层）

**cited 判定同步（📎 变更文件补充区误补防御）**：`unmentionedChangesHtml` 的「已引用」收集链从双语法扩为三——`[!名]` 自定义 → `![alt]` 标准图片 → `[名]` 本地链接；用链接形态引用过的文件同样算「已交代」，不再被补充区误补资产框。

**验证（node 单测）**：四种形态渲染分流全对 + cited 三语法收集全命中。**纯前端改动，Ctrl+F5 生效**（8000 实例取新静态资源走 /update-assets）。

### 资产框 error 降级：音频/视频框 onerror + 「文件已不存在」统一提示（2026-09-18，用户问诊，commit deded59）

**缘起**：用户问诊——「这一轮创建文件，下一轮又删掉了，那前一轮 answer 气泡中对该文件的引用是怎么处理的？」盘点全链路后的结论：**气泡是事件时刻的快照**，不因文件后续被删而自动重绘或移除引用，但各引用形态对「文件已不在」各有降级表现——盘点同时暴露两个缺口：**音频/视频框完全没有 onerror 处理**（文件 404 → 静默空播放框），图框虽有 `.err` 灰显（onerror 原有）却**无文字说明**（分不清加载失败还是路径写错）。用户拍板补齐（commit `deded59`）。

**改动**（src/static/index.html，纯前端）：

| 位置 | 改动 |
|---|---|
| CSS `.asset-box.err::after` | `content:'⚠️ 文件已不存在'` 居中说明文字——err 框从「半透明空壳」变成有明确交代的占位框；**图片框同受益**（onerror 原有，本轮只补提示） |
| 音频框 `<audio>` | `onerror` → 宿主 `.asset-box` 加 `.err` + `this.remove()` 移除播放控件 → 剩 err 框 + 统一提示 |
| 视频框 `<video>` | 同款（[内嵌播放器](#视频资产内嵌播放器渲染2026-09-17用户问诊)之上补）；右上角 ↗ 链接保留可试 |

**四形态降级矩阵（改后全量）**：

| 引用形态 | 文件已被删时的表现 | 状态 |
|---|---|---|
| 图框（`[!名](x.png)` / `![alt](x.png)`） | `.err` 灰显 + 「⚠️ 文件已不存在」 | onerror 原有 / 提示本轮补 |
| 音频框 | 播放条移除 → err 框 + 同款提示 | **本轮新增** |
| 视频框 | 播放器移除 → err 框 + 同款提示（↗ 链接保留） | **本轮新增** |
| 文本 / 未知后缀嗅探框 | 点击时 `❌ 读取失败：HTTP 404` → err 灰显 | 原有 |

**触发时机边界**：live 渲染当轮文件还在、不触发；降级只在**刷新 / 读档 / 翻页重渲染**（此时文件已被后续轮删除）时出现——与「快照不重绘」并不矛盾：气泡文本不变，重绘的是资产框内的媒体元素，媒体加载失败才走 onerror。

**验证**：node 语法 ✓ + 三处结构断言（CSS `::after` / audio `onerror` / video `onerror`）全过。**纯前端，Ctrl+F5 即生效**。

### 📎 本轮变更文件容器限高 + 可折叠（2026-09-22，用户提案）

**用户提案（2026-09-22）**：「给 answer 气泡下面的 [本轮变更文件] 容器一个最大高度吧，然后支持折叠」——变更文件多的轮（几十个文件的施工轮典型）此前容器无高度上限，把气泡撑得老长，正文被文件列表淹没。

**改动**（`unmentionedChangesHtml` + CSS，src/static/index.html，纯前端）：

| 位置 | 改动 |
|---|---|
| 标题行 `.ac-title` | `onclick="this.parentElement.classList.toggle('folded')"`——点击在容器 `.ans-changed` 上切换 `folded` class；`cursor:pointer` + `user-select:none` + hover 变色，`title="点击折叠/展开"` |
| 箭头 `.ac-arrow` | 标题行右侧 `▾`（`float:right`），`transition:transform .15s`；`.folded` 时 `rotate(-90deg)`——**▾ 旋成 ▸**，一个元素两态（初版 `::before` content 切换方案被同轮替换为 rotate 方案） |
| 体区 `.ac-body` | 展开态 **`max-height:240px; overflow-y:auto`**——超限内部滚动，气泡不再被撑爆；`.folded` 时 `display:none`，只剩一行标题 |

```
┌─ answer 气泡 ──────────────────────────────┐
│ ……正文……                                    │
├─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┤
│ 📎 本轮变更文件（回答中未交代，共 N 个）   ▾ │ ← 标题可点击
│ ┌─ ac-body（max-height 240px 内部滚动）────┐│
│ │ ✏️ src/agent.py   ✏️ src/server.py  …    ││
│ └───────────────────────────────────────┘│
└────────────────────────────────────────────┘
   folded 态：┌─ 📎 本轮变更文件（共 N 个）▸ ─┐（只剩标题行）
```

- **原有交互全部保留**：图片/音频/视频内嵌、文本预览抽屉、deleted 半透明灰框、「已引用」剔除——只动容器外框，不动内层资产分流
- **实时渲染与历史读档同函数**（`renderAnswerPages` → `unmentionedChangesHtml`），一处改动两处生效；复制回退排除清单中的 `.ans-changed` 语义不变（非 answer 正文不进剪贴板，见[复制内容与剪贴板降级](#复制内容与剪贴板降级)）

**验证**：JS 语法 1/1 + 6 处结构实存（toggle onclick / ac-arrow / max-height / overflow-y / folded display:none / transition）。**纯前端，Ctrl+F5 即生效**。

## 气泡最小宽度与表格媒体控件最小尺寸（2026-09-18，用户提案）

用户三问（2026-09-18，commit `3d38959`）：① answer 气泡最小宽度取总宽度 50%；② 表格里的音频播放条 / 视频播放器给足最小尺寸，保证播放按钮点得到；③ 复制按钮放入剪贴板的应是**原始 answer markdown**——③ 查证后为**已然实现**（见[复制内容与剪贴板降级](#复制内容与剪贴板降级)），本轮只补一处排除清单；①② 为纯 CSS 改动。

**CSS 三行**（src/static/index.html）：

```css
.row.bot .bubble { min-width: max(50vw, 300px); }                /* 原 min-width:250px */
.bubble table audio, .bubble table video { min-width: 240px; }   /* 窄列里播放键/进度条点得到 */
.bubble audio, .bubble video { max-width: 100%; }                /* 顺带防溢出 */
```

**为什么用 `vw` 而不是 `%`（关键坑，已写进代码注释）**：bubble 的 containing block（`.turn`）是 **shrink-to-fit 的 flex 子项**——百分比 `min-width` 在此上下文**按 0 解析、不生效**（写 `min-width:50%` 等于没写）。`50vw` ≈ 消息区（msgArea）半宽——右侧 spec/🐞/文件预览等抽屉都是 `fixed` 浮层、不占文档流，故 vw 近似就是「总宽度」；`max(…, 300px)` 兜底防窄屏下 50vw 过小（短回答原来只有 250px，太窄不好读）。

**媒体控件最小尺寸**：表格单元格宽度受列宽约束，音频播放条（播放键 + 进度条 + 时长）在窄列里会被挤到控件不可点/拖不动；`min-width:240px` 是能放下播放键 + 可拖进度条的最小实用宽度。仅**表格内**生效——气泡里独立引用的资产框自有尺寸规则（`.asset-box.video` max-width 560px 等，见[视频资产内嵌播放器](#视频资产内嵌播放器渲染2026-09-17用户问诊)）；后一条 `max-width:100%` 防大屏资产框撑破窄气泡。

**生效**：纯前端，**Ctrl+F5 强刷即生效**（8000 实例取新静态资源走 `/update-assets`），无后端改动。

> **过程区同宽跟随（2026-09-20）**：`.turn` 的宽度正是由这条 min-width 撑起——「过程」trace 由此改 `width:100%` 即与气泡永远同宽（旧 `max-width:85%` 单边限宽撤销），见 [trace-fold · 过程区宽度跟随 answer 气泡](trace-fold.md#过程区宽度跟随-answer-气泡2026-09-20用户提案)。

### 追记（2026-09-20 · v2）：`.row.bot .bubble` 加 `max-width:none`——bot 气泡填满 .turn 与过程区恒同宽

「过程」宽度 v2（见 [trace-fold · v2](trace-fold.md#过程区宽度跟随-answer-气泡2026-09-20用户提案)）揭出通用 `.bubble { max-width:85% }` 的残留副作用：当轮出现宽内容（diff 两栏/长表格）时 `.turn` 被撑宽，bot 气泡被 85% 压住 → `trace（width:100%）` 反而比气泡宽一截。修法：`.row.bot .bubble` 在 min-width 之外加 **`max-width:none`**——解除 85% 上限，bot 气泡填满 `.turn`（其宽度=本节锚定的 `max(50vw,300px)`），与 trace 恒同宽；**右侧用户气泡（`.row.me .bubble`）的 85% 限宽保留不动**。纯前端，Ctrl+F5 生效。

## bash 代码块执行按钮：Agent 执行改逐条指令——清理 shebang/行尾注释/空行（2026-09-17，用户提案，commit 60f3c6d）

**按钮本体（此前未入册）**：answer 气泡里 ` ```bash / sh / shell / shellscript ` 代码块下渲染两个执行按钮——**▶ Agent 执行**（把代码发 `/call run_shell`，经 Agent 工具链执行）与 **💻 终端执行**（整块发独立 shell）。

**用户提案（2026-09-17，commit `60f3c6d`）**：Agent 输出的块常含**多行指令 + `#` 开头注释**（安装指引典型形态）——整块执行不成立，「更可能是针对其中的每一行在去掉末尾注释和空白后才是有效的指令」。

### 逐行清理规则（runShellCode，src/static/index.html）

| # | 规则 | 示例 |
|---|---|---|
| 1 | shebang 行（`#!` 开头）删除 | `#!/bin/bash` → 丢 |
| 2 | 行尾注释切除（**`#` 前有空白才切**——防误杀 URL fragment / 字符串里的 `#`） | `pip install agt  # 安装` → `pip install agt` |
| 3 | 纯注释行（`#` 开头）删除 | `# 下面是配置` → 丢 |
| 4 | trim 空白、空行删除 | |
| 5 | 剩余 = 有效指令数组（`data-cmds`） | |

### 行为分工

| 按钮 | 语义 |
|---|---|
| **▶ Agent 执行（N 条）** | 逐条发 `/call run_shell`——后端 work_q **串行**按序执行，每条结果独立显示；多行时按钮标注条数 |
| **💻 终端执行** | **保持整块原文**——独立 shell 原生支持 `#` 注释与多行，无需清理 |

**边界**：全注释块（无有效指令）→ 数组为空，按钮点了是空操作（不报错）。

**验证**：node 单测——shebang + 行尾注释 + 纯注释 + 空行混合块，`data-cmds` 正确切出有效指令，且 URL 中的 `#`（`echo "https://x.com/a#frag"`）完好不被误切。**纯前端，Ctrl+F5 生效**。

## 气泡级复制按钮（index.html，2026-08-19）

### 交互效果

鼠标悬停到 **user 气泡**（右侧蓝色）或 **answer 气泡**（左侧白色）→ 底部角落浮现 `📋 复制` 小按钮 → 点击 → 整个气泡内容进剪贴板 → 按钮变 `✓ 已复制`，1.5 秒后恢复。

按钮 `.bubble-copy` 默认 `pointer-events:none`（未 hover 时不拦截气泡下层点击），hover 浮现时才恢复可点——「透明/浮层元素不拦点击」的防坑，与 [用户交互 · toast 透明条遮挡输入框失焦](user-interaction.md#前端-ui-遮罩坑toast-透明条遮挡输入框失焦2026-08commit-0a415bc) 同源。

**hover 断链修复（2026-09-09，用户报障「移过去按钮就消失」，commit c54a004）**：按钮原定位 `top:100%; margin-top:2px`——**margin 区域不属于宿主盒**，鼠标从气泡底部滑向按钮必经这 2px 缝：此刻不在 `.turn:hover` 触发区 → 按钮 `opacity:0 + pointer-events:none` → 鼠标到达按钮原位置也点不了（确定性死锁，不是灵敏度问题）。修复：去掉 margin，`top:calc(100% - 2px)` 让按钮顶部**伸进宿主盒内 2px**——气泡→按钮路径几何连续，hover 冒泡不断；视觉上按钮自带边框贴着气泡底边，看不出位移。

> ⚠️ 教训：悬浮按钮与宿主 hover 触发区之间**不能留 margin 缝**（会断 :hover 链导致按钮自锁消失），间距应靠按钮自身 padding / border 或与宿主盒重叠实现。

### 四处挂载（实时 + 历史全覆盖）

| 位置 | 函数（均在 `static/index.html`） |
|---|---|
| 实时 user 气泡 | `addUserBubble` |
| 实时 answer 气泡 | `newTurn` |
| 历史 user 气泡 | `renderHistTurn` |
| 历史 answer 气泡 | `renderHistTurn` |

挂载点形如 `col`/`urow`/`row`/`host`（见下）——注意是**宿主容器**而非 bubble 本身。

### 关键设计——按钮挂在宿主（row/col）上而非 bubble 里

- hover 触发区也用宿主（`.row.me:hover` / `.turn:hover`）——鼠标在气泡和按钮之间移动不会闪烁（触发区连成一片）。⚠️ 2026-09-09 修复前并不完全成立：按钮 `margin-top:2px` 在宿主与按钮间留缝，穿缝即 `:hover` 断链（详见[上文 · hover 断链修复](#交互效果)）
- 这是「DOM 会被整体重写的容器，交互控件必须挂到不被重写的祖先上」的通用范式，后续给气泡加其它悬浮按钮时同理

### 复制内容与剪贴板降级

- **复制 markdown 原文**（2026-09-09 定案，用户问询「复制的是 markdown 原文吗」后改，commit c54a004）：`renderAnswerPages` 渲染 answer 时把**当前页 markdown 原文**挂在 bubble 元素 `__md_text` expando 上（`innerHTML` 重写不清 expando）；`attachCopyBtn` 点击时优先取 `__md_text`（非空才用）——表格是 `|` 分隔、代码块带 ` ``` ` 围栏，可直接再编辑/投喂。此前取渲染后 innerText：表格变制表符对齐、代码块丢围栏，**不可再渲染**，是本次问询暴露的语义缺陷
- **回退链**：spec/问卷/中断卡片等非 answer 渲染在重写 answer 区时各自置 `__md_text = null`——复制按钮取到 null 即回退 `innerText`（仍走克隆排除法：`cloneNode(true)` 后 remove 排除清单，见下），不会拿到上一次 answer 的旧原文
- **多 Agent 分页**：`renderAnswerPages` 单页/多页两分支都在重渲染时挂 `__md_text = 当前激活页原文`——切页随重渲染自动更新，复制到的始终是当前页
- **历史轮**：读档走同一条 `finishAnswer` → `renderAnswerPages` 路径，自动覆盖
- **user 气泡**本为纯文本，复制行为不变
- 取 `innerText` 而非 `textContent` 的回退语义保留——表格/代码块至少保留文本结构，不是一坨裸文本
- `clipboard API` 失败自动降级 `execCommand`（兼容老浏览器）
- **回退路径排除清单补 `.ans-changed`（2026-09-18，用户提案）**：用户再次确认「复制到剪切板的应该是原始 answer 正文 markdown」——查证：**主链路已然实现**（2026-09-09 起 `__md_text` 优先，历史轮同路径 ✓），本轮只修回退路径的细节：克隆排除清单从 `['.ans-tabs','.copy-btn','.run-btn']` 补入 **`.ans-changed`**——它是 answer 尾部的「📎 本轮变更文件」补充块（`unmentionedChangesHtml` 产出，非 answer 正文，见[本节](#本轮变更文件补充区图片音频直接内嵌渲染2026-09-06用户提案)），此前会被当作正文一起复制进剪贴板

### 与代码块级复制的层级

`index.html` 原有 `.copy-btn`（代码块级复制）与气泡级按钮形成两层：整气泡要 → 气泡按钮；只要某个代码块 → 代码块按钮。两者互不干扰。

## answer 多 Agent 分页（index.html + agent.py，2026-08-21）

### 背景：同步子 Agent 输出串台

现象：同一轮 answer 气泡里混入子 Agent 的回应消息和主 Agent 的 answer，互相覆盖混排。

```
根因链（当年 spec_tools.py L482——explore_subagent 构造 SubAgent 时传 on_event=agent.on_event，
       该工具 2026-09-09 已删（commit ce6de5f），on_event=agent.on_event 的同步子 Agent 形态仍存）
  → 子 Agent 的 answer 事件（type="answer"）直接流入主事件流
  → 前端 finishAnswer 写入当前轮 answerEl
  → 与主 Agent 的 answer 互相覆盖 ← 串台
```

**范围界定**：只有**同步调用**的子 Agent（update_wiki 等仍存；explore_subagent 已于 2026-09-09 删除，探索前置改走外置 explore——见 [spec 工具集](spec-tools.md)）有此问题——主 Agent 正在等它的工具结果时，它的 answer 先到，写进了同一个气泡。异步 `agent_prompt` 路径 on_event=None 本就不串——其 answer 走 inbox → 主 Agent 新一轮处理（见 [多 Agent 体系](../architecture/multi-agent.md)）。

### 修复：事件统一打 agent_id（后端一处改动全覆盖）

`src/agent.py` `_emit`：

```python
event.setdefault("agent_id", self.agent_id)   # 主=_main_，子 Agent=各自 id
```

所有 Agent 的所有事件（answer/thinking/step/tool_*）统一打标——前端据此分流渲染，而不是各发射点各自补标。

### 前端分页渲染

```
┌─ answer 气泡 ─────────────────────────────┐
│ [🤖 主] [reader] [wiki-updater]  ← tag 按钮（当前页高亮）│
│ （当前页的 markdown 渲染内容）                  │
└───────────────────────────────────────────┘
```

| 行为 | 实现 | 说明 |
|---|---|---|
| 页收集 | `finishAnswer(text, agentId)`：`id = agentId \|\| '_main_'`，`curTurn.pages[id] = text` | 主 answer 与每个子 Agent 回应各一页 |
| 自动激活 | `curTurn.activePage = id`（最新到达的页） | 子 Agent 回应到达时自动切过去看；主 answer 后到再切回 |
| tag 按钮 | `renderAnswerPages()`：多页时顶部渲染 `.ans-tabs` 一排 `.ans-tab`（11px 圆角小标签），`switchAnswerPage` 点击翻页 | **仅对该轮有效**——新轮 `newTurn` 后 pages 重置 |
| 单页 | 同样走 `renderAnswerPages`，但无 tabs | 与旧版渲染完全一样 |
| trace 前缀 | `step`/`thinking` 事件：`m.agent_id !== '_main_'` 时加 `[agent_id] ` 前缀 | 子 Agent 的过程事件不再裸混进主 trace |

**历史渲染兼容**：`renderHistTurn` 构造的临时 curTurn 无 pages 字段，`finishAnswer` 内 `curTurn.pages = curTurn.pages || {}` 兜底——读档路径不炸。

### 与复制的配合

分页引入后，answer 气泡的 innerText 会带上 tabs 按钮文字 → 复制按钮改为**克隆排除 UI 元素**再取文本（见上文「复制内容」小节），复制内容始终是当前页正文。

## answer 中断轮充值入口按钮 · 回退链全失败一键打开（2026-09-08，用户提案）

**用户提案（2026-09-08）**：回退链全部失败、轮中断后，answer 区域除「▶ 继续」按钮（`finishInterrupted` 既有）外，把余额不足的几个 provider 充值入口也带上——点击一键打开充值页。此前配额/鉴权类失败只能翻日志找充值页（flatkey 欠费 403 那类场景），现在按钮直达。

**数据链**：

```
provider 403（flatkey 欠费）
  → llm.last_failures 逐跳收集 {model, cls, msg, provider, url}（每次调用开头重置）
  → 回退链耗尽 → run() except 捕获（agent.py）
  → 从 last_failures 去重（按 url）提取 hints：
      错误消息内嵌链接 > preset recharge_url > register_url（详见 [配置体系 · 一键充值](../guides/config-and-models.md#回退链中断一键充值preset-recharge_url--401403404-纳入回退2026-09-08用户提案)）
  → interrupted 事件带 recharge: [{provider, url, reason}]
  → 前端 case 'interrupted' → _rechargeHints → finishInterrupted() 渲染按钮组
```

**前端**（src/static/index.html）：

- `_rechargeHints` 模块级变量：`interrupted` 事件带来，`finishInterrupted` 消费后清空（不跨轮残留）
- `finishInterrupted` 渲染：`▶ 继续` 按钮旁附 `💰 <provider>` 按钮组（`<a target=_blank>` 新标签打开 `url`，URL 单引号转义 `%27` 防属性注入）；按钮 `title` 悬停显示失败原因摘要
- 无充值入口（网络/限流断链）→ 只有「▶ 继续」，行为与旧版一致

**CLI 侧**（src/chat.py）：`interrupted` 事件同样打印 `💰 {provider} 充值入口: {url}`。

**失败归类纪律**：只对 **quota**（余额/配额/欠费）和 **auth** 类失败生成按钮——`_classify_err`（llm_client）把网络/限流/超时归其它类，断链时不冒无关充值按钮；`title` 悬停可看具体原因（如 `Failed to pre-deduct quota...`）。

**验证（全绿）**：flatkey 403 真实消息归类 quota ✓ / 消息内嵌 wallet 链接精确提取 ✓ / 全链失败 mock（2 个 flatkey 条目都 403）→ 2 条 last_failures 去重 1 个按钮 ✓ / CLI 中断打印 ✓ / JS 语法 + 5 项结构断言 ✓。

**生效方式**：引擎层（agent.py 事件 + index.html），需 `/restart`——下次 provider 欠费断链，气泡上直接点开充值页，充完点「▶ 继续」从断点续跑。

## spec 批阅气泡 · 通过后自动收起成摘要（2026-09-08，用户提案）

**用户诉求**：spec 通过并开始实施以后，answer 区的 spec 大卡片应从气泡中隐藏——详情转为主要在 📐 抽屉查看，把 answer 区还给施工过程与最终回答。此前大卡片在批准后仍占着 answer 区直到轮末（施工几十步都被它遮住）。

**时序变化**（`src/static/index.html`）：

| 时刻 | 之前 | 现在 |
|---|---|---|
| commit_spec 阻塞批阅 | 大卡片（design + steps + 通过/返工按钮） | 同左——**批阅交互不变** |
| 点「通过并施工」（answer 区或抽屉批阅栏任一入口） | 按钮区变一行字，卡片其余部分继续占着 answer 区 | 大卡片收起 → `📐「标题」已通过，开始实施 · N 步 [📐 在抽屉中查看]` 一行摘要 |
| 施工过程（几十步） | answer 区一直是 spec 死内容 | 干净一行摘要，过程区不再被遮 |
| 最终 answer | 覆盖 | 覆盖（顺带清标记） |

**关键实现**：

- `_specBubbleActive` 标记：`renderSpecBubble` 入口置 true（answer 区被 spec 气泡占用）；`case 'answer'/'wrap_answer'` 置 false（最终回答到来时清标记）
- `collapseSpecBubble(m)`：置 false 标记 + 把 answer 区气泡重写为一行摘要（标题 + 通过徽章 + 步数 + `[📐 在抽屉中查看]` 按钮 → `toggleSpecPanel(true)` 打开抽屉）；详情完整渲染不受影响——`renderSpec` 照常全量填充抽屉（标题/状态徽章/设计概述/steps），approved 态本就有完整详情，现在成为主要查看入口
- `case 'spec'` 事件（WS 分发）：`m.review_state === 'approved' && window._specBubbleActive` → `collapseSpecBubble(m)`

**边界处理**：

- **draft / rejected 不收起**——返工流程原样（马上会被新一轮 `spec_pending` 重渲染，收起反而闪烁）
- **从抽屉里批准的**（抽屉批阅栏也有通过/返工按钮）同样生效——收起逻辑挂在 `case 'spec'` 事件上，不关心批准入口在哪
- **读档场景天然无此问题**：`spec_pending` 是 UI 事件不落盘，历史轮只有 answer 文本；重连补发只在 committed 态（那时才需要交互气泡）

**验证**：node 行为模拟全过（pending 置位 → draft/rejected 事件**不**收起 → approved 事件收起、步数与抽屉按钮渲染正确）+ JS 语法 + 6 项结构断言全绿。**纯前端改动，Ctrl+F5 生效**。

抽屉侧形态（headbar 钉顶 + 滚动区独立 + 设计概述自然撑高）见 [编辑器 UX · spec 抽屉](../features/editor-ux-improvements.md#批次十二092a0dfspec--日志抽屉布局统一标题栏钉顶--滚动区独立)。

## 轮收藏与收藏视角（2026-09-22 主体开发收官，commit fe1b2ac，用户提案）

**用户提案（2026-09-21）**：WebUI 交互中有些轮需要经常回看——可把这样的轮**收藏**（answer 气泡 ⭐）；再做一个**「收藏视角」**，专门回看被收藏轮的上下文。

**前情**：v0.29.8 已入库读侧预备——answer 事件带 `"turn": len(turns)+1`（收藏按钮 data-turn 回填，answer 发出时轮尚未归档）、session_history payload 带 `"favorites"` 收藏表（存 `session.extra_state`，见 [workflow-hooks · before_turn 专用超时](../architecture/workflow-hooks.md#before_turn-钩子专用超时-60s2026-09-21-用户裁定v0298-发布)——主体开发曾因该插话暂停）。**2026-09-22 主体收官（commit `fe1b2ac`）：写侧端点 + ⭐ 按钮 + 视角切换三层全通。**

### 三层实现

| 层 | 内容 |
|---|---|
| **持久化**（src/server.py） | `POST /api/favorite` body `{"turn": 轮号, "on": true/false}` → 写 `session.extra_state.favorites`（数组）——随 session 存档 meta.json 落盘，**重启/读档收藏随会话走** |
| **⭐ 按钮 + 轮号贯通**（src/static/index.html） | `.fav-btn` 挂 **`.turn` 宿主**左上角（已收藏恒显 ★ 橙色 / 未收藏 hover 浮现 ☆；hover 触发区用宿主 + pointer-events 纪律——同[复制按钮挂载范式](#气泡级复制按钮indexhtml2026-08-19)）；`_favs` Set 随 `session_history` 事件初始化；**实时轮** answer 事件带 turn → bot row + 最近的 user row 成对标 `data-turn` 并 `attachFavBtn`（轮已完成正好可收藏）；**历史轮** `renderHistTurn` user/bot 成对标号 + `applyFavMarks()` 统一回填 |
| **收藏视角**（src/static/index.html） | 控件栏「☆ 收藏」按钮 → `toggleFavView()`：`body.fav-only` CSS 过滤——未收藏轮整体隐藏（user + 过程 + answer **成对显隐**），顶部蓝字提示条「⭐ 收藏视角：显示 N 个收藏轮（点击退出）」 |

**旧后端兜底**：`/api/favorite` 未升级（后端仍是旧版）时，点击收藏降级为**本页内存**（`_favs` 本地生效，刷新即失）+ toast「后端未升级，仅本页生效」——纯前端体验不断。

**顺带**：历史用户气泡补挂同款复制按钮（`attachCopyBtn`，对齐[四处挂载清单](#四处挂载实时--历史全覆盖)）。

**验证（三层全过）**：node JS 语法 1/1 + 结构断言 16/16；playwright 真实页面——控件栏「☆ 收藏」按钮存在 ✓、21 个历史轮 42 行 user/bot 全部成对标号并挂 ⭐ ✓、视角切换开（未收藏轮隐藏 + 蓝字条出现）/ 关（全恢复）✓。

**生效方式**：**Ctrl+F5 立即可用**（静态资源 mtime 热更新，前端已生效）；`/restart` 后 `/api/favorite` 持久化生效。

## 与后端的关系

- 气泡内容由 `agent.py` 事件流 `_emit` → WS broadcast → 前端渲染；**所有事件统一携带 `agent_id` 字段**（主=`_main_`，子 Agent=各自 id，`setdefault` 兜底）——前端 answer 分页 / trace 前缀均据此分流
- 系统气泡 vs 用户气泡的区分依据：事件类型（`system` / `user`）——前端按类型赋默认 collapsed 状态
- async 钩子工作流（见 [工作流引擎与钩子](../architecture/workflow-hooks.md#async-元信息字段2026-08-新)）的返回值不注入主循环，但若产生日志/副作用事件，仍以系统气泡形式展示（默认折叠）
- 气泡级复制、answer 分页翻页均为纯前端行为（只读 innerText / 切换已存页面），不涉及后端额外改动
- **例外：answer 行内资源渲染**（2026-09-04 起）需要后端配合——`server.py` 的 `GET /api/asset` 为图框/音频控件供文件（workspace 沙箱服务）；2026-09-09 起新增 `GET /api/file-kind`（未知后缀引用先嗅探内容类型/编码再定渲染形态，见[本节末章](#未知后缀引用按内容嗅探渲染apifile-kind--编码感知解码2026-09-09用户提案)）——是本页仅有的两个非纯前端特性；2026-09-22 起再加 `POST /api/favorite`（轮收藏持久化，见[轮收藏与收藏视角](#轮收藏与收藏视角2026-09-22-主体开发收官commit-fe1b2ac用户提案)）——本页共三个非纯前端特性

## 相关页面

- [多 Agent 体系](../architecture/multi-agent.md)：同步/异步子 Agent 的 on_event 差异、事件流 agent_id 打标
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：async 元信息字段、钩子链路
- [系统总览](../architecture/overview.md)：事件流 _emit → broadcast 链路
- [运维与排障](../guides/ops.md)：可观测性（llm_calls.jsonl / events.jsonl）
- [v0.18.2 发布记录](../releases/v0.18.2.md)：气泡折叠为该版交付项之一
- [WebUI 过程区折叠](trace-fold.md)：trace 内思考/工具/钩子三级降噪（共享 toggleFold 基建，与气泡折叠两套机制）

