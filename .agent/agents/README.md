# .agent/agents/ — 子 Agent 声明目录

**一个子 Agent 一个目录**（2026-10-06 起）：

```
<name>/
  <name>.yml    # 声明：name/description/model/tools/assembly/hooks/fallback
  <name>.md     # 人设（persona，assembly 用 file: 引用）
  tools/        # 专属工具（agt_register 约定，同 tools/builtin）
```

目录形态自包含（声明+人设+专属工具整体拷贝即分发）；平铺 `<name>.yml` 旧形态仍兼容读取。
create_agent 默认按此结构创建；随包默认子 Agent 播种也按目录结构。
声明后自动出现在团队（agent_prompt 派活；kill_agent 删整个目录）。
