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

## 双层与激活

- repo 本地 `.agent/skills/` 直接可用；全局 `~/.agt/skills/` 需在本 repo
  `.agent/skills/global-skills.json`（纯字符串数组）里点名激活。
- 同名时本地覆盖全局（shadow）。

## 深入

- 七件套工具：read_skill / save_skill / skill_navigate / skill_run_code / skill_evaluate / 技能携带工作流（workflows/ 子目录）：
技能包可带 `workflows/*.xml`——`exec_workflow('<名>', {...入参})` 可直接调用（与 repo 工作流同名时 repo 侧优先；仅 exec/debug 入口可见，不进编辑器与钩子发现）。
`read_skill` 尾部会自动列出本技能可调用工作流的**名称/描述/入参 schema**（从工作流文件现场解析，作者零负担）——技能作者只需在 XML 根标签写好 `description`、在开始节点声明好出参，Agent 即可感知这层用法。

skill_equip / skill_use
- 已有范例：`test-svc`（服务协议）、`pbridge`（服务+工作流+SOP 完整体）、`yangzi-aistudio`（全局技能）
