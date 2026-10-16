# 技能体系 · SKILL.md 技能包 + 双层寻址（repo 本地 / 全局 ~/.agt/skills）+ 技能工具九件套（含装卸闭环 + 命令口）

> 技能 = 「frontmatter（name/description/when_to_use）+ SOP 正文」的 `SKILL.md` 目录包，可选带脚本/资产。SYSTEM 注入一行一技能的【可用技能】清单，Agent 任务匹配时按需取 SOP 执行；完成可复用任务后 `save_skill` 沉淀。核心实现全在 `src/agent_config.py`（技能工具注册处），SYSTEM 摘要文案在 `src/chat.py`。

## 职责

- **可复用任务的知识封装**：技能名 + 一句话作用 + 使用时机常驻 SYSTEM（token 便宜），详细 SOP 按需 `read_skill`（大技能再 `skill_navigate` 分节读）——三级递进的上下文经济
- **双层技能源（2026-09 双层化后）**：repo 本地 `.agent/skills/`（随 repo 提交，项目私有）+ 全局 `~/.agt/skills/`（跨 repo 共享，一次安装处处可用、按 repo 激活）
- **技能包可执行**：技能不止是文档——自带脚本（`scripts/`、`tools/`）经 `skill_run_code` 执行，评测约定经 `skill_evaluate` 走起

## 双层体系：全局技能目录 + repo 激活清单（2026-09，用户提案，spec s_4ac5ccb6）

用户原案：「全局目录 `~/.agt/` 里放一个 skill 目录放技能包；每 repo 放一个 `.agent/skills/global-skills.json` 标记本 repo 激活哪些全局技能；给技能添加 skill_run_code / skill_evaluate / skill_navigate 等工具，**混淆本地技能和全局技能的读和用**」——最后半句是设计灵魂：统一寻址，Agent 用起来不分本地全局。

### 统一寻址 `_resolve_skill`：本地优先 shadow，全局须激活

```python
def _resolve_skill(name, workspace=None):
    # 1) repo .agent/skills/<name>/SKILL.md 存在 → (dir, "local")
    # 2) name ∈ 激活清单 且 ~/.agt/skills/<name>/SKILL.md 存在 → (dir, "global")
    # 3) 否则 → (None, "")
```

- **本地优先（shadow）**：同名技能本地覆盖全局——repo 可 fork 全局技能做项目特化，不必改全局份
- **全局须激活**：`~/.agt/skills/` 里躺着不等于可用，必须在 repo 的激活清单里点名——防全局技能无限膨胀撑爆每个 repo 的 SYSTEM
- 所有技能工具（read/navigate/run_code/evaluate）**透明走它**，「读」和「用」都不感知层级；返回 scope 仅供展示（navigate 输出 `🌐 全局 ~/.agt/skills` / `repo .agent/skills`）

### 激活边界（静默语义，现行为零影响）

- 激活清单 `.agent/skills/global-skills.json` = **纯数组**（`["yangzi-aistudio", ...]`；dict 形态/损坏 JSON/文件不存在 → 一律零全局激活）
- 清单里点了但包不存在/无 SKILL.md → 静默跳过（`load_skills_index` 不报错）
- 组件：`_global_skills_root()`（`AGT_DIR/skills`，经 paths 单一数据根）+ `_enabled_global_skills(workspace)`（清单解析）+ `load_skills_index(workspace)`（本地扫 + 全局已激活合并，条目带 `scope` 字段）

## 技能工具九件套（原二 + 新五 + 装卸配对 skill_unequip + 命令口 skill_command）

`SKILL_TOOLS = Toolbox(Tool(read_skill), Tool(save_skill), Tool(skill_navigate), Tool(skill_run_code), Tool(skill_evaluate), Tool(skill_equip), Tool(skill_use), Tool(skill_unequip), Tool(skill_command))`

| 工具 | 职责 | 关键语义 |
|---|---|---|
| `read_skill(name)` | 读完整 SKILL.md（含详细 SOP） | 统一寻址；找不到时 `_skill_not_found` 指路：「若是全局技能，需在本 repo 的 global-skills.json 激活清单里启用」 |
| `save_skill(name, description, when_to_use, sop)` | 沉淀新技能 | **仍写 repo 本地** `.agent/skills/`（全局技能的维护直接编辑 `~/.agt/skills/` 文件） |
| `skill_navigate(name, section, list_only, file)` | 结构浏览 / 分节读 / 读包内文件 | 见下 |
| `skill_run_code(name, script, args)` | 执行技能包内 .py（一次性无状态） | 见下 |
| `skill_evaluate(name, input)` | 技能约定评测入口 | 见下 |
| `skill_equip(name)` | 装备技能常驻服务（server.py） | 见下 |
| `skill_use(name, command)` | 向已装备服务发命令 | 见下 |
| `skill_unequip(name)` | 卸载技能（撤插段/钩子 + 停自有服务 + 清登记） | 装备/卸载配对语义见「技能装卸生命周期」节 |
| `skill_command(name, command)` | 轻量命令口：command 首词路由到技能命令脚本执行 | 白名单闸门 + 自带发现性，见「skill_command · 轻量命令口」节 |

### skill_navigate · 大技能不必整读

- 默认返回：目录树（**≤3 层**，跳 `.` 开头 / `__pycache__`，120 条上限防刷屏）+ SKILL.md 章节清单（1~3 级标题带行号）；**2026-09-20 起树附各 .md 文件的标题清单**（见后记二）
- `section="章节标题"`（含/不含 `#` 均可，大小写不敏感）→ 只读该章节正文（**12K 字截断**）；未命中则回显全部可用章节
- `file="包内路径 / 裸文件名 / 部分名"` → 读该文件正文（超长 **12K 字截断**；markdown 文件附章节清单，纯文本直读；可再配 `section=` 分节精读）——**宽松定位三级匹配链**（见后记二）
- `list_only=True` 只列结构不附提示尾注

#### 后记：file= 读包内任意文件——技能包文件读取的一等通道（2026-09，用户问句触发，commit e5b0e16a）

用户问「技能里有很多文件，读取其它文件的话是什么工具」——问句实测暴露缺口：`read_file` 限 workspace 内（全局技能包在 `~/.agt/skills/`，workspace 外读不了），`skill_navigate` 原来只导航 SKILL.md 本身。结果：包内其它文件（洋子技能的 8 个专业模块、口令卡、使用说明……）**此前只能 `run_python` `open()` 绕行**——能用但不体面，且模型不一定想得到。

**修复**：`skill_navigate` 加 `file=` 参数（`src/agent_config.py`；`_md_sections` / `_md_section_text` 两个 markdown 章节 helper 从 SKILL.md 专用扩展到任意包内文件）：

| 用法 | 返回 |
|---|---|
| `skill_navigate(name)` | 目录树 + SKILL.md 章节清单（当时默认行为；后记二升级为树附各 .md 标题） |
| `skill_navigate(name, file="专业模块/08-XXX.md")` | 该文件正文 + 章节清单（超长 12K 截断） |
| `skill_navigate(name, file="…", section="章节名")` | 大文件分节精读 |
| `skill_navigate(name, file="02-口令卡.txt")` | 纯文本直读 |

- **防逃逸与 skill_run_code 同款**：禁 `..`、resolve 后必须落在技能目录内（统一寻址得到目录后再校验，本地/全局技能均可读）
- 文件不存在 → 提示 `list_only=True` 查目录树纠错
- 实测 6 项全绿：44KB 大模块章节化读取 / 分节精读 / txt 直读 / 逃逸拦截 / 不存在提示 / 默认行为不回归
- 效果：从总控路由到 08 号模块分节精读，全程不出技能工具集，不再依赖 run_python 绕行

**生效注意**：新参数进工具 schema 需实例重启（与 SYSTEM 摘要同口径，无热重载）——`/restart` 后 agent 的工具 schema 才带 `file`。

#### 后记二：树附各 .md 标题清单 + file 宽松定位——先见全包地图、再按名取文件（2026-09-20，用户两问，commit 88de034）

用户两问直指 `skill_navigate` 的两个别扭：①「Agent 要怎么知道文件里有哪些 section？」——目录树只给文件名，Agent 得先盲读一遍才知道有什么章节可分节；②「file 是完整相对路径会不会偏严格了」——从大纲树拿到的往往只有文件名，强求写全路径容易错。

**改动一：目录树附各 .md 标题清单**——每个 .md 文件名下列出其 `#/##` 级标题（每文件 ≤6 条 + `…`；全树总预算 160 行防大包刷屏）：

```
📦 技能 'yangzi-aistudio'（🌐 全局 ~/.agt/skills）
  • 04-跨阶段参考记忆模板.md
    · 本地知识库跨阶段参考记忆
    · 权威顺序
    · 使用规则 …
  📁 专业模块
    • 08-CINEDANCE视频提示词.md
      · CINEDANCE V5 — 专业镜头规划与双模型提示词系统 …
```

一次 navigate = 全包「文件 × 章节」地图——选准了再 `file + section` 精读。

**改动二：`_find_skill_file` 宽松定位三级匹配链**（`src/agent_config.py`）：

| 传 file= | 命中方式 |
|---|---|
| `"专业模块/08-XXX.md"` | ① 完整相对路径精确命中 |
| `"04-分场大纲.md"` | ② 包内 rglob 同名 basename |
| `"分场大纲"` / `"00-系统指令"` | ③ stem 部分包含（搜 `.md`/`.txt`） |

- **唯一定位** → 直接读（输出头部标明实际路径 + 章节清单）；**多候选**（如两个 AGENTS.md）→ 列出完整路径让模型重选；找不到 → 提示附树参考
- 裸文件名恰好等于根路径时精确命中（`AGENTS.md` → 根的；要读外挂同名文件用完整路径）
- 防逃逸不变（`../` 拒 + resolve 后须在技能目录内）

**验证 10/10**：树附标题 / 文件名直读 / 部分名(.md) / 部分名(.txt) / 完整路径 / file+section 分节 / `../` 拦截 / 不存在提示（附树参考）/ 默认导航回归 / 裸名命中根。

### skill_run_code · 技能包内脚本执行（安全三道闸）

- 脚本须为技能目录内 `.py` 相对路径；三道闸：①必须 `.py` 后缀 ②路径 parts 禁 `..` ③`(d/script).resolve()` 后技能目录 `resolve()` 必须在其 parents 内（防符号链接/规范路径逃逸）
- `cwd=技能目录`（脚本可用相对路径读包内资产）；`args` 经 shlex 分词（支持引号）；120s 超时；输出截断 8000 字；返回带 `[exit=N]`
- 脚本不存在 → 列出包内全部 .py（≤40 条）辅助纠错

### skill_evaluate · 评测约定三级降级

1. `scripts/evaluate.py` 存在 → 以 `input` 作 **stdin** 执行（cwd=技能目录，120s，输出 8000 字截断）
2. 否则包内 `EVAL.md` → 返回评测说明（6K 截断）
3. 都没有 → 明确提示「技能作者未约定评测方式」（不猜不编）

### 技能服务 skill_equip / skill_use · 常驻有状态服务（2026-09-27，用户提案，commit b33f230）

- 技能包可带 `server.py`——**类 MCP 的常驻服务**，但按需装备：不拉起 agent 时自动注册、工具不进 tools schema（零 schema 膨胀、零启动开销）
- **与 skill_run_code 的分工**：run_code = 一次性无状态执行（每次冷启动）；equip/use = **常驻有状态服务**（模型加载、会话、窗口焦点等跨调用状态得以保持）
- 起源：用户提案「skill 的目录里可以有个 server.py，类似 mcpServer 但不需要拉起 agent 时自动注册全部工具，而是 skill_equip 时以 cwd 拉起服务、返回能力说明，skill_use(name, "do_sth2 --y") 发命令执行」——实现 `src/agent_config.py`，demo 技能 `.agent/skills/test-svc/`

**stdio 行协议**（极简，技能作者一个循环就能写）：

```python
# server.py（cwd=技能目录被拉起）
print("do_sth1:      # 执行do_sth1操作，用来XXXXX")
print("  --file          # 文件路径")
print("do_sth2:      # 执行do_sth2操作，用来YYYYY")
print("  --y             # 可选是否XXX")
print("===CAPS_END===")          # 能力清单结束标记
for line in sys.stdin:          # 每行一条命令（自己 shlex 解析）
    ...处理...
    print(结果)
    print("===DONE===")         # 每条命令结束标记
```

**框架行为**：

| 环节 | 语义 |
|---|---|
| `skill_equip(name)` | Popen 拉起 `server.py`（`cwd=技能目录`、`PYTHONUNBUFFERED=1`）→ 读能力清单（30s 超时）→ 返回给 Agent。重复装备 = 杀旧起新（幂等）；无 server.py → 提示走 read_skill/run_code；坏服务（启动即退/超时未出清单）→ 带已产出输出的明确诊断 |
| `skill_use(name, command)` | 向已装备服务发一行命令 → 读结果到 `===DONE===`（120s 超时）。**未装备时自动装备**；服务退出/发送失败均有明确诊断并提示重新 equip |
| 生命周期 | 服务常驻到 Agent 进程退出（atexit `_skill_services_cleanup` 全杀） |

- **验证 9 场景全绿**：equip 清单完整 / `add 5→add 3→show = count=8`（**跨调用状态保持——核心价值实证**）/ 中文参数原样 / 幂等重启清零 / 无 server.py 提示 / 未装备自动装备 / 坏服务诊断
- **调试中抓到的两个坑**：① 子进程 stdout pipe **全缓冲**——4 行清单憋在缓冲区父进程永远读不到（死锁）→ 框架侧 `PYTHONUNBUFFERED=1` 兜底（作者忘写 flush 也不死锁）；② `_USE_DONE` 常量误写 `==DONE===`（少一个等号）与协议 `===DONE===` 永不匹配 → use 恒超时——最小复现二分定位抓出，常量处已注释警示
- demo：`.agent/skills/test-svc/`（32 行计数器服务，协议最小范例，可删）

## 技能装卸生命周期：skill_unequip + replace 幂等 + 装备登记（2026-10-16，用户裁定，commit e5a74f5）

> **装备 = 临时能力**：进程生命周期 = 装备生命周期（登记纯内存，重启即隐式全卸）；**卸载 = 按来源标记精确撤除**（只删该技能插进去的项，不重跑整体装配、不碰宿主与其它技能）。实现全在 `src/agent_config.py`（`_equipped_table` / `_revoke_equipped` / `skill_equip` / `skill_unequip`）+ `src/multiagent.py`（`_apply_extra_assemblies` 打标 / `_revoke_skill_items` 撤销）。

### 一次装备的三个落点（各自有独立的撤销语义）

| 落点 | 装备时 | 卸载/重装时 |
|---|---|---|
| 技能服务 `server.py`（进程表 `_SKILL_SERVICES`） | `_svc_kill` 幂等先杀旧实例 → Popen 拉起（`cwd=技能目录`、`PYTHONUNBUFFERED=1`）→ 读能力清单到 `===CAPS_END===` | `_svc_kill(name)` 杀进程 + 从表移除 |
| 依赖服务（SKILL.md frontmatter `services:`，同子 Agent yml 语法 `- 名: 命令 \| reuse=URL`） | 复用 `multiagent._ensure_agent_services`（单一真源）：探测活 → **复用不拥有**（不进登记、卸载不停）／死 → 拉起并记入 `started` | 只停登记里 `services`（= 自己拉起的那些）；复用来的**不拥有不停** |
| 装配插段 / 钩子（`session.assembly_plan` / `hook_specs` 快照） | `_apply_extra_assemblies` 给每个插入项打 `_skill_src=<技能名>` 标记（+ `_xsrc` 位置） | `_revoke_skill_items(plan, hooks, name)` 只删标记匹配项；快照就地更新（`set_assembly_plan` 同时清 once 缓存），**不重跑整体装配** |

- **`_skill_src` 是唯一真源**：撤销不依赖登记表内容——登记丢了照样按快照标记撤净（登记只用来定位「哪些服务是我拉起的」）。
- **装备登记 `session._equipped_skills`**（`_equipped_table`）：`{技能名: {"services": [自己拉起的], "item_keys": ["after:steps×1", …], "desc": "SKILL.md description"}}`——三键；`desc` 为 2026-10-17 新增（装备时从 frontmatter 捎带，供每轮【技能装备中】提醒段展示，见下节）。**纯内存、不进 meta 持久层**（Session 不预声明该属性，无/坏型就地初始化空表，避免污染持久化路径）。
- **前提**：技能包必须带 `server.py` 才能装备（纯 SOP / 只声明 `assemblies:` 的技能无法动态装备，返回 `[无服务]` 引导 read_skill / run_code）；插段应用与声明服务拉起都需主 Agent 运行时挂点 `_RUNTIME_AGENT`（未挂 → 明确告警、不静默）。

### skill_unequip(name)：三件事 + 两处容错

1. **撤插段/钩子**（`_revoke_equipped` → `_revoke_skill_items`）——异常也「容错继续」（`撤销插段异常（容错继续）`），不影响后续停服务
2. **停自有服务**：`server.py` 实例 + 登记里 `services`（复用来的跳过；单个停失败记进 notes 不中断）
3. **清登记** → 报告 `✅ 已卸载技能 'x'（撤销插段 N 项 / 钩子 N 项；停自有服务 …）`

- **登记丢失容错**（进程重启过）：`⚠️ 装备登记丢失（进程重启过？）——已按插段标记尽力撤销；声明服务无从追溯未停`
- **未装备**：`[未装备] 技能 'x' 当前未装备（无登记、装配快照也无其插段/钩子）`
- 非法名先在入口被 `_NAME_RE` 拦（`[非法名称]`）；报告尾注统一：装配快照已剔除该项、其余装备与宿主段不动、重启即隐式全卸
- **斜杠命令等价入口**：`/skill_equip <名>` / `/skill_unequip <名>`（`src/commands.py`，无参数时打印用法提示）

### replace 幂等：重复 `skill_equip` 同名 = 撤旧 + 装新

`skill_equip` 开头 `pop` 旧登记 → `_revoke_equipped`（撤旧插段 + 停旧自有服务）→ `_svc_kill` 杀旧 `server.py` → 重新拉起 / 应用 / 登记，返回值带 `（replace：撤旧装备——…）` 或 `（replace：旧装备无插段/自有服务，直接重装）`。

**幂等实证**（`test/test_skill_lifecycle.py::test_replace_idempotent`）：二次装备后 `_skill_src` 插段数**不变**、钩子位条目数不变、旧自有服务被 stop 一次、新登记指向新实例（停旧→拉新各一次）。

### 装备提醒段【技能装备中】：每轮 system-reminder 可见 + unequip 提醒（2026-10-17，用户提案）

用户提案：装备期间 system-reminder 里需要有提醒——「当前装备了 xx 技能，暂时不需要使用了记得 unequip 卸」。落地两处（commit 本地）：

| 文件 | 改动 |
|---|---|
| `src/agent_config.py` | `skill_equip` 装备时从 SKILL.md frontmatter 捎带 `description` 存进装备登记——登记升三键 `{services, item_keys, desc}`（desc 供提醒段展示，见上节登记结构） |
| `src/agent.py` | `_runtime_system_extra`（动态 system 注入点，与【后台服务状态】【子 Agent 任务看板】同族）新增第三段【技能装备中】——遍历装备登记列 `· 名字——描述` + 尾注：「技能装备是临时能力：暂时不需要时记得 skill_unequip('技能名') 卸载释放（进程重启也会隐式全卸）」 |

- **零开销**：服务清单 / 装备登记 / 子 Agent 看板三者皆空不注入（无装备不占一字节）
- **老登记容错**：无 `desc` 的旧条目只显示技能名（重启隐式全卸后，下次装备即带描述）
- **验证**：L3 stub 三态（空→不注入 / 带 desc→名字+描述 / 无 desc→只名字）+ L2 隔离实跑（旁路实例直跑新代码：六钩子链完整注册、无异常）全绿；注入点属引擎代码，**`/restart` 生效**——重启后装备需重新拉起（下次 `skill_use` 自动拉起）

### 注册与验证

- 工具面从七件套升为**八件套**（`SKILL_TOOLS` 尾部增 `Tool(skill_unequip)`；新工具进 schema 需 `/restart`）；§技能工具九件套 表内一行对应本节（2026-10-17 再增 `skill_command` 为九件套）。
- `test/test_skill_lifecycle.py` 9 例：装备登记 + `_skill_src` 标记 / `services:` 复用不拥有 · 死则拉起并登记 / unequip 撤净且**不吞宿主项与他技能项** / 未装备提示 / replace 不重复插段 + 停旧服务 / 登记丢失尽力撤且不停服务 / `_apply_extra_assemblies` 稳定追加 + `_revoke_skill_items` 精确撤销（含 plan·hooks 传 None 与无匹配的纯函数边界）/ `_ensure_agent_services` 的 `started` vs `reused` 语义。
- **作用域**：`skill_equip` / `skill_unequip` 走 `_RUNTIME_AGENT` → 作用于**主 Agent 运行时会话**；子 Agent 的装备发生在派活链（`multiagent._agent_skill_gear`），随实例退出自然消失，无卸载配对需求。

### 注意事项

- **/restart 生效**：SYSTEM 技能摘要在 agent 构建时装配——新装全局技能 / 改激活清单后需重启实例（无热重载）
- `save_skill` 永远写 repo 本地；全局技能的沉淀/更新直接编辑 `~/.agt/skills/`（跨实例共享一份）
- 激活清单务必保持**纯字符串数组**——dict 等其它形态被静默忽略（宁缺勿错）
- 本地与全局同名时本地 shadow：想临时覆盖全局技能行为，在 repo 放同名技能即可，不必动全局份
- 技能名只允许字母数字/下划线/连字符（`_NAME_RE` 校验，各工具入口统一报 `[非法名称]`）
- **equip 的服务生命周期**：常驻到 Agent 进程退出（atexit 全杀）；`PYTHONUNBUFFERED=1` 由框架注入——server.py 忘写 flush 也能工作；命令协议（能力清单格式）由技能作者自定，`===CAPS_END===`/`===DONE===` 两个标记是唯一硬约定
- **装卸配对（2026-10-16）**：`skill_unequip(name)`（或重复 equip = replace）按 `_skill_src` 标记撤插段/钩子 + 只停自己拉起的服务 + 清 `session._equipped_skills`；登记纯内存、**重启即隐式全卸**——没有持久「已卸载」清单，卸载是会话内语义（详见 §技能装卸生命周期）
- **命令口（2026-10-17）**：`skill_command` 只认技能包内实际存在的命令脚本（`scripts/commands/<名>.py` 或 `scripts/<名>.py`），**不接受任意 shell**——防注入；未知命令回显可用清单（详见 §skill_command · 轻量命令口）

## skill_command · 轻量命令口（2026-10-17，用户提案）

用户提案（2026-10-17）：以 command 形式调用技能能力更轻量——`skill_command(name="desktop-ops", command="mouse_click -x 500 -y 300 -button left")`，指令说明写在 SKILL.md 里，装备时 SOP 注入 system（模型直接可见指令表），`read_skill` 都可省。`SKILL_TOOLS` 升为**九件套**（新工具进 schema 需 `/restart`）。实现：src/agent_config.py `skill_command(name, command, timeout=60)`。

### 约定与语义

| 环节 | 语义 |
|---|---|
| 命令注册 | 技能包 `scripts/commands/<首词>.py`（约定位置，优先）或 `scripts/<首词>.py`——**加命令 = 放一个脚本 + SKILL.md 写一行说明，零注册样板** |
| 参数 | 首词后的 token 经 shlex 分词（引号安全）作为命令行参数传入；`cwd=技能目录`；stdout 即回执（带 `[exit=N]`） |
| 白名单闸门 | 首词必须是包内实际存在的命令脚本——**不接受任意 shell**（防注入）；未知命令 → 列出全部可用命令清单（**自带发现性**，模型不用猜） |
| 异常形态 | exit≠0 → ✗ + stderr；超时 → 终止 + 明确标注；空命令 → 回显用法示例 |

- **发现性闭环**：SKILL.md 写指令说明 → 装备时 SOP 注入 system（模型天然可见指令表）→ 命令口自报可用清单兜底——三层发现性
- 命令实现统一 `.py`（跨平台 + 参数解析一致；`.bat` 不做——要跑批处理走 `run_shell`）

### 验证

五态冒烟全绿（命中回执 / 白名单列清单 / 失败回执 / 超时终止 / 空命令）+ `test/test_skill_command.py` 4 例 + 技能域回归 **35 passed**。

### 与 tools/ 的双轨定案

用户判断「command 形式更轻量」有实证支撑：强模型时代拼命令行准确率已不输 schema 防呆，而 **schema 膨胀是每轮实付的全局成本**；但 tools/（schema 工具）也有真优势——结构化校验前置挡错、进程内执行快。定案**双轨并存**：

| 轨道 | 适用 |
|---|---|
| `tools/`（schema 工具） | 高频、参数复杂、要前置校验的能力（desktop-ops 的 drag 四参数这种） |
| 命令口 `skill_command` | 指令式简单操作、想快速加能力不想写工具样板 |

同一技能可双轨并存；desktop-ops 已实战验证的 tools/ 形态**不动**，命令口给将来技能多一个选项。

## desktop-ops 形态三步演进：11 工具 → 命令口帧源 → stdin 全量服务（2026-10-17，用户三连提案）

desktop-ops 一日内完成能力形态整体迁移，最终定型「**画面走命令口、键鼠走 stdin 服务**」的零 schema 形态（commit 本地 + 5e80326）：

| 步 | 用户定调 | 落地 |
|---|---|---|
| ① 帧源迁命令口 | 「image_feed 就可以装成 `skill_command(name=…, command=…)` 的造型」 | `screen_frame` 工具退役 → `scripts/commands/screen_capture.py`（截屏→**覆盖写固定文件**→打印路径，无临时文件堆积）；SKILL.md 装配声明改 `{tool: skill_command(...)}`；session.py 求值器补**命名参数**支持（见 [image-feed · tool: 表达式](image-feed.md)） |
| ② 键鼠全量迁 stdin | 「把 click drag key 什么的都弄到 stdin 里」「避免装配为工具，stdin 就挺好的」 | `tools/desktop_tools.py` **退役**；`server.py` stdin 行协议承载全部 10 命令（click/move/drag/key/type/hotkey/clip_r/clip_w/wins/focus） |
| ③ 端到端实测 | 「装备后立刻在工具返回中发现可用指令；打开 ProcessHub 看一眼再最小化，告诉我选中进程」 | 全链路通过：equip 返回即带 CAPS 清单 → focus 前置 → image_feed 识帧（选中=LiteDrop-Agt）→ `hotkey win down` 最小化 → 新帧二次确认（不盲信返回值） |

### stdin 服务形态：全量命令化

- 调用形态：`skill_use("desktop-ops", "click --x 500 --y 300")`——`--k v` 传参（shlex 引号安全、类型强转、必填校验）；未知命令回执可用清单不炸
- CAPS 能力清单开局即报（10 命令 + 参数说明）——**装备后工具返回里立即可发现**，无需读 SKILL.md（③ 的实测验收点）
- 坐标规范保留：k=屏宽/1280 自动换算不变
- **schema 投影 11 个 → 0 个**——动机即上节双轨定案认的账：schema 是每轮实付的全局成本，指令式操作交给强模型的命令行准确率

### 能力面三形态选型（定稿；技能包 README 范例同步三形态说明）

| 形态 | 适用 | desktop-ops 归属 |
|---|---|---|
| `tools/` schema 工具 | 参数复杂、要前置强校验 | （已清空） |
| `scripts/commands/` 命令口 | 指令式、低频、要进 image_feed 等表达式 | screen_capture |
| `server.py` stdin 服务 | 常驻状态、高频交互、零投影 | 键鼠 10 命令 |

新技能按性质选型，desktop-ops 成为 stdin 形态的完整体范例；播种源 `src/assets/skills/desktop-ops/` 已同步全套 stdin 版（robocopy /MIR）。

### 实测揪出的两个坑（commit 5e80326）

1. **YAML 逗号截断表达式**：`assemblies:` 里 `{tool: skill_command(name="x", command="y")}` 裸写时逗号被 YAML flow mapping 当键值分隔符——帧提供者拿到半截表达式报「无效」。**首装才暴露**（旧声明 `{tool: screen_frame()}` 无逗号没踩到），修法：值整体加单引号（详见 [image-feed · YAML 坑](image-feed.md)）
2. **focus 对最小化窗口静默无效**：pygetwindow `activate()` 不抛异常但也不生效——desktop-ops server.py 补 `isMinimized → restore()` 再激活

### 顺手修：测试全局卫生

test_skill_command 的 `_resolve_skill` patch 泄漏，污染后续测试文件（合跑 5 failed）——改 autouse fixture 还原（patch 必还愿，测试文件间全局卫生立规矩）。回归：test_skill_assemblies 18 + 合跑 31 passed + subagent_skills rc=0。

## 技能自带装配清单（assemblies:）

### 技能自带装配清单（SKILL.md `assemblies:` 字段，2026-10-16）

技能从"工具+SOP"升级为**自带装配清单**的能力单元（plan p_76f6623b）：装备时把装配段/钩子/帧提供者插进宿主。

```yaml
assemblies:
  - position: after:steps        # 装备时往宿主装配清单 steps 段之后插入
    ass:
      - text: 【注入段】          # ass 复用 yml assembly 段语法（file:/cmd:/text:/workflow:…）
      - image_feed: {tool: screen_frame()}   # 帧提供者抽象：每步取帧=调工具/技能服务
  - position: hook:turn_end      # 技能 workflows/ 下的工作流自动挂到钩子位
                                 # （缺省 ass = 技能包 workflows/ 全部；可显式 ass: ["wf名|async"]）
```

- **两个装备口共用一套声明**：静态口（子 Agent yml `skills:` 装备链读 frontmatter）与动态口（`skill_equip` 时技能服务 caps 上报 `EXTRA_ASSEMBLIES: <json>` 单行）。
- **position**：`before:<段名>` / `after:<段名>`（锚=宿主装配清单现有段名；锚不存在→装备时报错清单化，不静默丢弃）/ `hook:<钩子位>`（before_turn/before_tool/after_tool/before_answer/turn_end）。
- **两条铁律**：①装备时冻结——声明解析后作为静态清单快照进宿主 session（set_assembly_plan），逐轮不再重算（缓存安全）；②作用域对称——子 Agent（`multiagent._agent_skill_gear`）与主 Agent（`skill_equip` → `agent_config.apply_extras_to_session`）共用同一插入逻辑 `multiagent._apply_extra_assemblies`，不写两份。
- 多技能同位置按装备顺序稳定追加（先装备的先插）；装备的产物是**冻结快照**（逐轮不重算）——**2026-10-16 起有了撤销通道**：`skill_unequip` / 重复装备（replace）按 `_skill_src` 标记撤旧项，快照就地更新（见 §技能装卸生命周期）
- 首个真实用户：`desktop-ops`——SKILL.md 声明 `image_feed: {tool: screen_frame()}`，desktop-operator.yml 不再手写 image_feed 段、不再拉 8765 帧服务（瘦身为最小声明）；配套 `image_feed` 段 `tool:` 前缀解析（`session._asm_image_feed_from_tool`：data URL / 文件路径 / 双参工具位置映射，http/file 源零回归）。

### 落地链：解析 / 插段 / 钩子 / 帧提供者（plan p_76f6623b，commit 3609d97）

worker 施工线 7 步交付，上节语义落到四个文件：

| 文件 | 件 |
|---|---|
| src/agent_config.py | `parse_extra_assemblies`（frontmatter 解析：position 三前缀 + 锚段名/钩子位白名单，坏声明清单化报错）· `load_skill_assemblies` · `parse_caps_extra_assemblies`（动态口 caps 单行 JSON）· `fill_default_hook_items`（hook 缺省 ass = 技能包 workflows/ 全部）· `apply_extras_to_session`（主 Agent 应用点；hook_specs=None 时先物化旧式 meta.hook 再合并——不吞既有钩子）；`skill_equip` 双口收集 + 清单透出 |
| src/multiagent.py | `_agent_skill_gear` 返回值 tuple→**dict**（tools/sop/extras/services/error）；`_apply_extra_assemblies` 统一插段——after 跳同簇、before 紧贴锚（装备顺序稳定追加，宿主原有项不吞并，锚缺失清单化报错）+ 钩子挂载 + hooks 段开关；派活**新建/复用/复活三路径**基线处一处合并、三路生效；`_asm_item_from_dict` 非 str 值原样保留 |
| src/session.py | image_feed `tool:` 帧提供者 `_asm_image_feed_from_tool`：data URL / 文件路径 / 双参工具按 schema 位置映射；http/file 源零回归；非视觉模型静默门控保留 |
| src/agent.py | `_hook_tasks` workflow 项支持 `dir=`——装备时冻结的技能包 workflows/ 路径（不污染 workspace 命名空间） |

**验证**：test/test_skill_assemblies.py 18 例（解析 / 插段锚定位 / 钩子挂载 / 冻结性 / 两作用域对称 / tool: 帧源 / 未知段名报错）+ 真实链路 e2e（desktop-operator 声明 + 真实技能包 → 11 技能工具在箱含 screen_frame → extras 合并后 image_feed 紧贴 steps → **真实截屏 @@IMGFEED@@ 哨兵帧注入 ✓**）。

**遗留**：①动态口端到端（真服务进程 caps 上报 `EXTRA_ASSEMBLIES`）未做子进程级 e2e，仅函数级覆盖——首个服务形态技能接入时可补；②装配为一次性动作无卸载还原（spec 既定语义），多次装备同一技能会重复插段——装备幂等性待裁定。**两项均于 2026-10-16 收口**：`skill_unequip` + replace 幂等落地（按 `_skill_src` 标记精确撤销、不重跑整体装配）——见 §技能装卸生命周期。

### 后记：YAML 逗号截断 + desktop-ops 声明迁移（2026-10-17，实测 commit 5e80326）

- desktop-ops 声明迁为命令口造型：`image_feed: '{tool: skill_command(name="desktop-ops", command="screen_capture")}'`——**值必须整体加单引号**：YAML flow mapping 把裸值里的逗号当键值分隔符，帧提供者拿到半截表达式（`tool: skill_command(name="desktop-ops"`）报「无效」。旧示例 `{tool: screen_frame()}` 无逗号，换带逗号表达式**首装才踩**（端到端实测抓到，commit 5e80326）
- 求值器同步升级**命名参数**：`k="v"` token 按 schema 命名映射（类型强转），函数签名演化不破坏声明；desktop-ops 实测命名/位置两造型均产出真帧。帧源侧详见 [image-feed · tool: 表达式](image-feed.md)

## skill_suggest · 技能按需建议钩子（SYSTEM 技能清单下线：jev 快判 + LLM 全文复核，2026-09-27/28，用户提案）

### 动机与形态翻转

技能 name/description 常驻 SYSTEM 每轮一份清单 token，而绝大多数轮根本不涉及技能。用户提案（2026-09-27）：清单不进 SYSTEM，改由 **skill_suggest 工作流挂 before_turn 钩子**逐轮检测——命中才注入 name/description/when_to_use + `read_skill` / `skill_equip` 引导，不命中**完全静默**（一行不占上下文）。`skills_summary` 注入段已从装配移除（下节「SYSTEM 摘要与使用教育」所述机制**退役**，教育文案职责转由建议注入承载）；钩子挂 `~/.agt/main.yml`，全部实例生效。

### 编排（v3 两级，6 节点）

```
start → 扫技能(.agent/skills/*/SKILL.md：description + 全文各截 6000 字) → 短路(user_message < 4 中文字符/英文单词 → 静默)
  → ① jev 快判（intent_nano，criteria=技能 name+description，~1s 零 token，route=false 单出口）
       命中（非 __none__ 且 conf≥0.5）→ selector true 分支 → 直接组装注入
       未命中/低置信 → ② LLM 复核（全文 + user → 只回 name 或 NONE）→ 组装注入
  → 最终无匹配 {inject:false} 完全静默
```

两级分工：一级判别模型（NanoJev 0.6B）**宁可放过**——快路径零 token；二级 LLM 拿 SKILL.md 全文兜底——慢路径每次只花一次 utility 短调用（~1s 本地）。

### 六场景实测（全绿）

| 场景 | jev 一级 | 最终 |
|---|---|---|
| protobuf 流量解码 | conf 0.37 <0.5 未命中 → LLM 复核 | 静默（LLM 判 NONE） |
| 写技能包 | test-svc 0.53 命中（快路径） | 注入 test-svc 建议 ✓ |
| 代码库探索 | explore-codebase 0.81 命中（快路径） | 注入 explore 建议 ✓ |
| "在吗" | 短路（<4 字） | 静默 ✓ |
| 无关消息 ×2 | __none__ 且 conf<0.5 | 静默 ✓ |

### v3 调试修正三处（workflow DSL 形态坑）

- **selector 条件**：`<condition>` 子元素 parse 侧忽略 → branches 空 → **恒走 false**；改 `<cond op="11" left="810001.hit" left_type="boolean"/>`（before_turn_retrieval 同款验证形态）
- **llm 节点参数**：`<prompt>` 裸元素不被 parse → llmParam 空 prompt（用户实锤）；改 `<param name="prompt">` / `<param name="systemPrompt">`
- **criteria 不拼 when**（description 保持纯净判别语义）
- 节点侧地基：[intent_nano](intent-nano.md) 的 intents ref 动态装填 + route=false 单出口（2026-09-27/28 强化，commits 9584309 + dbe846b + 2cec328）

### 已知局限

pbridge 类消息 jev 一级判分偏低（conf 0.37 vs __none__ 0.36 几乎打平）→ 落 LLM 复核兜底，不算漏报；改善路径：技能 description 措辞迭代（补 grpc-web / 流量解码 / 逆向等高频词）或调低 threshold（放行更多模糊匹配，自行权衡）。

## SYSTEM 摘要与使用教育（src/chat.py）

> **【2026-09-27/28 已下线】** `skills_summary` 注入段已从装配中移除——技能清单不再常驻 SYSTEM（每轮省一份清单 token），改为 [skill_suggest](#skill_suggest--技能按需建议钩子system-技能清单下线jev-快判--llm-全文复核2026-09-2728用户提案) 按需建议（见上节）。以下为退役前形态存档。

装配 SYSTEM 技能段（`skills_summary`）时一行一技能，全局技能加 **🌐 前缀**：

```
=== 可用技能（repo .agent/skills/ + 已激活全局技能；🌐=全局）===
任务匹配某技能时，先 read_skill(name) 取详细 SOP 再按它执行
（大技能先 skill_navigate(name) 浏览结构/分节读；技能自带脚本用 skill_run_code(name, script) 执行；带 server.py 的技能可 skill_equip(name) 装备常驻服务、skill_use(name, '命令 --参数') 调用）：
- yangzi-aistudio: …（使用时机: …）
（完成可复用任务后可用 save_skill 沉淀新技能到本 repo）
```

教育文案随双层化同步升级：七件套的使用路径（read → navigate → run_code → equip/use）写进 SYSTEM，模型免探索。

## 首个全局技能实战：yangzi-aistudio

- 位置 `~/.agt/skills/yangzi-aistudio/`：**27 文件整包**（洋子 AIStudio 工作流指南）+ 壳 `SKILL.md`（快速路由表 / 模块地图 / 知识桥脚本表——壳指向包内详细文档，配合 skill_navigate 分节读）
- `agt` 与 `Agt` 两 repo 的 `.agent/skills/global-skills.json` 均已激活
- 实测两连：`skill_navigate("yangzi-aistudio")` → 🌐 标识 + 目录树（含知识桥 3 层脚本）+ 章节清单 ✓；`skill_run_code("yangzi-aistudio", "本地知识库外挂/tools/configure_knowledge_base.py", "--help")` → `[exit=0]` usage 输出 ✓

## 验证：test/test_global_skills.py 13/13

单测头注明 spec s_4ac5ccb6 / plan p_8e16d6ed；`monkeypatch _global_skills_root` 把全局根隔离到 tmp_path（不碰真 `~/.agt`）。覆盖：统一寻址（本地优先 shadow / 全局须激活）、激活清单解析（未激活 / dict 形态忽略 / 损坏忽略 / 包不存在静默）、🌐 前缀摘要、skill_run_code 三路逃逸拦截（`../` / 链式 / 非 .py）+ 正常执行 + cwd 实证、skill_evaluate 三级降级、分节导航隔离。

施工中一插曲：`test_navigate_sections` 首跑失败——测试直接用未激活环境的解包变量，修为解包 `env` 后先写激活清单再断言（13 passed）。

## 注意事项

- **/restart 生效**：SYSTEM 技能摘要在 agent 构建时装配——新装全局技能 / 改激活清单后需重启实例（无热重载）
- `save_skill` 永远写 repo 本地；全局技能的沉淀/更新直接编辑 `~/.agt/skills/`（跨实例共享一份）
- 激活清单务必保持**纯字符串数组**——dict 等其它形态被静默忽略（宁缺勿错）
- 本地与全局同名时本地 shadow：想临时覆盖全局技能行为，在 repo 放同名技能即可，不必动全局份
- 技能名只允许字母数字/下划线/连字符（`_NAME_RE` 校验，各工具入口统一报 `[非法名称]`）
- **equip 的服务生命周期**：常驻到 Agent 进程退出（atexit 全杀）；`PYTHONUNBUFFERED=1` 由框架注入——server.py 忘写 flush 也能工作；命令协议（能力清单格式）由技能作者自定，`===CAPS_END===`/`===DONE===` 两个标记是唯一硬约定
- **装卸配对（2026-10-16）**：`skill_unequip(name)`（或重复 equip = replace）按 `_skill_src` 标记撤插段/钩子 + 只停自己拉起的服务 + 清 `session._equipped_skills`；登记纯内存、**重启即隐式全卸**——没有持久「已卸载」清单，卸载是会话内语义（详见 §技能装卸生命周期）

## 相关页面

- [工作流执行工具](workflow-exec-tools.md)：`exec_workflow` 正式执行入口 + **技能携带工作流**（`_load_wf_canvas` 二级加载）——带 server.py 的技能配常驻服务，带 workflows/ 的技能配确定性流水线
- [工具外置体系](tool-externalization.md)：技能工具属引擎内置（`src/agent_config.py` 注册，子 Agent 继承），不走外置件扫描
- [run_python](run-python.md)：同为子进程执行工具，但通用工作区任意脚本 vs 技能包限定 + cwd 锚定技能目录
- [多 Agent 体系](../architecture/multi-agent.md)：技能工具随 Agent 注入，子 Agent 自动继承
- [运维与存档布局](../guides/ops.md)：`~/.agt/` 单一数据根（全局技能目录挂这里，多实例共享）
- [doctor · BadRequest 诊断修复 Agent](doctor.md)：坏图 / 超长 / 畸形消息 400 的 L2 诊断修复线（典型案例源头即 worker 施工线坏图三连炸）

