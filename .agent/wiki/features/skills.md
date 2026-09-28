# 技能体系 · SKILL.md 技能包 + 双层寻址（repo 本地 / 全局 ~/.agt/skills）+ 技能工具七件套

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

## 技能工具七件套（原二 + 新五）

`SKILL_TOOLS = Toolbox(Tool(read_skill), Tool(save_skill), Tool(skill_navigate), Tool(skill_run_code), Tool(skill_evaluate), Tool(skill_equip), Tool(skill_use))`

| 工具 | 职责 | 关键语义 |
|---|---|---|
| `read_skill(name)` | 读完整 SKILL.md（含详细 SOP） | 统一寻址；找不到时 `_skill_not_found` 指路：「若是全局技能，需在本 repo 的 global-skills.json 激活清单里启用」 |
| `save_skill(name, description, when_to_use, sop)` | 沉淀新技能 | **仍写 repo 本地** `.agent/skills/`（全局技能的维护直接编辑 `~/.agt/skills/` 文件） |
| `skill_navigate(name, section, list_only, file)` | 结构浏览 / 分节读 / 读包内文件 | 见下 |
| `skill_run_code(name, script, args)` | 执行技能包内 .py（一次性无状态） | 见下 |
| `skill_evaluate(name, input)` | 技能约定评测入口 | 见下 |
| `skill_equip(name)` | 装备技能常驻服务（server.py） | 见下 |
| `skill_use(name, command)` | 向已装备服务发命令 | 见下 |

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

## 相关页面

- [工作流执行工具](workflow-exec-tools.md)：`exec_workflow` 正式执行入口 + **技能携带工作流**（`_load_wf_canvas` 二级加载）——带 server.py 的技能配常驻服务，带 workflows/ 的技能配确定性流水线
- [工具外置体系](tool-externalization.md)：技能工具属引擎内置（`src/agent_config.py` 注册，子 Agent 继承），不走外置件扫描
- [run_python](run-python.md)：同为子进程执行工具，但通用工作区任意脚本 vs 技能包限定 + cwd 锚定技能目录
- [多 Agent 体系](../architecture/multi-agent.md)：技能工具随 Agent 注入，子 Agent 自动继承
- [运维与存档布局](../guides/ops.md)：`~/.agt/` 单一数据根（全局技能目录挂这里，多实例共享）

