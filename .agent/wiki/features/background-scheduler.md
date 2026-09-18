# 定时/到点任务调度 · add_schedule（src/background.py + background_tools.py）

> v0.23.1（2026-09-04，commit `3c287c8`）给 at 补上**每日闹钟**短格式——本页是该子系统首次建页。

## 职责

- **src/background.py**：后台调度线程，`_loop` 周期扫描 `next_fire`，到点把消息推给 Agent 触发一轮（唤醒链见 [user-interaction · 后台通知 wake 语义](user-interaction.md)）
- **src/background_tools.py**：工具入口九件——服务管理五件（`start_service` / `stop_service` / `list_services` / `service_logs` / `send_to_service`）+ 后台任务查询（`check_bg_task`，2026-09-06 注册）+ 调度三件（`add_schedule` / `cancel_schedule` / `list_schedules`），LLM 可直接调用
- 触发三类：**interval**（每 N 秒）/ **at**（到点；v0.23.1 起支持每日闹钟）/ **组合**（every_seconds + at 同给：at 相位起步、之后每 N 秒循环，2026-09-18）

## Schedule 数据结构（dataclass）

| 字段 | 含义 |
|---|---|
| id / name | 任务标识与名字 |
| kind | `"interval"` \| `"at"` |
| spec | interval=秒数；at=触发时间戳 |
| message | 静态推送文本（与 action 二选一） |
| action | `{"tool":..., "args":...}` 到点执行该工具拿结果（动态消息，如 web_search） |
| repeat | interval 是否循环；at+daily 每日闹钟 |
| daily | at 每日模式锚点 `"HH:MM[:SS]"`（每日闹钟触发后据此重算） |
| next_fire | 下次触发时间戳 |

## add_schedule 语义（v0.23.1 起）

- 触发方式二选一：`every_seconds>0`（repeat 控制是否循环，默认循环）；`at` 完整 ISO 或短格式
- 推送内容二选一：`message` 静态文本；`tool`(+`tool_args`) 到点执行拿结果
- `repeat` 参数默认 **None**：按 at 格式**语义分发**（显式传值优先）
- **组合**（2026-09-18）：every_seconds + at 同给 = at 相位起步、之后每 N 秒循环——见[组合模式](#组合模式every_seconds--at--at-相位起步之后每-n-秒循环2026-09-18commit-9a88107用户提案)

| at 写法 | repeat 缺省行为 | 显式 repeat |
|---|---|---|
| 短格式 `'09:00'`（`HH:MM[:SS]`） | **每日闹钟**（True） | False → 只响下一个该时刻一次 |
| 完整 ISO `'2026-07-20T17:30:00'` | **单次到点**（兼容不变） | True → 每日循环（取时刻部分做锚点） |

## 每日闹钟实现要点（src/background.py）

- `_DAILY_RE = ^\d{1,2}:\d{1,2}(:\d{1,2})?$` 判短格式；短格式校验时/分范围（`25:00` 报时间格式错误），ISO 已过时报「时间已过」，at 空串同样报格式错误（不静默吞任务）
- **`_next_daily_fire()` 同秒边界**：触发后从 daily 锚点重算下一未来时刻，候选 `<= now`（同一秒内触发也算过期）即再 +1 天——**当天绝不二次触发**，死循环根治；重算恒取下一未来时刻
- 触发后：每日任务**保留**（重算 next_fire）；单次任务触发后**删除**
- background.py imports 相应补 `re` / `timedelta` / `Optional`

## 组合模式：every_seconds + at = at 相位起步，之后每 N 秒循环（2026-09-18，commit 9a88107，用户提案）

`add_schedule` 第三类触发（`add_interval_at`，src/background.py）：**every_seconds 与 at 同给**时，at 为【首触发相位起点】，之后每 seconds 循环一次。

| at 位置 | 首次触发 |
|---|---|
| 未来 | 等到 at 到点 |
| 已过去 | 自动对齐到下一个相位点立即起步：`at + ceil((now−at)/sec)·sec` |

**相位对齐（关键细节）**：`at=10:00` + 每 5min、现在 11:43 → 首次对齐 **11:45**（at 相位栅格的下一个点），不是「now+5min」那种会漂移的算法——固定在 at 的栅格上，多少次触发都不走偏。实现复用 `kind="interval"` 的现成循环重排，`_loop` 零改动。

**边界**：组合模式 at 只收**完整 ISO**（含日期）——短格式 `HH:MM` 无日期、与「相位起点」语义冲突，明确报错「组合模式要求 at 为完整 ISO」（不静默）；`repeat=False` 组合 = 只在 at 触发一次。

**验证（五场景全绿）**：① at 未来 → 首触发 = at 本身；② at 过去 + 每 5min → 对齐 11:45:00（相位不漂移）；③ 短格式 HH:MM 组合 → 报错提示 ✓；④ 单独 every_seconds / 单独 at → 回归不变；⑤ 完整 ISO 组合 →「首次 09-18 11:45:00（每 300s 循环，相位 10:00:00）」。

## 定时任务持久化：session.extra_state 落盘 + at_origin 相位锚（2026-09-18，commit 95649e3）

**动机**：此前调度任务只存内存 `_schedules`——`/restart` 即全丢，用户设的每日闹钟 / 循环巡检重启后消失、需逐条重设。本改（commit 95649e3，随 [v0.29.4](../releases/v0.29.4.md) 发布）把定时任务落 `session.extra_state["schedules"]`（随 meta.json 持久化），任务**跨重启存活**（组合模式 / 每日闹钟 / interval 循环全覆盖）。

**存什么**：任务定义 + **`at_origin` 相位锚**——**不存 `next_fire`**（存档期间时钟推进、重启耗时都会让快照过期，恢复时按锚点重算才是稳的）。

| 存档内容 | 语义 |
|---|---|
| 任务定义 | Schedule dataclass 各字段（name/kind/spec/message/action/repeat/daily…） |
| `at_origin` | 组合/interval 的相位起点；建任务时 at 未填 → 默认记 now（「假装第一次已在 now 触发过」） |

**恢复语义**：重启读档后按 `at_origin` 重算 next_fire——`at_origin + ceil((now−at_origin)/sec)·sec`，与组合模式的相位对齐同一套算法：**栅格不漂移**（重启前每 5min 一发，重启后仍落在同一栅格上）；每日闹钟按 daily 锚点重算。

**两个顺手修**：`Lock → RLock`（注册/摘旧/持久化嵌套加锁，可重入锁消死锁隐患）+ `_LOG` 补定义。

**验证**：与同名摘旧（8ed09c6）联测——同名再设后持久化 `extra_state["schedules"]` 无双份。生效方式 `/restart`（引擎层改动），重启后恢复链路读 `extra_state` 重建任务。

## 同名覆盖摘旧：重复投递根因修复（2026-09-18，commit 8ed09c6，pre_post 实锤）

**bug**：同名任务再设（如改触发时间重设）时，`_by_name[name]` 被新 id 覆盖，但 `_schedules[旧id]` **残留**——`_loop` 扫的是 `_schedules`，两个同名任务各自到点**各投一次** → 重复投递（commit 8ed09c6）。

**实证链**：① events.jsonl 里同一条 `[后台通知·pre_post]` 消息出现两次 turn_start（一字不差）；② 当时 `list_schedules` 已空——该任务是单次且已触发完，双投只可能来自「旧条目残留」；③ 复现：`add_at("pre_post", ...)` 设第二次，`_schedules` 出现两条。故事还原：用户先后设过两次同名任务（改触发时间）——名字只指向新的，旧的还在跑，到点各推一遍同一条消息。

**修复**：三处注册段（`add_interval` / `add_interval_at` / `add_at`）统一——`_by_name` 已有同名 → 先 `pop` 旧 `_schedules` 条目再注册新条目。

**验证**：同名改时间再设 / 同名改间隔再设 → `_schedules` 各只剩 1 条；持久化 `extra_state["schedules"]` 无双份；py_compile ✅。

**与正常语义的边界**（哪些「重复」不是 bug）：

| 情况 | 性质 |
|---|---|
| 同名再设 → 双投（本次修的） | bug，已修 |
| 循环任务每周期推一次（message 静态文本每次相同） | 正常语义 |
| service_exit 退出通知迟到（如 852 轮） | 旧事件迟到投递，内容相同但只此一条 |

## 展示适配

- `list_schedules` / `/api/status` snapshot：每日任务展示「每天 09:00 (还有Ns)」，单次任务带「单次」标注
- 后台看板的定时任务分组同步可见每日任务

## 后台进程一览与任务查询（list_services 合并视图 + check_bg_task 真工具，2026-09-06，commit e72c0e1）

**背景**：后台进程只有「服务」没有「任务」——run_python / run_shell 超时自动转后台的一次性任务（`_bg_tasks` 登记）此前只能靠返回文案里的 bg_id 单独查；且 **check_bg_task 自 v0.17.1 起只有提示文本承诺它、工具本体从未注册**（空头支票：模型按 docstring 调它 → 未知工具报错）。本次两件事一起补齐（src/background_tools.py，commit e72c0e1）。

## list_services 合并视图：后台服务 + 后台任务一处看全

输出分两段（`svc.list()` 服务段 + `real_tools._bg_tasks` 任务段拼接）：

```
后台服务:
  demo(运行中, pid=123, 已跑 60s)
后台任务 (run_python/run_shell 超时转后台, check_bg_task 查详情):
  bg_111 [run_python] 运行中, 已跑 45s
  bg_222 [run_shell] 已结束 rc=0, 已跑 300s
```

- 无任务时输出与原版完全一致（`(无后台服务)` 原样返回），**有任务才追加段落**——存量消费方零感知
- 任务段每行：bg_id / 工具名 / 状态（运行中 | 已结束 rc=N）/ 已跑时长
- 拼接换行修复：`head = "" if base.endswith("\n") else "\n"`（不再强制多补一个换行）

## check_bg_task 真工具落地（此前只有提示文本承诺）

| 用法 | 行为 |
|---|---|
| `check_bg_task()` 不传参 | 列出全部后台任务——**bg_id 枚举找回**（上下文折叠吃掉 bg_id 也能兜底） |
| `check_bg_task("bg_xxx")` | 状态 + 已跑时长 + 累计行数 + 尾部输出（≤2000 字） |

三块短板全补上：中途查进度 / 补看结果 / bg_id 丢失枚举。实现读 `real_tools._bg_tasks`（import 兜底空 dict）；注册列表补 `Tool(check_bg_task)`。

**它同时是 bg_task 完成通知的合成记录名**（2026-08-30 起）：任务跑完自动推一条 `check_bg_task` 合成工具记录唤醒（`{tool, args, result, reasoning}` 四键，键名契约见 [用户交互 · 键名漂移修复](user-interaction.md#键名漂移修复bg_task-合成记录-name--tool2026-09-11用户观察触发)）——**合成记录与真实查询共用同一心智模型**：模型看到通知后想深挖，直接真调 `check_bg_task(bg_id)` 拿全量输出。

**验证**：空任务 / 运行中+已结束混合 / 单任务详情 / 不存在的 id（报错并列出当前登记）/ 服务+任务拼接换行——五场景全过，py_compile ✅。**生效方式**：`/restart` 后新进程注册该工具；`real_tools.py` 的转后台提示文本无需改——它承诺的 check_bg_task 现在真的存在了（提示文本与工具本体终于对得上）。

## start_service 的 on_exit_wake：退出唤醒策略（2026-08-30 策略化 → 2026-09-14 自定义指令）

服务退出时是否唤醒 Agent，由启动参数逐服务声明（`start_service(name, command, cwd, on_exit_wake="never")`）；策略判定与通知注入在 src/agent.py `_on_service_exit`：

| on_exit_wake | 语义 |
|---|---|
| `never`（默认）/ 空串 | 仅登记，并入下次自然轮（v0.19.2 防套娃基线） |
| `crash` | rc≠0 唤醒一轮；同名 5 分钟内连续崩溃自动退避为登记 |
| `always` | 任何退出都唤醒（单次任务跑完即报） |
| 非枚举任意文本 | **自定义作业指令：退出即无条件唤醒（无退避），指令原文注入通知**——2026-09-14（commit 7283f52，用户裁定）：LLM 把本参数当「退出时的返回提示」填自然语言，旧实现静默丢弃按 never；误用收编为特性。通知三处注入形态见 [user-interaction · 误用收编](user-interaction.md) |

docstring 已写选择指引：常驻关键服务建议 `crash`；单次任务建议 `always`；**退出后要 Agent 照办的事，直接把作业写进本参数**（启动时留指令、醒来照办）。

## 与其他模块的关系

- [user-interaction](user-interaction.md)：schedule 唤醒的轮走 inbox；通知语义标签体系给 ⏰ `schedule:` source（通知气泡形态 + 混合批批首归属 `schedule:z` 判定）；service_exit / bg_task / schedule 三族唤醒全景表见该页
- [run-python](run-python.md)：run_python/run_shell 超时转后台的 bg_task 完成通知是另一族唤醒（恒唤醒），与本调度互补
- [api-status](api-status.md)：snapshot 携带任务列表（展示字段随 v0.23.1 更新）

## 注意事项

- 引擎层改动需 `/restart` 生效；随 v0.23.1 上 PyPI（`pip install -U agt-agent`）
- 用法例：`add_schedule('morning', at='09:00', message='早会时间')`
- 本页 2026-09-18 三连（组合模式 + 持久化 + 同名摘旧）随 **v0.29.4** 上 PyPI（PyPI 已上线，见 [v0.29.4 发布记录](../releases/v0.29.4.md)）
- 定时任务已持久化（`session.extra_state["schedules"]`）——重启后自动恢复，无需重设

## 相关页面

- [user-interaction · 后台通知 wake 语义](user-interaction.md) — schedule 唤醒轮的路由与语义标签
- [v0.23.1 发布记录](../releases/v0.23.1.md) — 每日闹钟的发布收口
- [v0.29.4 发布记录](../releases/v0.29.4.md) — 2026-09-18 scheduler 三连（组合模式/持久化/同名摘旧）发布收口
- [api-status](api-status.md) — snapshot 中的任务展示

