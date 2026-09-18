# Z.AI MCP Server · zai-mcp（智谱 BigModel 联网搜索/抓取/文件解析，独立 repo）

> 原为 agt 外置件 `tools/builtin/zai_tools.py`（2026-09-11 建，用户个人工具不随包播种）；**2026-09-17 迁出为独立 MCP repo**（用户提案：改 MCP、放单独 repo、本机使用）。主仓已删外置件（commit `7aca791` 已推送；确认不在打包 manifest，wheel 不受影响），本机 `~/.agt/mcp.json` 注册 `zai` 条目，`/restart` 后以 `__mcp__zai__*` 三工具生效。三工具复用智谱系（z.ai / glm-official，base_url 含 bigmodel.cn）的 `api_token`——**零独立配置，设置里配过一次 z.ai key 即开箱可用**。

## 三工具与端点

| 工具（MCP 名，agt 内带 `__mcp__zai__` 前缀） | 端点 | 用途 |
|---|---|---|
| `zai_web_search(query, count=10, recency="noLimit", domains="")` | `POST /api/coding/paas/v4/web_search` | 实时联网搜索（新闻/版本号/文档更新/价格），中文与国内技术内容比 DuckDuckGo 更准 |
| `zai_web_reader(url)` | `POST /api/coding/paas/v4/reader` | 抓取单个 URL 正文（替代 open_url 的通用抓取——对部分站点/JS 渲染页更干净） |
| `zai_file_parser(path, file_type="")` | `POST /api/paas/v4/files/parser/sync` | 同步解析本地文档/表格/图片（27 种类型）提取文本/Markdown——图片走 vision 转 Markdown（见下文专章） |

## MCP 形态与配置（2026-09-17 迁移）

- **repo**：`D:\Projects\zai-mcp\`——`zai_mcp.py`（FastMCP stdio server，三工具同源实现整体搬入）+ README；git 初始提交 `56a4150`（「zai-mcp 初始提交：Z.AI 三件套 MCP Server」）；README 自检入口 `python zai_mcp.py --selftest`
- **注册**：`~/.agt/mcp.json` 加 `zai` 条目（command 形态与 scnet / lsp 同款）——全局配置，任何 MCP 客户端（agt / Claude Code / Cursor 等）都能接
- **生效**：agt 侧 `/restart`（装配期 `make_mcp_tools` 注册）；`reload_mcp_server('zai')` 只适合改 server 内部实现、工具名不变的场合——新增/改名工具需重启（见 [mcp-config · 缺口](mcp-config.md)）
- **收尾**：主 Agent 随后 `e196fab` 消除双 zai（重复注册）问题

## 外置件 → MCP：形态差异

| | 外置工具（旧） | MCP（新） |
|---|---|---|
| 可用范围 | 仅 agt | 任何 MCP 客户端（agt / Claude Code / Cursor…） |
| 热更新 | `/reload tools` | server 独立进程——改完即新（重启客户端会话即可） |
| agt 侧耦合 | 工具注册进引擎 | 零耦合（`mcp.json` 声明即用） |
| 工具名 | `zai_web_search` 等 | `__mcp__zai__zai_web_search` 等（前缀变长，语义同） |

## 迁移验证（三层全绿，2026-09-17 开发轮）

1. `--selftest`：token ✓（复用 `~/.agt/models.json` 智谱系 key）+ web_search 真实搜索返回结果 ✓
2. MCP stdio 握手：server `{name: zai}` ✓；`tools/list` 三工具描述完整 ✓
3. 鉴权链路：与旧版同源——读 models.json 找 bigmodel.cn 系 provider 的 api_token ✓

## 鉴权：复用智谱系 api_token（零配置）

`_zai_token()` 轻量读 `models.json`（**零引擎依赖**，实现同源搬入 zai-mcp），找智谱系 provider（`name == "z.ai"` 或 `base_url` 含 `bigmodel.cn`）的 `api_token`：

- **三级路径语义**：`AGT_HOME` env > `~/.agt`（与 kv_tools 同款）；
- **兼容新旧结构**：新版两级 `{"models": {provider: {...}}}` / 旧版扁平 `{provider: {...}}`；
- **兼容多形态 token**：`api_token` 为 str / list（实测 list）/ `api_tokens` list 三种形态都取第一个非空。

WebUI 设置页配过一次 z.ai 或 glm-official 的 key 即全局复用，无需单独配 env 或独立 key。

## 关键实现与踩坑（实测修正，已写进代码注释）

1. **reader 端点名**：文档给的路径不对，实测锁定 `/reader`（**不是 web_reader**）——响应结构 `{"reader_result": {"content" / "title" / "url"}}`；
2. **parser 端点名**：正确在 `/api/paas/v4`（**不带 coding 前缀**）；`file_type` 现为**可选**——默认按扩展名自动识别并经 `_FT_ALIAS` 别名归一（`.tif→tiff`/`.htm→html`/`.markdown→md`/`.jpe→jpg`，大小写归一），不再要求与扩展名严格一致；
3. **token 是 list 形态**：第一版只取 str，用户机器实际是 list，当场补兼容（`api_token` str/list + `api_tokens` list 三态兜底）。

## zai_file_parser 27 种类型：白名单 + 别名归一 + 图片转 Markdown（2026-09，用户请求）

`_ZAI_FT` 白名单常量（官方口径 27 种，2026-09 用户请求补齐），`zai_file_parser` 只认这张表：

- **文档 11 种**：pdf / docx / doc / xls / xlsx / ppt / pptx / csv / txt / md / html
- **图片 16 种**：png / jpg / jpeg / bmp / gif / webp / heic / heif / jp2 / eps / icns / im / pcx / ppm / tiff / xbm
- **别名归一 `_FT_ALIAS`**：tif→tiff、markdown→md、htm→html、jpe→jpg（其余同名直传，大小写统一 lower）
- **不支持的类型明确报错**：返回 `[错误] 不支持的文件类型 'py'。支持：…`（全清单列出），不再静默失败；特殊扩展名可显式传 `file_type`
- 注册 `version` 1→2；`file_type` 参数描述把 27 个值全列出（模型可读的枚举提示，同 recency 的写法）

**实测发现：图片解析不是纯 OCR，而是「图片转 Markdown」**（vision 级解析，已按真实口径改写 docstring）：

- 覆盖两张实测图：含文字的架构封面图 → 标题/副标题/底部说明文字全部转写为 markdown，图示部分以 `![](images/xxx-image.png)` 图片引用保留；纯 logo 图（无文字）→ 只有图片引用，不硬编内容
- 结论：截图/扫描件/带图 PDF 可直接得到结构化 Markdown；对纯 logo 等无文字图不要抱「能读出内容」的期待
- 与 web_search / web_reader 不同，parser 走 `/api/paas/v4`（不带 coding 前缀），`file_type` 现在不传也 OK（自动识别）
- **消费端闭环（2026-09-14，commit 6215ed1）**：parser 输出的 `![](相对路径)` 标准图片引用现已在 answer 气泡直接渲染成资产框——见 [气泡交互 · 标准 markdown 图片语法渲染支持](bubble-interaction.md#标准-markdown-图片语法渲染支持2026-09-14用户问诊commit-6215ed1)

## 与其他模块的关系

- 已脱离 [工具外置](tool-externalization.md) 体系（`tools/builtin` 清单 15→14）；能力经 MCP 通道回归 agt（chat.py 装配期 `make_mcp_tools`，见 [MCP 配置页](mcp-config.md)）
- token 解析与 kv_tools 的 AGT_HOME 三级路径同款语义（agentid-tools 同族）
- 搜索能力分工：[grep](grep.md)（仓库内内容）/ [glob_files](glob-files.md)（文件名）面向**本地代码**；`zai_web_search` 面向**互联网实时信息**

## 注意事项

- agt 内工具名带 `__mcp__zai__` 前缀——旧工具习惯 `zai_*` 已失效，调用时用新名；
- 三工具都先查 token，未配置智谱系 key 时返回引导错误（提示在设置里添加 z.ai / glm-official）；
- `web_search`：`count` clamp 1~30、`recency` 枚举校验（noLimit/oneDay/oneWeek/oneMonth/oneYear）、`domains` 逗号分隔可选；
- 截断：搜索摘要 200 字 / reader 正文 6000 字 / parser 提取文本 12000 字；
- 超时：web_search 30s / reader 45s / parser 90s；
- 自检入口：`python zai_mcp.py --selftest`（README 同款）。

## 相关页面

- [MCP 配置页](mcp-config.md) —— mcp.json 配置与连接状态、reload_mcp_server 缺口
- [工具外置](tool-externalization.md) —— 原外置件体系（Z.AI 组已迁出）
- [grep](grep.md) / [glob_files](glob-files.md) —— 本地检索能力分工（对照）
