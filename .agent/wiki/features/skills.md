# 技能体系 · SKILL.md 技能包 + 双层寻址（repo 本地 / 全局 ~/.agt/skills）+ 技能工具五件套

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

## 技能工具五件套（原二 + 新三）

`SKILL_TOOLS = Toolbox(Tool(read_skill), Tool(save_skill), Tool(skill_navigate), Tool(skill_run_code), Tool(skill_evaluate))`

| 工具 | 职责 | 关键语义 |
|---|---|---|
| `read_skill(name)` | 读完整 SKILL.md（含详细 SOP） | 统一寻址；找不到时 `_skill_not_found` 指路：「若是全局技能，需在本 repo 的 global-skills.json 激活清单里启用」 |
| `save_skill(name, description, when_to_use, sop)` | 沉淀新技能 | **仍写 repo 本地** `.agent/skills/`（全局技能的维护直接编辑 `~/.agt/skills/` 文件） |
| `skill_navigate(name, section, list_only)` | 结构浏览 / 分节读 | 见下 |
| `skill_run_code(name, script, args)` | 执行技能包内 .py | 见下 |
| `skill_evaluate(name, input)` | 技能约定评测入口 | 见下 |

### skill_navigate · 大技能不必整读

- 默认返回：目录树（**≤3 层**，跳 `.` 开头 / `__pycache__`，120 条上限防刷屏）+ SKILL.md 章节清单（1~3 级标题带行号）
- `section="章节标题"`（含/不含 `#` 均可，大小写不敏感）→ 只读该章节正文（**12K 字截断**）；未命中则回显全部可用章节
- `file="包内相对路径"` → 读该文件正文（超长 **12K 字截断**；markdown 文件附章节清单，纯文本直读；可再配 `section=` 分节精读）——见下节后记
- `list_only=True` 只列结构不附提示尾注

#### 后记：file= 读包内任意文件——技能包文件读取的一等通道（2026-09，用户问句触发，commit e5b0e16a）

用户问「技能里有很多文件，读取其它文件的话是什么工具」——问句实测暴露缺口：`read_file` 限 workspace 内（全局技能包在 `~/.agt/skills/`，workspace 外读不了），`skill_navigate` 原来只导航 SKILL.md 本身。结果：包内其它文件（洋子技能的 8 个专业模块、口令卡、使用说明……）**此前只能 `run_python` `open()` 绕行**——能用但不体面，且模型不一定想得到。

**修复**：`skill_navigate` 加 `file=` 参数（`src/agent_config.py`；`_md_sections` / `_md_section_text` 两个 markdown 章节 helper 从 SKILL.md 专用扩展到任意包内文件）：

| 用法 | 返回 |
|---|---|
| `skill_navigate(name)` | 目录树 + SKILL.md 章节清单（默认行为不变） |
| `skill_navigate(name, file="专业模块/08-XXX.md")` | 该文件正文 + 章节清单（超长 12K 截断） |
| `skill_navigate(name, file="…", section="章节名")` | 大文件分节精读 |
| `skill_navigate(name, file="02-口令卡.txt")` | 纯文本直读 |

- **防逃逸与 skill_run_code 同款**：禁 `..`、resolve 后必须落在技能目录内（统一寻址得到目录后再校验，本地/全局技能均可读）
- 文件不存在 → 提示 `list_only=True` 查目录树纠错
- 实测 6 项全绿：44KB 大模块章节化读取 / 分节精读 / txt 直读 / 逃逸拦截 / 不存在提示 / 默认行为不回归
- 效果：从总控路由到 08 号模块分节精读，全程不出技能工具集，不再依赖 run_python 绕行

**生效注意**：新参数进工具 schema 需实例重启（与 SYSTEM 摘要同口径，无热重载）——`/restart` 后 agent 的工具 schema 才带 `file`。

### skill_run_code · 技能包内脚本执行（安全三道闸）

- 脚本须为技能目录内 `.py` 相对路径；三道闸：①必须 `.py` 后缀 ②路径 parts 禁 `..` ③`(d/script).resolve()` 后技能目录 `resolve()` 必须在其 parents 内（防符号链接/规范路径逃逸）
- `cwd=技能目录`（脚本可用相对路径读包内资产）；`args` 经 shlex 分词（支持引号）；120s 超时；输出截断 8000 字；返回带 `[exit=N]`
- 脚本不存在 → 列出包内全部 .py（≤40 条）辅助纠错

### skill_evaluate · 评测约定三级降级

1. `scripts/evaluate.py` 存在 → 以 `input` 作 **stdin** 执行（cwd=技能目录，120s，输出 8000 字截断）
2. 否则包内 `EVAL.md` → 返回评测说明（6K 截断）
3. 都没有 → 明确提示「技能作者未约定评测方式」（不猜不编）

## SYSTEM 摘要与使用教育（src/chat.py）

装配 SYSTEM 技能段（`skills_summary`）时一行一技能，全局技能加 **🌐 前缀**：

```
=== 可用技能（repo .agent/skills/ + 已激活全局技能；🌐=全局）===
任务匹配某技能时，先 read_skill(name) 取详细 SOP 再按它执行
（大技能先 skill_navigate(name) 浏览结构/分节读；技能自带脚本用 skill_run_code(name, script) 执行）：
- yangzi-aistudio: …（使用时机: …）
（完成可复用任务后可用 save_skill 沉淀新技能到本 repo）
```

教育文案随双层化同步升级：五件套的使用路径（read → navigate → run_code）写进 SYSTEM，模型免探索。

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

## 相关页面

- [工具外置体系](tool-externalization.md)：技能工具属引擎内置（`src/agent_config.py` 注册，子 Agent 继承），不走外置件扫描
- [run_python](run-python.md)：同为子进程执行工具，但通用工作区任意脚本 vs 技能包限定 + cwd 锚定技能目录
- [多 Agent 体系](../architecture/multi-agent.md)：技能工具随 Agent 注入，子 Agent 自动继承
- [运维与存档布局](../guides/ops.md)：`~/.agt/` 单一数据根（全局技能目录挂这里，多实例共享）
