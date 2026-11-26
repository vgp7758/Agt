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
- 实现位置：`_exec_tool_guarded`——**串行路**的统一包装；非白名单工具提交共享线程池 `_TOOL_GUARD_EX`（类属性，全实例共用 2 个工作线程）+ `fut.result(timeout=)` 限时
- 白名单 `_TOOL_TIMEOUT_WHITELIST` 不重复包（双层超时会干扰既有语义）——直调、不包硬超时：
  - `run_python` / `run_shell`：超时自动转后台（自带管家语义）
  - `sleep` / `wait_subagents`：显式长等
  - `remote_ask` / `explore` / `explore_resume`：参数化长等（timeout / budget 参数）
  - `agent_prompt`：全异步派活（立即返回）
  - `skill_equip` / `skill_use`：技能服务装配 / 命令（可能长拉起）
- **白名单只豁免硬超时，不豁免巡检**：直调期间心跳原本不动，`sleep(1800)` 到 20 分钟照样被巡检判静默拉 doctor——2026-11-26 起白名单分支由 `_HeartbeatKeeper` 包住调用维持心跳（见下方后记二）

## ② 心跳 `_hb_ts`（agent.py，判卡唯一依据）

七个刷新点覆盖轮生命周期：

| 位置 | 注释口径 |
|---|---|
| 步循环顶 | 看门狗判卡死的步进展信号 |
| 工具进入执行 / 工具完成 | 工具层自愈后即恢复于此（线程池分支；白名单分支不走这里，改走末行的续期器） |
| LLM 调用完成 | LLM 网络挂起时静默从这里起算 |
| 并行收割进展 | 并行工具部分完成也算活着 |
| hold 闸门等待循环（2026-11-11 新增） | 挂起是用户指令下的正常等待——每次唤醒（0.5s 轮询）touch 一次，防止挂起期静默被误判（见下方后记） |
| 白名单长等工具执行期间（2026-11-26 新增） | `_HeartbeatKeeper`：进入即刷一次 + daemon 线程每 30s 续一次（上限 3600s）——`sleep(1800)` 这类设计内的等待不再被巡检判静默（见下方后记二） |

末两行是同一条思想的两个落点：**设计内的等待不算卡死**——hold 是用户指令下的等待，白名单长等是工具语义内的等待。

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

### 后记：hold 挂起误判拉 doctor——闸门等待循环内维持心跳（2026-11-11，用户提案，commit f3e4ee9）

#### 后记：hold 挂起误判拉 doctor——闸门等待循环内维持心跳（2026-11-11，用户提案，commit f3e4ee9）

**现象**：hold on 挂起时看门狗照样触发「疑似卡死」拉 doctor 自动诊断——挂起明明是用户指令下的正常等待。

**根因**：hold 闸门（`_PROC_HOLD["event"]`，进程级挂起所有 Agent 的 react 步循环，0.5s 轮询便于 Ctrl+C 打断）挂在**步循环顶**——此时 `_react_running=True`（轮未结束）、循环内无人 touch `_hb_ts` → 静默超阈值 → `stuck_candidates` 命中 → 派 doctor。

**修复（一行，src/agent.py）**：闸门等待循环内每次唤醒 touch 心跳——

```python
while self._hold and not self._stop_flag:
    _PROC_HOLD["event"].wait(0.5)
    self._hb_ts = time.time()   # hold 期间维持心跳
```

**边界语义（两向都保住）**：

| 场景 | 行为 |
|---|---|
| hold 挂起等待 | 心跳常新 → 不再误判 ✅ |
| 轮内真卡死（LLM 挂起 / 工具卡点） | 发生在**闸门外**的步内（LLM 调用完成 / 工具进出才 touch）→ 静默照旧被逮到 ✅ |
| 空闲 agent | `_react_running=False` 本就不参与判定 ✅ |

生效需 /restart（引擎代码）。

### 后记二：白名单长等工具执行期间心跳续期——sleep(1800) 不再 20 分钟拉 doctor（2026-11-26，用户实锤，commit e01bed2）

**现象**：Agent 主动调 `sleep(1800)`，到第 20 分钟仍触发巡检、拉 doctor。用户问诊：「这个 sleep 期间是不是也需要加一下心跳？」——结论：需要。

**根因**：`sleep` 在 `_TOOL_TIMEOUT_WHITELIST` 里，走 `_exec_tool_guarded` 的白名单分支**直调不包硬超时**（豁免本身是对的）；但直调期间**心跳一次都不刷**——「工具进入执行 / 工具完成」两个刷新点都在线程池分支里，白名单分支碰不到。巡检按 `stuck_secs`（默认 1200s）判静默 → `sleep(1800)` 第 20 分钟必中。一句话：**第一道防线豁免了它，第二道防线不认**——两道防线对「设计内的等待」口径不一致。

**修复（src/agent.py，`_HeartbeatKeeper` 上下文管理器）**：白名单分支由裸直调改为续期器包住调用——

```python
if name in _TOOL_TIMEOUT_WHITELIST:
    # 白名单直调（不包硬超时）——但执行期间要维持心跳
    with _HeartbeatKeeper(self, name):
        return self._exec_tool(name, args)
```

| 要素 | 值 | 语义 |
|---|---|---|
| 进入即刷 | `__enter__` touch 一次 `_hb_ts` | 与线程池分支的「工具进入执行」对称 |
| `_HB_KEEPALIVE_INTERVAL` | 30.0s | daemon 线程 `hb-keepalive` 按间隔续期 |
| `_HB_KEEPALIVE_MAX` | 3600.0s | sleep 的合法上限；累计超过即停止续期 + log warning「之后交由看门狗按常规判定」 |
| 退出即停 | `__exit__` 置 `_stop` | 工具返回线程即止，不留孤儿续期 |

续期线程启动失败静默降级（`try/except pass`，行为同修复前）。**一处改动覆盖整个白名单 10 个工具**（清单见上文 ①）。

**边界语义**：

| 场景 | 行为 |
|---|---|
| 串行路白名单长等（`sleep(1800)` / `wait_subagents` 长 join / `remote_ask` 长等） | 心跳常新 → 不再误判 ✅ |
| 白名单工具真卡死 | 续期到 3600s 停 → 静默满 1200s 后巡检照常逮到；**代价：捕获时刻从 20min 推迟到约 80min**（3600 + 1200） |
| 非白名单工具 | 行为不变：线程池限时 + 超时注入判卡死文本 ✅ |
| 审批等待（`run_python` / `run_shell` 的 `_ask_approval`，在工具函数体内，src/real_tools.py） | 位于 `_exec_tool` 之内 → 串行路上这段等待现在也在续期范围内（此前同样算静默） |
| **并行路不受影响** | 多工具并行走 `_exec_tool` 直连、不经 `_exec_tool_guarded`：`sleep` 与其它工具同批并行时整组仍受 `_tmo` 兜底，到点给未完成项注入判卡死文本——原有行为未动，需要的话可另行放开 |

与 [hold 闸门维持心跳](#后记hold-挂起误判拉-doctor闸门等待循环内维持心跳2026-11-11用户提案commit-f3e4ee9) 同一思想：**设计内的等待不算卡死**——这是该思想的第二个落点。

**验证**：`test/test_heartbeat_keepalive.py` 四项——① 进入即刷 / 执行期间持续续期 / 退出即停；② 超上限后不再续期；③ 巡检端到端语义（纯函数拼装）：无续期对照判卡死 → 续期期间 `stuck_candidates` 判不出 → 越过续期上限恢复判定；④ `_exec_tool_guarded` 接线：白名单分支真 `sleep(0.12)` 返回时心跳仍新鲜，非白名单正常路径不变、卡死仍走既有硬超时注入。隔离实例真实每轮路径跑 3 轮全过（其中一轮模型真调了白名单工具 `run_python`）。**未覆盖**：真实 20 分钟阈值的端到端验证（单测把间隔 / 阈值缩到测试尺度）；让模型调 `sleep(3)` 的那一轮模型只推理未真调——长等分支由单测的真实短时 `sleep` 覆盖。

**生效**：引擎代码，需 `/restart`；重启前 `sleep(1800)` 仍会触发 doctor。配套：`sleep` docstring 补一句「等待期间引擎维持心跳，不会被看门狗判卡死」（见 [misc_tools](misc-tools.md)）；`wait_subagents` 侧指针见 [多 Agent · wait_subagents](../architecture/multi-agent.md#wait_subagents干等调查与诊断埋点2026-08commit-22cf719)。

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
