# wiki 工具集 · wiki_tools.py 十件套（页面级六件套 + 章节级四件套）

> 载体：`tools/builtin/wiki_tools.py`（外置件，纯函数整体外置；随包副本 `src/assets/tools_builtin/wiki_tools.py`）。
> 数据主权：`.agent/wiki/*.md` **自写自读**——真限界上下文，外置完全自洽（见 [判别标准](../architecture/tool-externalization-criteria.md)）。
> 主要消费者：wiki-updater 子 Agent（[wiki_auto_maintenance](wiki-auto-maintenance.md) 工作流调 update_wiki 触发）。

## 工具十件套

| 工具 | 层级 | 语义 |
|---|---|---|
| `wiki_read / wiki_list / wiki_tree / wiki_search` | 页面·读 | 读单页 / 列目录（附标题大纲）/ 全树（附大纲）/ 全文搜索 |
| `wiki_write` | 页面·写 | **整页覆盖**——中断会留残页/丢内容（home.md PLACEHOLDER 教训），只用于新建页面或整页大重构 |
| `wiki_delete` | 页面·写 | 删整页 |
| `wiki_add_chapter(path, title, content, level=2, after="")` | **章节·写（新）** | 新增章节；锚点=该章节**子树之后**（子章节不拆散），after=空 → 页尾 |
| `wiki_update_chapter(path, title, content=None, new_title="")` | **章节·写（新）** | content 传值 → 替换章节正文（含子章节）；new_title → 纯重命名（正文/子章节原样保留）；可同时用 |
| `wiki_remove_chapter(path, title)` | **章节·写（新）** | 标题 + 正文 + 全部子章节**整棵子树移除** |
| `wiki_move_chapter(path, title, after="")` | **章节·写（新）** | 整棵子树移到锚点子树后（after=空 → 页尾）；多次调用可完成任意重排 |

（新 = 2026-08，commit `fe590a3`；此前只有页面级六件套。动机：结构化 markdown 每页章节边界清楚，应支持章节级外科手术式小改，而非只有整页覆盖一个写入口。）

## 关键设计：章节边界 = 标题 + 全部子树

章 = 本标题行起，到**下一个层级不深于它的标题行或 EOF** 为止（含尾部空行）。这是"结构化 markdown 章节边界清楚"的自然含义，也让 add/move 的**锚点定位天然正确**。

**平铺切分的教训**（首轮实现，测试当场抓到 bug）：若章 = 标题到下一标题（不管层级），`move C after=A` 会把 C 插进 A 和 A 的子章节 A1 **中间**——A1 变成 C 的子节，结构被无声破坏。子树语义下"插到 A 的子树之后"才是预期锚点。

实现细节（`_chapter_spans`）：

- **fence 感知**：代码围栏（`` ``` `` / `~~~`）里的 `#` 不是标题（切分时跟踪 in_fence 状态）
- **move 块尾空行分隔**：原 span 若贴着下一标题则块尾无空行，移动时块尾补 `\n`（保证插入后章节间空行分隔，往返无损）

## 增量维护优先（wiki-updater 约定，同 commit）

- **persona 原则**：已有页面优先用章节四件套做小改（不动页面其余部分）；`wiki_write` 整页覆盖只用于**新建页面 / 整页大重构**——直接针对上次 home.md 整页重写中断留 PLACEHOLDER 的教训（整页重写的脆弱性）
- **白名单**：`.agent/agents/wiki-updater.yml` 补四件套（现 12 工具：wiki 六件套 + read_file/list_dir/grep + 章节四件套）
- **定位信息现成**：wiki-updater 每轮注入的 wiki_tree 本就带各页标题大纲（层级+行号），章节定位无需额外读页
- 哲学与 edit/insert/delete/move 行级文件工具一致：小改不重写整文件

## 验证与调试插曲

- 合成页全流程测试通过
- 真实 home.md（15K 导航页）**往返无损断言**失败过一次——排查确认为 **async 的 wiki_auto_maintenance 钩子并发写 home.md 的假阳性**（钩子的改动被算进了"往返差异"）；还原后单独重跑：**add+move+remove 往返逐字节无损**
- 教训：对活跃文件做测试，先确认无并发写入者（async 钩子随时可能在写同一批文件）

## 注意事项

- 改完 `tools/builtin/wiki_tools.py` **必须同步随包副本**（`src/assets/tools_builtin/wiki_tools.py`，本次已同步）
- `/reload tools` 热加载生效（无需 /restart）
- ctx 注入：`agt_register(ctx)` 覆盖 `_WORKSPACE`（引擎扫描时传真实 workspace，见 [tool-externalization](tool-externalization.md)）
- ⚠️ ctx 只作用于扫描器加载的模块实例；别处直接 `import wiki_tools` 拿到的是新实例（开发期误报多源于此）

## 相关页面

- [工具外置体系](tool-externalization.md) —— wiki_tools 是外置件清单成员（纯函数整体外置形态）
- [工具外置判别标准](../architecture/tool-externalization-criteria.md) —— wiki 为何能外置：自写自读（真限界上下文）
- [wiki_auto_maintenance](wiki-auto-maintenance.md) —— 章节四件套的主要消费者链路（update_wiki → wiki-updater）
- [wiki_auto_query](wiki-auto-query.md) —— 读侧用户（before_turn 自动检索）
