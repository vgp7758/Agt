# Agent 管理页 · /agents + v2.1 声明格式 + `_main_` 主 Agent 纳入管理

> src/server.py（页面路由 + REST 五件套）+ src/static/agents.html（管理页）+ src/static/index.html（入口按钮）。2026-08，commit be61b69（v2.1）；`_main_` 纳入 + 入口 + 编辑器往返增强：commit 3f0ef32；列表"装配零段"修复 + 5 子 Agent 声明规范化：commit f177674。

## 职责

Agent 声明的可视化管理：**子 Agent（`.agent/agents/`，v2.1 格式）名称/描述/模型/回退链/工具/assembly/hooks 全部表单化编辑**，不再手写 YAML；**主 Agent（main.yml）置顶纳入**——直接编辑原始 assembly 清单（2026-08-31 起 repo 级覆盖：`<cwd>/.agent/main.yml` 存在则读写本地，见 [config_file 解析](../guides/config-and-models.md)）。顺带定稿 **v2.1 声明格式**（用户插话设计）：persona 拆独立 md，yml 回归纯配置。

## 页面：/agents（agents.html）

- 列表卡片 + 编辑表单两态：点卡片加载声明 → 各字段编辑 → 保存；`_main_` 置顶（读 main.yml，显示 assembly 段数 + hooks 位置）
- **入口：控件栏 🤖 Agent 按钮**（🧠 记忆右侧，新标签打开）——commit 3f0ef32 前只建了页面忘加入口
- **存量迁移**：对旧格式声明（coder/explorer/vision 等旧 .md 声明）保存一次 → 自动转 v2.1（persona 拆到独立 md）
- **与 Agent 专属对话页的路径区分**（commit 5393ee4）：`/agents`（**无 id**）= 本声明管理页；`/agents/<agent_id>` = 该 Agent 的专属对话页（同一 index.html，前端读 URL 初始化交互目标）——同前缀两条路由按路径形态区分互不冲突，详见 [用户交互 · Agent 专属页 URL 路由](user-interaction.md)

## REST 五件套（server.py）

| 端点/函数 | 语义 |
|------|------|
| `GET /agents` | 管理页 HTML（`_AGENTS_HTML`，随 memory.html 同款静态加载） |
| `GET /api/agents`（api_agents_list） | list——**`_main_` 置顶**（seed_main_agent 定位 main.yml）+ 子 Agent `load_agents_index(_workspace)`，**.yml 优先** |
| `GET /api/agents/<name>`（api_agents_get） | get——`_main_` 特判见下节；子 Agent 走 `_persona_from_decl` 双形态提取 persona |
| save / create | 子 Agent：`_dump_agent_yml` 写 v2.1；`_main_`：原样写回 main.yml（见下节）；create 拒绝 `_main_`/`main` 保留名 |
| delete | 删子 Agent 声明（.yml 与 .md 一并清理）；拒绝 `_main_` |

关键函数：

- `_agent_safe_name(name)`：名字清洗（`[^A-Za-z0-9_-]` → `_` + 取 basename），防路径注入
- `_persona_from_decl(meta, system)`：**双形态**读取——新格式 file: 引用（读独立 md）or 旧格式 text: 内联/system 兜底
- `_read_persona_md(rel_path)`：读 persona md，剥 frontmatter（`multiagent._split_frontmatter`，兼容存量旧格式声明 md 带 frontmatter 的情况）；路径基准 `_workspace` + resolve 后 `relative_to` 沙箱越界拒绝——与写盘 `_dump_agent_yml` 读写对称
- `seed_main_agent(_workspace)` / `load_agent_yml`（agent_config）：定位并读取 main.yml——管理页读主 Agent 的通道（2026-08-31 起 repo 级覆盖，commit 10d717e：`config_file` 解析，`<cwd>/.agent/main.yml` 存在优先返回；**播种仍写全局** `~/.agt/main.yml`，不覆盖本地独立主声明）

## `_main_` 主 Agent 纳入管理（2026-08，commit 3f0ef32）

### 为什么不用 persona 单框模型——主 Agent 的 SYSTEM 是 assembly 完整配方

`~/.agt/main.yml` 的 assembly（当前 17 项）不是一块静态人设，而是**人设分块与动态动作交错的完整配方**：

```
[0-4]  text    人设分块（主体 826 字 / 长期记忆使用规则 / 随包资产说明 / 步距衰减披露规则 / 多 Agent 协作规则 671 字）
[5]    text    可用模型：{func:load_models()}          ← 动态插值
[6-7]  text    工作流编排说明（XML 写法/节点速查）
[8]    tool    read_file(AGENTS.md)                     ← 动作：每轮读文件注入
[9]    tool    concat_files(.agent/rules/*.md)          ← 动作：规则目录
[10-11] text   技能清单规则 / 子 Agent 清单 {func:load_agents()}
[12-16] seg   history=tiered / ltm / user_message / steps / tail
```

**"清单即装配顺序"**——子 Agent 的简单 persona（单 md）模型不适合它，所以 `_main_` 编辑走**原始 assembly 清单**（编辑器这次全补齐往返能力，见下节）。

### REST 特判（server.py）

| 项 | 行为 |
|---|---|
| list | `_main_` 置顶（读 main.yml，返回 assembly 段数 + hooks 位置） |
| get | `is_main=True`；**不含 persona/tools 字段**（不适用）；assembly 原样返回 |
| 保存 | `yaml.safe_load` 现有 main.yml 为 base，仅覆盖提交字段（description/model/assembly/hooks…），**保留未识别字段**（如 fallback 声明）；**不写 .md**；**写 `config_file` 解析的那份 main.yml**（repo 级覆盖、写侧跟随读到的那份，2026-08-31 commit 10d717e——本地存在则读写本地，多实例角色实例独立主声明）；提示 `/restart` 后生效（启动时装配） |
| create | 拒绝 `_main_`/`main` 保留名作子 Agent 名 |
| delete | 拒绝——主 Agent 声明不可删除 |

前端 `loadEdit` 对应：is_main 时隐藏 persona 组（gPersona）+ 工具组（fTools/toolChips/toolsHint）+ 隐藏删除按钮；保存 toast 显示后端 note（主 Agent：`/restart` 后生效 + **实际写入路径**（`已写 {p}；/restart 后生效`，动态而非硬编码 `~/.agt`）；子 Agent：新派活生效，reuse 实例下一任务生效）。

## 编辑器 assembly 往返增强（agents.html，同 commit）

子 Agent 与主 Agent 共用的 assembly 编辑器此前会**静默转丢**部分 DSL 形态，这次补齐：

- **带模式段名**（`history=tiered`）：`asmToRow` 拆成 name + mode 两字段；history 段渲染额外 mode 输入框
- **未知段名**：已知段名（SEG_TYPES：system/rules/history/ltm/user_message/steps/tail/hooks）→ 下拉；未知段名/带模式 → 文本框——**保往返不丢**
- ACT_TYPES（text/file/dir/cmd/workflow/tool/func）与 seg 两 kind 统一行模型

### 类型下拉修复：全行显示 + 选项 = seg + ACT_TYPES（2026-09-02，commit 504a518）

**现象（用户在 /agents#edit= 复现）**：点击 `+ 添加段` 创建段后，类型下拉框只显示一半段类型。

**根因（两个）**：① **seg 行此前没有类型下拉**（`isSeg ? '' : select`）——动作类型（text/file/dir/cmd/workflow/tool/func）对 seg 行**不可达**；② 类型选项是 `[...SEG_TYPES, ...ACT_TYPES]`——**把段名混进类型选项**，选中后 `asmKind` 会把 kind 写成 `'system'` 等（段名当类型用，语义混乱）。

**修复（src/static/agents.html）**：所有行统一显示类型下拉；类型选项 = `['seg', ...ACT_TYPES]`——`seg`=投影段（具体段名走右侧名称下拉，SEG_TYPES：system/rules/history/ltm/user_message/steps/tail/hooks），动作项 = text/file/dir/cmd/workflow/tool/func；title 提示「类型：seg=投影段（名称见右下拉）/ 动作项」。`asmKind` 切换时 seg 行保留原段名（默认 `user_message`）。JS 语法验证通过——与 [段形态简化定稿](../architecture/multi-agent.md) 同 commit 504a518。

### func 值下拉框：选项 = FUNC_REGISTRY 注册函数（2026-09-02，commit bab7dee）

**用户请求**：类型选 `func` 以后看不出有哪些注册的函数可选——值控件此前是自由文本框，得先知道函数名才能填。改成下拉框。

- **后端（src/server.py，api_agents_list）**：`GET /api/agents` 返回新增 `funcs` 字段 = `sorted(FUNC_REGISTRY.keys())`（`from agent_config import FUNC_REGISTRY`，兜底 `except → []`）——assembly 编辑器 func 下拉的选项源
- **前端（src/static/agents.html）**：列表加载时存 `window._asmFuncs = d.funcs || []`；`func` 行的值控件从文本框改 **select**——
  - 选项 = 函数名 + `()` 形态（`print_time()`、`load_models()`… 当前 8 个）
  - **当前值不在清单时插入首项保往返**（历史值/自定义值不丢）
- 效果：以后往 `FUNC_REGISTRY` 注册新函数，刷新 agents 列表下拉自动跟上（选项源每次 `GET /api/agents` 现取）

前端改动走 mtime 热更新，Ctrl+F5 强刷即用（不依赖 /restart）。

### ### steps 段模式下拉：reminder / reasoning（2026-09-02，commit dd5b0b4）

steps 段（尾段注入模式）的 mode 编辑从文本框改为 **select 下拉**（与 history 段文本框 mode 对照——history 是自由文本、steps 是枚举）：

- 选项：空（= 默认 reminder）/ `reminder` / `reasoning`
- title 提示：「尾段注入模式：reminder=&lt;system-reminder&gt;包裹并入末条 content（默认）/ reasoning=注入末条 assistant 的 reasoning_content 前缀（steps 后的段以思考链姿势呈现）」
- `onchange="asmData[i].mode=this.value"`——选择即写入，`asmToRow`/保存往返不丢

语义对照见 [context-engine · steps 注入模式](../architecture/context-engine.md#steps-段注入模式stepsreasoning2026-09-02用户提案)。

## v2.1 声明格式（用户设计）

```
.agent/agents/<name>.yml      # 纯配置：name/description/model/tools/assembly/hooks…
  assembly:
    - {file: .agent/agents/<name>.md}    # 首项固定引用 persona md
    - system
    - …
.agent/agents/<name>.md       # persona 正文（纯文本，读时剥 frontmatter）
```

三个设计点：

1. **改 md 即时生效**：file: 项每轮投影重读——任何编辑器改 persona md，下次派活即生效，不用碰 YAML 缩进/转义，也不用重建 Agent
2. **yml 不臃肿**：persona 长文本不挤进 yml，配置保持纯结构可读
3. **与旧格式同名不冲突**：加载 .yml 优先；yml 存在时同名 .md 被**声明扫描跳过**（就地转职 persona 载体，不会形成双声明）

写盘（`_dump_agent_yml`）：编辑器提交的 assembly 首项**统一转 file: 引用**（读侧仍兼容 text: 内联）；**不删同名 .md**——旧保存逻辑为防旧格式声明遮蔽而删，现在它是 persona 载体，删了丢内容。

## 顺带修掉：_asm_evaluate 两个既有 bug（session.py）

从未有声明用过 `file:` 项，所以 bug 从未暴露——v2.1 首次使用即触发：

1. **file/dir 项读错键**：旧代码只读 `item["path"]`，而解析产物把值存在同名键下（`{file: path}` → `item["file"]`）→ 恒空路径**静默跳过**。修为 `item.get("path") or item.get("file") or item.get("dir")`（path 键兼容手写清单）
2. **路径基准 + 沙箱**：从 `real_tools.WORKSPACE` 相对解析改为 `session.workspace`（子 Agent 复活/临时目录场景与全局 WORKSPACE 可能不同）+ resolve 后 `relative_to(workspace)` 越界拒绝

另：assembly `timing: once` 缓存 key 拼全键（kind/path/file/dir/cmd/name/text）——file/dir 项纳入 once 去重。

## 列表"装配零段"修复 + 5 个子 Agent 声明规范化（2026-08，commit f177674）

### 装配零段：被静默吞掉的 str/Path bug

用户报告：/agents 列表里**所有子 Agent 装配 0 段、hooks 空**——根因不在声明，在 list 端点读侧：

```
load_agents_index 返回的 path 是 str（".agent/agents/coder.yml"）
  → api_agents_list 调 load_agent_yml(it["path"])
  → load_agent_yml 内 path.read_text()——str 没有该方法 → AttributeError
  → except Exception: pass 静默吞掉 → meta 恒空 → assembly_count=0 / hooks=[]
```

一行修复：`load_agent_yml(Path(it["path"]))`。**测试盲区**：此前验证用的临时 workspace 里没有子 Agent——列表的子 Agent 分支从未真正跑过，`_main_` 置顶分支掩盖了它。

### 声明规范化：5 个子 Agent 全部转正（所见即所装）

修完 bug 暴露出第二个问题：各子 Agent 装配项数不一（coder 管理页只显示 1 个 text）、recap_gen 钩子看不见。逐项规范化：

| Agent | 规范化后 assembly（f177674 时点） | hooks |
|------|------|------|
| coder / explorer / reviewer / vision | `file(persona.md) + user_message + steps` | `turn_end: recap_gen\|async` |
| wiki-updater | `file + tool:wiki_tree() + user_message + steps` | 同上 |

- **recap_gen 显式声明**：此前 recap 挂 turn_end 是 `agent_prompt` 派活时的**运行时注入默认**（yml 未声明 hooks → 注入），yml 里没有 → 管理页看不见。现在显式写进声明；注入逻辑幂等（已有即跳过），不会双跑
- **assembly 显式化**：coder 系列此前裸 `[text]`，靠引擎**必装段自动补插**兜底——编辑器只见 1 项，实际投影跑 3 段。现在必装段显式入清单，**所见即所装**
- **vision 死 system 段删除**：yml 无正文，渲染恒空
- persona 全部拆独立 .md（v2.1 格式）；原文件备份 `tools/_agents_backup/`（未提交）
- 生效语义：`/restart` 后新进程的派活直接消费新声明；persona md 每轮投影重读，之后单独改 md 即时生效

**后续追加：`history|optional`（2026-08，commit 1e3b206）**——`|optional` 尾标从纯文档性注释变为**真语义**（默认不装配，`agent_prompt assembly="history=on"` 按需打开；主 Agent SYSTEM 有 `[可选装配: …]` 提示行）。coder / explorer / reviewer / vision 四个的 assembly 在 file 与 user_message 之间插入 `history|optional`（列表 asm=3 → 4）；wiki-updater 不加（一次性摘要任务无记忆需求）。四处改动（解析/投影/覆盖/agents_summary 提示）见 [multi-agent · assembly DSL](../architecture/multi-agent.md#assembly-dsl上下文装配配方)。

## 回退链表单 + 钩子行布局修复（2026-08，commit a667da4）

### 回退链表单：声明级 fallback 此前只能手写 yml

引擎侧 `_parse_agent_fallback`（src/multiagent.py）早已支持**三形态声明**——`"a,b"` 逗号串 / list / `{chain, policy}`——并在 agent_prompt 的**新建/复活/reuse 三条路径**消费；但管理页一直没有入口（"回退链怎么设置的？"此前答案：手写 yml）。本次补齐，与工具选择同款 chips 交互（用户提案）：

```
基本信息组新增：
  回退链 [glm, deepseek-chat, qwen    ] [策略:跟随全局 ▾]
  ○glm ○glm-official-1 ○deepseek ○qwen …   ← 模型 chips（点击顺序=链序）
```

- **chips 交互**（`renderFbChips` / `toggleFb`，agents.html）：点模型 chip 增删，点击顺序即链序；手填逗号串同样有效
- **留空 = 继承全局 settings**（fallback_chain / fallback_policy）——保存不写 `fallback` 键；「显式关回退」（区别于继承全局）仍走手写 yml 空串，管理页不做这个语义（表单留空表达"跟随"更自然）
- **`_main_` 主 Agent 同样支持**（编辑视图同款回退链区；留空保存 = 删 fallback 键继承全局）
- 生效语义与其它声明字段一致：**改链后 reuse 实例下一任务即生效**；页面本身 `/restart` 后可见（HTML 启动时载入内存）

读写两侧（server.py）：

| 函数 | 职责 |
|---|---|
| `_fb_of(meta)` | 读侧归一：三形态 → `(chain 逗号串, policy)`；未声明 → `('', '')` |
| `_fb_yaml_value(body)` | 写侧：非空才产出 yml 值（list / `{chain,policy}`，与 `_parse_agent_fallback` 读形态对齐）；空 → None 不写键 |

引擎侧解析与消费详见 [multi-agent · 声明级回退链](../architecture/multi-agent.md)；全局链配置见 [配置体系](config-and-models.md)。

### 钩子行布局：async/× 不再被挤下一行

用户观察："async 复选框和 ✕ 可以和位置选择/类型选择两个下拉框放在一排"——本该如此，根因是全局 `input[type=text]{width:100%}` 把值输入框撑满整行。一行 CSS 修复（agents.html）：

```css
.hkrow .val{flex:1 1 120px;width:auto;min-width:110px}
```

现在 `位置下拉 | 类型下拉 | 值输入(flex 自适应) | async | ×` 同一排。

## 验证

- v2.1 round-trip 6/6（list/get/save/create/delete 全通）；端到端：persona md 真实读进投影（`[assembly:file ...]` 标签 / `/context` 出现 `asm:file` 段）；越界路径拒绝（沙箱生效）
- `_main_` 纳入后 6/6：含 PUT 保留 custom_field（未识别字段不丢）、不误写 .md、子 Agent round-trip 不受影响
- f177674：列表 `_main_ asm=17 | coder/explorer/reviewer/vision asm=3 | wiki-updater asm=4`，hooks 全部 `[turn_end]`；GET 逐项核对（file 首项 / persona 读回 / hooks / wiki-updater 的 tool 项）全部正确（1e3b206 后 coder 系 asm=4）
- 1e3b206：optional 语义端到端 5/5（opt 解析 / 默认投影跳过历史 / `history=on` 历史进投影 / `=off` 移除 / summary 提示注入）
- a667da4：回退链写读往返三态（声明链 → 表单回显+chips 选中 / 留空保存不写键=继承全局 / `_main_` 留空=删键）；hidden 三态往返见 [workflow-hooks](../architecture/workflow-hooks.md)

## 注意事项

- **生效语义不同**：子 Agent 保存 → 新派活即生效（reuse 实例下一任务）；`_main_` 保存 → 需 `/restart`（主 Agent 启动时装配）
- 迁移存量声明的最短路径：管理页打开 → 保存一次 → 自动 v2.1；之后 persona 直接改 md 即时生效
- **`except Exception: pass` 的代价**（f177674 教训）：api_agents_list 的兜底 except 把 str/Path 的 AttributeError 吞成"恒空 meta"，表象指向声明而根因在读侧——宽 except 只配 debug 日志；且验证必须覆盖"列表里真有子 Agent"的分支（临时 workspace 无子 Agent 时该分支从未执行，`_main_` 分支掩盖了它）
- 主 Agent assembly 的具体项数/内容随 main.yml 演进变化，上表是 2026-08 快照；结构性事实（text/动作交错 + 尾部 seg 段）是稳定的
- assembly DSL 完整语义（段级 + 项级 + 必装段自动补插 + **`|optional` 真语义**）见 [multi-agent · assembly DSL](../architecture/multi-agent.md#assembly-dsl上下文装配配方)
- 模型/回退链字段语义（models.json 档案）见 [配置体系](config-and-models.md)

## 相关页面

- [multi-agent](../architecture/multi-agent.md)——声明与生命周期（两种格式）、assembly DSL（含 optional 真语义）、主 Agent 配方、recap_gen 挂钩两代来源
- [config-and-models](config-and-models.md)——模型/回退链配置
- [bubble-interaction](bubble-interaction.md)——answer 多 Agent 分页（子 Agent 输出的前端展示侧）
