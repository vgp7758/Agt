# 团队管理工具 · team_up / team_status（外置件 team_tools.py）

> 2026-09-02（night_tasks #4，commit f232bd8）。多实例组网时成员的经验绑在各自 session 上——手动逐个拉起太被动；team_up 按清单一键完成「启动成员进程 → 等端口就绪 → remote_connect 组网 → 恢复指定 session」，team_status 总览团队状态。MCP 形态留作后续（当前工具组已覆盖核心操作；跨 agt 复用时再 MCP 化）。

## 职责

- 把多实例组网从「手动逐个 remote_connect + /resume」变成「读一份成员清单，一键拉起整个团队」
- 成员经验绑定 session——恢复指定 session = 成员带着历史上下文上岗（新成员 session 留空=全新开始）
- 组网拓扑：**星型**——成员都连到本实例，本实例的 Agent 用 remote_ask / remote_message 与各成员通信，成员间经本实例中转；成员进程独立于本进程（崩了互不影响）

## 清单格式（.agent/team.yml，样例 .agent/team.example.yml）

```yaml
members:
  - name: director          # server_id（组网标识，remote_ask/message 用它）
    repo: D:/Projects/X/director   # 成员 repo（agt-web 的 cwd）
    port: 9201
    session: ""             # 要恢复的 session 名（空=不主动恢复，保持其自动行为）
    role: 导演——把控叙事与分工
```

必填：name / repo / port；session 可空。清单不存在 / 无 members / 成员缺必填项 → ValueError（带「参考 .agent/team.example.yml 建一份 .agent/team.yml」指引）。

## team_up(manifest, dry_run=True, connect=True)

按清单拉起团队，流程四步：

1. **启动成员进程**：`agt-web --port <port>`，cwd=成员 repo（`subprocess.Popen`，Windows 下 `CREATE_NO_WINDOW`）
2. **等端口就绪**：最多 60s（冷启动含 session 恢复），`probe_server` 每秒轮询
3. **remote_connect 组网**（connect=false 只启动不组网）
4. **恢复指定 session**：`remote_message(server_id, "/resume <session>")`

**dry_run 默认 true**——只打印执行计划不真拉；确认后传 `dry_run=false` 执行。已在运行的成员跳过启动直接组网。返回逐成员结果行（✅ / ⚠️ 60s 未就绪 / ❌ 启动失败）。

## team_status(manifest)

团队成员状态总览：逐成员 `POST /api/status` 探测（超时 4s）——🟢 在线（模型 · session · ⏳busy/💤空闲 · turns）/ 🟡 服务在但 Agent 未就绪（可能在恢复 session）/ 🔴 不可达（提示 team_up 拉起）。

## 注册与实现

- 载体 `tools/builtin/team_tools.py`（外置件，`agt_register(ctx)` 注册 `team_up` / `team_status`，group="团队"）；`/reload tools` 热加载。⚠️ **随包副本 `src/assets/tools_builtin/` 待同步**（2026-09-02 只写了 tools/builtin/）
- 复用引擎 remote_* 基础设施：`remote_tools.probe_server` / `connect` / `send_message`
- 配套声明：`.agent/agents/team-manager.md`——team-manager 子 Agent（local-lfm 巡检省 token）；`agent_prompt("team-manager", "把团队拉起来")` 即可让它运营

### team-manager 定位与装配修复（2026-09-02，commit cb57597）

用户评审指出两问题并落地：

1. **装配缺 user_message/steps（白名单坑实锤）**：原声明 assembly 只列 text——装配是**白名单语义**，没列的段不装 → 任务消息不进投影，**Agent「看起来活着但收不到活」**。补 `user_message / steps / tail.time / tail.system` 四段
2. **定位改为 VideoGameTeam 固定成员**：是视频游戏开发团队的成员，不是本 repo 的临时 sub-agent
3. **tools 显式白名单**：`team_up, team_status, remote_*`——team_tools 作它的**专属插件**（不继承主 Agent 全量工具）

**坑的通用化**：`create_agent` 默认装配已显式含 `user_message,steps` 防同类（见 [multi-agent · create_agent 传参拓展](../architecture/multi-agent.md)）；手工声明务必把必需段列全（/agents 管理页保存也会显式化）。

## 与其他模块的关系

- 上游：[multi-instance · 组网](../architecture/multi-instance.md)——remote_connect / remote_message / auto server_id 是团队组网的底层通道；team_tools 是把它们编排成「团队级操作」的一层
- 成员经验：session 恢复（/resume）——[存档布局](../guides/ops.md#存档布局)
- 与 [remote-client](../features/remote-client.md) 的区分：remote 是单实例对单实例；team 是清单驱动的多实例编排

## 注意事项

- 成员进程用 PATH 里的 `agt-web` 启动——成员 repo 环境需保证 agt-web 在 PATH（启动失败会在结果行标 ❌）
- dry_run 先行：先看执行计划再真拉
- 星型拓扑：成员间通信经本实例中转，本实例下线则成员间不能直接互通

## 相关页面

- [多实例组网](../architecture/multi-instance.md)
- [工具外置体系](../features/tool-externalization.md)
- [跨实例客户端](../features/remote-client.md)
