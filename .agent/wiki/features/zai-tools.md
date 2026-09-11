# Z.AI 工具集 · zai_tools（智谱 BigModel 联网搜索/抓取/文件解析）

> 本地个人工具（**不随包播种**）：`tools/builtin/zai_tools.py`，改完 `/reload tools` 热加载。三工具复用同一智谱系（z.ai / glm-official，base_url 含 bigmodel.cn）的 `api_token`——**零独立配置，设置里配过一次 z.ai key 即开箱可用**。

## 三工具与端点

| 工具 | 端点 | 用途 |
|---|---|---|
| `zai_web_search(query, count=10, recency="noLimit", domains="")` | `POST /api/coding/paas/v4/web_search` | 实时联网搜索（新闻/版本号/文档更新/价格），中文与国内技术内容比 DuckDuckGo 更准 |
| `zai_web_reader(url)` | `POST /api/coding/paas/v4/reader` | 抓取单个 URL 正文（替代 open_url 的通用抓取——对部分站点/JS 渲染页更干净） |
| `zai_file_parser(path, file_type="")` | `POST /api/paas/v4/files/parser/sync` | 同步解析本地文档/表格/图片（27 种类型）提取文本/Markdown——图片走 vision 转 Markdown（见下文专章） |

## 鉴权：复用智谱系 api_token（零配置）

`_zai_token()` 轻量读 `models.json`（**零引擎依赖**），找智谱系 provider（`name == "z.ai"` 或 `base_url` 含 `bigmodel.cn`）的 `api_token`：

- **三级路径语义**：`AGT_HOME` env > `~/.agt`（与 kv_tools 同款）；
- **兼容新旧结构**：新版两级 `{"models": {provider: {...}}}`（t561 两级重构）/ 旧版扁平 `{provider: {...}}`；
- **兼容多形态 token**：`api_token` 为 str / list（**实测 list**）/ `api_tokens` list 三种形态都取第一个非空。

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

## 注册形态

`agt_register()` **无参**（不需要 `ctx["cwd"]`——token 读 `~/.agt/models.json`，不依赖 workspace 路径）；三工具 `hidden: False` 全部进正常工具集。`/reload tools` 实测注册 **50 个（47 旧 + 3 新）**。

## 与其他模块的关系

- 属 [工具外置](tool-externalization.md) 体系的外置件，但**不随包播种**（用户个人工具，与 cache_tools / explore_tools 等随包副本不同）——发布 pip 安装的用户不带此工具；留在 `tools/builtin/`（开发处）而非 `src/assets/tools_builtin/`（随包副本）；
- token 解析与 kv_tools 的 AGT_HOME 三级路径同款语义；
- 搜索能力分工：[grep](grep.md)（仓库内内容）/ [glob_files](glob-files.md)（文件名）面向**本地代码**；`zai_web_search` 面向**互联网实时信息**。

## 注意事项

- 三工具都先查 `_zai_token()`，未配置智谱系 key 时返回引导错误（提示在设置里添加 z.ai / glm-official）；
- `web_search`：`count` clamp 1~30、`recency` 枚举校验（noLimit/oneDay/oneWeek/oneMonth/oneYear）、`domains` 逗号分隔可选；
- 截断：搜索摘要 200 字 / web_reader 正文 6000 字 / file_parser 提取文本 12000 字；
- 超时：web_search 30s / reader 45s / parser 90s。

## 相关页面

- [工具外置](tool-externalization.md) —— 外置件清单与装配
- [缓存断点分析工具](cache-tools.md) —— 同目录另一个纯函数整体外置件
- [grep](grep.md) / [glob_files](glob-files.md) —— 本地检索能力分工（对照）