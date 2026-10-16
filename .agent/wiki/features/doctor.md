# doctor · BadRequest 诊断修复 Agent——四件套工具族 + 失败现场存档 + 回退链耗尽自动派单（2026-10-16，用户提案，commit 656193a）

> LLM 请求因【数据问题】（坏图 / 超长 / 畸形消息）400、回退链整链冷却仍救不活时，引擎自动派 doctor 诊断修复——**子 Agent 走 `_bg` 异常分支，主 Agent 走轮异常出口 `dispatch_doctor`**（2026-10-16 · 二补齐，见「主 Agent 分支」节）。定位 = BadRequest 自愈的 **L2**：[L1 引擎自愈](llm-network-resilience.md)（拒图降投影 / 坏图剥图重试）是绕过，doctor 是「找到坏数据在哪、修在源头、恢复流程」的根治线。实现：src/multiagent.py `make_doctor_tools` + src/llm_client.py 存档点 + src/chat.py 注册 + `.agent/agents/doctor/` 声明。

## 职责与设计（用户提案 2026-10-16）

**「诊断→试修→验证→恢复」四件套闭环**——工具族按 doctor 专属定形。设计动机直接来自 worker 施工线坏图三连炸的实战教训：L1 剔图救活了 worker，但「图是哪来的」没有答案——doctor 的 `req_inspect` 就是为回答这个而生（该案例已内置进 doctor 人设）。

```
BadRequest → 回退链耗尽（RuntimeError）→ 引擎派 doctor
  req_inspect 定位嫌疑 → req_probe(patch₁) ─ 200 ✓ → req_patch 落盘 → req_resume 恢复原流程 → 汇报
                          └ 400 ✗ → 加大力度（drop 更多 / 极简模式只留 system+末条）→ req_probe(patch₂)…
  → 穷尽 5 次仍 400 → 判定非数据问题（模型端 / 账号）→ 上报，不再瞎试
```

## 三层自愈定位：L1 引擎剔除 / L2 doctor 四件套 / L3 主 Agent 上报（2026-10-16）

BadRequest（数据问题：坏图 / 超长 / 畸形消息）三层防线，越往下越贵、越靠近根治：

| 层 | 谁 | 手段 | 性质 |
|---|---|---|---|
| **L1 引擎自愈** | src/llm_client.py | 端点拒图 → 历史轮图片投影即降文字占位（`ImageUnsupportedError`）；400 图解码失败 → `_strip_image_parts` 剥图重试 | **绕过**——不修数据，先把这一轮保住（[LLM 客户端韧性 · 五/七](llm-network-resilience.md)） |
| **L2 doctor** | `make_doctor_tools` 四件套 | `req_inspect` 定位嫌疑 → `req_probe` 最小请求验证修法（cap 5）→ `req_patch` 过验证门后落盘（存档消息 + 当前轮 toollog 结果）→ `req_resume` **清 `_provider_cooldown`**（修好了不该白等 300s 冷却）+ `resume_interrupted()` 续跑 | **根治**——坏数据在哪、修在源头、恢复原流程（本页） |
| **L3 上报人** | 主 Agent 轮异常出口 | doctor 穷尽 5 次仍 400（判非数据问题：模型端 / 账号）或派单失败 → 中断事件 + 一键充值/重试入口，交用户决策；**派单失败只告警、不掩盖原异常** | 止损——自愈层承认修不动，交给人的上下文（[气泡交互 · 中断轮充值入口](bubble-interaction.md)） |

- **层间关系**：L1 不产出诊断信息（只让轮不炸）→ L2 消费 L1 留下的失败现场（`last_failed_messages` 存档点在回退链耗尽处深拷贝落档）→ L3 是 L2 的兜底报告面。三层共用同一个触发前缀特征串「回退链不可用」。
- **不套娃铁律**：L2 永不自派 doctor（`_bg` 侧 `_name != "doctor"` 熔断 + 主 Agent 出口判据 `agent_id ∈ {"", "_main_"}`）——自愈层不能把自己递归进去。
- **冷却与自愈的咬合**：数据坏 → 回退链上每个 provider 都 400 → 全进 `_provider_cooldown`（默认 300s）→ 整链耗尽抛 RuntimeError；修好后 `req_resume` 清冷却，否则「改对了也得干等 5 分钟」。

## 四件套（src/multiagent.py `make_doctor_tools(agent)` 闭包）

| 工具 | 职责 | 关键语义 |
|---|---|---|
| `req_inspect(agent_id, part="")` | 目标 agent 最近失败请求的逐条体检 | [i] role / content 形态(str\|list) / 长度 / image_url×N（base64 头 60 字）/ tool_calls 形态 / 可疑特征标记；`part=images\|tools\|system` 只看一类 |
| `req_probe(agent_id, patch)` | 带 patch 发一次最小探测请求（同模型、`max_tokens=16` 级——省钱） | 深拷贝上应用 patch → `_chat_inner`；200=修正有效 / 400=新错误详情继续下一轮；每 agent 计数上限 `_DOCTOR_PROBE_CAP=5` |
| `req_patch(agent_id, patch)` | 真正应用到 session 缓存 | **内部强制再 probe 一遍，未 200 拒绝落盘**（防把好数据改坏）；通过则 in-place 修存档消息 + 同步修当前轮 toollog 结果的 `<img>`/超长 + `_autosave` + `invalidate_projection` + probe 预算清零 |
| `req_resume(agent_id)` | 恢复原 agent 流程 | **清 `_provider_cooldown`**（修好了不该再等冷却）+ `resume_interrupted()` 三态透传（"" / `__queued__` / 错误） |

**patch 四原语**（超长判据 `_DOCTOR_LONG_LIMIT=5万`）：`strip_images:[i…]` 剔图部件 / `truncate:{"i":2000}` 截超长 / `drop:[i…]` 删畸形消息 / `replace:{"i":"…"}` 替换损坏块——patch 归一化 / 应用 / 会话修复三个模块级助手单源。

## 数据源：失败现场存档点（src/llm_client.py）

回退链整链耗尽、`raise RuntimeError` 之前 `self.last_failed_messages = copy.deepcopy(messages)`——**深拷贝冻结**（后续重试不污染现场）且**成功后不清除**（保留最近一次失败现场，再次失败自然覆盖）。机制详见 [LLM 客户端韧性 · 八](llm-network-resilience.md)。

## 接线四处

| 处 | 内容 |
|---|---|
| 自动派单（子 Agent） | `_bg` 异常分支：`RuntimeError` +「回退链不可用」特征 **且失败者非 doctor** → `agent_prompt("doctor", …)`（prompt 含 target agent_id + 错误全文 +「按 doctor.md 工作循环处理」） |
| 自动派单（主 Agent，2026-10-16 · 二） | `Agent.run()` 轮异常统一出口（emit `interrupted` 处）：同款特征 **且 `agent_id` 空/`_main_`** → `multiagent.dispatch_doctor(agent, reason)`——详见下一节 |
| 熔断 | doctor 自身失败（`_name != "doctor"` 判定）即短路——**不递归派 doctor**，自愈层永不自我派单 |
| 注册 | src/chat.py `_reg(make_doctor_tools(agent), "诊断")`——挂主 Agent 工具箱；doctor.yml `tools: ""` 自动继承 |

## 主 Agent 分支：轮异常出口 dispatch_doctor 程序化派单（2026-10-16 · 二，用户裁定）

用户裁定（2026-10-16）：「主 Agent 出现 BadRequest 也可以触发 doctor」。此前 `_bg` 自愈网只覆盖子 Agent 的 `agent_prompt` 线程——主 Agent 回退链耗尽时只有中断事件 + 充值按钮，全靠人点。

- **接线点**：`Agent.run()` 的轮异常统一出口（emit `interrupted` 事件处）——异常特征含「回退链不可用」**且** `agent_id` 为空/`_main_`（主 Agent 判据）→ 自动派 doctor。派单失败只告警、不掩盖原异常（代码本身跑在异常路径里）。
- **`dispatch_doctor(agent, reason)`**（src/multiagent.py 模块级函数）：程序化派单——`agent.tools.run("agent_prompt", {...})` 直接进既有工具实现，**复用 agent_prompt 完整管线**（registry 登记 / answer 通知回流 / `_bg` 线程 / enum 快照），不经 LLM 决策；prompt 自带目标（`_main_`）+「按 doctor.md 工作循环处理」指引。与 `_prompt_subagent`（施工委托）同属「引擎程序化派发」家族。
- **不双派**：子 Agent 场景仍走 `_bg` 接线——那里带 caller 上下文更全，两条路径互斥。
- **防套娃三层**：① 主 Agent 出口判据 `agent_id ∈ {"", "_main_"}` → doctor 自身失败（agent_id=`doctor`）此处不派；② `_bg` 侧 `_name != "doctor"` 熔断；③ probe 上限 5，穷尽即上报。
- **验证**：mock 工具面三断言（派单参数 = `agent_prompt("doctor", …)` / 派单异常兜底不炸 / `agent.run` 接线特征存在）+ 全量回归 **26 passed**。
- **生效/发布**：引擎代码，`/restart` 生效；同日随 push-retry 推远端（`ad64506..96c2fda`）。

## 后记：四件套收敛为 doctor 专属（2026-10-17，用户裁定）

用户裁定（2026-10-17）：「doctor 的那几个工具只作为 doctor 自己的工具就可以了，它也只需要调用那几个工具」。四件套从「主 Agent 工具箱注册 + doctor.yml 继承」收敛为**实例化专属注入**：

- **chat.py 撤注册**：`_reg(make_doctor_tools(agent), "诊断")` 及 import 一并移除——主 Agent 工具箱不再带 4 个诊断工具。核心理由：**工具 schema 是全局公共负载**（每会话每轮投影都携带），诊断工具只有诊断 Agent 用得上，不该向所有会话征噪音税。
- **实例化注入**：multiagent 的 doctor 分支改 `Toolbox(*make_doctor_tools(agent))`——doctor 的工具面板 = **纯四件套**（req_inspect / req_probe / req_patch / req_resume），与 doctor.md 人设的「工作循环」约束双重对齐：只见诊断工具 + 只按循环行事。
- **派单链不变**：`dispatch_doctor` / `_bg` 自动派单走 agent_prompt 工具面（引擎程序化通道，不经 LLM 工具决策）——收敛对自动派单零副作用。
- **doctor.yml `tools: ""` 留空无碍**：实例化路径对 doctor 有专属分支，工具箱恒为四件套。

**验证**：四件套断言（req_inspect / req_patch / req_probe / req_resume 四名齐）· chat.py 活性引用清零（全仓 src 剩 multiagent 定义 + doctor 分支两处）· import 冒烟 · test_doctor_tools 8 passed。commit 本地（push 网络抖动，重试任务在岗）。

**语义收获**：工具跟着角色走——角色专属工具收敛进实例化路径，通用工具箱只留跨角色能力。

## 声明（.agent/agents/doctor/，目录形态）

- `doctor.yml`：`tools: ""`（留空无碍——2026-10-17 起实例化路径对 doctor 有专属分支，工具箱恒为四件套）/ `model: glm-official-flash` / 最小装配（system + file 人设 + user_message + steps）
- `doctor.md` 人设四段：**工作循环**（inspect → probe → patch → resume → 汇报，严格按序）/ **安全阀**（probe 上限 5、patch 必过验证门、只修消息数据不碰引擎代码与 session 结构、自己失败不派另一个 doctor）/ **实战案例**（worker 坏图三连炸内置）/ **常见坏数据模式**（坏图截断 base64 / 工具结果几十万字符 / 空 content 数组与 role 序列错乱 / 首条历史浓缩块损坏）

## 安全阀汇总

- probe 上限 **5 次/agent**——穷尽判非数据问题，如实上报不瞎试
- req_patch 落盘前强制 probe 验证门（工具内部实现，人设明令不可绕过）
- **防套娃三层**：主 Agent 出口判据排除 doctor（`agent_id ∈ {"", "_main_"}`）× `_bg` 侧 `_name != "doctor"` 熔断 × probe 上限 5 快速失败——自愈层永不自我派单、不递归
- probe 是最小请求（max_tokens=16 级）——穷尽 5 次也烧不了几个 token
- 主 Agent 派单失败只告警：异常路径里不掩盖原中断原因（2026-10-16 · 二）

## 验证（commit 656193a）

- test/test_doctor_tools.py **8 例全绿**：inspect 报告形态/目标查找、probe 深拷贝应用+计数上限、patch 验证门拒绝/落盘+session 源头修复、resume 冷却清零+三态透传、四原语文义、存档点（真实 LLMClient + patched `_chat_inner`，验证冻结性与深拷贝隔离）
- 全量 pytest 风格回归 **82 passed**；脚本式测试逐个跑全过
- test_context_panic.py FAIL=3 复跑核实为**既有失败**（token EMA / panic 折叠逻辑，与 doctor 零交集）——列入修复待办

## 施工插曲：worker 留 stash 未弹（同日）

worker 验证「测试失败是否既有」时 `git stash -u` 后因 Windows 管道命令报错中断，`git stash pop` 未执行——**全部施工成果滞留 stash@{0}**，worker 汇报里醒目标注「commit 前务必先 pop」。主 Agent 验收轮 `git stash pop` 无损恢复；顺带剥离 worker 首版 commit 误带的杂物（`.agt_test/` 等 coder 临时目录——`git rm -r --cached` + 物理清理 + `commit --amend`），终版干净 7 文件 810 行。

## 注意事项

- **/restart 生效**（引擎层接线）
- probe 计数为内存 dict、按 agent_id 持久计数：一次事件修不成功会占满 5 次预算直到成功落盘才清零，**跨事件不重置**——按施工牌「内存 dict 上限 5」实现，按事件重置留待裁定
- session 侧修复仅覆盖**当前轮** toollog 结果（历史档消息按牌面「只修缓存并说明」）；toollog 修复用 `record()` 重记一行（JSONL 重放取后值，持久化收敛，见 [toollog](toollog.md)）
- 存档点只在回退链耗尽时写——普通单次失败（还有后备可退）不产生现场

## 相关页面

- [LLM 客户端韧性](llm-network-resilience.md)——L1 自愈（拒图降投影 / 坏图剥图）与存档点宿主
- [中断轮恢复](resume-interrupted.md)——req_resume 的续跑机制
- [多 Agent 体系](../architecture/multi-agent.md)——引擎失败自愈派单（第四条派活入口）与施工委托对照
- [技能体系](skills.md)——doctor 人设实战案例（worker 坏图三连炸）的事发现场
- [toollog](toollog.md)——req_patch 同步修当前轮工具结果的落点
