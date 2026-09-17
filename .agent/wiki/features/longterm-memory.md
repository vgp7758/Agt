# 长期记忆 · 三类记忆 × episodic 召回三代演进 × ensure_ltm 共享单例

> 存储/检索核心 `src/longterm_memory.py`（支撑层，见 [系统总览](../architecture/overview.md)）；Agent 工具面五件套（外置件 `tools/builtin/ltm_tools.py` 注册）；WebUI `/memory` 管理页（`src/server.py` 路由）。
> **blog 04（记忆系统篇，2026-08-21 扩写完成 ~4000 字，结构：问题 → 三类框架 → 最难一类深度演进 → 写入侧设计 → 存储 → 边界 → 收尾）的三类×三种注入框架仍成立，但写于 provider 直调时代**——本页记录其后的架构演进，校对/续写 blog 04 以此为准。

## 职责：三类记忆 × 三种注入

| 类型 | 注入方式 | 说明 |
|------|---------|------|
| semantic | 常驻 | 每轮固定注入（用户偏好/背景等稳定事实） |
| episodic | 按召回 | 检索命中才注入，投影中呈 `[epi·长期记忆]` 行（tail ambient 段，见 [上下文引擎](../architecture/context-engine.md)） |
| procedural | 标题常驻 + 正文按需 | 只注入标题清单，Agent 需要时 `read_procedure(id)` 取全文——**刻意的 token 经济设计**：详情按需读取，避免记忆本身膨胀上下文 |

Agent 工具五件套：`add_memory` / `search_memory` / `read_procedure` / `update_memory` / `delete_memory`（blog 04 只提了 `add_memory`，已过时）。2026-08 起由外置件注册（见下）。

## ensure_ltm 模块级单例（2026-08，commit fd06c48）

`src/longterm_memory.py` 顶层 `_ltm_instance` + `Lock`，`ensure_ltm(workspace=None)` **线程安全惰性单例（双 checked locking）**：

- **per-workspace**：同 workspace 全进程共享一个实例；workspace 变化时重建；省略 → 沿用现实例的 workspace（无实例则 `Path.cwd()`）
- **谁在共享**：`Agent.__init__` 走 `ensure_ltm(self.session.workspace)`——**主/子 Agent 同实例**；外置工具 `tools/builtin/ltm_tools.py` 注册的五件套也走它
- **为什么必须单例**：LongTermMemory 带内存缓存，注入 provider 与工具各持一份会**缓存分裂**——工具写了一条记忆，provider 下一轮看不到
- **origin_session 元数据的轻量握手**：`_ltm_static_block`（provider，每轮必被调）顺带刷新单例的 `_origin_session = self.session.name or ""`；外置 add_memory 发生在轮内，读到的必是当前会话——不用把 session 名塞进工具参数

## 静态层投影快照缓存：add_memory 只落盘，投影到归档点才刷新（2026-09-17，用户提案，commit 12c7594）

**用户提案（2026-09-17，commit `12c7594`）**：「当前 add_memory 时会有断缓存的情况……add_memory 的时候只落盘但投影时先渲染旧的吧，直到触发需要 system 归档的时候再重读并更新长期记忆的投影信息」——semantic 常驻层此前每轮全量渲染（replace 语义），轮中途记忆更新 → 下次投影 ltm 段文本变化 → 从序列头断缓存、全序列重算。

## 机制（src/agent.py `_ltm_static_block` + src/session.py 归一化分支）

- **投影侧快照**：`_ltm_static_block` 产出走 `self._ltm_snap` 快照——epoch 未变直接返回上次文本（byte-stable，ltm 段前缀不动）；`session._ltm_refresh_epoch` 变化才重读记忆库刷新
- **失效信号**：`_ltm_refresh_epoch` 在 **system 归档/归一化点**自增（session.py 归一化分支顺带自增）——归一化本就断缓存（[append-not-replace 的断点清账](../architecture/context-engine.md)），快照刷新**搭归档断点的顺风车，零额外断点**
- **换档重置**：`set_session` 时 `self._ltm_snap = None`——切换 session 后首投影强制重读（记忆库已换，快照必失效）

## 两个边界

- **`_origin_session` 握手不受影响**——快照缓存的是**文本**，provider 本身仍每轮被调，顺带刷新单例 `_origin_session`（外置 add_memory 读它记来源会话，机制见上节）；add_memory 发生在轮内，读到的必是当前会话
- **episodic 层不动**——按 query 每轮召回、注入 tail 位置（本来随轮变化，在缓存未命中区），与静态层的常驻语义不同

## 验证（模拟四场景全绿）

| 场景 | 期望 | 结果 |
|---|---|---|
| 三次连续投影 | static_block 实读 1 次（快照生效） | ✓ |
| 记忆更新落盘（v1→v2） | 投影仍 v1（byte-stable） | ✓ |
| epoch+1（归档点） | 投影变 v2（到点刷新） | ✓ |
| `set_session` 换档 | 快照重置、首投影重读 | ✓ |

**效果**：中途 add_memory 的那几轮缓存全程连续；新记忆最晚在下一次 system 归一化（通常伴随毕业/升档/折叠）时进入投影。`/restart` 生效。

## 外置件：ltm_tools.py（第四批，注册外置 + 实现留框架）

rag 同款混合形态（判别标准四象限里 ltm 属真限界上下文，但实例被 provider 共享 → 只外置注册）：`agt_register()` 里 `import longterm_memory as lm` 注册五件套；函数本体留框架经 ensure_ltm 共享实例。`make_ltm_tools` 工厂删除、chat.py 装配线清理；`/reload tools` 热加载。详见 [tool-externalization-criteria · ltm 边界裁剪](../architecture/tool-externalization-criteria.md)。

## episodic 召回：三代演进（provider 直调 → 检索工作流）

| 代 | 方案 | 问题 / 突破 |
|----|------|------------|
| 第一代 | 标点分词 + 子串匹配（provider 直调 `episodic_block`） | 中文长句整段一个 token，命中率"基本靠缘分" |
| 第二代 | 本地 3B 提关键词 | **量力分工**：3B 做不了相关性判断（打分区分度差）但抽词绰绰有余 |
| 第三代 | 并入统一检索流水线（当前版） | **「该不该注入」从检索层上移到精排层**：召回管高召回、精排管高精度 |

当前版流水线（before_turn 检索工作流接管）：

```
user_message
  → LLM 提关键词（local-qwen 小模型，抽 3~8 个短词）
  → join → search_memory(type=episodic)
  → 收集为 kind="epi" 候选（与历史轮/语义检索候选同台竞争）
  → LLM 精排裁决（无关的 episodic 被过滤掉）
  → 注入 [epi·长期记忆] 行
```

- 「量力分工」与 [wiki_auto_query v4](wiki-auto-query.md) 核心结论同源：本地 3B 相关性打分区分度差（相关 0.1 / 无关 0.2 / 全 0.5），不如 embedding 余弦（0.69/0.42/0.16）；小模型只该干提词的活
- provider 路径（`episodic_block`）降级为**工作流不存在时的兜底**；旧直调示例已从博客删除
- 与 [wiki_auto_query](wiki-auto-query.md) 同挂 before_turn 钩子，**并行执行**（ThreadPoolExecutor，见 [workflow-hooks](../architecture/workflow-hooks.md)）——两条检索流水线同族不同料（wiki 检索 vs 记忆检索），博客第 3/4 篇互相引用成系列
- 活例子（2026-08-21 本 session）：before_turn_retrieval 刚好检索出「第 106 轮：上次 blog 补充的讨论」——跨 session 经验回流的直接体感，可作 blog 04 开篇引子

### 设计迁移：该不该注入，从检索层上移到精排层

episodic 召回并入统一检索流水线后，与 blog 03 的检索工作流共享「宽进候选 → 严出裁决」骨架：

- **召回层**只负责"可能相关"（关键词宽匹配，宁可多收）
- **精排层** LLM 负责"真的相关"（无关 episodic 过滤）

误注入率显著下降——旧版 top-3 检索到什么就注入什么，没有相关性裁决。原则一句话：**召回宁滥勿缺，注入宁缺勿滥**（与 wiki_auto_query 的 top1<0.5 不注入同一原则）。

## embedder LRU 缓存包装层（2026-08，v0.19.2）

`src/rag.py` 的 `rag.embedder.encode` 加 **LRU 缓存包装**——同文本重复 embed 直接命中缓存，不再重跑模型：

- **收益场景**：检索流水线每轮对相同文本（标题、固定关键词、wiki 条目名）反复 encode；[cosine_sim](cosine-sim.md) 工具复用同一 embedder，同文本比对也直接命中
- **实测**：92 次调用**全命中**（92x）——缓存层加上后重复 embed 成本归零
- 对调用方完全透明（包装层不变签名），检索延迟与 token/算力开销同步下降

## 写入幂等：add() 同 type+title 自动更新

`add()` 遇同 type+title **更新而非新增**。Agent 判断"值得记"的标准会漂移，同一条经验可能被记两三次（实测：本 session 中 replace_lines 参数那条记忆被沉淀了两次——博客以此为反面案例）。该去重语义让 `add_memory` 工具描述敢写"**放心重复调用同主题**"——2026-08 描述加详：明确「同 type+title 自动【更新】而非重复记录」+ 值得记的典型场景清单（踩坑及解法 / 用户偏好背景 / 重要决策与原因 / 可复用流程）+ type 三类选择指引（semantic 始终注入 / episodic 按召回 / procedural 渐进披露）。

## 双主权：Agent 沉淀 + 用户管理

Agent 自主沉淀的记忆会有错（如对用户偏好的误判），记忆不能是黑盒，用户必须能看、能改、能删——没有用户主权，记忆库会退化成「Agent 的偏见库」：

- **CLI**：`/memory` 命令
- **WebUI**：`/memory` 页面——Tab 按 type 筛选 / 搜索 / 原地编辑 / 删除

「Agent 记它的，用户管着的」——双向纠错，比单向"Agent 自己记忆"完整得多。

## 存储

`~/.agt/repos/<fixed-cwd>/memories/`——cwd 斜线替换为 `-`（如 `D:\AI_Usings\Agt` → `D--AI_Usings-Agt`），一眼可辨是哪个 repo；旧 hash 目录启动自动迁移（**hash → 可读转写**的工程决策：对着 `18f8db495cec` 这样的目录名无法知道是哪个项目——这条修正本身作为小决策点写进了博客）。存档布局全貌见 [guides/ops](../guides/ops.md)。

## 注意事项（blog 04 校对清单）

| 位置 | blog 写的 | 现状 |
|------|----------|------|
| 存储路径 | `~/.agt/repos/<workspace_hash>/memories/` | `<fixed-cwd>/memories/`（hash → 路径可读化） |
| episodic 代码段 | `self.search(query, type_="episodic", limit=3)` | 代码本身仍对，但 query 来源已变（小模型提关键词，见三代演进） |
| 工具 | 只提 `add_memory` | 五件套（见职责节），且已外置注册 |
| （缺章节） | — | 建议补两节：「episodic 召回的演进：从标点分词到关键词提取」「记忆要能被用户管理」+ add 防重复沉淀一句（预计 +800~1000 字） |

## 相关页面

- [wiki_auto_query](wiki-auto-query.md)：同族 before_turn 检索流水线（wiki 检索版：本地提词 + 余弦阈值裁决，零云端 token）
- [cosine_sim](cosine-sim.md)：复用同一 embedder 的余弦相似度工具（同样受益于 LRU 缓存）
- [上下文引擎](../architecture/context-engine.md)：`[epi·长期记忆]` 行注入 tail ambient `<system-reminder>` 段
- [工作流引擎与钩子](../architecture/workflow-hooks.md)：before_turn 多工作流并行执行、检索型钩子输出纪律
- [系统总览](../architecture/overview.md)：longterm_memory.py 归支撑层、`/memory` 路由
- [guides/ops](../guides/ops.md)：存档布局（memories/ 目录）
- [v0.19.2 发布记录](../releases/v0.19.2.md)：embedder LRU 缓存随该版发布
- [工具外置](tool-externalization.md)：ltm_tools.py 外置件（第四批）
