---
name: test-svc
description: 技能功能全覆盖最小范例（read/navigate/run_code/evaluate/equip/use 演练场）——可运行、可删除
when_to_use: 想看技能包各文件各自对应哪个工具时；写自己的技能前参考结构时
---

# test-svc — 技能七件套最小范例

本技能包刻意做小，但**覆盖技能体系的全部工具入口**：

| 文件 | 对应工具 | 演示什么 |
|---|---|---|
| `SKILL.md`（本文件） | `read_skill` / `skill_navigate` | SOP 本体 + 章节（navigate 按标题分节读） |
| `server.py` | `skill_equip` / `skill_use` | 常驻**有状态**服务（计数器，跨调用保持） |
| `scripts/greet.py` | `skill_run_code` | 包内脚本一次性执行（CLI 参数） |
| `scripts/evaluate.py` | `skill_evaluate` | 验收入口：跑自检输出 PASS/FAIL |

## 用法

```python
read_skill("test-svc")                # 全文 SOP
skill_navigate("test-svc")            # 包结构 + .md 标题大纲
skill_run_code("test-svc", "scripts/greet.py", "阿三 --times 2")
skill_evaluate("test-svc", "")        # 自检 → EVAL PASS / FAIL
skill_equip("test-svc")               # 装备常驻服务 → 返回能力清单
skill_use("test-svc", "add 5")        # → count=5（跨调用状态保持）
```

## 服务协议（server.py）

启动打印能力清单到 `===CAPS_END===`；每条命令输出结果到 `===DONE===`。
照这个骨架写自己的 server.py，技能即可携带常驻服务（模型加载/会话/焦点等跨调用状态）。

## 深入

- 协议与七件套详解：wiki `features/skills.md`
- 双层激活：全局 `~/.agt/skills/` + 本 repo `.agent/skills/global-skills.json`
