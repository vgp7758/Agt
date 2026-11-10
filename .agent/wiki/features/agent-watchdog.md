# agent_watchdog · Agent 卡住看门狗自愈网（src/agent_watchdog.py + agent.py 工具硬超时；施工牌 p_aa314b2f，2026-11）

> 动机：Agent react 循环卡死此前只能人肉发现 + 手动 `/restart`（实锤：agent_prompt 单调用卡 5h+）。看门狗自愈网三层合围：**① 工具硬超时**（第一道，工具卡点层内自愈）→ **② 心跳 + ③ 巡检 daemon**（第二道，兜锁死锁 / 投影锁 / LLM 网络挂起等非工具卡点 → 拉 doctor 诊断）；同批还有 **scheduler `_loop` 线程永不死**（见 [background-scheduler](background-scheduler.md)，治倒计时归零不触发）。第一版**只诊断不自动重启**——数据风险留给用户裁定，doctor 回报里给建议命令。

## 职责与分工语义（src/agent_watchdog.py docstring 定稿）

两道防线按「卡点能不能定位到工具」分流：

| 防线 | 卡点类型 | 触发后果 |
|---|---|---|
| ① 工具硬超时（agent.py） | 单次工具执行超阈值 | 宿主注入「判卡死」结果文本让 React 继续跑——心跳恢复，巡检不触发（自愈闭环在工具层完成） |
| ③ 巡检 daemon（agent_watchdog.py） | **非工具卡点**：锁死锁 / 投影锁 / LLM 网络挂起等 | `_react_running=True` 且心跳静默超阈值 → py-spy dump + 拉 doctor 诊断 |

## ① 工具硬超时（agent.py `_TOOL_STUCK_SECS`）

- 默认 **1200s=20min**，可经 settings.json `watchdog_stuck_secs` 配置（`config.load_watchdog_stuck_secs()`）——与巡检判心跳静默**共用同值**（一处配置两处消费）
- 超时动作：注入「判卡死」结果文本让宿主继续跑；**底层线程不杀**（无法安全 kill，孤儿线程泄漏可接受）
- 白名单 `_TOOL_TIMEOUT_WHITELIST` 不重复包（双层超时会干扰既有语义）：`run_python` / `run_shell`（超时自动转后台的管家语义）、`sleep` / `wait_subagents`（显式长等）等

## ② 心跳 `_hb_ts`（agent.py，判卡唯一依据）

五个刷新点全覆盖轮生命周期：

| 位置 | 注释口径 |
|---|---|
| 步循环顶 | 看门狗判卡死的步进展信号 |
| 工具进入执行 / 工具完成 | 工具层自愈后即恢复于此 |
| LLM 调用完成 | LLM 网络挂起时静默从这里起算 |
| 并行收割进展 | 并行工具部分完成也算活着 |

## ③ 巡检 daemon（agent_watchdog.py）

- `_SCAN_INTERVAL=120s` 扫一轮 `registry.list_active()`；`check_once` 里**每轮读盘** `load_watchdog_stuck_secs()`——settings 热改即生效
- 判定 `stuck_candidates`（纯函数，单测直接喂伪造 entries）四条件缺一不可：`status=='running'` + `_react_running=True`（idle/wait 静默是正常语义，不判卡）+ 心跳静默 > stuck_secs + 过 30min 冷却（`_COOLDOWN_SECS=1800` 防重入）
- 防误报两则：无 `_hb_ts` 的实例（老代码构造）不判——**宁可漏报不误报**；冷却先记再触发（派发中异常也不重入）

## 触发动作：`probe_stuck_agent` 四步

1. **py-spy dump 本进程全线程栈** → 存档 `<session_dir>/agents/<aid>/stuck_stack.log`（py-spy 不在 PATH 时写占位说明，doctor 仍有料可读；单次超时 30s）
2. **近期事件摘要**：该 Agent 自己 events.jsonl 尾 10 条（agent_query_events 同款视角）
3. **派 doctor**：栈 + 事件 + 任务摘要拼诊断材料，`agent_prompt("doctor", …, caller="user")`——fire-and-forget **不唤醒烧轮**；prompt 明确「只诊断与建议，不修引擎代码、不碰 session；栈显示已自愈则仅归档报告」
4. **通知**：log warning + `_emit` llm_log 事件（WebUI 日志抽屉，`scene="watchdog"`）

## 装配与配置

- chat.py **web_main / cli 两入口** agent 建好后各一行 `start_watchdog(agent, registry)`
- 幂等（模块级 `_started`）；daemon=True 进程退出自然亡，无清理需求
- 防呆：registry 缺 `list_active` 直接拒启并报错——chat.py 里同名局部变量 `registry` 是 CommandRegistry 命令表，传错对象巡检会**静默空转**（显式拒绝优于静默）

## 生产实锤（上线当日即触发）

doctor 实例的当前任务即「【看门狗触发】Agent 'main' 疑似卡死（非工具卡点兜底诊断）」——巡检对主 Agent 真实派发过诊断，链路（心跳判定 → py-spy → doctor → 回报）生产闭环。

## 注意事项

- **不自动重启**：第一版只诊断 + 建议（重启命令写在 doctor 回报里）——自动重启的数据风险（写盘半截、session 状态）留给用户裁定
- 工具超时注入的是**结果文本**不是杀线程——孤儿线程仍在跑，可能稍后自行完成（副作用照常发生）；看 `stuck_stack.log` 判断是否需要人工清理
- 与 [restart_watchdog](../guides/ops.md)（进程级 /restart 拉起看门狗）是两码事：那个守**进程**存活，本页守 **Agent react 循环**不卡死
- 心跳静默阈值即工具硬超时阈值：调小会更早注入判卡死结果（长工具误伤），调大则巡检触发也晚——`watchdog_stuck_secs` 一个值平衡两道

## 相关页面

- [doctor · BadRequest 诊断修复 Agent](doctor.md)：巡检的诊断消费端（agent_prompt 派单，caller='user'）
- [background-scheduler · `_loop` 线程永不死](background-scheduler.md)：自愈网第三件——调度线程静默死亡根治（8000 实锤 2026-11-09）
- [LLM 客户端韧性](llm-network-resilience.md)：LLM 网络挂起是巡检兜底的卡点类型之一
- [多 Agent 体系](../architecture/multi-agent.md)：registry / agent_prompt 派发通道
