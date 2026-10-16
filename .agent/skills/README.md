# .agent/skills/ — 技能包目录

一个目录一个技能：`SKILL.md`（frontmatter + SOP 正文）为主，可选携带可执行资产。
SYSTEM 常驻一行摘要（name + description + when_to_use），任务匹配时 Agent 再 `read_skill` 取全文。

## 技能包结构（全部可选，SKILL.md 为核心）

```
my-skill/
├── SKILL.md              # ---\nname/description/when_to_use\n--- + SOP（markdown）
├── scripts/              # skill_run_code("my-skill", "scripts/x.py", "--args") 可执行
│   └── evaluate.py       # 技能约定评测：skill_evaluate("my-skill", input) 时以 input 为 stdin 执行
├── server.py             # 常驻服务（可选）：skill_equip 拉起、skill_use(name, "cmd --args") 调用
├── workflows/            # 技能自带工作流：exec_workflow("…") 可直接执行
└── EVAL.md               # 无 evaluate.py 时的评测说明
```

## server.py 行协议（常驻服务）

启动后 stdout 打印能力清单到 `===CAPS_END===`；之后每读 stdin 一行命令，
输出结果若干行到 `===DONE===`。示例见本框架自带技能 `test-svc`。

## 装配声明（SKILL.md frontmatter `assemblies:`，2026-10-16）

技能可自带装配清单——装备时把装配段/钩子/帧提供者插进宿主 Agent：

```yaml
assemblies:
  - position: after:steps        # before:<段名> / after:<段名>：往宿主装配清单锚段前/后插
    ass:
      - text: 【注入段】          # ass 复用 yml assembly 段语法（file:/cmd:/text:/workflow:…）
      - image_feed: {tool: screen_frame()}   # 帧提供者：每步取帧=调工具表达式
  - position: hook:turn_end      # 技能 workflows/ 下全部工作流挂到钩子位
                                 #（或显式 ass: ["wf名|async"]）
```

- 两个装备口：静态口（子 Agent yml `skills:` 装备链读此字段）+ 动态口（skill_equip 时
  server.py 的 caps 输出带一行 `EXTRA_ASSEMBLIES: <json>`）。
- 锚必须是宿主已装配的段名——锚不存在装备时清单化报错（不静默丢弃）。
- 装备时冻结：声明解析后作为静态快照进宿主 session，逐轮不再重算；多技能同位置按
  装备顺序稳定追加。范例：`desktop-ops`（画面帧 `image_feed: {tool: screen_frame()}`）。

## 装卸生命周期（skill_equip / skill_unequip，2026-10-16 用户裁定）

技能装备 = **需要就装、用一阵子、不需要就卸的临时能力**（不做持久声明；重启即隐式全卸）。

```
skill_equip("x")     → ①extra_assemblies 中间件带上（插段/钩子/帧提供者，带 _skill_src 来源标记）
                       ②frontmatter services: 默认拉起（见下）③登记进 session._equipped_skills（纯内存）
                       ④重复 equip = replace（先撤旧再装新，不重复插段）
skill_unequip("x")   → 按标记撤插段/钩子 → 停自有服务（reuse 复用的不拥有、不停）→ 清登记
```

`services:` 声明（frontmatter，同子 Agent yml 语法，`| reuse=<探测URL>` 支持跨实例复用）：

```yaml
services:
  - desktop-frame: python tools/frame.py 8765 | reuse=http://127.0.0.1:8765/frame
```

- **拉起语义**：探测活 → 复用（不拥有、unequip 不停）；连接拒绝 → 自己拉起（记入登记，unequip 时停）
- **重启语义**：登记纯内存——进程结束装备即失（临时定位）。重启后再 unequip 走容错：
  尽力按标记撤 + 提示登记丢失；自有服务因无从追溯不停（已提示）
- **三句话义对齐**：市场层 /install（文件安装）→ 装备层 skill_equip（临时能力装卸）→
  实例层 yml `skills:`（子 Agent 声明式，实例死即卸）

## 双层与激活

- repo 本地 `.agent/skills/` 直接可用；全局 `~/.agt/skills/` 需在本 repo
  `.agent/skills/global-skills.json`（纯字符串数组）里点名激活。
- 同名时本地覆盖全局（shadow）。

## 深入

- 八件套工具：read_skill / save_skill / skill_navigate / skill_run_code / skill_evaluate / skill_equip / skill_unequip / skill_use
- 已有范例：`test-svc`（服务协议）、`pbridge`（服务+工作流+SOP 完整体）、`yangzi-aistudio`（全局技能）
