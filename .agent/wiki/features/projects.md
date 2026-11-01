# 项目组 · projects.json + plan.md + 每 5 分钟巡检 + coordinator 协调 Agent（施工牌 p_2d4f784f，2026-11-19）

> 一句话：**项目 = 一组成员实例 + 一份计划文档 + 一个自动巡检定时任务**。看板一键建项目，巡检发现「全员空闲且久未派活」就把 coordinator 唤醒，coordinator 收进展→对照计划派活→记账，全完成再通知人。

## 三件套与真源

| 件 | 载体 | 谁写 |
|---|---|---|
| 项目清单 | `.agent/projects.json`（repo 级，随 git 走） | `create_project` / `add_project_member` / `remove_project_member` / `delete_project`（工具组单源；前端只经 `/api/tool/exec` 调） |
| 计划文档 | `.agent/projects/<id>/plan.md` | coordinator 用 read/write/edit 维护（勾进度 / 「进行中」段 / `last_dispatch:`） |
| 巡检任务 | Scheduler 定时任务 `project-<id>-patrol`（每 300s，`code` 型，`mode=idle`） | 创建项目时 `_ensure_patrol_schedule` 自动挂；`delete_project` 自动 cancel |

- `projects.json`：`{"projects": [{id, name, desc, members:[{id,kind,url}], plan_file, created_at, patrol, schedule}]}`；`_LOCK`（RLock）护「读-改-写」，前端/工具/巡检并发安全；文件**惰性创建**——无项目时不存在是正常的。
- id 形态 `<uuid6>-<序号>`（如 `85d08b-1`）；**项目名唯一**（重名 ValueError）。
- 成员双源解析 `resolve_member`：先 remote 表（`remote_tools.REMOTE_SERVERS` 的 server_id，精确匹配）再 registry（本地子 Agent 的 agent_id，排除 `_main_`/`main`）；都没有 → ValueError 带候选清单（提示 remote 实例先 `remote_connect`）。
- 工具组 `.agent/tools/project_tools.py`（外置件，`group="项目"`，改完 `/reload tools` 热加载）。

## 工具六件

| 工具 | 语义 |
|---|---|
| `create_project(name, member_ids, desc="", patrol=True)` | 写 projects.json + 建 plan.md 骨架 + 挂巡检；返回项目卡摘要（id/成员/巡检任务名） |
| `list_projects(project_id="", probe=True)` | 列项目 + 成员忙闲实时探测（⏳忙 / 💤空闲 / ❔不可达）；`probe=false` 跳探测（快） |
| `add_project_member` / `remove_project_member` | 双源解析后增删（已在/不在则幂等返回，不动实例本身） |
| `delete_project` | 三步：cancel 巡检 + 删条目 + 计划目录 **rename 进 `.agent/projects/.trash/<id>-<ts>/`**（归档非硬删，防误删不可恢复） |
| `patrol_projects` | 手动跑一轮巡检（与 schedule code 同逻辑），返回唤醒消息或「无事可做」说明 |

## 巡检判定（`patrol_once` / `_patrol_code_src` 双形态同逻辑）

1. 逐成员探忙闲：remote → `POST /api/status`（busy / session_turns，timeout 4s）；local → registry `entry.status == "running"`。
2. **全员空闲**才进下一步；任一成员忙，或**探测失败（busy=None）→ 整个项目静默跳过**（不假设、不误催）。
3. 冷却窗口 `PATROL_COOLDOWN=15min`：plan.md 里 `last_dispatch: <ISO>` 距今 <15 分钟 → 静默（防重复派活风暴；无该行 = 0.0 = 立即可唤醒）。
4. 命中 → 产出唤醒文案（🔔 项目《X》全员空闲——请巡检推进：read plan → 收进展 → 对照计划派活；派活后更新 last_dispatch 与「进行中」段），作为 schedule 消息注入**主 Agent**，由主 Agent 转派 coordinator。

**code 体独立成串**（`_patrol_code_src()` 生成）：schedule exec 环境只注入 `agent`/`result` 两个全局量，不 import 本模块——热加载后旧 code 不悬空。相同逻辑因此有两份（工具态 `patrol_once` / 定时态 code 串），改动需双份同步。

## plan.md 骨架（`_PLAN_TEMPLATE`）

目标 / 里程碑（`- [ ]`）/ **进行中**（coordinator 维护：谁在干什么 + 何时派的；段非空且成员 idle → 先问询再重派）/ 进展记录 / 末行 `last_dispatch:` —— 计划文档即「防重派」与「冷却」两个机制的共用账本。

## coordinator 协调 Agent（只协调不代做）

声明 `.agent/agents/coordinator/`（目录形态）：`model: glm-official-flash`；装配 = `file: coordinator.md` + `func: load_remote_instances()` + `func: get_team_profiles()` + `user_message` + `steps`；钩子 `turn_end: recap_gen`（async，看板可见）。

**工具面刻意收窄（21 件）**：通信/查询/计划/团队类——`list_team, agent_notify, agent_ask, agent_query_events, remote_list, remote_message, remote_ask, remote_call_tool, remote_connect, team_up, team_status, create_plan, update_plan, add_step, list_plans, read_file, write_file, edit, add_schedule, list_schedules, cancel_schedule`；**无 run_python / run_shell / grep / explore**——「动手的活永远派给成员」，写盘只允许维护 plan.md。

巡检推进 SOP（人设正文八步）：读计划 → 查忙闲（忙则不打断）→ 收进展（本地 `agent_query_events`，远端 `remote_ask` 一句话）→ 对照里程碑找下一步 → **防重派检查**（「进行中」段非空且成员已空闲 → 先问询再重派）→ 带上下文派活（`remote_message` / `agent_notify`）→ 记账（「进行中」+ `last_dispatch`）→ 收尾（勾里程碑；全完成 `agent_notify("_main_", …)` 并清空「进行中」）。边界：连续两轮无进展上报主 Agent、不无限重试；一轮唤醒只推进一轮。

## 看板「项目组」区（src/static/index.html `renderTeamDash` 置顶段）

- **项目卡**：名称 + id + 巡检态（✅ 巡检中 / ⚪ 无巡检）+ 📄 计划（`openFilePreview` 打开 plan.md）+ 🧭 协调（新页签 `/agents/coordinator/`）+ ✕ 删除 + 说明 + **成员点阵**（⏳忙 / 💤空闲 / ❔不可达/未知，悬停看 id·kind）。
- **＋ 创建弹窗**（`projAddModal`，body 级——同 `remoteAddModal` 范式，填一半不被看板 3s 重渲染重置）：名称 / 说明 / 成员多选（候选 = 上一次 `/api/dash` 的 `remotes + team`，排除主 Agent，零新端点）/ 「每 5 分钟巡检」复选框；提交 → `POST /api/tool/exec {name:'create_project', arguments:{name, member_ids: ids, desc, patrol}}`。
- **删除**：confirm 提示「巡检任务一并取消，计划文档归档到 .trash」→ `/api/tool/exec delete_project`。
- 删除后 `renderTeamDash()` 立即重渲染（不靠等 3s 轮询）。

## `/api/dash` 的 `projects` 字段（src/server.py `api_dash`）

只读 + 探测，不写盘：遍历 projects.json → 逐成员 `_proj_probe_member`（remote `POST /api/status` timeout 2s；local registry `status=="running"`），**结果缓存 20s**（前端 3s 轮询，远端探测有网络开销）；探测失败/超时 `busy=null`（前端渲染 ❔，不假设）。附带 `plan_mtime`（计划最近改动时间）。异常一律吞掉返回空列表——项目区坏不影响看板其余部分。

## 实锤 bug：`member_ids` 传数组被 `str()` 化（2026-11-01 e2e）

前端弹窗传的是 **JSON 数组**（`ids`），旧实现 `str(member_ids).split(",")` → `"['coder']"` 脏串 → `resolve_member` 报「不在双源」——**前端创建项目 100% 失败**。修复（`create_project` 入口宽容归一）：`list`/`tuple` 逐项取 + strip，字符串仍走逗号分隔（含中文逗号归一）；空则 ValueError。热加载生效后复测通过。

## 验证（2026-11-19 施工轮 p_2d4f784f，五步全绿）

- coordinator 页（playwright）：`/agents` 列表加载、工具面精确匹配 spec、**无 run_python** —— 「不干活只协调」落到工具层。
- 看板（playwright）：创建弹窗 → 提交 → 项目卡渲染（成员忙闲 / 计划链接 / 协调入口 / 删除）→ API + UI 双确认。
- 清理：测试项目 `delete_project` 归零——schedule 取消 + 计划目录挪 `.trash` 归档。
- `projects.json` 惰性建：首装无该文件属正常，第一次创建项目才落盘。

## 注意事项

- **`.gitignore`**：本 repo 已加 `.agent/projects/.trash/`（归档产物不入库；`.agent/construction_dispatch.json` 同批在列）。⚠️ 首启种子 `_GITIGNORE_SEED`（src/agent_config.py）**尚未含这两行**——新 repo 需手工补或待种子更新。
- 删除是「归档」不是「销毁」：`.trash/<id>-<ts>/` 里能捞回 plan.md。
- 巡检唤醒的是**主 Agent**，靠人设/提示词转派 coordinator——不是引擎直派；主 Agent 收到 🔔 后应 `agent_prompt("coordinator", …)`。
- 探测失败不催办是刻意语义：宁可漏一轮，不可对不可达成员连发派活。
- 改 `project_tools.py` 后 `/reload tools`；改 coordinator 声明按 mtime 热重读（下一次 `agent_prompt` 生效）。

## 相关页面

- [多 Agent 体系](../architecture/multi-agent.md)——coordinator 在子 Agent 谱系中的位置、声明目录形态
- [多实例组网](../architecture/multi-instance.md)——remote 成员来源（REMOTE_SERVERS）、看板远程实例管理
- [定时/到点任务调度](background-scheduler.md)——巡检任务用的 `add_schedule code` 通道与静默语义
- [施工委托](construction-delegation.md)——同源的「引擎/钩子派活给具名子 Agent」范式
- [coordinator 声明](../../.agent/agents/coordinator/coordinator.md) · [project_tools.py](../../.agent/tools/project_tools.py)