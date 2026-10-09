# session 分支机制 · /branch——从任意轮带记忆分出支线（v1 dc92c32 → v2 b37709e 链式 → v3 19d0c4e 平铺化）

> 会话记忆是线性的，但任务不总是。主线（如 Agt 框架的自我版本迭代）积累了上千轮的项目架构认知；支线任务（B 站技术向视频、写简历、协助其它 repo 实例写 sub-agent、编写 MCP）想复用这份记忆作为经验，却不能把支线低相关上下文灌回主线稀释注意力。`/branch` 从当前会话第 N 轮处**引用式**分出一个支线 session：读侧合成完整记忆、写侧天然隔离。v2 起支持**支线上再分叉**（链式基底，记忆逐级继承）；**v3（2026-10-09）平铺化**——分支与主线平级放在 sessions 根下，目录名即 id、`branch_of` 单段父引用（相对路径即分支关系），不再有 `branches/` 目录。

## 职责与动机（用户提案原文场景）

- **主线**：当前 repo 当前 session 的主任务——Agt 框架不断自我迭代，记忆 = 对项目核心架构的认知能力
- **支线**：与主线无关但需要这份认知的任务——继承主线的架构理解，又不污染主线记忆
- **目录形态（用户设计，v3 收敛）**：分支 = sessions 根下的**平级目录**（`sessions/a` 主线、`sessions/b` 分支、`sessions/c` 嵌套），分支关系由 `meta.branch.branch_of`（单段父目录名）表达——**相对路径即分支关系**（2026-10-09 用户裁定）
- 实现**只读引用、不复制**：支线不拷贝主线历史文件，load 时按需合成——零复制成本、基底可校验
- 形态演进：v1/v2 分支住 `<主线>/branches/<名>/`（物理嵌套），v3 平铺化后该形态只作迁移残留兼容；**唯一例外是 toollog**——它自 2026-10-09 起是**全 repo 共享单文件**（见下文），不再是「每 session 一份」

## 用法

```
/branch 项目社交卡片制作        ← 从当前会话（主线或支线）开支线，继承全部记忆（如 1380+ 轮）
/branch 协助写简历 600          ← 只继承当前会话 events 前 600 行（自选切点）
  ↓ 在分支上干支线活——新轮只写分支自己的 events.jsonl
/resume a                      ← 目录名（字母 id）直达（v3 起目录名即 id；name 也行）
/resume 主线名                  ← 回主线（记忆原地未动）
/resume 项目社交卡片制作         ← 纯分支名，或「主线名/分支名」（主线段仅用于消歧）
/merge [count]                 ← 把分支自己的最后 count 轮合回**根主线**（分支保留不删）
```

- 继承源 = **当前所在会话自己**的 events.jsonl：主线上分叉 = 主线前 N 行；支线上再分叉 = 该支线前 M 行（上游基底由 load 链式自动补上，见 v2/v3 章）
- 新分支目录 = `sessions/<新字母 id>/`（与主线平级），`branch_of` = 当前会话目录名；创建回执打印 `id=b、目录 sessions/b、调用 id b-N`
- `/list` 与 WebUI 会话下拉显示**完整链**：`主线名 ⇢ 分支名 [⇢ 二级支线]`（`display_chain` 父链 + 本名拼接）
- `/resume` 解析顺序：路径 → 时间戳目录名 → `name` → **目录名字母 id** → 分支名（纯名 / `主线名/分支名` / 旧 `主线id/分支id` 残留）→ 旧扁平回退

## 目录与元数据

v3（2026-10-09 平铺化）后的实际布局——**分支与主线平级、目录名即 id、相对路径即分支关系**：

```
~/.agt/repos/<repo-key>/sessions/
  a/                    # 主线（目录名 = 字母 id，全局唯一）
    agents/<子id>/      # 子 Agent 嵌套 session（同样挂在本目录下）
  b/                    # 一级分支（meta.branch.branch_of = "a"）
  c/                    # 二级分支（meta.branch.branch_of = "b"）
  toollog.jsonl         # repo 共享一份（见下文 toollog 共享单文件章）
```

（旧形态 `<主线>/branches/<分支名>/` 为迁移前残留——读侧兼容、不再产生）

- **不再有 `branches/` 目录**：`/branch` 直接在 sessions 根下建新目录，`branch_of` 只写**单段父目录名**
- **目录名 = session id = call_id 前缀 = `branch_of` 取值**：同一套字母 id（`a`/`b`/`c`/…/`aa`），`/resume a` 直达（`_resolve_session_path` 有目录名直查通路）
- 每 session 目录内：`events.jsonl` + `llm_calls.jsonl` + `meta.json`（**toollog 已迁出**为 repo 级共享文件）
- 子 Agent 目录 = `<主线>/agents/<agent_id>/`，可多层嵌套（`agents/<a1>/agents/<a2>/`）；`_sessions_root_of` 沿父链找 `sessions` 目录，四形态统一

分支 meta.json 顶层带 `branch` 字段（主线/普通会话**不写该字段**，零侵入；`save()` 中 `if self.branch_meta` 才落）：

| 键 | 语义 |
|---|---|
| `branch_of` | **单段父目录名**（v3）：一级 = `a`；二级 = `b`（父本身就是分支）；由 `_resolve_base_dir` 逐段解析，缺段时按 `created_at` 反查自愈 |
| `branch_id` | 本分支的字母 id（= 目录名，call_id 前缀即用它） |
| `inherit_lines` | 继承**该层** events.jsonl 前 N 行（不带 N = 全部） |
| `base_hash` | 该层前 N 行内容的 sha256[:16]（`_events_fingerprint`，创建时算、加载时校验） |
| `display_chain` | **父的链**（一级 = 主线名；二级 = `主线 ⇢ 一级`）；`list_sessions` 拼成 `<父链> ⇢ <本分支名>` 显示 |

读取侧守卫：仅新文件夹结构（`path.name == "meta.json"`）支持分支；旧扁平迁移路径无主线目录可指。

## 机制：引用式合成 + 写侧隔离

**读侧（Session.load）**：meta 带 `branch` 字段 → 沿 `branch_of` 链逐层取该层 events.jsonl 前 N 行（`_branch_chain_bases`）+ 分支自己的事件行 → **拼成完整事件流喂现有重放器**——投影 / tier / 折叠引擎**零感知零改动**，分支里看到的历史与「主线真的跑到第 N 轮」完全同构。基底合成提为**无条件前置步骤**（新分支还没有自己的 events.jsonl，首次 load 也要有基底记忆）。

**写侧**：分支的 `events.jsonl` / `llm_calls.jsonl` 句柄只绑分支目录，追加天然隔离——测试实测主线 events.jsonl **字节级不变**。

**toollog 是唯一共享的写侧**（2026-10-09 二轮定稿）：一个 repo 一份 `sessions/toollog.jsonl`，所有 session（主线 / 分支 / 子 Agent）的调用都 append 进去，`call_id` 自带归属——**写侧共享、语义隔离**，不再有分支物化 / 合载 / 过滤那一套（详见 [toollog 共享单文件](#toollog-共享单文件一个-repo-一份call_idsession_id-n2026-10-09-二轮定稿commits-1c320c8--f0d1e92)）。

**周边流**：`llm_calls` / `recaps` 仍按「基底各层 + 分支自己」逐层加载（`_chain_dirs`，每层 `_load_recaps(dir)` 使合成流 idx 与 recap 对齐）；toollog 已共享、**无需合载**——基底轮的工具调用完整详情（`get_tool_detail` / `agent_query_tool_detail`）、recall_turn、折叠摘要全部照常可用（旧 call_id 形态 `c1` / `m1000-c1` 兼容可查）。

### 基底指纹：base_hash 漂移校验

`_events_fingerprint(events)`：逐事件 `json.dumps(sort_keys=True)` 规范序列化后 sha256 取 [:16]。`/branch` 创建时对继承层前 N 行算出写进 meta；`Session.load` 用同一函数校验（每层独立校验）：

| 层现状 | 行为 |
|---|---|
| 指纹一致 | 正常合成 |
| 该层被 rewind 重写（指纹漂移） | **告警仍继续合成**——记忆尽力保留 |
| 某层目录整个没了（挪走/删除） | 该层降级为空，**不断链**（下游剩自己可用的层） |
| 某层目录**改名**（时间戳名 → 字母 id） | `_resolve_base_dir` 按 `meta.created_at` 反查自愈，并把 `branch_of` 回填成现路径（2026-10-09） |

## v2：支线上再分叉——链式基底合成（2026-10-08 用户提案，commit b37709e）

**问题（用户原话）**：「如果已经在支线了，再创建支线的时候是把所在支线 copy 一份后把分支切过去对吧？」
答案：**语义等价**（带全链记忆过去），实现仍是**引用式链**而非物理 copy——主线与上游支线保持唯一事实源、零冗余、每层可校验。

### 链式形态

（**路径图按 v3 平铺形态改写**；v2 时代曾写成 `<主线>/branches/A`、`<主线>/branches/B` 物理嵌套，已废弃）

```
主线（如 1380 轮，sessions/a）          ← 唯一事实源，被各分支共享
  └─ /branch A → sessions/b            # 一级分支（branch_of = "a"，继承主线前 N 行）
      └─ /branch B → sessions/c        # 二级分支（branch_of = "b"，继承 A 自己 events 前 M 行）
```

- 缩进只表达**语义上的父子**（由 `branch_of` 单段引用表达），物理上全部平铺在 sessions 根下
- B 的 load 记忆 = **主线 events[:N] + A events[:M] + B 自己新增**（三级拼接，喂同一重放器）
- recall / 工具详情召回跨级全命中（场景⑥实测：二级支线 recall 到主线内容与一级支线内容）

### 两处实现改动

（下表记录 v1→v2 的当时差异；`branch_of` 的取值形态在 v3 又简化为**单段父目录名**，见 v3 章）

| 位置 | v1 | v2 |
|---|---|---|
| `meta.branch.branch_of`（src/commands.py） | 主线目录名 | **相对 sessions 根的路径串**：一级=`<主线ts>`；二级+=`<主线ts>/branches/<支线名>` |
| `Session.load` 基底合成（src/session.py） | 单层（主线前 N 行） | `_branch_chain_bases()` 沿 `branch_of` **逐层上溯到根**（叶→根收集、根→叶返回），每层带自己的 `events[:inherit_lines]` + `base_hash` |

- 继承源改为「当前会话自己的 events.jsonl」：主线上分叉=主线前 N；支线上分叉=该支线自己 events 前 M（其上游基底由 load 递归补上）
- 每层 llm_calls / recaps 逐层合载（`_chain_dirs`）；每层 `_load_recaps(dir)` 使合成流 idx 与 recap 对齐（**toollog 的逐层合载 v3 已废除**——改为 repo 共享单文件）
- 目录定位：~~`sessions_root = sdir.parents[2]`、`top_dir = sdir.parents[1]`~~——v2 的嵌套层数假设，**平铺后必错**（会算到 `~/.agt/repos/`）；现行统一 `_sessions_root_of(sdir)` + `_resolve_base_dir()`，见 v3 章

### 边界（`_branch_chain_bases` 三重防御）

| 情形 | 行为 |
|---|---|
| 某层 events 缺失 / `inherit_lines=0` | 该层降级为空 + 日志警告，**不断链** |
| 链上某层 meta 不可读 | 停止上溯（已收集层仍生效） |
| 环 / 超深（>8 层） | 循环上限 8 层截断 |

### 显示链：display_chain

创建时写入真值（父链 + `⇢` + 本分支名，如 `主线测试 ⇢ 测试分支 ⇢ 二级支线`）；`list_sessions` 优先读它（缺失回退 `主线名 ⇢ 分支名`），WebUI 会话下拉同链可见——支线上再分叉不再只看到最后一段。

## v3：分支平铺化——相对路径即分支关系（2026-10-09，用户裁定，commit 19d0c4e）

**用户原话**：「感觉整个切分支现在还是比较混乱，这样吧，分支目录别放在 `branches/` 目录里了，直接就放在根目录里，**相对路径即分支关系**」。

**新布局**（对照 v2 的 `<主线>/branches/<名>` 物理嵌套）：

```
sessions/
  a/        # 主线（目录名 = 字母 id，全局唯一）
  b/        # 一级分支   meta.branch.branch_of = "a"
  c/        # 二级分支   meta.branch.branch_of = "b"（父本身就是分支）
  toollog.jsonl
```

| 维度 | v2（嵌套） | v3（平铺，现行） |
|---|---|---|
| 分支目录 | `<主线>/branches/<名>/` | `sessions/<字母id>/`（与主线**平级**） |
| `branch_of` | 相对 sessions 根的**路径串**（`<主线ts>` / `<主线ts>/branches/<支线名>`） | **单段父目录名**（`a` / `b`，父可为分支→链式逐层） |
| 目录名 | 时间戳 / 分支名 | **字母 id**（= session id = call_id 前缀 = `branch_of` 取值，三层同一套字符串） |
| `display_chain` | 存**全链**（含自己） | 存**父链**（不含自己），显示侧拼 `<父链> ⇢ <本名>`——否则平铺下分支名丢失（显示成父的名字） |
| `/merge` 目标 | 直接父层 | 沿 `branch_of` 链上溯到**根主线**（二级分支的中继自动跨过） |

**实现要点**（改动面：`src/session.py` + `src/commands.py`）：

| 件 | 说明 |
|---|---|
| `_sessions_root_of(sdir)`（src/session.py） | 从任意 session 目录反查 sessions 根：**沿父链向上找名为 `sessions` 的目录**（初版 ≤5 层，共享 toollog 五轮放宽到 **≤12 层**——多层嵌套子 Agent 目录也够得到），兜底旧行为。四形态统一——主线 / 平铺分支 / 旧 branches 嵌套 / **子 Agent**（`<主线>/agents/<agent_id>/`）。共享 toollog 定位的基石 |
| `_resolve_base_dir(sessions_root, bo)` | 基底层目录解析 + **失链自愈**：逐段解析 `bo`；某段不存在且形如旧时间戳目录名（`YYYYMMDD_HHMMSS`）→ 按 `meta.created_at` 反查现目录（「目录名即 id」改造会改名既有目录，`branch_of` 字符串还指着旧名）；解析失败返回 None → 调用方降级为空基底、不断链 |
| `commands._cmd_branch` | 新分支建在 sessions 根下（`branch_of = sdir.name` 单段）；回执 `→ 分支「X」（id=b，目录 sessions/b，调用 id b-N）` |
| `commands._cmd_merge` | 沿 `branch_of` 链（`_resolve_base_dir` + 跳数上限 8）上溯到「meta 无 `branch` 字段」的根主线再 append |
| `session.list_sessions` | 扫**根下全部目录**（不再只扫主线 + `branches/`）：主线条目 + 平铺分支条目（`display_chain ⇢ name` 拼接、`branch: true` 标记），旧嵌套残留顺带列出 |
| `_find_branch_dir_by_name` | `/resume` 分支定位三形态：纯名（根下按 `meta.name` 全局搜、重名取最新） / `主线名/分支名`（主线段仅消歧） / 旧 `主线id/分支id`（`branches/` 直查兼容） |
| `_resolve_session_path` | 新增**目录名直查**通路——`/resume a` 直达 `sessions/a/`（目录名与显示名脱钩后曾 FileNotFoundError） |

**平铺化当场抓掉的真 bug**：`sdir.parents[2]`（v2 时代「sessions 根 = 上两级」的嵌套假设）在平铺形态下会算成 `~/.agt/repos/` 而非 sessions 根——`_branch_chain_bases` 调用点与失链回填的 `relative_to` 两处都错 → **基底解析全废**；统一收敛到 `_sessions_root_of(sdir)`。另清掉 `top_dir`/`top_ts` 残留引用 ×3（NameError，L1 漏网）。

**生产迁移（当日实盘）**：`bug诊断` 由 `<主线>/branches/bug诊断` 迁到 `sessions/b`，`branches/` 空目录清除；`/resume bug诊断` 与 `/resume b` 均可回。

**验证（e2e 七场景全绿）**：①一级分支平铺（`sessions/b` · `branch_of='a'`）②二级分支平铺（`sessions/c` · `branch_of='b'`）③链式合成（二级 load = 根 4 轮 + 一级 2 轮 = 6 轮）④`list_sessions` 三条平铺 + `display_chain` 拼接 ⑤`/resume` 三形态（纯名 / id / 主线名/分支名）⑥`/merge` 二级分支轮 append 根主线（b 中继自动跨过）⑦根下无 `branches/` 目录。

> 说明：下游 v2 各节里的 `branches/<名>` **路径图是 v2 时代形态**——链式合成语义（基底逐层拼接 / 三重防御边界 / `display_chain`）不变，只有目录与引用形态换了。

## toollog 共享单文件：一个 repo 一份、call_id=「&lt;session_id&gt;-N」（2026-10-09 二轮定稿，commits 1c320c8 + f0d1e92）

**症状（用户实锤）**：「a / b / c 三个 session 的 `toollog.jsonl` 都是 41M……b 和 c 不是 a 的分支吗？为啥 toollog 全量 copy 过去了？」

**根因**：分支创建/load 时把基底各层 toollog 记录**物化进分支自己的文件**（每 session 一份 `toollog.jsonl` + 无前缀分支 = 40MB 基底全量落盘），一级分支拷一次、嵌套分支再拷一次。

**中间方案（当日一轮，已推翻）**：分支 call_id 加锚点前缀（`m1000-c1`）+ `_foreign_ids` 合载过滤（内存保留、不物化）——补丁太厚：物化 / 合载 / `_mine` 过滤 / `call_prefix` 锚点链 / `/merge` 搬运 / rewind 重写，五套机制全是治标。

**终版（用户二轮定稿）**：**一个 repo 一份 `sessions/toollog.jsonl`**，所有 session（主线 + 各分支 + 子 Agent）的工具调用都 append 进去，`call_id = "<session_id>-N"`（`a-1024` / `b-35`）——**归属从 id 直读、跨 session 物理不撞**，合载/过滤/搬运全部不再需要。

| 件 | 说明 |
|---|---|
| `ToolLog(prefix=...)`（src/toollog.py） | `_prefix` = 本 session 的 call_id 前缀；`next_id()` → `<prefix>-N`；无前缀（旧路径/未绑定）退 `cN` 兼容 |
| `_call_prefix_of(sdir)`（src/session.py） | 前缀 = **相对 sessions 根的路径去掉容器段**（`agents`/`branches`）后以 `-` 连接：主线/平铺分支 `a`/`b`；子 Agent `a-coder`、`a-wiki-updater_2`；旧嵌套 `a-b`。**五轮定稿**——初版直接用 agent_id，不同主线的同名子 Agent（`a/agents/coder` 与 `b/agents/coder`）撞同一 `coder-1`，共享文件下后者覆盖前者、counter 互顶 |
| `_own_counter_from(call_id)` | 只数**本前缀**的序号——共享文件里别的 session 记录不顶本 counter；兼容旧形态 `c1`（2026-10-09 前，per-session 计数）与 `m1000-c1`（锚点前缀时代） |
| `_shared_toollog_path(sdir)`（src/session.py） | `_sessions_root_of(sdir) / "toollog.jsonl"`——`_bind_persistence_paths` / `Session.load` / 旧扁平迁移三处统一走它 |
| `set_path` / `_append_line` | 文件不存在才 flush 建立，存在即假定已 load（**不重写**）；record 一行一次 append（O(1)） |
| rewind（`_rewrite_persistence`） | **不再重写 toollog**——append-only 是共享契约，重写会误删别的 session 记录；rewind 只截 events（+ recaps），被回退轮的多余记录留着无害（call_id 不撞、`get` 直查仍可用） |
| `/merge` | 不再搬运 toollog——同一个文件，轮/事件 append 即可 |
| `list_tool_logs` | 只列**本前缀**的 id（共享文件里别的 session 记录不显示） |

**子 Agent 同享一份（2026-10-09 四轮，commit f0d1e92）**：子 Agent 也是 session（`<主线>/agents/<agent_id>/`，可多层嵌套），同样 append 进 repo 共享文件。为此把 `_sessions_root_of` 从「按相对层数判形态」改成**沿父链向上找名为 `sessions` 的目录**（层数放宽到 12）——否则子 Agent 目录会被算成 `<主线>/agents/`，共享文件错位写进 `agents/` 下（各子 Agent 互串、不进 repo 共享文件）。

**数据迁移（实盘）**：41 repo / 62 份 per-session toollog → 各 repo 一份共享文件、残留 0 份；含子 Agent 共 18 repo / **221 份（105.6MB）** → 各 repo 一份。用户那例：a/b/c 三份 41MB → **一份 40.6MB**（43779 行 → 去重后 14724 条，省 29055 行）。

> ⚠️ **如实记录一处损失**：子 Agent 的旧记录 id 是**裸 `cN`**（旧代码与主线撞号的历史缺陷），迁移按 `call_id` 去重时子 Agent 的旧记录被主线同 id 记录挤掉 → **部分子 Agent 历史工具详情会张冠李戴**（不崩，但显示成同 id 的别的调用），原件已删不可还原。新前缀（`a-coder-N`）从机制上杜绝撞号。教训：迁移去重前应先备份、或同 id 两份都保留。

**消费端不变**：`get_tool_detail` / `agent_query_tool_detail` / [recall_turn](recall-tools.md) / 折叠摘要都按 call_id 直查同一份文件；旧 `c1` / `c3309` 式 id 兼容可查。完整机制见 [toollog · 工具调用详情库](toollog.md)。

## v0.34.1 收尾配套：历史展开卡死三连修 + 越界 tier 状态修剪（2026-10-09，随 v0.34.1 发布）

与平铺化 / 共享 toollog 同批打包的两件收尾（发布摘要口径，详见 [v0.34.1 发布记录](../releases/v0.34.1.md)）：

- **历史展开卡死三连修**：大 session / 分支链的历史展开偶发卡死，三处合围——①**失链自愈**（`_resolve_base_dir` 按 `created_at` 反查回填，见上文基底指纹与 v3 章）；②**钳制**（异常超大批次的钳制防护，不再拖死展开）；③**前端死条超时**（加载占位行超时自动释放，不再永久转圈）
- **越界 tier 状态修剪**：分支基底降级 / 失链后，持久化的 tier 状态可能越界（指向超出实际合成事件流的位置）——load 时修剪越界项，投影边界与真实事件流重新对齐

## WebUI 切会话 UI 自动刷新：会话身份检测 → 广播（2026-10-08，用户提案，commit 0192d33）

**用户提案**：WebUI 输入 `/branch 社交卡片设计` 创建分支后，UI 毫无变化——「切分支的时候要不就自动刷一下浏览器吧」。

**根因——前端对会话切换零感知**：`/branch` 的 `set_session` 在 WS 斜杠命令 dispatch 里同步执行，跑完只 `_send` 命令回显文本——`session_history`（历史区）与 `sessions`（会话下拉）都没有广播，前端停留在主线视图，看起来像命令没生效。

**修复：会话身份检测 → 自动刷**（src/server.py + src/static/index.html）：

| 件 | 说明 |
|---|---|
| `_refresh_ui_if_session_switched(agent, pre_sess_id)` 新 helper | dispatch 前后对比 **session 实例身份**（`id(agent.session)`）——变了 = `set_session` 换了实例：`broadcast_session_state`（session_history + team_list + spec）+ 广播 `sessions` 列表（`list_sessions`，新分支即刻进下拉） |
| 两处 dispatch 接线 | 插话兜底路径 + 主路径两处 dispatch 前捕获 `_pre_sess = id(agent.session)`，dispatch 与回显之后调用刷新 helper——沿袭「两处同改」纪律（见 [WS 斜杠命令回显清洗](user-interaction.md#ws-斜杠命令回显清洗系统气泡混入-cli-spinneransi-噪音redirect_stdout-进程级全局2026-10-08用户实锤commit-9343107)） |
| 前端下拉后缀匹配 fallback | 分支 history 事件的 `sid` 是分支目录名（`社交卡片设计`），下拉里分支项 id 是复合形态（`主线ts/社交卡片设计`）——精确匹配 miss 落回旧主线。补 suffix 匹配（`o.value.endsWith('/'+_curSid)`），切分支后下拉自动选中并指向新分支项 |

**通用性**：机制不认具体命令——自动覆盖 `/branch`、`/resume`、`/reset` 等所有切会话命令；在分支上正常聊天时 session 实例不变（`id` 恒定），不会多余广播。且不走浏览器 reload——纯 WS 广播：`/branch` 一敲，历史区切到分支视图（基底合成 + 新轮）+ 下拉自动选中分支 + 系统气泡显示创建回执，三件套齐活。

**生效方式**：引擎层（src/server.py）+ index.html，需 `/restart`；commit `0192d33` 已推送，site-packages 已同步。

**关联**：[用户交互 · /reset 清空后新会话立即可见](user-interaction.md#reset-清空后新会话立即可见--会话下拉框跟随当前会话2026-09-23用户实锤commit-3350a68)（同族「切会话 → 列表/下拉可见性」，本节是其泛化）、[用户交互 · /restart 重启双坑](user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc)（`broadcast_session_state` 自该处提取公共化，本节是第三条消费路径）。

## 调试中抓到的两个坑

1. **新分支首载零基底**：基底合成最初写在 `if events_path.exists()` 分支内——新分支**还没有自己的 events.jsonl**，条件恒假 → 首次 load 记忆全空（场景②测试抓的）。修复：基底合成提为无条件前置步骤（v2 的链式合成同样在 events 判断之前）。
2. **主线目录定位**：`sdir.parent / branch_of` 与 `parent.parent` 都不对（目录嵌套层级随部署形态变）。修复：**从 sessions 根按 `branch_of` 名遍历解析**（`_find_session_dir_by_name` 同款），场景④实测主线目录挪走后正确降级。v2 升级为路径串解析（`sessions_root / branch_of`），仍需 `parents[2]` 定位 sessions 根。

## 验证

- 37 项场景测试全绿（v1 28 项含写隔离字节级比对、四场景定位链；v2 加场景⑥ 链式嵌套分叉：`branch_of` 相对路径 + `display_chain` 完整链 + 二级支线 recall 跨级命中主线与一级支线内容）
- L2 隔离实跑（regr-9678）：分支实例启动 + 钩子 + react 全程无 traceback
- **v3 平铺化 e2e 七场景全绿（2026-10-09，commit 19d0c4e）**：一级/二级分支平铺、链式合成（根4轮+一级2轮=6轮）、`list_sessions` 三条平铺、`/resume` 三形态、`/merge` 二级分支轮 append 根主线、根下无 `branches/`（详见 v3 章）
- **共享 toollog 验证（2026-10-09，commit 1c320c8）**：e2e 四项（共享路径 / 形态 `a-1`·`a-2`·`b-1` / counter 各数各的——重载后 `a-3`、`b-2` / list 只列本 session）+ 真实库（主线 prefix=`a`、分支 prefix=`b`、旧 id `c1`/`c3309` 可查、投影 4241 msgs 正常、两 session 同指 `sessions/toollog.jsonl`）
- commits `dc92c32`（v1）+ `b37709e`（v2 链式嵌套）+ `3598be3`（is_branch 钩子短路）+ `19d0c4e`（v3 平铺化）+ `1c320c8` / `f0d1e92`（共享 toollog）已推送；site-packages 已同步（**pip 实例需 /restart 生效**）
- **发布通道**：v1/v2 + is_branch 随 v0.34.0 发布（2026-10-08）；**v3 平铺化 + 共享 toollog + 收尾配套随 v0.34.1 发布（2026-10-09，commit 720f64a，[发布记录](../releases/v0.34.1.md)）**——`pip install -U agt-agent` 即得，pip 实例 /restart 生效

## 与其它模块的关系

- [检查点快照与回溯](snapshot-rewind.md)：主线 rewind 重写 events → base_hash 漂移告警（校验即为此设计）；**rewind 不再重写 toollog**（共享 append-only 契约）
- [中断轮恢复](resume-interrupted.md)：/resume 体系入口（解析顺序：路径 → 时间戳目录名 → name → **目录名字母 id** → 分支名 → 旧扁平回退）
- [toollog · 工具调用详情库](toollog.md)：repo 共享单文件 + `call_id=<session_id>-N`——分支/子 Agent 的工具详情从 id 直读归属
- [recall_turn](recall-tools.md)：基底轮召回依赖 toollog call_id（共享文件直查，跨级全命中）
- [上下文引擎](../architecture/context-engine.md)：投影/tier/折叠零感知——分支只是「另一份事件流」喂进同一重放器
- [运维与排障](../guides/ops.md)：sessions 存档树现状 + 单实例约束
- [wiki 自动维护](wiki-auto-maintenance.md)：`before_answer`/`turn_end` 钩子 context 注入 `is_branch` → check_changes 节点短路——**支线干活 wiki 维护静默**（只有主线轮触发）；支线产出的 wiki 知识回主线后由主线轮自然维护

## 注意事项

- 分支是**只读引用主线历史**：主线后续新增轮次不会自动进分支（继承边界固定在创建时点的 N 行）
- 支线的产出想回主线需显式操作（`/merge`，或让主 Agent 消化支线结论）——机制本身不自动回流，这正是「不稀释主线注意力」的语义
- **链式嵌套时上游不可缺**：删/挪上游目录会让链上所有下游分支一起降级（各层基底降为空，只剩自己的轮）；链上限 8 层，超深或成环自动截断。**目录改名不致病**——`_resolve_base_dir` 按 `created_at` 反查自愈并回填 `branch_of`
- 仅新文件夹结构支持；旧扁平迁移存档无分支能力
- **toollog 是 repo 共享资源**：分支/子 Agent 的历史工具详情来自同一份 `sessions/toollog.jsonl`——删它等于全 repo 失去工具详情（events 仍在，投影里的工具结果会显示「（详情已失效）」）；**只 append、不重写**（rewind 也不碰它）
- 一个 repo 只支持**单实例**运行（存档文件无跨进程锁，双实例会互踩；见 [ops · 同 repo 单实例约束](../guides/ops.md)）
- 改动在 `src/session.py`（`_sessions_root_of` / `_call_prefix_of` / `_resolve_base_dir` / `_branch_chain_bases` / 合成 / 指纹 / list_sessions）+ `src/commands.py`（/branch、/merge、/list、/resume 解析）+ `src/toollog.py`（prefix / 共享 append）+ WebUI 会话下拉；生效需 /restart

## 相关页面

- [toollog · 工具调用详情库](toollog.md)：repo 共享单文件的完整机制（call_id 三代形态 / 消费端 / 迁移教训）
- [运维与排障 · 存档布局](../guides/ops.md)：`sessions/` 目录全景（主线/分支/子 Agent/共享 toollog）
- [检查点快照与回溯](snapshot-rewind.md)：主线被重写的漂移来源
- [上下文引擎与缓存优化](../architecture/context-engine.md)：被「零感知」的投影/折叠引擎

