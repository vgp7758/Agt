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

## 创建子 Agent（官方路径：直接写 yml）

create_agent 工具已退役（DSL 字段越来越多，工具参数面板跟不上声明演进）。
直接 `write_file` 一个 yml 即创建（下一轮 SYSTEM 自动列出、agent_prompt 可派活）：

```yaml
# .agent/agents/<name>/<name>.yml —— 最小模板
name: my-agent
description: 一句话作用 + 何时调用（投影给主 Agent 决定何时派活）
model: glm-official-flash        # 留空=主 Agent 当前模型
tools: ""                        # 留空=继承全部；或逗号分隔白名单
assembly:
  - file: .agent/agents/my-agent/my-agent.md   # 人设（可选，也可用 text: 内嵌）
  - user_message
  - steps
  # - image_feed: http://127.0.0.1:8765/frame  # 实时画面段（视觉模型）
# services:                      # 依赖服务（实例化幂等拉起，kill 时同停）
#   - my-frame: python .agent/agents/my-agent/tools/frame.py 8765
```

人设 md（可选）、专属工具 `tools/*.py`（agt_register 约定）放同目录。
写完 yml 的下一步，agent_prompt/kill_agent 的 name 下拉与 SYSTEM 清单自动刷新。
