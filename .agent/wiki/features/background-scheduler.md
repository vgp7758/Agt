# 定时/到点任务调度 · add_schedule（src/background.py + background_tools.py）

> v0.23.1（2026-09-04，commit `3c287c8`）给 at 补上**每日闹钟**短格式——本页是该子系统首次建页。

## 职责

- **src/background.py**：后台调度线程，`_loop` 周期扫描 `next_fire`，到点把消息推给 Agent 触发一轮（唤醒链见 [user-interaction · 后台通知 wake 语义](user-interaction.md)）
- **src/background_tools.py**：工具入口九件——服务管理五件（`start_service` / `stop_service` / `list_services` / `service_logs` / `service_stdin`；旧名 `send_to_service` 保留为 hidden 别名，只透传 name+message）+ 后台任务查询（`check_bg_task`，2026-09-06 注册）+ 调度三件（`add_schedule` / `cancel_schedule` / `list_schedules`），LLM 可直接调用
- 触发三类：**interval**（每 N 秒）/ **at**（到点；v0.23.1 起支持每日闹钟）/ **组合**（every_seconds + at 同给：at 相位起步、之后每 N 秒循环，2026-09-18）

## Schedule 数据结构（dataclass）

| 字段 | 含义 |
|---|---|
| id / name | 任务标识与名字 |
| kind | `"interval"` \| `"at"` |
| spec | interval=秒数；at=触发时间戳 |
| message | 静态推送文本（2026-10-05 起与 action/code 可**同给**——message 为附言，主从语义见[下节](#三通道主从语义codeaction-主通道--message-附言2026-10-05用户两轮裁定commits-458475c--6c3f770)） |
| action | `{"tool":..., "args":...}` 到点执行该工具拿结果（动态消息，如 web_search）——主通道之一，产物参与拼接（顺序居中）；空返回不算产物 |
| code | 触发时跑的 Python 代码（2026-10-04 · [autonomous 融合](#autonomous-融合code--deadline--mode-三参数--旧纯自主模式整体退役2026-10-04用户提案commits-0716fc0--01edc18)）——stdout 尾部 + `result` 变量作为消息；空产物=code 段自身不产出（**主通道 code+action 全空 → 该次全静默**，message 附言不单独发） |
| deadline | 截止时间戳（2026-10-04）——`_loop` 扫描时过期任务自动删除（取代 autonomous 的 end_time） |
| mode | 注入策略（2026-10-04）：`immediate` \| `idle`（默认）\| `skip`，busy 时三分岔 |
| repeat | interval 是否循环；at+daily 每日闹钟 |
| daily | at 每日模式锚点 `"HH:MM[:SS]"`（每日闹钟触发后据此重算） |
| next_fire | 下次触发时间戳 |

## add_schedule 语义（v0.23.1 起）

- 触发方式二选一：`every_seconds>0`（repeat 控制是否循环，默认循环）；`at` 完整 ISO 或短格式
- 推送内容三通道（2026-10-05 起可**同给**：code/action 主通道 + message 附言，见[三通道主从语义](#三通道主从语义codeaction-主通道--message-附言2026-10-05用户两轮裁定commits-458475c--6c3f770)；10-04 融合版曾为互斥三选一）：`message` 静态文本；`action`(+`action.tool`/`action.args`) 到点执行拿结果；`code` 触发时跑 Python 代码拿 stdout 尾部 + `result` 变量（主通道产物全空=该次全静默，message 不单独发）
- `deadline`（2026-10-04）：ISO 截止时间，过期任务 `_loop` 扫描时自动删除
- `mode`（2026-10-04）：注入策略 `immediate` \| `idle`（默认）\| `skip`——agent busy 时三分岔（打断 / 排队 / 放弃），详见[下节](#autonomous-融合code--deadline--mode-三参数--旧纯自主模式整体退役2026-10-04用户提案commits-0716fc0--01edc18)
- `repeat` 参数默认 **None**：按 at 格式**语义分发**（显式传值优先）
- **组合**（2026-09-18）：every_seconds + at 同给 = at 相位起步、之后每 N 秒循环——见[组合模式](#组合模式every_seconds--at--at-相位起步之后每-n-秒循环2026-09-18commit-9a88107用户提案)

## autonomous 融合：code / deadline / mode 三参数 + 旧纯自主模式整体退役（2026-10-04，用户提案，commits 0716fc0 + 01edc18）

**动机**：早期「纯自主模式」（autonomous：`set_autonomous` 开启后打断 answer、续跑当前轮 react loop）实际应用中渐渐被 schedule 定时唤醒替代——用户裁定（2026-10-04）把自主能力**融合进 add_schedule**：自主循环退化为「一个带 code + immediate 的循环任务」，autonomous 全家（工具五件 + /autonomous 命令 + agent 状态机 + WebUI 开关）整体退役。两段提交 `0716fc0` + `01edc18` 已推送，净 **-318 行**。

**三个新参数**：

| 参数 | 语义 | 取代的旧物 |
|---|---|---|
| `code` | 触发时执行一段 Python（`agent` 变量可用），`result` 变量 + stdout 尾部作为消息推送；**空产物 = 该次触发静默**——自主循环「有话要说才说话」的等价物 | goal_check（PASS 才停 → code 返回空即不发声） |
| `deadline` | ISO 截止时间；`_loop` 扫描时 `now > deadline` 自动删除任务 | end_time / duration_minutes |
| `mode` | agent busy 时注入三分岔（见下表） | autonomous 的打断语义收敛为其中一态 |

**mode 三态**：

| 值 | busy 时 | 空闲时 |
|---|---|---|
| `immediate` | 塞 `pending_messages` 步边界插话（打断当前轮——原 autonomous 行为） | 正常唤醒一轮 |
| `idle`（默认） | 排队，等轮结束后再注入（原 schedule 行为） | 正常唤醒一轮 |
| `skip` | 直接放弃本次注入 | 正常唤醒一轮 |

**自主循环等价用法**：

```python
add_schedule(name="auto-loop", every_seconds=300, deadline="2026-10-04T22:00",
             mode="immediate", code="done = 检查目标(); result = '' if not done else '目标达成，汇报收尾'")
```

**删除清单（7 文件）**：

| 文件 | 删了什么 |
|---|---|
| real_tools | `make_autonomous_tools` 五工具（set / exit / status / set_goal_check / check_goal） |
| agent.py | autonomous 状态字段 + 方法 + run loop 两处续轮块 + CLI 三事件（-102 行；**`pending_messages` 保留**——忙时插话与 schedule immediate 共用通道） |
| commands | `/autonomous` 命令组 + 注册块 + `/clear` 调用点 |
| server.py | status 字段 + insert_message 门控（busy 插话改**无条件入队**）+ 炸点分支 |
| index.html | autonomousMode / setAutonomousMode / case 三事件（12 处清零） |
| tool_briefs | 五条简报 |
| background.py / background_tools.py | （增侧）Schedule 三新字段 + 三 add 方法签名扩展 + deadline 扫描删除 + restore/export 透传 + 工具参数校验 |

**验证**：①七项语义单测全过（code 产物 / 空静默 / 异常回显 / idle 排队 / immediate 插话 / skip 放弃 / deadline 到期删除）；②持久化往返——export/restore 三分支全带新字段（重启存活）；③L2 隔离实跑——旁路实例 3s 就绪 + callback 注入真实一轮健康运行 190s+ 无 NameError。

**生效与存量兼容**：引擎层改动，`/restart` 后新工具面可用（`add_schedule(code=..., deadline=..., mode=...)`）、旧五工具消失；历史 meta.json 里的 `autonomous_*` 键变**无害冗余**（restore 不再读）。

**关联**：[user-interaction · 后台通知 wake 语义](user-interaction.md)（idle 排队复用 inbox/唤醒链）· [气泡交互 · 插话机制](user-interaction.md)（immediate 的 pending_messages 步边界通道）。

## 三通道主从语义：code/action 主通道 + message 附言（2026-10-05，用户两轮裁定，commits 458475c + 6c3f770）

**动机**：用户问询「同时传了 message / code / tool，是都执行并注入吗？」——问出融合版实现两处暗坑：

1. **互斥三选一**：优先级 `code > action > message`，只有第一个非空通道被执行并注入，其余静默忽略——「心跳 code + 固定 message」这类组合无法表达；
2. **code 静默吞掉一切**：code 空产物（该次静默）时 message / action 一并被吞，code 异常同理——「有事才说 + 没事报平安」做不到。

**语义（`_produce` 重写，src/background.py；v1 初版=三通道独立执行、产物拼接，v2 按用户裁定收敛为主从）**：

| 通道 | 执行 | 产物 |
|---|---|---|
| `code` | exec（`agent` 变量可用） | stdout 尾部 + `result` 变量 |
| `action` | 到点调工具拿结果 | 工具返回 |
| `message` | 静态文本 | 原文 |

- **主从裁定（v2，用户）**：code/action 是【主通道】（干活/判定），message 是【附言】——传了主通道且其产物全空 → **该次全静默（message 不单独发）**；有产物 → 按 **code → action → message** 顺序拼接注入；只传 message → 正常注入（心跳场景）。
- code 失败算「有产物」（错误段入拼接，不静默）；tool 空返回不算产物（与 code 静默对齐；**工具层空输出占位符「(无输出)」同样归一化为空**——v2 漏的口子，见[下方后记](#后记无输出占位符击穿-tool-静默归一化补齐2026-10-05--三用户实锤commit-9d6cadd)）。

| 场景 | 行为 |
|---|---|
| 只传一个 | 与旧版完全一致（向后兼容） |
| code 静默 + message | **全静默**（v2：message 是附言不单独发） |
| code 失败 + message | 错误段照常入拼接，message 仍发出（不短路） |
| tool 空返回 + message | **全静默**（空返回/「(无输出)」占位符都不算产物，v2 + 后记） |
| 三全（都有产物） | code 结果 → action 结果 → message 依次拼接注入 |

**验证**（七场景全过）：仅 message → 发；仅 code 静默 → None；code 静默+message → 全静默；code 有产物+message → 产物+附言；tool 空返回+message → 全静默；tool 有产物+message → 产物+附言；code 失败 → 错误+附言。同日 · 三 v3 补验三场景（占位符/空串/有产物）全过，见后记。

**生效**：site-packages 已同步（commit `6c3f770`），重启实例后新语义生效；**已存在任务无需重建**（持久化的是参数本身，语义在代码侧）。

### 后记：「(无输出)」占位符击穿 tool 静默——归一化补齐（2026-10-05 · 三，用户实锤，commit 9d6cadd）

**触发**：用户实锤——run_python / run_shell 这类工具在输出为空时，工具层返回的不是空串而是占位符 **「(无输出)」**（real_tools 两处：`return out or "(无输出)"`）。对 v2 的判空来说它是**非空字符串** → 被当产物 → 「tool 空返回 + message → 全静默」被击穿：明明啥都没干却照常注入一段「(无输出)」+ 附言。

**修复（src/background.py `_produce` tool 分支，commit `9d6cadd`，site-packages 已同步）**：tool 产物归一化时把占位符视为空：

```python
result = str(self._agent.tools.call(tool, args) or "").strip()
if result in ("(无输出)", "(no output)"):
    result = ""   # 工具层空输出占位符（real_tools L364/L1896）不算产物（用户实锤 2026-10-05）
if result:   # 空返回不算产物（与 code 静默对齐，用户裁定 v2）
    primary.append(f"[{sch.name} · {tool} 结果]\n{result}")
```

**验证（三场景全过）**：tool 返回「(无输出)」+ message → 全静默（★修复点）✓；tool 空串 + message → 全静默（回归）✓；tool 有产物 + message → 产物+附言（不受影响）✓。

**至此三条静默通道闭环**：code 空产物 → 静默；tool 空串/占位符 → 静默；主通道全空 → message 附言一并静默。重启实例后生效。

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

### 后记：三 bug 叠加——持久化从未生效，meta.json 从未出现 schedules（2026-09-18 · 二，commit e22062c，用户实锤）

**触发**：用户实锤——「meta.json 里没看到 schedules，重启电脑后 scheduler 任务也全没了」。排查确认 95649e3 的持久化**实际从未生效**，三个 bug 叠加：

| # | bug | 机制 |
|---|---|---|
| ① | **覆盖式重建抹掉直写值**（主凶） | `_persist()` 直写 `session.extra_state["schedules"]`，但 session 每次落盘前 `_capture_state()` 跑 `self.extra_state = self._state_provider() or {}`——用 Agent 收集清单（capture_runtime_state：plan/spec/autonomous/background_tasks）**整体覆盖** extra_state；清单里没有 schedules → **任意一次落盘都把直写值抹掉**，meta.json 从未出现该键（与 77 轮 `_agent_meta` 丢失同源的机制坑：extra_state 的写权归 provider） |
| ② | **恢复时机错误** | Scheduler 在 `Agent.__init__`（L296）创建——早于 session 装载；`__init__` 里的 `_restore()` 读 extra_state 时还是空的 = **恒空跑**（即使有值也恢复不了） |
| ③ | **旧任务沉没** | 用户旧任务创建于持久化生效前 + 直写值被抹 → 从未落过盘，重启后**无法恢复**（沉没成本，修复只保未来任务） |

**修复（commit e22062c）——单一真源架构**：

```
Scheduler._schedules（真源）
   ├─ export_state() ──→ capture_runtime_state 收集清单加入 schedules 键
   │                      （与 background_tasks 同款模式——覆盖式重建不再抹掉）
   ├─ _persist() ──→ 只调 sess.save()（save→capture→provider 收集→meta.json 单源落盘）
   └─ restore_state(items) ←─ restore_runtime_state（set_session/load 后的标准恢复点）
```

- **`Scheduler.export_state()`**：从 `_schedules` 序列化全部任务定义（name/kind/spec/at_origin/message/action/repeat/daily）——采集姿势从「调度器自己写 extra_state」改为「provider 收集时来取」
- **`capture_runtime_state`（src/agent.py）**：收集清单加入 `"schedules": self.scheduler.export_state()`——extra_state 覆盖式重建从此带着 schedules 走
- **`_persist()`**：删掉直写，只调 `sess.save()`——落盘路径单一化，meta.json 是唯一出口
- **恢复挂标准恢复点**：`restore_runtime_state`（set_session/load 后调用，此时 scheduler 已存在、extra_state 已从 meta.json 读入）调 `scheduler.restore_state(items)`；`__init__` 的 `_restore()` 调用删除（恢复时机 bug 消除）；恢复后按 `at_origin`/`daily` 锚点重算 next_fire（相位不漂移，与 95649e3 同一套算法）

**验证（mock 全链路，复刻 session 覆盖式重建）**：① add 2 任务 → `_persist` → 覆盖式重建后 `extra_state['schedules']` = 2 条 ✓（旧行为：被抹）；② 模拟重启 restore → watch/morning 都回来、相位重算正确（5min 循环 / 每日闹钟 894min=明早 09:00）✓。

**生效**：`/restart` 后——之后**新设的**定时任务才真正跨重启存活（`/restart` 一次落盘一次，meta.json 里即可见 `schedules` 键）。修复前经 v0.29.4 设置且已丢失的任务须重设。

**教训**：`session.extra_state` 是 provider 覆盖式重建的领地——**引擎/工具直写必被下一次落盘抹掉**（同族案例：`_agent_meta` 丢失）；凡需随 session 持久化的运行时状态，一律走 `capture_runtime_state` 收集清单申报 + `restore_runtime_state` 标准恢复点恢复。

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
- （2026-10-07 起）抽屉定时任务组头部「＋ 添加」+ 每行 ✏ 编辑 / 🗑 删除——CRUD 见下一节

## 抽屉定时任务 CRUD：reschedule 部分更新 + 手动添加/编辑/删除弹窗（2026-10-07，用户提案，commits 5118753 + 25c23e3）

**动机（用户提案）**：schedule 创建后「下次触发时间 / every_seconds / deadline」就改不了了（只能 cancel 再重建，任务 id 也跟着换）——提案：抽屉里可以直接改；并支持手动添加日程（弹窗填表单）。

### 后端：Scheduler.reschedule 部分更新（src/background.py）

```python
def reschedule(self, name, every_seconds=None, at=None, deadline=None,
               repeat=None, message=None) -> str
```

| 参数 | 更新语义 |
|---|---|
| `every_seconds` | 改间隔：`kind=interval` + **新相位锚 `at_origin=now`** + `_phase_next` 重算 next_fire；≤0 拒绝 |
| `at` | 改首触发/下次触发时刻：interval 任务以 at 为新相位锚重算 next_fire；非 interval 转 `kind=at` 单次到点 |
| `deadline` / `repeat` / `message` | 直接覆盖 |

关键设计：**未提供的字段保持原值；任务 id 不变**（进行中的对 id 的引用不断）——与 add 的「同名覆盖摘旧」（换 id）形成对照。无此任务返回 `[无此任务] name`；成功返回新 next_fire 摘要。改完 `_persist()` 落盘（extra_state 真源 `export_state` 自动带上新值）。

### 三端点（src/server.py）

| 端点 | body | 行为 |
|---|---|---|
| `POST /api/sched_upd` | `{name, every_seconds?, at?, deadline?, repeat?, message?}` | → `scheduler.reschedule`（部分更新） |
| `POST /api/sched_add` | `{name, every_seconds?, at?, deadline?, repeat?, message?}` | at 有值 → `add_interval_at`；否则 → `add_interval`；两者都没填明确报错 |
| `POST /api/sched_del` | `{name}` | → `scheduler.cancel` |

### 前端：弹窗表单（src/static/index.html）

后台看板抽屉定时任务组：

- 组头部「⏰ 定时任务 **[＋ 添加]**」→ `schedModal()` 空表单
- 每行下方「**✏ 编辑**」「**🗑**」→ `schedModal(name)` 预填 / `schedDel(name)`（confirm 后取消）

`schedModal` 弹窗六字段：任务名（编辑态 readonly）/ 间隔秒（every_seconds）/ 首触发 at（ISO）/ 截止 deadline（ISO，留空=不限）/ 循环触发勾选 / 推送消息（到点注入给 Agent）。`schedSave` 按有无原名分流 `/api/sched_upd` vs `/api/sched_add`；成功 → toast + 关弹窗 + `renderSvcDash()` 即时刷新（该函数因此改 async）。回填数据源：渲染时 `window._dashSchedules` 暂存 schedules。

**边界**：表单只覆盖 message + 时间参数族（every_seconds/at/deadline/repeat）；action/code 类复杂任务（三通道主从语义见上）仍走 `add_schedule` 工具——弹窗新增走的是 `add_interval(_at)` 真实管线，与 Agent 创建完全同源。

**插曲**：弹窗代码插入时锚点误吞了 `renderSvcDash` 的 `async` 前缀——JS 语法自检（node --check）当场抓到并修复（commit `25c23e3`）。

**生效**：commits `5118753` + `25c23e3`；site-packages 已同步，`/restart` 后生效。

**关联**：同名覆盖摘旧（add 的换 id 语义对照，见上）· 服务看板交互四件套（同抽屉服务组的按钮排，见下）。

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

## 服务看板交互四件套：⭐收藏 / 📄 完整日志 / ⏹Stop / ▶Start（2026-10-06，用户提案，commits 9a039c5 + d22bd34）

服务卡片展开区（日志 pre 下方）按钮排（commit `9a039c5` 三件 + `d22bd34` 补 📄，src/static/index.html + src/server.py）：

| 按钮 | 条件 | 行为 |
|---|---|---|
| ☆ 收藏 / ★ 已收藏 | 恒显示（⭐态读 dashboard 快照 `fav_services` 名单） | `svcFav()` → POST `/api/svc_fav`：服务名 + 启动指令写**当前 repo 的 main.yml** services 段（无 main.yml 先从全局复制）；已收藏再点=取消（从声明移除） |
| 📄 完整日志 | 恒显示 | 新页签打开 `GET /api/svc_log?name=…` 全量日志页（见下方专节） |
| ⏹ Stop | 运行中 | `svcOp('stop')` → POST `/api/svc_op` |
| ▶ Start | 已退出 | `svcOp('start')`：用条目登记的 command+cwd 重启；进程重启过登记丢失 → 明确提示改走收藏路径（收藏后启动期自动拉起） |

- 工具侧（本页 list_services / start_service / stop_service）零改动——两轮均纯 WebUI 交互 + server.py 端点层
- 启动指令取法：优先进程内登记，DOM 兜底取卡片 `.cmd` span（带 title 悬浮全文）
- **收藏 = 开机自启闭环**（写 main.yml services → 主 Agent 启动期拉起）、后端端点职责表：见 [multi-agent · 看板收藏](../architecture/multi-agent.md)

### 📄 完整日志页：/api/svc_log 新页签全量日志（2026-10-06 · 二，commit d22bd34）

卡片内嵌日志 pre 只有尾部视图（环形缓冲已截断），想看全量得回终端翻文件——用户提案「点击时从浏览器打开一个新页签查看更完整的日志」。新端点 `GET /api/svc_log?name=…`（src/server.py）返回**独立 HTML 页**（非 JSON）：

| 项 | 语义 |
|---|---|
| 数据源 | `agent.services._services[name]` 进程内条目——**全量环形缓冲**（上限 3000 行防爆；卡片 pre 只是尾部视图） |
| 页面形态 | 暗色全屏 + sticky 头部（钉顶不随滚动）：运行态 / 行数 / 启动命令 |
| 自刷新 | `<meta http-equiv='refresh' content='5'>`——5s 自动重载，盯日志不用手动刷 |
| 安全 | title / header / 正文全部 HTML 转义（服务名与命令回显进 HTML 的注入面；服务名自取低危，同轮二补严谨化，commit `d22bd34`） |
| 容错 | 服务不在当前进程登记 → 读持久化日志尾部兜底（[后记 2026-10-08](#后记restart-后完整日志-404日志-tee-持久化到-agtservice_logs2026-10-0820048-实锤commit-b948a01)）；文件也没有才 404 提示页 |

与 `service_logs` 工具（LLM 侧，JSON 给模型消费）的分工：同源数据、两种消费端——本端点面向人眼（独立页签 + 自刷新），工具面向模型。

#### 后记：restart 后完整日志 404——日志 tee 持久化到 ~/.agt/service_logs/（2026-10-08，20048 实锤，commit b948a01）

用户实锤：20048 restart 后点 unity-repl 的完整日志，恒提示「服务不在当前进程登记」。根因：本端点读的是 `ServiceManager` **内存环形缓冲**（deque）——新进程的登记表是空的，旧服务的历史日志随旧进程蒸发。commit `b948a01`（src/background.py + src/server.py，site-packages 已同步）两层修复：

| 层 | 机制 |
|---|---|
| **tee 持久化**（src/background.py `start()`） | 每个服务启动即开 `~/.agt/service_logs/<name>.log` 挂进 entry（名字 sanitize `[^A-Za-z0-9_.-]→_`，`repl:` 的冒号同样归下划线）；reader 线程逐行 tee 落盘（行缓冲）；启动写分隔头 `===== start 时刻 · pid · 命令 =====`——多次重启的历史在一个文件里分段可辨；同名覆盖重建（[839f445 死服务重拉](../architecture/multi-agent.md) 路径）先关旧句柄再开新 |
| **读取兜底**（src/server.py `/api/svc_log`） | 进程未登记 → 读持久化文件尾部 3000 行渲染，头部标注「○ 进程未登记（实例重启过）· 显示持久化日志尾部 N 行」；文件也没有才 404 提示页 |

**边界**：投影侧 [watch_tail](#watch_tailbg_services-投影段附服务日志尾部-n-行2026-10-07用户提案commit-9e1d523) 仍读**内存** deque——重启后旧日志磁盘有但不投影（watch_tail 只看本进程运行期的实况）；持久化兜底只接在人眼端点 `/api/svc_log`。

**冒烟**：persist-demo 服务跑 5 行输出 → stop 后日志文件在、`line-4` 已落盘 ✓。

**插曲（except 静默降级教训）**：首版冒烟失败——try 块里笔误 `_re.sub`（import 的是 `re`），NameError 被 except 兜底吞掉、静默降级 `_lf=None`，读取侧永远走不到兜底。修为 `re.sub` 后全绿。教训：**兜底 except 块里的名字错误会无声降级，冒烟必须盯结果，不能只看「没报错」**。

### 后记：Stop 恒报「缺少agent/name」——svc/sched 六端点统一 or agent 兜底（2026-10-08，20048 实锤，commit 2ecc6f4）

**现象（用户实锤）**：服务看板点 ⏹Stop 恒报 `缺少agent/name`——name 明明传了。直测复现定责：POST `{"name":"unity-repl","op":"stop"}` 直打 `/api/svc_op` 仍报同款 → **排除前端丢参，是端点自身的取值判定挂了**。

**根因——agent 的两条注入路径**：部分启动形态（20048）下，`agent` 经**模块级全局变量**注入，`_state` dict 里根本没有 `"agent"` 键；而 svc/sched 系端点只查 `_state`：

```python
_agent = _state.get("agent")            # 旧：None
if not _agent or not name: ...          # 「缺少 agent/name」判定必炸
```

报错文案把 agent / name 两个判定**合并成一句** → 强误导性（缺的其实是 agent，不是 name）。

**为什么 📄 完整日志此前一直正常**：`/api/svc_log` 先查服务登记、未登记才 404——**不走 agent 判定路径**。同一家族端点里症状分叉（Stop/Start 恒撞缺参 vs 日志页恒能弹）正是这个差别。

**修复（commit 2ecc6f4，src/server.py，6 处）**：统一补 `or agent` fallback（对齐 plan 推送端点 L2524 的既有写法）：

```python
_agent = _state.get("agent") or agent   # 部分端点用短名 ag = ...，同理
```

| 覆盖端点 | 家族 |
|---|---|
| `svc_op` / `svc_log` / `svc_fav` | 服务看板三件（Stop/Start · 完整日志 · ⭐收藏） |
| `sched_upd` / `sched_add` / `sched_del` | [抽屉定时任务 CRUD](#抽屉定时任务-crudreschedule-部分更新--手动添加编辑删除弹窗2026-10-07用户提案commits-5118753--25c23e3) 三端点 |

**生效**：已推送 + site-packages 已同步（20048 / 9000 同批），实例 `/restart` 后生效。

**顺带辨析（restart 遗留孤儿服务）**：修复生效后，对 restart 前启动的服务点 Stop 会转报「无此服务」——**登记在 ServiceManager 内存里，新进程是空的**（这不是缺参 bug，是登记生命周期）。正解已备：▶ Start 走[死服务覆盖重建](#start_service-撞死服务被拒stop-保留-entry--start-只查登记stopstart-重启路径断裂2026-10-0820048-实锤commit-839f445)；历史日志看 [tee 持久化兜底](#后记restart-后完整日志-404日志-tee-持久化到-agtservice_logs2026-10-0820048-实锤commit-b948a01)；常要跨重启可用的服务走 ⭐ 收藏（main.yml services 启动期拉起）。

**关联**：[user-interaction · plan 面板连接即推](user-interaction.md)——`_state.get("agent") or agent` 模式的**首例**（d573e1e，注释即「兼容两条取 agent 的路径」）；本节是该模式在 svc/sched 家族的推广收编。

## watch_tail：bg_services 投影段附服务日志尾部 N 行（2026-10-07，用户提案，commit 9e1d523）

**动机**：image_feed 段已让 Agent 每步看到实时画面（[姊妹特性](image-feed.md)），而后台服务的实况还停在「按需查」——状态行只有 pid + 已跑时长，stdout 里的运行实况每步投影不可见，想知道就得多调一次 `list_services`/`service_logs`。用户提案：仿 image_feed 的每步注入思路，让 bg_services 装配段也带上服务状态信息。

**设计取舍：零协议，放弃 stdin `/status`**：初版设想「`start_service` 带行数参数，投影前向服务 stdin 传 `/status`、取返回前 N 行」——要求每个服务配合实现协议、且必须是 REPL 型常驻 stdin，负担大不通用。最终按用户中途补充方向裁定：**stdout 日志 deque 天然即状态**——服务只要正常打日志（谁不打呢），`watch_tail` 一参数即达；零协议、零改造，存量服务无需任何配合。

**机制**（src/background.py + src/background_tools.py，commit `9e1d523`，site-packages 已同步）：

| 触点 | 改动 |
|---|---|
| `ServiceManager.start()` | 新参 `watch_tail: int = 0`；服务条目登记该值（`max(0, int(...))` 负数防御） |
| 装配段 `func:bg_services()` | 每服务状态行下：`watch_tail>0` 时附 `list(logs)[-N:]` 日志尾部（`│` 前缀缩进行）；=0 只状态行——**存量消费方零感知** |
| `start_service` 工具 | schema 新参 `watch_tail`（int），docstring 即提示词：关键服务（帧服务/监控器）设 3~5，每步投影可见实况；透传 `svc.start()` |

投影形态示例：

```
【后台服务状态】当前服务：
  watch-demo(运行中, pid=xxx, 已跑 Ns)
    │ log line 4
    │ log line 5
    │ log line 6          ← watch_tail=3 的服务附日志尾部三行
  quiet-demo(运行中, pid=yyy, 已跑 1s)   ← 未 watch 的服务只状态行
```

**冒烟验证**：watch-demo（watch_tail=3）正确渲染日志尾部三行；quiet-demo（未 watch）无尾部——**按服务粒度 opt-in** ✓。

**生效方式**：引擎层改动，`/restart` 后生效；`watch_tail` 是启动期参数（`start_service` 时按服务声明），需要看实况的关键服务在启动时带上即可。

**补记（2026-10-08，commit 174131f）**：`repl:` 前缀服务**不适用本模式**——stdout 只归协议响应（`/status` 一行摘要），`status_lines` 改走每步自动协议轮询（`wt > 0 and not name.startswith("repl:")` 显式豁免尾部模式），见 [repl: 协议服务](#repl-协议服务命名潜规则--每步投影自动-status2026-10-08用户提案commit-174131f)。初版「放弃 stdin /status」的裁定由此部分回摆：**不强迫、但也不禁止**——普通服务继续零协议走日志尾部，愿意实现 `/status` 的 repl: 服务升级为协议摘要。

**豁免条件随判定扩展升级（2026-10-08 同日续 `c0e9768`）**：repl 判定已从「命名前缀」扩为 **`repl:` 前缀 或 交互自动 `repl_seen`**（任何服务被 `service_stdin` 发过一次参数即打标）——上文 `wt > 0 and not name.startswith("repl:")` 应读作「解析后的 repl 判定」：被 stdin 交互过的服务同样自动让位日志尾部模式，改投影 `/status` **前 N 行（≤5）**。详见 [repl: 协议服务](#repl-协议服务命名潜规则--每步投影自动-status2026-10-08用户提案commit-174131f) 章节后记。

**关联**：[image-feed](image-feed.md)（姊妹特性：每步实时画面——image_feed 走 tail_images 画面通道、watch_tail 走文本通道，帧服务可同用）· [agents-admin · FUNC_REGISTRY](agents-admin.md)（bg_services() 装配函数）· [本页 on_exit_wake](#start_service-的-on_exit_wake退出唤醒策略2026-08-30-策略化--2026-09-14-自定义指令--2026-09-23-默认翻转-notify)（start_service 的另一族逐服务参数）。

## service_stdin 往返语义：expect 正则 + timeout——写入后等 stdout 响应才返回（2026-10-07，用户提案，commit b1fbfe6）

**动机（用户提案）**：`service_stdin` 旧行为是「写入即返回」——发一行指令立刻拿到 📤 回执，服务的响应得再调一次 `service_logs` 才能看见。用户指出：一般用 stdin 驱动服务时，预期就是能拿到 stdout 的结果，应该有个参数声明「**stdout 出现怎样的输出后，这次工具调用才算完成**」。

**签名与语义**（src/background.py `ServiceManager.send` + src/background_tools.py `service_stdin`，commit `b1fbfe6`）：

```python
service_stdin(name, message, expect="", timeout=10.0)
```

| 参数 | 语义 |
|---|---|
| `expect` | 非空 = 【正则】：写入后**轮询 stdout 新增输出**（0.15s 间隔），命中才返回——**工具结果 = 写入后的新增输出**（尾部 ≤4000 字，REPL 往返语义：发代码等 `>>>` 或结果回显）；空 = 旧行为（立即返回 📤 回执） |
| `timeout` | 等待上限秒（默认 10，下限 0.5）；**超时不空手**：返回已收到的新增输出 + 「未匹配 /expect/（可加大 timeout 或确认服务真的回显）」标注——agent 可自行加大重试 |

**三个实现要点**（src/background.py `send`）：

- **增量基准**：`before = len(logs)`（写入前的 deque 行数）——正则只对**本次交互产生的新行**匹配，返回也只含新增行，不混旧日志
- **断管防御**：stdin 已关（BrokenPipeError/OSError）→ `[发送失败] name: BrokenPipeError（进程可能已关闭 stdin）`
- **兼容**：`expect` 空 = 完全旧行为，存量调用零感知；旧名 `send_to_service` 别名只透传 name+message（不带新参）

**REPL 往返语义的价值**：驱动另一个 agt 实例（发任务 prompt 等回答）、python REPL、交互式 CLI 时，一次调用直接拿到响应——省掉「send → 猜时长 → service_logs 二次查询」的两步往返。被驱动侧配套：src/chat.py `_stdin_thread`——stdin 非 tty（被 start_service 以管道启动）时逐行消费进 work_q，外部 Agent 才能用 service_stdin 驱动本实例。

**冒烟验证**（python -i REPL 真跑，两路径全过）：① 发 `print('pong-12345')` + `expect=r"pong"` → ✅ 匹配返回响应行；② 发无回显赋值语句 + 永不匹配的 expect → ⏱ 超时如实返回「期间新增 0 行」。

**生效**：commit `b1fbfe6` 已推送；**site-packages 待同步**（pip 实例跑 site-packages 实体，见 [运维排障](../guides/ops.md)），同步后 `/restart` 生效。

**关联**：[watch_tail](#watch_tailbg_services-投影段附服务日志尾部-n-行2026-10-07用户提案commit-9e1d523)（同日姊妹——watch_tail 管 stdout「看得见」（每步投影）、expect 管「等得到」（调用内往返），一个读通道一个写通道）· [user-interaction · 后台通知 wake 语义](user-interaction.md)（服务退出通知链）。

## repl: 协议服务：命名潜规则 + 每步投影自动 /status（2026-10-08，用户提案，commit 174131f）

**动机（用户提案）**：watch_tail 是「零协议」方案（stdout 日志即状态），但常驻业务服务的实况日志是过程性的——真正有状态价值的是**协议级摘要**（任务数 / fps / 队列深度）。提案：要一个 repl 类的服务通道，像 MCP 那样**一次 stdin 对应一次 stdout**；每轮 bg_services 投影时自动发 `/status` 拿输出、截取前 L 行投影；日志写文件不污染 stdout；服务是否支持 repl 用**命名潜规则**判定（名称匹配某格式），规则写进 `start_service` 提示词。

**命名潜规则**：服务名带 **`repl:` 前缀** = REPL 协议服务。约定（已写进 `start_service` docstring 即提示词）：

> ⚠️ 本节两条口径（判定=**仅** `repl:` 前缀；摘要=**首行**）已在同日被本章末的[后记](#repl-协议服务命名潜规则--每步投影自动-status2026-10-08用户提案commit-174131f)扩展为「**前缀 或 交互自动 `repl_seen`**」+「**前 N 行（≤5）**」——以该后记为准。

- stdout **仅用于协议响应**——`/status` 返回**一行摘要**（多行信息压成一行）；
- 过程日志写文件（`--log xxx` 或服务内自行落盘），**不污染 stdout**；
- 配 `watch_tail>0`：每步投影的 bg_services 段自动发 `/status` 并投影摘要（5s 节流）——首版取**首行**，后记扩为**前 N 行（≤5）**。

**机制**（src/background.py + src/background_tools.py，commit `174131f`，site-packages 已同步）：

| 触点 | 改动 |
|---|---|
| `ServiceManager._repl_status()`（新） | 协议轮询：复用 [service_stdin](#service_stdin-往返语义expect-正则--timeout写入后等-stdout-响应才返回2026-10-07用户提案commit-b1fbfe6) 的 expect 往返——`send(name, "/status", expect=r"\S", timeout=2.0)`；从返回中剥出新增输出、挑**首个非空且非 `>` 开头**的行（回显过滤）作摘要；超时/异常返回空串（不投影摘要行，不炸投影） |
| `status_lines()` | 判定 `repl:` 前缀 + 运行中 + `watch_tail>0` → 摘要行 `│ ` 前缀缀在状态行下。**repl: 服务不再走 watch_tail 日志尾部**（stdout 只归协议，尾部模式只会看到「暂无输出」假象） |
| 节流缓存 | `_repl_cache[name] = (时刻, 响应行)`——投影每步求值，5s 内连续步进不重复打 stdin 往返 |
| `start_service` 提示词 | docstring 增潜规则约定段 + yml 冒号键写法警示（见下） |

投影形态示例：

```
【后台服务状态】当前服务：
  repl:unity-frame(运行中, pid=12052, 已跑 3600s)
    │ STATUS ok uptime=99 tasks=3 fps=24    ← 框架自动发 /status 收的首行摘要
```

**锁外轮询（自查抓到的雷）**：协议轮询内部要调 `send()`——它自己要拿 `_lock`（写 stdin + 读 logs），而 `status_lines` 本身持锁遍历服务表；`threading.Lock` **不可重入**，锁内直接调 = 必死锁。修法：锁内只记占位符（`lines.append(None)` + `repl_marks` 位置清单），**锁外**逐服务轮询回填，空摘要占位行最后过滤掉。

**yml services 段写法警示**（提示词同步带上，落盘 main.yml 时适用）：`repl:` 名字含冒号，建议引号包裹（`- "repl:名字": 命令`）——不加引号 YAML 也能解析（键值分隔判定是「冒号+空格」），但编辑器高亮易歧义；⚠️ **禁止写成 `repl: 名字`**（冒号后带空格会把键截断成 `repl`，直接 ScannerError）。

**冒烟验证**：真 REPL 服务（stdin 收 `/status` 回一行摘要、其它输入写日志文件）→ `status_lines` 正确渲染出摘要行 ✓；非 repl 服务行为不变（watch_tail 日志尾部模式）✓。

**生效**：commit `174131f` 已推送 + site-packages 已同步，`/restart` 后生效。`repl:` 是启动期命名约定——常驻业务服务（如 unity-frame）起名加前缀 + 实现 `/status`，每步投影即得协议级实时状态。

**关联**：[watch_tail](#watch_tailbg_services-投影段附服务日志尾部-n-行2026-10-07用户提案commit-9e1d523)（同段两种状态投影：watch_tail=stdout 日志尾部零协议 / repl:=协议摘要，repl: 服务自动豁免尾部模式）· [service_stdin](#service_stdin-往返语义expect-正则--timeout写入后等-stdout-响应才返回2026-10-07用户提案commit-b1fbfe6)（复用其 expect 往返机制）· [multi-agent · services 依赖声明](../architecture/multi-agent.md)（yml services 段落盘处）· [image-feed](image-feed.md)（每步实况注入家族）。

### 后记：交互即判定（repl_seen）+ 多行 /status 静默窗口——前 5 行封顶（2026-10-08 · 二，用户提案，commit c0e9768）

**动机（用户提案）**：「调用了 `service_stdin` 向服务发送参数之后，也可以判定这个服务是 repl 的，所以下一轮投影时的 bg_services 里该服务可以拿一下 `/status`，比如前 5 行输出」。两点升级：① 判定不再只靠起名，**交互即判定**；② 输出不只是首行，**要前 N 行**。

**改动一：判定双路（或关系）**

```python
# src/background.py status_lines()
if (name.startswith("repl:") or e.get("repl_seen")) and rc is None:
    repl_marks.append((len(lines), name))   # 占位，锁外轮询填充
```

`ServiceManager.send()` **成功写入 stdin**（`stdin.write/flush` 未抛 BrokenPipeError/OSError）即给条目打 `repl_seen = True`——**能接 stdin 的即 REPL 语义**。任何普通服务只要被 `service_stdin` 发过一次参数，下轮投影自动升级为协议服务，**不用起名、不用改 yml**。

**改动二：多行 /status（静默窗口收集）**

`_repl_status` 从「expect 往返挑首个非空行」改为**多行收集**：

| 项 | 语义 |
|---|---|
| 发送 | **直操 stdin/logs，不经 `send`**——规避 `_lock` 不可重入（原章节锁外轮询）+ 嵌套等待 |
| 收集 | 发 `/status` 后等**静默窗口**：0.4s 无新行 = 响应收完；**2s 封顶** |
| 行数 | 取前 **N 行**（N = 该服务 `watch_tail`，**上限 5 行**） |
| 渲染 | 每行 `│ ` 缩进缀在状态行下；`> ` 回显行过滤（含首行摘要判定） |
| 失败 | 超时/异常 → 空摘要（不投影、不炸投影） |

**形态对照**：

```
  auto-repl(运行中, pid=20792, 已跑 1s)          ← 交互前：只有状态行
  auto-repl(运行中, pid=20792, 已跑 1s)          ← service_stdin 交互一次后（自动判定生效）：
    │ STATUS ok uptime=120
    │ tasks=3 running=1
    │ fps=24 queue=0
```

**配套两处**：

- `status_lines()` 的 `watch_tail` **日志尾部模式排除 repl 服务**（防同份输出渲染两遍）——判定扩展后该豁免条件随之升级为「解析后的 repl 判定」（`repl:` 前缀 **或** `repl_seen`）；原章节 watch_tail 正文补记里写的 `name.startswith("repl:")` 按此理解；
- `start_service` docstring（提示词）同步改写：两种判定并列为或关系 + `/status` 口径（**前 5 行封顶、多行请压紧**）+ REPL 约定（一次 stdin 对一次 stdout、过程日志写文件不污染 stdout）+ yml 冒号键写法警示（原样保留）。

**冒烟（真跑）**：无前缀的普通服务 `auto-repl`，`service_stdin("auto-repl", "hello")` 交互一次 → 下轮 `status_lines` 自动带上 3 行 `/status` 输出 ✅。（同轮脚本里那个 ❌ 是断言写成「数列表元素」的误报——三行 `│` 全部渲染出来了。）

**生效**：commit `c0e9768` 已推送 + site-packages 已同步，`/restart` 后生效。

**关联**：原章节（命名前缀路，判定收敛为「前缀 **或** `repl_seen`」）· [service_stdin 往返语义](#service_stdin-往返语义expect-正则--timeout写入后等-stdout-响应才返回2026-10-07用户提案commit-b1fbfe6)（`repl_seen` 打标点 = 发射端；本节的收集语义同源）· [watch_tail](#watch_tailbg_services-投影段附服务日志尾部-n-行2026-10-07用户提案commit-9e1d523)（N 行上限来源 + 尾部模式互斥）。

## start_service 撞死服务被拒：stop 保留 entry × start 只查登记——stop→start 重启路径断裂（2026-10-08，20048 实锤，commit 839f445）

**现象（20048 实锤，用户粘贴日志）**：`stop_service("unity-repl")` → 「已停止」；紧接 `start_service(...)` → 「[已存在同名服务] unity-repl，先 stop_service 再启动」——第 16/17 步原样重试仍被拒。**刚 stop 过的服名 start 不回来，stop→start 重启路径断裂**，glm-official-flash 卡在循环里。

**根因（状态机不对称，两个各自正确的语义撞车）**：

- `stop_service` 是**有意保留 entry** 的——退出复盘、on_exit_wake 通知注入（合成 stop_service 工具记录要 command）、watch_tail 日志尾部都依赖 entry 留存；
- 但 `start()` 的同名检查**只看「登记在不在」**，不看 `proc` 死活。

于是 `stop（进程死、entry 留）→ start 撞名被拒 → 永远起不来`。

**修复**（src/background.py `Scheduler.start`，锁内）：

```python
old = self._services.get(name)
if old is not None and old["proc"].poll() is None:
    return f"[已存在同名服务] {name}，先 stop_service 再启动"   # 同名且仍在跑 → 维持拒绝（防双实例）
# 同名但已退出（stop 过 / 自行崩过）→ 覆盖重建；保险补杀 _kill_tree（try/except pass，防僵尸占位）
```

| 同名状态 | 旧行为 | 新行为 |
|---|---|---|
| 进程仍在跑 | 拒绝 | **拒绝（不变）**——幂等防双实例语义保留 |
| 已退出（stop 过 / 崩过） | 拒绝（**bug**） | **覆盖重建** + 保险 `_kill_tree` |

**连带修复**：声明了 `services:` 的依赖服务**自行崩掉**后，下次实例化 `_ensure_agent_services` 重新拉起同样撞这个拒绝——同一根因一并解决（见 [multi-agent · services 后记](../architecture/multi-agent.md#后记死服务重拉撞名被拒start-同名已退出改覆盖重建2026-10-0820048-实锤commit-839f445)）。WebUI 服务看板的 **▶ Start** 按钮（原 command+cwd 重启）同受此惠及。

**冒烟**：start → stop → start，新 pid 正常起来 ✓。

**生效**：commit `839f445` 已推送 + site-packages 已同步，20048 `/restart` 后 unity-repl 正常重启闭环。

**关联**：[服务看板交互四件套](#服务看板交互四件套收藏--📄-完整日志--stop--start2026-10-06用户提案commits-9a039c5--d22bd34)（▶ Start 消费端）· [on_exit_wake 退出唤醒](#start_service-的-on_exit_wake退出唤醒策略2026-08-30-策略化--2026-09-14-自定义指令--2026-09-23-默认翻转-notify)（stop 保留 entry 的动机侧）· [watch_tail](#watch_tailbg_services-投影段附服务日志尾部-n-行2026-10-07用户提案commit-9e1d523)（同因依赖 entry 留存）。

## start_service 的 on_exit_wake：退出唤醒策略（2026-08-30 策略化 → 2026-09-14 自定义指令 → 2026-09-23 默认翻转 notify）

服务退出时是否唤醒 Agent，由启动参数逐服务声明；策略判定与通知注入在 src/agent.py `_on_service_exit`。

> ⚠️ **2026-09-23 起默认从 `never` 翻转为 `notify`（通知进 inbox），并新增 `on_exit_style` 注入姿势参数**——见[下节](#退出通知默认进-inboxnevernotify-翻转--on_exit_style-注入姿势2026-09-23用户提案commit-275049c)；本节保留演进史。

| on_exit_wake | 语义 |
|---|---|
| `never`（2026-08-30~09-23 旧默认）/ 空串 | 仅内存登记，并入下次自然轮（v0.19.2 防套娃基线；**无自然轮则不可见**——翻转动机） |
| `crash` | rc≠0 唤醒一轮；同名 5 分钟内连续崩溃自动退避为登记 |
| `always` | 任何退出都唤醒（单次任务跑完即报；2026-09-23 起为 notify 同义别名） |
| 非枚举任意文本 | **自定义作业指令：退出即无条件唤醒（无退避），指令原文注入通知**——2026-09-14（commit 7283f52，用户裁定）：LLM 把本参数当「退出时的返回提示」填自然语言，旧实现静默丢弃按 never；误用收编为特性。通知三处注入形态见 [user-interaction · 误用收编](user-interaction.md) |

docstring 已写选择指引：常驻关键服务建议 `crash`；单次任务建议 `always`；**退出后要 Agent 照办的事，直接把作业写进本参数**（启动时留指令、醒来照办）。

## 退出通知默认进 inbox：never→notify 翻转 + on_exit_style 注入姿势（2026-09-23，用户提案，commit 275049c）

**动机**：旧默认 `never` 的「并入下次自然轮」有可见性盲区——通知只进内存 `_notices`（不持久化、不触发轮），**没有自然轮就永远看不到**（agent_watch 退出通知深夜躺一整夜、次日才发现即此症状）。用户裁定翻转：**默认 `notify`——通知进 inbox**。

**notify 三重保证**：

| 保证 | 机制 |
|---|---|
| 不丢 | `inbox.jsonl` 持久化，`/restart` 不丢 |
| 可见 | Agent 空闲时自动消费成轮（无自然轮也必有一轮） |
| 不打断 | 忙时排队到下一步边界注入，不抢进行中的轮 |

**on_exit_wake 翻转后全表**：

| 值 | 行为 |
|---|---|
| `notify`（**新默认**） | 进 inbox（上表三重保证） |
| `never` | 仅内存 `_notices` 登记（最安静；无自然轮则看不到）——旧默认，显式声明仍可用 |
| `crash` | rc≠0 才进 inbox + 5 分钟同名连续崩溃退避（防套娃；常驻关键服务） |
| `always` | notify 同义别名（历史枚举保留） |
| 非枚举任意文本 | 自定义作业指令：退出即通知 + 指令原文注入（2026-09-14 收编语义不变） |

**存量兼容**：entry 已存显式值的按启动时约定走，不受翻转影响——只有缺省值从 `never` 变 `notify`。

**新参数 `on_exit_style`（注入姿势，按服务实例）**：

| 值 | 形态 |
|---|---|
| `tool`（默认） | 合成 `stop_service` 工具记录（启动参数 + 退出码 + 尾部日志——信息最全） |
| `text` | 纯文本通知（轻量——仅 header + 简要命令，不落工具记录） |

签名：`start_service(name, command, cwd, on_exit_wake="notify", on_exit_style="tool")`；三处实现同步（background.py `start()` / background_tools.py `start_service` docstring / agent.py `_on_service_exit`），docstring 参数描述即提示词。

**验证**（mock Agent 策略矩阵六项全过）：①默认=notify（wake + seed 工具记录）✓ ②never 登记不唤醒 ✓ ③crash rc0 静默 / rc1 唤醒 / 二连崩退避 ✓ ④always / 自定义文本 ✓ ⑤style=text 纯文本无 seed vs tool 合成记录 ✓ ⑥ServiceManager 签名（notify 默认 + style）✓。**生效方式**：引擎层三文件（background.py / agent.py / background_tools.py），`/restart` 后新启动的服务按新默认/新参数走。

**关联**：[user-interaction · 唤醒策略化](user-interaction.md)（策略化起点）/ [user-interaction · 误用收编](user-interaction.md)（自定义文本语义）/ [user-interaction · 语义标签](user-interaction.md)（通知轮渲染）。

## 与其他模块的关系

- [user-interaction](user-interaction.md)：schedule 唤醒的轮走 inbox；通知语义标签体系给 ⏰ `schedule:` source（通知气泡形态 + 混合批批首归属 `schedule:z` 判定）；service_exit / bg_task / schedule 三族唤醒全景表见该页
- [run-python](run-python.md)：run_python/run_shell 超时转后台的 bg_task 完成通知是另一族唤醒（恒唤醒），与本调度互补
- [api-status](api-status.md)：snapshot 携带任务列表（展示字段随 v0.23.1 更新）

## 注意事项

- 引擎层改动需 `/restart` 生效；随 v0.23.1 上 PyPI（`pip install -U agt-agent`）
- 用法例：`add_schedule('morning', at='09:00', message='早会时间')`
- 本页 2026-09-18 三连（组合模式 + 持久化 + 同名摘旧）随 **v0.29.4** 上 PyPI（PyPI 已上线，见 [v0.29.4 发布记录](../releases/v0.29.4.md)）
- ⚠️ 定时任务持久化的**首版（95649e3）实际从未生效**——extra_state 直写被覆盖式重建抹掉 + 恢复时机过早恒空跑，e22062c（单一真源架构）修复后才真正跨重启存活（见上方后记）；修复 `/restart` 前设置且已丢失的任务须重设
- ⚠️ 2026-10-04 起 **autonomous 已整体退役**：五工具 + `/autonomous` 命令 + agent 状态机 + WebUI 开关全删——等价能力用 `add_schedule(code=..., deadline=..., mode=...)` 表达（见[融合章节](#autonomous-融合code--deadline--mode-三参数--旧纯自主模式整体退役2026-10-04用户提案commits-0716fc0--01edc18)）；历史 meta.json 里的 `autonomous_*` 键为无害冗余，无需清理

## 相关页面

- [user-interaction · 后台通知 wake 语义](user-interaction.md) — schedule 唤醒轮的路由与语义标签
- [v0.23.1 发布记录](../releases/v0.23.1.md) — 每日闹钟的发布收口
- [v0.29.4 发布记录](../releases/v0.29.4.md) — 2026-09-18 scheduler 三连（组合模式/持久化/同名摘旧）发布收口
- [api-status](api-status.md) — snapshot 中的任务展示

