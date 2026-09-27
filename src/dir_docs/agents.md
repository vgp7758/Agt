# .agent/agents/ — 子 Agent 声明目录

一个文件一个子 Agent（`.md` 推荐，frontmatter 声明 + 正文人设；也兼容 `.yml`）。
声明后自动出现在团队里：主 Agent SYSTEM 的团队清单会列出，`agent_prompt(name, 任务)` 派活。

## 最小示例

```markdown
---
name: translator
description: 中英互译。何时调用：需要高质量中英翻译时
model: deepseek
fallback: glm, deepseek-flash
assembly:
  - user_message
  - history
  - skills
---

你是专业译者……（SYSTEM 人设正文）
```

## 常用字段

| 字段 | 说明 |
|---|---|
| model / fallback | 主模型 / 回退链（逗号分隔，空=显式关闭） |
| assembly | SYSTEM 装配清单：user_message / history / skills / ltm / team_board / remote_instances / steps / recent_file / plan …；`段名\|optional` 默认关闭、按需开启；`//` 后为注释 |
| hooks | 钩子挂工作流：`before_turn: [wf名]`、`turn_end: …`，可加 `async: true` |
| tools | 限制工具集（默认继承全部） |

## 调用与恢复

- `agent_prompt("translator", "翻译：…")` —— `reuse: yes/no` 控制复用/新建；`current_turn_only: true` 让它只看本轮
- WebUI 下拉框可切到子 Agent 视角；实例与 session 一起持久化恢复

## 深入

- 管理页：`/agents`（可视化编辑声明/装配/钩子）
- 装配 DSL 细节：wiki「子 Agent 与 assembly」页
