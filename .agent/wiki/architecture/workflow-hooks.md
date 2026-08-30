# 工作流引擎与钩子

> src/workflow.py + workflow_xml.py + agent.py(_run_hooks)。节点细节见 [docs/architecture/04-workflow.md](../../../docs/architecture/04-workflow.md)，本页补充钩子链路、async 元信息、运行观测与快照检测闭环。

## 双格式与热加载

- `.agent/workflows/<名>.xml`（推荐，CDATA 免转义）或 `.json`（Coze 原生画布）
- `.meta` 旁车 / XML 根属性：name/description/**hook**/enabled/**hidden**/**async**/auto/coze_url
- 每轮对话开始自动扫描；**hidden 默认 true（2026-08，commit a667da4 起翻转）——未写 hidden 不注册为 `wf_*` 工具**，只有显式 `hidden="false"`（编辑器取消勾选+保存）才进工具箱 schema；钩子触发/子工作流调用不看 hidden（保证不变，见下下节）
- meta.hidden 的 XML 往返已修复（api_wf_get 读根属性）——历史丢 hidden 的文件已补回；写侧现显式写 true/false 两值（往返幂等）
- **type3 LLM 节点的 model 序列化为独立 `<model>` 标签**（workflow_xml.py 写侧），不是 `<param name="model">`——grep param 形态搜不到 ≠ model 未设置。误诊实例（2026-08-30，commit e8ef64a 修复）：曾据 param 搜不到错判 recap_gen「model 未设置→utility 兜底」，真实是 `<model>proxy</model>` → proxy 路由 → glm（bigmodel）429 余额不足（319 条失败）；同批清掉播种源 `src/workflows/recap_gen.xml` 的新老形态**双声明**残留（老 `<param name="model">local-qwen</param>`——该 provider 键已不存在，若生效会报未知模型 + 新标签并存，删旧留新）
- **编辑器里改模型不点保存不落盘**：「日志还在用旧模型」先读磁盘 XML 核实再怀疑刷新（recap_gen 案例：用户选了 local-lfm 但没点保存，磁盘一直 proxy）；反向同理——改完 XML **当轮即生效**（每轮重扫，下轮钩子触发就走新模型，无需 /restart）

## `_get_llm` 静默 fallback 加日志（2026-08-30，commit 0d852a0）

**背景（recap_gen 三轮排障收官，续 e8ef64a）**：recap_gen.xml 已改成 `<model>local-lfm</model>`，用户仍见「调的是 utility 然后走回退链」，怀疑反序列化读的是旧 `<param name="model">local-qwen</param>` 残留、`<model>` 标签没读。

**param 假设五层排除（全过）**：磁盘 XML（`<model>local-lfm</model>` 唯一，param 残留 e8ef64a 已清）→ `xml_to_canvas` 解析（cfg['model']='local-lfm'）→ `get_profile('local-lfm')` 子进程 OK → LLMClient 构造 OK → 8081 服务在线（lfm2.5-2.6b 已加载）。

**真凶：`_get_llm` 静默 fallback**（src/workflow.py，LLM 节点取模型处）：

```python
except KeyError:
    return ctx.llm      # 悄悄回退，无任何日志——排障黑洞
```

**根因：`config.MODELS` 是启动时快照**——长寿进程启动于 local-lfm 条目加入 models.json **之前**，此后运行时对该键一无所知：`get_profile('local-lfm')` KeyError → 静默回退 ctx.llm（utility）→ utility 400/429 → 回退链（glm 429×3 → proxy 成功）。子进程测试全通（读的是新 models.json）——「子进程全通、运行时不通」的谜底。

**排查方法论（可复用口诀）**：llm_client 通用异常路径**有** llm_calls 记录——请求只要发出过（成功或失败）必有一条 model=X 的记录。**llm_calls 无 X 记录 = 请求从未发出 = 换模型发生在请求前（get_llm 层）**；有 X 记录（400/429）才是网络/provider 侧问题。

**修复（commit 0d852a0）**：回退处加 `_LOG.warning`——`LLM 节点模型 'X' 未找到（KeyError）——回退 ctx.llm=Y。检查 models.json 键名/是否需 /reload models`——LLM 节点选了 X 却悄悄用 ctx.llm 跑的情况一眼定位，不再有侦探剧。处置：`/reload models`（MODELS 热刷新）或 `/restart`，之后 LLM 节点才真正走所选模型（llm_calls 会出现 model=X 的记录）。

关联坑：[config 踩坑 · MODELS 启动快照](../guides/config-and-models.md#踩坑记录)（与窗口值副本坑同源不同症状）、[ops 常见错误对照](../guides/ops.md#常见错误对照)、续 [双格式与热加载](#双格式与热加载) 的 `<model>` 标签形态误诊。

## demo / 子工作流 hidden 归类（2026-08，commit d59dcbd）

**背景**：每轮扫描把 `.agent/workflows/` 下所有工作流注册为 `wf_*` 工具投影给主 Agent，但 demo 与引擎级子工作流**不是主 Agent 该直接调用的**——白占工具表位置与 schema 体积。

**改动**：
- 全部 demo 与引擎级子工作流标 `hidden="true"`：10 个 XML（含 `dir_snapshot.xml` / `diff_snapshots.xml` / `react_agent_demo.xml` / `custom_tool_demo.xml` / `stock_query.xml` 等）+ 7 个 JSON demo meta（`"hidden": true`，greet 原已有）
- **播种源同步**：`src/workflows/` 下 5 个 XML 副本一并写 hidden（播种/重建时保持一致）
- scan 验证 18/18 hidden=True

**关键保证——hidden 只挡主 Agent 视野**：
- 钩子触发**不看 hidden**（`get_hook_workflows` 按 hook 字段取，继续正常工作）
- 子工作流调用**不看 hidden**（`_find_local_workflow` 按名字取，subworkflow 节点的子工作流照常可调）
- 编辑器里仍可见可编辑，仅 LLM 工具投影移除

**收益**：`/restart` 后工具表少 17 个无用的 `wf_*` 工具——工具箱更干净、schema 更小、**折叠估算更准**（工具 schema 是折叠估算分子的最大头，见 [上下文引擎](context-engine.md)）。

**配套事实**：LIGHT_TOOLS 内部工具本就整箱 `hidden=True`，对主 LLM 不投影、仅工作流节点可用——无需再改（当时 13 个：cosine_sim / diff_lines / get_list_item / starts_with / ends_with / pass_through 等）。2026-08 纯函数批（commit 17312eb）后其中 8 个已外置，**LIGHT_TOOLS 只剩 5 件框架状态型**（ReAct 原语三件套 `_WF_CTX` 注入 + dir_outline/concat_files `_resolve` 沙箱），见 [工具外置](../features/tool-externalization.md)、[判别标准 · 纯函数批](tool-externalization-criteria.md)。

## before_turn 钩子并行执行（2026-08 新，v0.18.2 发布）

**设计**：同一 hook（如 before_turn）可挂多个工作流（如 `before_turn_retrieval` + `wiki_auto_query`），由 `ThreadPoolExecutor` 并发执行，**等待全部完成才返回**。

**实现**（`src/agent.py` `_run_hooks`）：

```python
with ThreadPoolExecutor() as pool:
    futures = {pool.submit(wf.run, **ctx): wf for wf in workhooks}
    for fut in as_completed(futures):
        result = fut.result()  # 异常抛到外层
        hooks.append((wf.name, result))
```

**关键保证**：
- **全部完成才返回**：`as_completed` 遍历完毕 → 所有 future 解析完毕 → 主循环才继续（不会出现「一个钩子未完成就开始第1步」的现象）
- **UI 并行跟踪**：前端 `runningAutoWf` 改为 `Map` 按 `hook::name` 索引，并行钩子各自独立显示「执行中」，结束独立移除（见 [user-interaction · UI 修复](../features/user-interaction.md#并行钩子执行中状态跟踪修复2026-08-19)）

**相关页**：[wiki_auto_query](../features/wiki-auto-query.md)（before_turn 典型实例）

## async 元信息字段（2026-08 新，v0.18.2 正式发布）

钩子工作流可标记 `async=true`，使其**异步执行不阻塞主循环**。全链路读写：

| 层 | 文件 | 职责 |
|----|------|------|
| 运行时 | `src/agent.py` `_run_hooks` | 读 meta.async → 若 true 则后台线程执行钩子，主循环不等返回、不注入 inject |
| 引擎 | `src/workflow.py` | WorkflowMeta dataclass 含 `async_` 字段；`run_workflow` 不感知 async（由调用方决定同步/异步） |
| XML 解析 | `src/workflow_xml.py` | XML 根属性 `async="true"` → WorkflowMeta.async_；序列化时写回根属性（往返幂等） |
| API | `src/server.py` | `api_wf_get` / `api_wf_save` 读写 async 字段（与 hidden 同级，根属性往返） |
| 编辑器 | `src/static/workflow_editor.html` | meta 面板 async 复选框；保存时随 meta 一起提交 |

**设计意图**：部分钩子（如通知、日志、后台索引）不需要将结果注入当前轮上下文，同步等待会拖慢主循环响应。async 钩子在后台线程跑完即丢弃返回值（或写日志/副作用文件），主循环零等待。

**注意事项**：
- async 钩子**不参与 inject 注入**——即使返回 `{inject: ...}` 也会被忽略（主循环已继续）
- async 钩子内 LLM 仍走 `utility_client`（scene=hook:xxx），可在 llm_calls.jsonl 观测
- 同一钩子工作流不要同时被 async 和非 async 调用——行为不确定
- 后台事件（含 async 钩子完成）的唤醒语义：v0.19.2 起通知**不独立触发轮**（见 [user-interaction · wake 语义](../features/user-interaction.md#后台通知-wake-语义service_exit-不再独立触发轮2026-08v0192)）

## 检索型钩子的输出纪律：选择+摘录，禁止生成（2026-08-20）

**通用原则**：凡以「检索知识 → inject 注入主上下文」为职责的钩子，其 LLM 节点只做「**选择 + 摘录**」——每条命中必须是源文档原文的逐字/近逐字摘录（可截断、不可改写语义）；**严禁**在原文之外生成分析、建议、「结合问题谈谈看法」等内容。宁可少摘也不要补全：源里没写的就让它不存在。

**为什么（机制级理由）**：
- **职责错位**：推测需要完整上下文（对话历史、任务状态、决策脉络）——主 Agent 有，检索钩子只见 system+query；钩子的推测必然是低配版的主 Agent 自己能做的分析
- **可信度污染**：注入格式上无区分——原文（可信事实）与生成（可信度未知）混排，主 Agent 分不清哪些能当依据
- **幻觉通道**：query 与源匹配度低时，模型倾向「补全」出源里不存在的行为描述
- **「歪打正着」不构成例外**：偶尔指对路与一本正经地错在格式上无法区分，而错误注入直接进入下一轮推理前提

一句话：**检索钩子的价值全部来自它读到的东西（原文），而非它对东西的想法。**

实例与修复（SYSTEM 硬约束原文）：[wiki_auto_query · LLM2 输出纪律](../features/wiki-auto-query.md#llm2-输出纪律只摘录不生成2026-08-20-修复)

### hook_ctx 上下文袋 + hook_write 工具：回写从引擎特判移到工作流（2026-08，commit 91b8437）

**背景（用户提案）**：recap 等钩子副作用的回写逻辑原来在引擎侧特判（`_async_hook` 的 recap 分支，meta.recap/name 兜底 17 行）——问题：**turn_end 挂着多个工作流时以谁为准**？引擎特判无法表达。用户方案：给钩子 start 输入加一个 `{}` 上下文袋参数，定义 `hook_write` 工具以袋为入参，工作流里组装 payload 后调用，由工具按字典键值对决定如何处理。

**四层实现**：

| 层 | 内容 |
|---|---|
| **① hook_ctx 上下文袋** | `_run_hooks` 入口统一注入：`turn_idx`（turn_end=len(turns)，此前「turn_idx 只有引擎知道」的障碍解除）+ `hook_ctx`（整袋快照）。start 声明 `hook_ctx(object)` 输入即可整袋取 |
| **② hook_write 工具**（`multiagent.make_hook_side_effects`） | `payload={"action":"set_recap","value":"≤60字","turn":N}` → 三落点（`_recap` / registry / Turn.recap+recaps.jsonl）；错误特征过滤（`_RECAP_ERR_MARKS`：APIStatusError 等不污染 recap，保持旧值）+ JSON 字符串/turn 容错。`hidden=True` 仅钩子工作流 plugin 节点调用，不投影主 LLM |
| **③ recap_gen.xml 改造** | `start(+hook_ctx) → llm → code 组装 payload → plugin hook_write → end`——回写链路在工作流里**显式可见**，编辑器可改可观测 |
| **④ 引擎去特化** | `_async_hook` 的 recap 回写分支删除——工作流接管 |

**多钩子共存语义**：谁调 `hook_write` 谁负责，后写覆盖先写（recap 语义本来就是最新的）；条件判断/只在特定轮写/各写各的字段——全在工作流里连线表达，引擎零特判。

**主/子 Agent 双注册**：`make_hook_side_effects` 闭包绑定 agent——子 Agent（multiagent.py 的 `_setup_subagent`）重绑自身版本，防继承主 Agent 闭包错写。

**验证全过**：hook_write 单元（三落点/错误过滤/JSON 串容错/turn 容错）+ recap_gen e2e（mock llm，turn=9 落点正确）+ 播种一致 + 编译 ×3。生效方式：工作流每轮重扫，不用重启。

### 钩子声明面三层：编辑器协议下拉 + 磁盘 meta 保底 + yml 挂载（2026-08，commit 9f8f085 + 628f5b1）

**背景**：钩子挂载曾从编辑器迁到 /agents 管理页（批次七删了顶栏钩子下拉），但发现两件事：① 钩子工作流需要**协议 schema 规范化**（start 注入什么、end 返回什么）的编辑器特性；② 编辑器 UI 不再管理 hook/async/recap 字段后，**保存请求缺这些字段 → 每次编辑器保存逐步丢光**（实测 22 个 XML 里只剩 2 个还带 hook 标志，播种面新装机钩子全死）。

**三层分工（当前定稿）**：

| 层 | 职责 | 入口 |
|---|---|---|
| **编辑器** | 声明「本工作流实现什么钩子协议」+ schema 规范化 | 顶栏「钩子」下拉（写 meta.hook 根属性；start 自动补协议输入 / end 自动补 inject+result 输出；已有自定义输入 confirm 保护；toast 明示「挂载由 agent 的 .yml 声明」） |
| **/agents 管理页** | 声明「哪些 Agent 挂哪些钩子」 | hooks 编辑（yml 优先，运行时权威） |
| **磁盘 meta** | 播种面兜底（无 yml 声明场景的 `get_hook_workflows` 扫描） | **server PUT 保底**：保存请求缺 hook/async/recap/enabled 时从磁盘现有 XML 根属性合并——UI 不管理的字段不被 UI 保存抹掉 |

**schema 规范化（HOOK_INPUTS，workflow_editor.html）**：各钩子位置 start 输入约定——`before_turn`：[user_message, hook_ctx]、`before_tool`：[user_message, tool_name, tool_args, hook_ctx]、`after_tool`：[…, tool_result, changed_files, hook_ctx]、`before_answer`：[…, draft_answer, changed_calls, hook_ctx]、`turn_end`：[user_message, draft_answer, turn_context, hook_ctx]；end 统一返回 `{inject:boolean, result:string}` 协议对。

**修复记录**：5 个工作流补根属性（before_turn_retrieval/wiki_auto_query 补 `hook="before_turn"`、py_auto_diag/cs_auto_diag 补 `hook="after_tool"`、recap_gen 补 `hook="turn_end" async="true" recap="true"`）+ server PUT 保底合并 + `get_hook_workflows` 修复后识别全部 6 个钩子。

## 运行观测（run registry，2026-08-20 新，commit 8aeb21a；全文查看 commit bb56a82）

工作流执行的节点级实时观测，消除「钩子在跑但不知道跑到哪」的盲盒感。**接入点**：

| 调用路径 | 注册方式 |
|----------|----------|
| `execute(canvas, inputs, ..., run_id=...)` | 节点 start/end/error 事件写入 `_WF_RUNS`（标题/类型/耗时/预览 200 字 + 全文） |
| `run_hook(..., run_id=...)` | 透传给 execute（钩子工作流） |
| `make_workflow_tool._run` | wf_* 工具调用注册 `new_wf_run(name, "tool")`；agent=None（测试）不注册 |
| `src/agent.py` `_run_hooks` | 同步（线程池）+ async（后台线程）钩子全覆盖，`auto_wf_start`/`auto_wf`/`auto_wf_error` 事件带 run_id |

注册表 `_WF_RUNS`（`threading.Lock` 线程安全，最近 50 次内存上限）+ `new_wf_run / list_wf_runs / get_wf_run / get_wf_node_full` API。节点 end/error 事件并行存 `preview`（200 字）与 `full`（`_full_str`：保留换行/JSON 结构，单节点上限 `_FULL_CAP=200K` 超限截断标注，总预算 `_FULL_BUDGET=20M` 字符防爆内存、evict 时扣减 `_full_total`，预算耗尽只存预览）；`get_wf_run` 轮询视图**剥离 full、补 has_full**（2s 轮询不传大 payload），全文走 `GET /api/wf/runs/<id>/node/<nid>` → PlainTextResponse 纯文本页。观测入口：对话中「⏳ 执行中…」行可点击 → `/wf/monitor?run=<id>` 节点时间线甘特图 2s 轮询，has_full 预览可点击开全文。详见 **[工作流运行观测](../features/wf-monitor.md)**（主页面：实现/路由/前端/内存防线/与其他可观测能力对比）。

### 嵌套子画布轨迹：复合节点 / 子工作流的子节点事件（2026-08，commit 31d5ef3）

**背景（用户请求 2026-08）**：观测页只能看到顶层节点（loop/batch/subworkflow 是一个黑盒节点），子画布内部跑到哪看不到——调试 wait_extract 的等待循环 / 子工作流时缺关键视野。

**引擎侧（src/workflow.py）**：

- **`track_stack` 嵌套观测容器栈**：`execute(canvas, ..., track_stack=[])` 新增参数——子工作流执行时 `_handle_subworkflow` 把子节点事件写**栈顶容器**而非顶层 run；栈非空时本 execute **不发 run_done**（整体结束态归最外层）
- **复合节点（loop/batch）轮容器**：`_run_composite_body` 每轮迭代收集体内节点事件 → 每轮尾部实时更新 `node_meta`（`children`=最后一轮轨迹 + `rounds` + `childmeta` 子节点标题映射）——运行中展开观测页即可看到最后一轮逐轮刷新
- **子工作流**：push 容器 → 子 execute 继承 → pop 后 `node_meta` 挂 `children` + `wf_name` + `childmeta`；嵌套复合（子画布里还有 loop）经栈自然支持任意深度
- 子节点事件也走 `_track_apply`（`store_full=False`：嵌套子节点只存 preview，**全文与预算仍归顶层节点**——防 20M 预算被嵌套爆掉）

**前端（wf_monitor.html）**：`▸ 循环 200001 ♻ 12 轮 · 5 子节点` 可点击展开子轨迹表（子节点/类型/状态/耗时/输出预览）；子工作流行显示目标名（`🔗 sub_test · 9 节点`）。展开状态跨 2s 轮询保持。

**顺手修的真 bug**：execute 初始 ready **无条件排除 type 2**——`start→end` 直连的子工作流 exit 永远不进 ready 队列 → 隐式结束返回 `{}`（输出丢失）；`execute_debug` 没有这个排除所以调试页一直正常，掩盖了问题。修复：只排除「非 entry 后继的孤立 end」。

**e2e 验证**：loop rounds=3 + 最后一轮 children + childmeta ✓；subworkflow wf_name + 完整子轨迹 ✓；run_done 只发一次（嵌套不发）✓；输出正确透传 ✓。需 `/restart` 生效。

## 13 类节点速查

start(1)/end(2)/llm(3)/plugin(4)/code(5)/selector(8)/subworkflow(9)/text(15)/loop(21)/intent(22)/batch(28)/aggregator(32)/assigner(40) + tojson/fromjson/http/break/continue/setvar/output + AND/OR/timestamp(N1)——**节点目录共 24 种**（插件节点目录条目 2026-08 起动态聚合，见 [node-plugins · catalog_entries](node-plugins.md)）。

新能力（2026-08）：
- **AND / OR 逻辑节点（2026-08，v0.19.2 新节点）**：条件组节点，与 selector(8) 的条件结构**同构**（条件组 × operator）——AND 全组真才真、OR 一真即真；输出**聚合 bool + 每组各自结果**（总开关与逐组定位一次拿到）；求值走 `eval_condition_lenient`，**未设置的条件恒真**（未设置 = 不参与否决，不报错不拦截）。以节点插件实现（py+js 配对，见 [node-plugins](node-plugins.md)），v0.19.2 wheel 共 12 组 24 文件
- **批处理 nth_output 对象组装模式（2026-08，v0.19.2 性能）**：批处理 nth_output 输出改为对象组装模式——按对象字段直接组装产出，省去整列表级的序列化/扫描开销
- **筛选条件未设置恒真（2026-08，v0.19.2 性能）**：批处理 filtered_outputs 的筛选条件**未设置时直接恒真放行**（全部命中），不再走逐条条件求值路径——与 `eval_condition_lenient` 同一「未设置恒真」语义，AND/OR 与批处理筛选共用
- **selector 左值**：`NODE.field.length`（string 也有）；条件值支持 `changed_files` 数组直传（零序列化）
- **pass_through 工具**（LIGHT_TOOLS，hidden）：input=Any（schema 空）→ 编辑器 any 类型不锁，可改 object 逐字段连线组装结构透传
- **starts_with/ends_with**：字符串前后缀判断（扩展名分流；隐藏工具，仅工作流可用）
- **diff_lines 工具**（2026-08 纯函数批起外置 `tools/builtin/diff_tools.py`）：两个文本块按行 Myers diff（无需落盘），算法与 diff_files 同源副本（详见 [diff_lines 页](../features/diff-lines.md)）
- **kv_cache_read/write 工具**（2026-08 新，外置 `tools/builtin/kv_tools.py`）：应用级 KV 结果缓存——同输入结果确定的 LLM 调用（如关键词提取）做 memoization，同轮多 before_turn 工作流共用一次提取；namespace 兼作版本号，重启清空（结果缓存语义，丢失=重算）
- **get_list_item 工具**（LIGHT_TOOLS，hidden，outputs=any）：从列表取单个元素，支持正/负索引、越界安全返回错误提示（详见 [get_list_item 页](../features/get-list-item.md)）
- **cosine_sim 工具**（本体 `src/rag.py`、注册外置 `rag_tools.py`，hidden）：语义余弦相似度，供工作流批处理节点做重排打分（详见 [cosine_sim 页](../features/cosine-sim.md)）
- **run_python 工具新增 args 参数**：`run_python(code="...", file="...", args="...")`，经环境变量 `PY_ARGS` 传递（code 和 file 两模式都生效），脚本内 `import os; a = os.environ.get("PY_ARGS", "")` 读取（详见 [run_python 页](../features/run-python.md)）
- **XML schema 往返**：list\<object> 的 field 子元素 / list 基础类型 itemType / 坐标幂等（编辑器保存不再丢结构）
- **git_commit 节点**：git 专用提交节点，内部以 **subprocess 列表参数**传参（不经 shell 字符串拼接），多行/特殊字符 commit message 安全；配合快照/diff 子工作流按变更清单提交。实例见 [wiki_auto_maintenance 的 commit_wiki](../features/wiki-auto-maintenance.md#commit_wiki-核心逻辑git_commit-节点)
- **dir_snapshot / diff_snapshots 子工作流**：引擎级快照能力（**hidden=true**，主 Agent 不直接调用，仅子工作流/钩子复用）——`dir_snapshot(path)` 对目录取文件快照（mtime 映射 JSON，排除 .git/__pycache__，path 留空=整个 workspace）；`diff_snapshots(before, after)` 对比两份快照输出变更清单（`files` 逗号分隔 + `count` + `changed` 结构化对象），供 git_commit 或选择器/聚合节点消费。**通用复用**：详见 [dir_snapshot / diff_snapshots 通用子工作流](snapshot-diff.md)，首个消费方为 [wiki_auto_maintenance 的快照重构](../features/wiki-auto-maintenance.md#snap_before--diff_wiki快照与变更清单重构为子工作流2026-08)
- **subworkflow 节点 literal 属性约定（2026-08）**：subworkflow(9) 节点调用子工作流传字面量参数时，**literal 必须用属性形式 `literal="值"`**，不能用于子元素形式（`<literal>值</literal>`）。子元素形式会导致参数传递失败（子工作流收不到字面量）→ path 空 → 快照全盘 → WinError 206。实例：wiki_auto_maintenance 的 snap_before 传 `path=".agent/wiki/"`（见 [快照重构中的 literal 坑](../features/wiki-auto-maintenance.md#subworkflow-节点-literal-属性坑2026-08-修复)）
- **工具节点输出是 dict（2026-08 修复，commit edd9851）**：`wf_diff_snapshots` 等**工具节点** raw 返回 **dict**（Tool.run() 把 end 的 dict json.dumps 成字符串 → `_handle_plugin._try_parse` 又解析回 Python dict），消费端必须引用**具体字段**（`files` / `count` / `changed`，`_dotted_get` 直接取）并**补 `<out>` 声明**；把整个 dict 当字符串 `.split(",")` 会报 `'dict' object has no attribute 'split'`。实例见 [wiki_auto_maintenance 的 dict split 修复](../features/wiki-auto-maintenance.md#dict-split-报错修复2026-08commit-edd9851)
- **subworkflow 节点输入框字面量保存后重开变空（2026-08 修复，commit 910fc1b）**：type 9 节点的输入框（画布节点内，非右侧属性面板）手动输入字面量保存后，重新打开工作流输入框变空——**根因在前端 `syncSubworkflowNode`**（openWf 时对 type 9 节点重跑 schema 同步：只保留连线、丢字面量，默认值造出 `blockID=''` 的空 ref → 输入框空白）。修复：连线 ref 完整保留 + 非空字面量保留，默认值改空 literal。生效需 `/restart` + Ctrl+F5。详见 [wiki-auto-maintenance · path 字面量坑](../features/wiki-auto-maintenance.md#path-字面量保存后重开变空2026-08-修复commit-910fc1b)
- **aggregator(32) 选值语义修复：第一个「执行过且值非空」（2026-08，commit 5117f41）**：`_handle_aggregator` 的 block-output 分支旧版**只判断「blockID 执行过」不判断值有没有**——var1 引用的上游节点执行了（在 `node_outputs` 里）、但它引用的**字段**解析为 None（分支没走到该字段 / 输出为空）→ `chosen=None` + `break`，整组直接 null，后续 var2 有值也被跳过（用户报告「var1=null 直接分组返回 null」）。修复后的选值语义：

  | 变量情形 | 行为 |
  |---|---|
  | block-output 执行过 + 值非 None/非空串 | 选定 ✓ |
  | 执行过但值为 None/空串 | 记为兜底，**继续找后面的变量** |
  | 未执行 | 跳过，继续找后面的变量 |
  | 全部执行过但都无值 | 兜底第一个执行过的值（执行过优先） |
  | 字面量 / 全局变量 | 非 None 即选（原行为不变） |

  **关键取舍**：空列表/空 dict 是合法值**不**触发跳过——`filtered_outputs=[]` 是批处理节点的合法产出（0 条命中也是有意义的信息）；只有 None 和空串（解析不到 / 渲染为空）才继续往后找。九场景验证通过（含 var1 未执行取 var2、空列表不误判、多分组互不影响）；引擎层修复需 `/restart` 生效。

### 2026-08 引擎语义补全：setvar XML 简写 / 循环变量终值 / break 携带值 / yield（commits ace13b2 + 8bc6c66）

**背景（extract_keywords wait_extract 调试链，用户实测驱动）**：等待循环里 recheck 读到数据、但输出的 keywords/nth_output 恒 null——排查出三个叠加的引擎缺口：

**① setvar(20) 的 left 支持 XML 简写（commit ace13b2）**：手写/编辑器保存的 `<in left="__entry__.keywords" right="ref:926184.raw"/>` 转成 canvas 后 left 是**字符串**，而旧引擎只认编辑器结构化形态（`{value:{content:{name}}}`）→ `var_name=None` → **setvar 执行了但一个字都没写**（观测显示 done 却是空转——最难的静默失败）。修复：`_setvar_left_name` 多形态解析（`__entry__.keywords` 点号路径取尾段 / 裸名 / 结构化 dict / round-trip 变体）+ `_setvar_right_value` 支持 `ref:节点.字段` 字符串简写。解析失败返回 None（调用方静默跳过——不炸循环体）。

**② 循环变量终值无条件并入 outputs（同 commit）**：`loop_vars` 只在声明了原生输出时才 merge——wait_extract 只声明 all/filtered/nth 三个约定输出 → `1275951.keywords` 恒 None → 聚合器四变量全空 → fallback 到 var3 的 `"pending"`（index=3 正是 var3 的序号——观测与代码互证）。修复：`loop_vars` **setdefault 无条件并入**——「复合节点.变量名」是一等输出引用面（编辑器 `__entry__` 的变量端口一直暴露着它），约定输出（all/filtered/nth）与显式原生输出优先不覆盖。

**③ Break(19) 携带值（commit 8bc6c66，await 语义）**：旧实现恒 `return "break", None` 且 break 轮 round_out 直接丢弃 → 就绪轮 set_keywords 拿到值后 `__break__` 退出，值死在退出瞬间。修复：与 Continue 同款解析唯一 result 字段——break 携带值退出（就绪轮终值进 all_outputs 末位；未连 result → None 不占位，保持纯退出语义）。批处理体内 break 同款修复（此前 break 信号被静默忽略——继续跑完剩余元素）。

**④ yield 节点（用户提案，同 commit）**：`ntype in ("29","2","yield")`——yield ≡ continue(result)，「本轮产出该值」的语义化一等节点；编辑器 TYPE_LABEL/TYPE_CATEGORY/流程出口排除同步注册。

**e2e 验证（复刻 wait_extract 结构）**：count 循环 + selector 分支（未就绪轮 continue(无 result) / 就绪轮 setvar→break(带 result)）→ `all_outputs=[null×4, kw]`、`filtered_outputs=[kw]`、`nth_output=kw`、`keywords 变量=kw`——用户期望三元组精确达成。extract_keywords.xml 配套：`__break__` 连上 `result ← 926184.raw`、`__continue__` 移除坏引用 `926184.raw.list`（引擎无 `.list` 派生属性）。

