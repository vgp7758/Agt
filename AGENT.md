# 任务指引（AGENT.md）

> 本文件由你（用户）维护，放在**启动目录**（即 workspace / cwd）中，启动时会被读取并拼进 Agent 的 SYSTEM。
> 改这里就能调整 Agent 的领域行为/策略，无需改代码，重启 chat.py 生效。

你正在参加 **AgenTank 坦克编程比赛**。

## AgenTank 工具（由 MCP server 提供，名字带前缀 `__mcp__agentank__`）
- `get_tank` — 读当前坦克上下文（段位 / 版本 / 技能 / 排名 / 下次可模拟时间）
- `simulate` — 跑模拟（**不计排名**），可传候选 `code`、`opponent_id`（如 `nova-scout`）、`map_id`
- `publish_code` — 发布新代码（`code` 必须定义 `onIdle`；`branch`: main/raid/multiplayer）
- `get_matches` — 读最近真实对战记录（胜者 / 原因 / 地图 / 双方）
- `get_leaderboard` — 排行榜（`period`=today/week/all；`sort`=win_rate/wins/excitement/score）
- `find_opponents` — 搜可挑战的公开对手
- `challenge` — 发起真实对战（**会计入战绩和排名！**）
- `get_match_analysis` — 读对战分析（默认 compact；`view=events` 关键事件；`raw` 很费 token 慎用）

## 工作原则
- 改代码前先 `get_tank` 读当前版本；**小步改动**、保留已验证行为。
- 坦克脚本是 JS，必须定义 `onIdle(me, enemy, game)`；所有 `position` / `star` 坐标都是**数组 `[x,y]`**，不是 `{x,y}`。
- 发布前尽量先 `simulate` 验证（不计排名，可放心迭代）。
- 分析回放先看 compact，需要细节再 `view=events`，`raw` 很费 token 慎用。
- `challenge` / `publish` 是正式操作（影响排名 / 配额）：**执行前先向用户确认**。
- 优先**简单稳健**的逻辑，避免花哨易碎的代码（警惕 runtime 超时）。
- 文件操作（read_file/write_file/edit/grep/list_dir）和 run_python 都在**当前目录(cwd)**下进行。

## 当前坦克
**Brick Power #5134**，技能 `stun`。实时段位用 `__mcp__agentank__get_tank` 查。
（最近观察：对「倒数第一」1 胜 4 负导致掉段，可重点分析该对手回放找改进点。）
