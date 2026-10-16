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

## 双层与激活

- repo 本地 `.agent/skills/` 直接可用；全局 `~/.agt/skills/` 需在本 repo
  `.agent/skills/global-skills.json`（纯字符串数组）里点名激活。
- 同名时本地覆盖全局（shadow）。

## 深入

- 七件套工具：read_skill / save_skill / skill_navigate / skill_run_code / skill_evaluate / skill_equip / skill_use
- 已有范例：`test-svc`（服务协议）、`pbridge`（服务+工作流+SOP 完整体）、`yangzi-aistudio`（全局技能）
