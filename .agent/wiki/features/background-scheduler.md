# 定时/到点任务调度 · add_schedule（src/background.py + background_tools.py）

> v0.23.1（2026-09-04，commit `3c287c8`）给 at 补上**每日闹钟**短格式——本页是该子系统首次建页。

## 职责

- **src/background.py**（现 414 行）：后台调度线程，`_loop` 周期扫描 `next_fire`，到点把消息推给 Agent 触发一轮（唤醒链见 [user-interaction · 后台通知 wake 语义](user-interaction.md)）
- **src/background_tools.py**（现 84 行）：工具入口 `add_schedule`（建任务）/ `list_schedules`（查任务），LLM 可直接调用
- 两类触发：**interval**（每 N 秒）与 **at**（到点；v0.23.1 起支持每日闹钟）

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

| at 写法 | repeat 缺省行为 | 显式 repeat |
|---|---|---|
| 短格式 `'09:00'`（`HH:MM[:SS]`） | **每日闹钟**（True） | False → 只响下一个该时刻一次 |
| 完整 ISO `'2026-07-20T17:30:00'` | **单次到点**（兼容不变） | True → 每日循环（取时刻部分做锚点） |

## 每日闹钟实现要点（src/background.py）

- `_DAILY_RE = ^\d{1,2}:\d{1,2}(:\d{1,2})?$` 判短格式；短格式校验时/分范围（`25:00` 报时间格式错误），ISO 已过时报「时间已过」，at 空串同样报格式错误（不静默吞任务）
- **`_next_daily_fire()` 同秒边界**：触发后从 daily 锚点重算下一未来时刻，候选 `<= now`（同一秒内触发也算过期）即再 +1 天——**当天绝不二次触发**，死循环根治；重算恒取下一未来时刻
- 触发后：每日任务**保留**（重算 next_fire）；单次任务触发后**删除**
- background.py imports 相应补 `re` / `timedelta` / `Optional`

## 展示适配

- `list_schedules` / `/api/status` snapshot：每日任务展示「每天 09:00 (还有Ns)」，单次任务带「单次」标注
- 后台看板的定时任务分组同步可见每日任务

## 与其他模块的关系

- [user-interaction](user-interaction.md)：schedule 唤醒的轮走 inbox；通知语义标签体系给 ⏰ `schedule:` source（通知气泡形态 + 混合批批首归属 `schedule:z` 判定）；service_exit / bg_task / schedule 三族唤醒全景表见该页
- [run-python](run-python.md)：run_python/run_shell 超时转后台的 bg_task 完成通知是另一族唤醒（恒唤醒），与本调度互补
- [api-status](api-status.md)：snapshot 携带任务列表（展示字段随 v0.23.1 更新）

## 注意事项

- 引擎层改动需 `/restart` 生效；随 v0.23.1 上 PyPI（`pip install -U agt-agent`）
- 用法例：`add_schedule('morning', at='09:00', message='早会时间')`

## 相关页面

- [user-interaction · 后台通知 wake 语义](user-interaction.md) — schedule 唤醒轮的路由与语义标签
- [v0.23.1 发布记录](../releases/v0.23.1.md) — 每日闹钟的发布收口
- [api-status](api-status.md) — snapshot 中的任务展示
