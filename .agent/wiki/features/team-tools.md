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

> **补记（同日 commit 2effa73）**：cb57597 补的 `- seg: user_message` 这类 **dict 形态此前被 `_asm_item_from_dict` 静默丢弃**（只认动作键与已知段名做键）——直到 2effa73 加 `seg:` 键分支 + 补 `tail.*` 白名单（`tail.time` 等连字符串形态也会被当未知段名丢弃）才真正生效。修复细节与「写→读→解析」全链验证教训见 [multi-agent · assembly DSL](../architecture/multi-agent.md#assembly-dsl上下文装配配方)。

#### 补记二：声明最终形态 = 裸字符串段 + func 项（同 commit 504a518）

> **补记二（同日 commit 504a518，用户裁定「没必要定义 {seg:x} 和 tail.* 这些东西」）**：`seg:` dict 形态与 `tail.*` 拆段**当天引入当天撤销**——最终声明形态 = 裸字符串段（`- user_message` / `- steps`）+ func 项（`- func: print_time()` 替代 tail.time、`- func: get_team_profiles()` 替代 tail.system 团队部分）。`tail.*` 六拆段恢复单一 `tail` 段（`_expand_tail` 恒等直通）。team-manager.md 已同步为最终形态（text 块 + user_message + steps + func 三件套）。详见 [multi-agent · 段形态简化定稿](../architecture/multi-agent.md)。

> **补记三（commit dd5b0b4）**：`seg:` dict 分支**当日撤销后于同批恢复**——steps 段模式提案（steps=reasoning）复盘发现 docstring 残留声明但代码无分支，`{seg: steps=reasoning}` 仍被静默丢弃；已恢复并委托 `_asm_item_from_str` 全语义解析（|optional/=mode//描述 全生效）。最终**三形态并存**：裸字符串 / `{steps: reasoning}`（段名做键）/ `{seg: steps=reasoning}`（段名做值）。详见 [multi-agent · 段形态简化定稿后记](../architecture/multi-agent.md)。

#### 补记四：六 Agent 声明巡检 + team-manager 补全 + 团队看板 func 视角化（2026-09-02）

> **补记四（2026-09-02，用户「有几个子 agent 没有加钩子，装配好像也不太全，检查一下」→ 六声明逐一核对）**：coder / explorer / reviewer / vision / wiki-updater 五者**健康**（recap_gen 钩子 + file + history|optional + user_message + steps 齐全）；**team-manager 是唯一问题户**——此前「已同步最终形态」的记录不实：hooks **无 recap_gen**（它的 recap 从没上过看板）、assembly **缺 history/user_message/steps**（巡检连续性/任务消息缺失）、还有占位残留 text 块（「You are a team manager. 见上方装配…」）。
>
> **补全三件**：hooks 补 `turn_end: recap_gen`；assembly 补 `history|optional` + `user_message` + `steps`；删占位残留。
>
> **同批六 Agent 统一挂团队看板 func**（`- func: get_team_profiles()` 加进各自 assembly 尾部）——配套做了**视角机制**：`resolve_assembly_func(name, viewer_id)`（src/agent_config.py）签名探测式传参会话所属 agent（src/session.py func 项求值转发 `self._asm_agent_id`），`get_team_profiles` 据此 **exclude 看者自己**——子 Agent 装配看板时能看到主 Agent 忙闲 + 其他队友 + 各自 recap（它们本就有 agent_ask/notify 通信工具，此前却不知道队友是谁）；**不再恒 exclude 主 Agent**。顺带修掉恒 ImportError 坏路径（`from multiagent import format_team`——那是 AgentRegistry 方法非模块级函数，异常被吞恒空串）→ 改走运行时挂点 `_RUNTIME_AGENT`。无 viewer_id 参数的函数（runtime_env 等）不受影响。详见 [multi-agent · func viewer_id](../../architecture/multi-agent.md) 与 [agents-admin · FUNC_REGISTRY 扩容](../agents-admin.md)。

#### 补记五：跨实例七 Agent 声明巡检——8000 实例（comfy repo）修补闭环（2026-09-02）

> **补记五（2026-09-02，用户「连到 8000 的服务实例上，检查那边的各 sub-agent 装配，不合适帮他补一补」→ remote_ask 探明 + remote 工具跨实例修补）**：把本 repo 六声明巡检（补记四）的同一套标准搬到**远程实例**（comfy repo，agt-8000）——7 声明逐一核对，**差距比本 repo 大得多**：
>
> - **只有 coder / explorer / reviewer 结构完整**，缺的只是新看板
> - **装配残缺三户**：vision 只有孤零零一个 file 项（无 history/user_message/steps）、wf-reviewer 只有 text 一项、wiki-updater 只有 file（缺 wiki_tree/装配段）——**白名单坑（没列的段不装）在手工声明里比预想普遍**，不止 team-manager 一例
> - **无钩子四户**：vision / wf-designer / wf-reviewer / wiki-updater（recap 从没上过看板）
> - wf-calibrator 基本健康（有 print_time + 钩子），只缺看板
>
> 修补动作：残缺者补全装配段（`history|optional` + `user_message` + `steps`；wiki-updater 加 `tool: wiki_tree()`）+ 四户补 `turn_end: recap_gen` + **七 Agent 统一加 `- func: get_team_profiles()`**。验证：7 份 yml 全量 `yaml.safe_load` 解析通过 + 看板/recap 钩子/装配段断言全绿。**声明按 mtime 热重读——对方下次 `agent_prompt` 即生效，无需重启**。
>
> **跨版本兼容洞察**：`get_team_profiles()` 若在**旧版本 agt**（FUNC_REGISTRY 扩容 commit c7a9339 之前）上求值——白名单查不到 → 返回空 → 该 func 段**自动跳过**，内插空判语义使其**无害**；对方升级后自动生效。func 项装配到跨版本实例上安全——**安全失败方向是变空而非报错**。
>
> **8000 侧坏 recap + 双层 `<system-reminder>` 均为内存态**：磁盘 recap 已确认为空、嵌套修复已进代码（本 repo 侧），那边 `/restart` 一并消失——与本 repo [坏 recap 定性](../architecture/multi-agent.md#坏-recap-定性磁盘为空内存残留重启即清2026-09-02) 同结论。

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
