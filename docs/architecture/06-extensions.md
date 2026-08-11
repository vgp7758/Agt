# 06 · 扩展模块

核心引擎（Agent / Session / Tools / RealTools）之外，agt 还有一组扩展模块，为智能体提供多 Agent 协作、后台服务、计划管理、施工方案、RAG 检索、知识库、反馈、资产下载、日志、工具调用详情、LLM 可观测性、自动更新等能力。本文逐一梳理其设计要点与协作关系。

---

## 目录

| 模块 | 文件 | 职责一句话 |
|------|------|-----------|
| 多 Agent 协作 | `src/multiagent.py` + `src/registry.py` | 全异步子 Agent 派活 + 团队注册表 + Agent 间通信 |
| 后台服务 + 调度器 | `src/background.py` | 长进程管理与定时/到点消息推送 |
| 计划工具 | `src/plan_tools.py` | 跨 session 持久化的 TodoList + SYSTEM 注入 |
| 施工方案 | `src/spec_tools.py` | 结构化 spec 生命周期（草稿→批阅→施工） |
| 本地 RAG | `src/rag.py` | faiss 向量索引 + 语义检索 |
| 知识库 | `src/wiki.py` | repo-wiki CRUD + 自动维护 |
| 用户反馈 | `src/feedback.py` | 本地落盘 + 飞书 webhook 推送 |
| 资产下载 | `src/download.py` | manifest 驱动的随包资产获取 |
| 会话日志 | `src/log.py` | 跟 session 走的 logging |
| 工具调用详情 | `src/toollog.py` | append-only JSONL 工具调用流水 |
| LLM 调用流水 | `src/llm_call_log.py` | per-model 可靠性可观测性 |
| 自动更新 | `src/updater.py` | 启动时检查 PyPI 并后台升级 |

---

## 1. 多 Agent 协作 — `multiagent.py` + `registry.py`

### 设计理念：全异步 + 团队感知 + Agent 间通信

子 Agent 采用 **声明式 + 按需实例化 + 全异步** 模式：

- **声明式**：子 Agent 声明存为 `.agent/agents/<name>.md`（frontmatter: `name`/`description`/`tools`/`model` + body: systemPrompt）。harness 每轮把可用子 Agent 清单投影进主 Agent 的 SYSTEM。
- **全异步**：`agent_prompt` 永远在后台 daemon 线程跑，立即返回"已启动"。调用者不被阻塞，可继续派更多子 Agent 或做其他事。
- **自动 caller 绑定**：派活时自动捕获 `caller_id = agent.agent_id`，子 Agent 完成后按 caller_id 路由 answer——如果 caller 是 agent，answer 入 caller 的 inbox（caller 下一步边界自动看到）；如果 caller 是 user（用户直接交互），answer 不入队。

### AgentRegistry — 团队注册表（`registry.py`）

所有活跃 Agent（主+子）注册在全局共享的 `AgentRegistry` 中，通过 `agent_id` 寻址。线程安全（RLock）。

```python
@dataclass
class AgentEntry:
    agent_id: str            # "_main_" / "coder_3" / 自定义
    name: str                # 声明名
    role: str                # "main" | "subagent"
    model: str               # 模型名
    task: str = ""           # 初始任务摘要
    agent: object = None     # Agent 实例引用
    status: str = "running"  # running | idle | done | failed
    caller_id: str = ""      # 谁派的任务 → 完成后按此路由 answer
    recap: str = ""          # 最近一轮的一句话总结（队友可见，不进入自己上下文）
```

**主 Agent 注册**：`agent_id = "_main_"`，在 `Agent.__init__` 时自动注册。

**子 Agent 注册**：`agent_prompt` 创建 SubAgent 时注册，完成/失败时更新 status（不 unregister，保留可查询）。

**历史恢复**：`format_team(session_dir=...)` 扫描 `session_dir/agents/` 目录，从各子 Agent session 的 `meta.json` 里的 `extra_state._agent_meta` 恢复已完成子 Agent 的信息——即使 registry 是内存的（读档后重建），历史子 Agent 仍能在团队列表中显示。

### 工具集

**派活与管理（5 个）：**

| 工具 | 作用 |
|------|------|
| `create_agent(name, description, system, tools, model)` | 写声明 md，不建实例 |
| `agent_prompt(name, prompt, tools, agent_id)` | 全异步派活，自动绑定 caller_id |
| `kill_agent(name)` | 删声明 md |
| `list_agents()` | 扫 `.agent/agents/` 列出 |
| `wait_subagents(agent_ids, timeout)` | 阻塞等子 Agent 完成，取结果 |

**Agent 间通信（5 个）：**

| 工具 | 作用 | 是否落盘 |
|------|------|----------|
| `list_team()` | 列出所有活跃 Agent（id/名称/模型/recap/状态） | — |
| `agent_ask(target_id, question)` | 无状态询问：用对方上下文+你的问题调对方 LLM，返回回答 | ❌ |
| `agent_notify(target_id, message)` | 有状态提示：等效用户插话，入对方 inbox | ✅ |
| `agent_query_events(target_id, count)` | 查对方最近 N 轮对话摘要（含 call_id） | — |
| `agent_query_tool_detail(target_id, call_id)` | 查对方某次工具调用完整详情 | — |

### 三种通信方式详解

```
方式1：agent_ask（无状态询问）
  A → registry.lookup(target_id) → 取 target.session.messages_for_llm() 快照(copy)
    → target.llm.chat(snapshot + question) → 返回 answer
    → target 的 session 不变（不记录这次询问）

方式2：agent_notify（有状态提示）
  A → target.queue_user_message(msg) → 复用现有插话队列
    → target 下一步边界自动看到（和用户插话效果一样）
    → 会锚定到某个 step → 滚入历史 → 落盘

方式3：agent_query（只读索引）
  A → 读 target.session.turns[-N:] → 格式化每轮 user + 工具调用(c7: read_file) + answer
  A → 读 target.session.toollog.view("c7") → 完整 (name, args, result)
```

### 团队感知注入（`_teammates_provider`）

每个 Agent 的 session 挂一个 `_teammates_provider` 回调（和 plan/spec/ltm provider 同模式），每轮注入 SYSTEM：

```
【当前 Agent 团队】
- coder [coder_1] (glm-5.2) 🏃 — 刚完成了 server.py 的重构 → _main_
- explorer [explorer] (glm-5.2) ✅ — 搜索认证模块完成
  ↳ 可用 agent_ask / agent_notify / agent_query_events / agent_query_tool_detail 与队友通信
```

每个 Agent 看到的都是**除自己外的所有队友**（`format_team(exclude_id=self.agent_id)`）。主 Agent 和子 Agent 的投影格式完全对称——主 Agent 没有特权。

### recap：每轮一句话总结

每个 Agent 每轮结束后，用 daemon 线程异步调 LLM 生成一句话 recap（"最近在干嘛"）：

```python
def _generate_recap(self, user_msg, answer, tool_names):
    # daemon 线程：用 user_msg + answer + tool_names 生成 ≤30 字总结
    # 成功 → self._recap = recap + registry.update_recap(agent_id, recap)
    # 失败 → 静默，recap 保持上一轮的值
```

recap **不进入自己的上下文**（省 token），但队友在 `teammates_block` 中能看到。子 Agent 完成后 recap 写入 `meta.json` 的 `extra_state._agent_meta.recap`，读档后可恢复。

### /agent 命令：用户直接与子 Agent 交互

```
/agent            → 列出所有可交互的团队成员
/agent coder_1    → 切换到与 coder_1 直接交互（仅允许 done/idle 状态）
/agent _main_     → 切回主 Agent
```

切换后用户输入直接路由到目标 Agent 的 `run()`。WebUI 通过下拉框实现（点击时自动刷新最新团队列表）。

**线程安全保证**：用户的直接交互和主 Agent 的任务走同一个 `work_q`、同一个 worker 线程→ 天然串行。子 Agent 的后台 `_bg` 线程只操作自己的 session → 不与其他 Agent 冲突。

### 关键设计

**工具继承与防递归**

```python
_AGENT_TOOL_NAMES = {"create_agent", "agent_prompt", "kill_agent", "list_agents",
                     "create_plan", "update_plan", "update_wiki"}
```

子 Agent 绝不能继承这组管理工具（防递归生子 Agent、互相操控）。通信工具（agent_ask/notify/query/list_team）不在黑名单里——它们不是"创建子 Agent"，无递归风险。

**SubAgent 类**

```python
class SubAgent:
    def __init__(self, name, model_name, system, tools, on_event=None,
                 max_steps=50, token_budget=0, session_dir=None,
                 registry=None, agent_id=None)
    def prompt(self, text: str) -> str  # 派任务，自主用工具完成，返回最终回复
```

max_steps 和 token_budget 与主 Agent 对齐（50 步 / 无限制），不再人为限制子 Agent 的能力。

## 2. 后台服务 + 调度器 — `background.py`

两类 producer 都把消息推进 `agent.inbox`（带锁 deque），由 chat/web 的串行消费者 + `Agent.run()` 内部循环消费触发推理。**任何时候只有一个 run 在跑**（`agent.run` 非线程安全，多 run 并发会踩 `session._current` 等共享状态）。

### ServiceManager — 长进程管理

| 方法 | 作用 |
|------|------|
| `start(name, command, cwd)` | Popen 不等待，后台线程收日志 |
| `stop(name)` | 杀整棵进程树（跨平台） |
| `list()` / `logs(name, lines)` | 查看服务列表 / 日志尾部 |
| `status_lines()` | 供 SYSTEM 注入：每个服务一行状态 |
| `stop_all()` | 退出时清理，防孤儿进程 |

**关键设计：**

- **独立进程组/会话**：启动时绑 `CREATE_NEW_PROCESS_GROUP`（Win）或 `start_new_session`（Unix），stop 时能整树杀（`taskkill /T` / `killpg`），避免 shell=True 下 terminate 只杀 shell、漏掉孙进程变孤儿。
- **退出通知**：进程自行退出时（stdout 关闭），读线程抓 rc，经 `on_exit` 回调把退出事件推 inbox 唤醒 Agent。手动 `stop_service` 时先置 `manual_stop=True`，读线程跳过被动通知，避免双重处理。
- **滚动日志**：每个服务一个 `deque(maxlen=1000)`，循环缓冲，不无限增长。

### Scheduler — 定时/到点调度

```python
@dataclass
class Schedule:
    id: str
    name: str
    kind: str          # "interval" | "at"
    spec: float        # interval=秒；at=触发时间戳
    message: str = ""  # 静态推送文本（与 action 二选一）
    action: Optional[dict] = None  # {"tool":..., "args":...} 到点执行拿结果
    repeat: bool = True  # interval 是否循环；at 恒为单次
    next_fire: float = 0.0
```

| 方法 | 作用 |
|------|------|
| `add_interval(name, seconds, message, action, repeat)` | 每 N 秒触发 |
| `add_at(name, dt_iso, message, action)` | 到某时刻触发一次 |
| `cancel(name_or_id)` | 取消 |
| `list()` | 查看所有定时任务 |

**调度循环**：后台 daemon 线程每 0.5s 轮询，到点 `produce` → `agent.push_message`。interval 循环重算 `next_fire`；at 单次触发后删除。

**动态消息**：`action` 字段可指定到点执行某工具拿结果（动态消息），而非静态文本。`_produce` 调 `agent.tools.call(tool, args)` 拿结果后推送。

---

## 3. 计划工具 — `plan_tools.py`

跨 session 的 repo 级计划/清单工具（类 Claude Code 的 TodoWrite，但持久化为文件）。

### 存储模型

计划存到 `~/.agt/repos/<hash>/plans/<plan_id>.json`，每个计划一个文件、带稳定 id。session 的 `extra_state` 只记一个 `plan_id`（当前活动计划引用），另一个 session 用 `join_plan(id)` 即可加入同一个计划继续推进。

**内存模型**：`agent.active_plan`（完整 dict，单一事实源）+ `agent.plan`（steps 的镜像，兼容旧读者）+ `agent.active_plan_id`。所有变更工具改 `active_plan` → `_flush`（同步镜像 + 原子落盘）→ `_emit`。

### 工具集（7 个）

| 工具 | 作用 |
|------|------|
| `create_plan(title, steps, design)` | 新建计划，设为活动 |
| `join_plan(plan_id)` | 加入已存在计划 |
| `exit_plan()` | 退出活动计划（文件保留） |
| `list_plans()` | 列出本仓库全部计划 |
| `update_plan(step, status, description)` | 更新某步状态/描述 |
| `add_step(description)` | 追加一步 |
| `edit_plan(title, design)` | 改标题/设计 |

### SYSTEM 注入

加入计划后，计划的完整信息（标题 / 设计 / TodoList 及各步状态）每轮被动注入 SYSTEM（`_format_plan_block`），让 Agent 始终清楚在干哪一步。`exit_plan()` 后停止注入（文件仍保留）。

```
【当前计划】p_abc12345 · 重构认证模块
设计：将 session 管理拆分为独立模块…
进度（共 5 步，已完成 2）：
  ☐ 1. 梳理现有 session 结构 (待办)
  ▶ 2. 提取 SessionContext (进行中)
  ✅ 3. 编写单元测试 (已完成)
  …
```

### 原子落盘

`_save_plan` 写 `.tmp` 再 `os.replace`，避免写一半崩溃导致文件损坏。`updated_at` 每次刷新。

### 兼容迁移

`restore_active_plan` 从 session 存档恢复活动计划：优先按 `plan_id` 从文件读回；旧存档只有内联 `plan` 列表（无 `plan_id`）则迁移成计划文件。

---

## 4. 施工方案 — `spec_tools.py`

在 plan_tools 之上叠加一套 spec 流程，解决「多处修改 / 跨文件 / 需要先探索再施工」的复杂任务场景。

### Spec 生命周期

```
draft → committed → approved → (自动生成 plan，开始施工)
                     ↘ rejected → regenerate_spec(新 id) → draft → …
```

| 状态 | 图标 | 含义 |
|------|------|------|
| draft | 📝 | 草稿，尚未提交批阅 |
| committed | 🔍 | 已提交，等待用户裁定 |
| approved | ✅ | 已通过，自动生成 plan |
| rejected | ❌ | 已返工，记录反馈 |

### 结构化 step

每个 step 是机器可读、可自动落地的结构：

```python
{
    "file": "src/auth/login.py",      # 要改的文件
    "action": "edit",                 # create/insert/edit/replace/delete/move/review
    "anchor": "after _run_hooks(约605行)",  # 定位
    "content": "...",                 # 要插入/替换的内容
    "rationale": "解耦 hook 执行逻辑"   # 为什么这么改
}
```

`_normalize_step` 把模型传入的各种格式（纯字符串、dict 含结构化字段、旧式 plan step）统一规整成上述结构。

### 工具集（8 个）

| 工具 | 作用 |
|------|------|
| `create_spec(title, steps, design)` | 新建 spec（draft 态） |
| `commit_spec(spec_id)` | 提交批阅（draft→committed） |
| `approve_spec(spec_id)` | 用户通过 → 自动生成 plan 开始施工 |
| `reject_spec(spec_id, feedback)` | 用户返工 → 记录反馈 |
| `regenerate_spec(spec_id, feedback, ...)` | 据反馈重新生成新 spec |
| `list_specs()` | 列出全部 spec |
| `recall_spec(spec_id)` | 查看完整内容 |
| `explore_subagent(name, goal, model)` | 派一次性探索子 Agent 并行读不同模块 |

### 探索 → 制定 → 批阅 → 施工 流程

1. **探索**（可选）：`explore_subagent` 派若干一次性子 Agent 并行去读不同模块/文件，各自返回发现报告，汇总后喂给主 Agent 生成更准的施工方案。
2. **制定**：`create_spec` 把结构化施工步骤落盘成 spec 文件（draft 态）。
3. **批阅**：`commit_spec` 触发 UI 事件「请批阅施工方案」，等用户裁定。
   - 用户「通过」→ `approve_spec` 自动生成对应 plan、设为活动计划、开始施工。
   - 用户「返工」+ 反馈 → `regenerate_spec` 标记旧 spec rejected，Agent 据反馈重新生成新 spec（新 id），再次提交批阅。

### spec → plan 转换

`_build_plan_from_spec` 把 approved spec 的结构化 step 压成 plan step 的 description 字符串（给 `create_plan` 用），复用 plan_tools 的数据结构。plan 的 design 字段追加 `[由 spec xxx 生成]` 标注来源。

### SYSTEM 注入

approved 态 spec 不再注入 SYSTEM（由生成的 plan 接管注入，避免双重注入）；draft/committed/rejected 态每轮注入，让 Agent 一直清楚在等批阅 / 需据反馈重新生成。

---

## 5. 本地 RAG — `rag.py`

faiss(HNSW 向量检索) + sqlite(片段元数据) + 本地 embedding 模型。

### 架构

```
用户查询 → embedder.encode → faiss HNSW 搜索 top-K → sqlite 取片段元数据 → (可选) reranker 精排 → 返回
```

- **HNSW 图索引**：O(logN) 检索，毫秒出 top-K，不是串行遍历。
- **切片保留行号**：`(file_path, start_line, end_line)`，查询返回带行号的 top-K 片段。
- **可选 reranker**：CrossEncoder 精排（先召回 `rerank_pool` 条，再 rerank 取 `top_k`）。

### Embedder 两种模式

| 模式 | 说明 |
|------|------|
| `local` | SentenceTransformer 本地模型（如 bge-small-zh-v1.5），对齐 `encode()` 接口 |
| `api` | APIEmbedder：OpenAI 兼容的 `/v1/embeddings` API（硅基流动/智谱/OpenAI 等），零重依赖 |

**重依赖懒加载**：faiss / numpy / sentence_transformers（→torch）不在顶层 import——没配 embedding 的用户根本用不到，却会被 torch 在 Windows 上的页面文件问题 (WinError 1455) 拖垮整个 agt 启动。改为用到时局部 import。

### 配置驱动

`<workspace>/.agent/rag.json`（见 `config.load_rag_config` / `DEFAULT_RAG_CONFIG`）。`enabled` 关或 embedder 配置不全 → `from_config` 返回 None（不抛）。

### 全局单例

`web.py` 启动时 `LocalRAG.from_config(ws)` 建全局单例，`set_rag()` 注入；`rag_query` 工具供智能体调用，`/rag` 页面供用户管理（配置/建库/查询）。

### 建库

`index_dir` 扫描 root 下指定扩展名文件，排除 `exclude_globs`（fnmatch），按行滑窗切片（默认 60 行/15 行 overlap），批量向量化入库。每次清空重建。`on_progress(done, total, last_file)` 回调供 UI 进度展示。

---

## 6. 知识库 — `wiki.py`

repo-wiki 知识库工具（`.agent/wiki/`，按业务/技术逻辑自由组织）。

### 设计理念

让 Agent 给仓库积累"项目记忆"：开始不熟悉的任务前先查 wiki；完成重要功能/修改后调用 `update_wiki(summary)`，由一个 **wiki 维护子 Agent** 按摘要更新对应页面。

wiki 结构不强制镜像仓库目录——按业务/技术逻辑自由组织。每篇 wiki 页可以：
- 引用相关代码的相对路径（如 `详见 src/auth/login.py`）
- 关联多个代码文件（不限于 1:1）
- 通过 Markdown 相对链接跳转到其他 wiki 页

### 工具集

**CRUD（不依赖具体 Agent）：**

| 工具 | 作用 |
|------|------|
| `wiki_read(path)` | 读 wiki 页面 |
| `wiki_list(path)` | 列子目录（附标题大纲 + 行号） |
| `wiki_tree()` | 显示整棵页面树 |
| `wiki_search(query, regex, max_results)` | 全文搜索 |
| `wiki_write(path, content)` | 写入/更新页面 |
| `wiki_delete(path)` | 删除页面 |

**自动维护（绑定主 Agent）：**

`update_wiki(summary="")` — 完成重要功能或修改后调用。summary 留空时自动从最近一轮 Turn 提取完整上下文（用户任务 + 工具调用 + 结果 + 计划）交给子 Agent 理解。起一个 `enable_thinking=False` 的静默子 Agent，用 `WIKI_UPDATER_SYSTEM` 引导它先了解现有 wiki 结构再按业务/技术逻辑更新/新建页面。

### 安全

`_wiki_resolve` 把路径解析到 `.agent/wiki/` 内，越界拒绝（`PermissionError`）。

---

## 7. 用户反馈 — `feedback.py`

让用户（CLI / WebUI / Agent）一键提交反馈。

### 双通道策略

1. **本地落盘**（永远做）：`~/.agt/feedback/<时间戳>_<类型>.json`，兜底，绝不丢。
2. **飞书 webhook 推送**（可选）：`enabled` 启用且配了 URL 时，组装交互卡片 POST 推送，实时到作者手机。

配置在 `~/.agt/feedback.json`：`{webhook_url, enabled}`。`enabled=false` 时只落盘不上报（隐私可关）。

### 飞书卡片

`_build_feishu_card` 把反馈记录组装成飞书交互卡片 payload，按反馈类型配色（bug=红/建议=蓝/问题=橙/赞美=绿）+ emoji。

### 工具

`submit_feedback(kind, content, contact)` — Agent 自主用的反馈工具（与 `/feedback` 命令同源）。`kind` 不在 `VALID_KINDS`（bug/建议/问题/赞美）时归为"建议"。

---

## 8. 资产下载 — `download.py`

manifest 驱动的随包资产获取。

### 与 seed 的关系

- `seed_default_workflows`：**隐式自动**播种（仅 workflows/*.xml，用户不可见不可控）。
- 本模块：**显式可控**（任意资产类型、看得见、可选目录、可指定覆盖）。

两者都"已存在不覆盖"，不冲突。

### 资产清单

`src/assets/manifest.json`（随包打进 wheel），每项 `{name, type, desc, src, default_dir}`。新增资产：放文件 + manifest 加一行。

### 工具

| 工具 | 作用 |
|------|------|
| `list_downloadable()` | 列出随包可下载资产（名称/类型/描述/是否已在本地） |
| `download_asset(name, dir, force)` | 下载某资产到指定目录（默认该资产 default_dir，已存在且非 force 跳过） |

---

## 9. 会话日志 — `log.py`

跟 session 走的 logging：`<session_dir>/log.log`，与 `<name>.json` 并排。

### 设计

标准 `logging` + 自定义 `SessionLogHandler`：

- **文件写全量**（DEBUG+）；**控制台只输出 WARNING+**（不刷屏，CLI 已有事件流）。
- **session name 未就绪**（首轮进行中）→ 内存缓冲；name 就绪（`_ensure_name` / `set_session`）→ flush 并切到直写。
- `set_session(workspace, name)` 切换目标文件（切 session 时跟着切）。

### 环境变量

| 变量 | 作用 |
|------|------|
| `AGT_LOG_CONSOLE=1` | 控制台输出全量 |
| `AGT_LOG_LEVEL=DEBUG/INFO/WARNING` | 改级别 |

### 格式

```
[07-20 17:30:45] [INFO ] [llm   ] 调用 glm-5.2...
```

`_Fmt` 把 logger 名取末段（`agt.llm` → `llm`），控制台/文件更紧凑。

### 状态机（与 ToolLog / LLMCallLog 同思路）

```
_path=None（buffer） → set_session(name 就绪) → flush 全量建立 → 之后纯直写
```

`BUFFER_CAP = 2000`，防爆。写失败也保住条目，待下次 flush。

---

## 10. 工具调用详情 — `toollog.py`

append-only JSONL 落盘的工具调用详情库。

### 设计

ToolCall 在 session 的 steps 里只存 `call_id`，完整 `name`/`arguments`/`result` 存本库。组装上下文（`session._steps_to_messages`）时按 `call_id` 召回 + **按步距衰减摘要**。落盘用 JSONL 流式 append——record 一行，O(1)，不再随 session 全量重写。

### 距离衰减

```python
DETAIL_BASE = 1500   # 当前步（d=0）的最大摘要字数
DETAIL_STEP = 15     # 每远一步减少的字数
DETAIL_FLOOR = 20    # 最远也至少保留的字数

def detail_limit(distance):
    return max(FLOOR, BASE - distance * STEP)
```

最近步的工具调用结果保留最多（1500 字），越远的步衰减越多（每步 -15 字，最低 20 字），控制上下文窗口膨胀。参数运行时可用 `set_detail_params` 改。

### ToolLog 类

| 方法 | 作用 |
|------|------|
| `next_id()` | 生成会话内自增 id：c1 / c2 / … |
| `record(call_id, name, arguments, result, step, turn)` | 记录一次调用详情 |
| `get(call_id)` / `view(call_id)` | 召回详情 |
| `search(keywords, max_hits)` | 按关键字子串匹配初筛（Agentic RAG 第一阶段） |
| `set_path(path)` | 绑定 jsonl 路径（buffer→flush→append 状态机） |
| `load_from_jsonl(path)` | 流式读 jsonl 恢复（resume 用） |

### 工具

| 工具 | 作用 |
|------|------|
| `get_tool_detail(call_id)` | 拉取工具调用的完整详情（支持逗号/空格分隔多个 id） |
| `list_tool_logs()` | 列出当前会话所有工具调用的 id 清单 |

---

## 11. LLM 调用流水 — `llm_call_log.py`

每次 LLM 调用追加一条记录，供 `/stats` 聚合 per-model 可靠性。这是「LLM agent 可观测性」的第一手数据。

### 记录字段

```
ts / model / attempt / max_tokens / finish_reason / usage / elapsed /
outcome(success|empty|truncated|error) / content_len / reasoning_len /
tool_calls / msgs_count / msgs_chars / error / completer
```

### 聚合统计

`aggregate_calls` 按 model 聚合：

| 指标 | 说明 |
|------|------|
| calls | 总调用次数 |
| success / empty / truncated | 各 outcome 计数 |
| errors | 按异常类型分组的计数 |
| completer | completer 补全次数 |
| tokens | 累计 token 用量 |
| avg_latency | 平均耗时（秒） |

`format_stats` 把聚合结果格式化成可读文本（CLI `/stats` 与 webui 共用）：

```
▣ glm-5.2: 42 次 | 成功 38(90%) | 空 2 | 截断 1；错误 Timeout:1 | 156K tokens | 均3.2s
```

### 跨 session 聚合

`load_all_calls(sessions_dir)` 递归扫描各时间戳文件夹的 `llm_calls.jsonl`，聚合某 repo 下所有 session 的调用记录，供 `/stats all` 看整体可靠性。

### 状态机

与 ToolLog 完全同构：name 就绪前 buffer 在内存，`set_path` 后 flush 全量建立 + 之后纯 append。

---

## 12. 自动更新 — `updater.py`

启动时后台检查 PyPI 新版本并（全自动模式）pip 升级。

### 策略

| 条件 | 行为 |
|------|------|
| editable / 本地 / 未识别安装 | 跳过（自动更新对开发安装无意义） |
| 24h 内查过 | 用缓存的 latest，不重复请求 PyPI |
| 有新版 + auto_update 开 | 后台 `pip install -U --no-input agt-agent`，打印升级提示 |
| 有新版 + auto_update 关 | 仅通知，提示手动升级 |
| 网络 / pip 失败 | 静默或一行小字，绝不阻塞启动 |

### 安装类型识别

`install_kind()` 通过 PEP 610 `direct_url.json` 判断：

| 类型 | 判定 |
|------|------|
| `editable` | `dir_info.editable=true`（`pip install -e`） |
| `local` | `url=file://`（本地路径安装） |
| `pypi` | `http(s)://` 来源，或旧式无 direct_url 的 pip 安装 |
| `unknown` | 找不到 dist-info（源码直跑等） |

仅 `pypi` 才自动更新。

### 编排

`check_and_update(force, announce)` 返回 `{current, latest, status, msg}`，status ∈ `skip` / `netfail` / `latest` / `notify` / `updated` / `fail`。

`start_background_check(announce)` 启动 daemon 线程，不阻塞 REPL，任何异常都不影响启动。

### 版本比较

`_parse_ver` 优先用 `packaging.version.parse`（PEP 440），不可用时回退到整元组比较（够 `0.9.5` 这类比较）。

---

## 模块间协作关系

```
                    ┌─────────────┐
                    │   Agent     │
                    │  (核心引擎)  │
                    └──────┬──────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   multiagent.py      plan_tools.py      background.py
   (子Agent派活)      (计划SYSTEM注入)    (服务+调度→inbox)
        │                  │                  │
        │    spec_tools.py  │                  │
        │   (spec→plan)     │                  │
        │      │            │                  │
        │   explore_subagent│                  │
        │   (派只读子Agent)  │                  │
        │                  │                  │
   wiki.py ─── update_wiki 起子Agent           │
   (CRUD+自动维护)                              │
                                                │
   rag.py ─── rag_query 工具 ─── 全局单例(set_rag)│
   (faiss+sqlite)                                │
                                                │
   feedback.py ─── submit_feedback 工具          │
   (本地+飞书)                                   │
                                                │
   download.py ─── list/download 工具            │
   (manifest驱动)                                │
                                                │
   log.py ─── SessionLogHandler (跟session走)    │
   toollog.py ─── ToolLog (JSONL append)        │
   llm_call_log.py ─── LLMCallLog (JSONL append) │
   (三者共享 buffer→flush→append 状态机)         │
                                                │
   updater.py ─── start_background_check (daemon)│
   (PyPI检查+pip升级)                            │
```

### 共享设计模式

1. **闭包绑定 Agent**：`make_xxx_tools(agent)` 返回绑定到指定 Agent 实例的工具列表（plan/spec/wiki/feedback/download/toollog），工具内通过闭包访问 `agent.session`、`agent.on_event` 等。

2. **buffer→flush→append 状态机**：`log.py`（SessionLogHandler）、`toollog.py`（ToolLog）、`llm_call_log.py`（LLMCallLog）三者共享同一模式——session name 就绪前 buffer 在内存，`set_path` 后 flush 全量建立文件 + 之后纯 append，O(1) 写入，不随 session 全量重写。

3. **原子落盘**：`plan_tools._save_plan` 和 `spec_tools._save_spec` 都用写 `.tmp` 再 `os.replace` 的原子写法，避免写一半崩溃导致文件损坏。

4. **SYSTEM 注入**：plan（`_format_plan_block`）和 spec（`_format_spec_block`）都通过 provider 槽每轮被动注入 SYSTEM，让 Agent 始终清楚当前在干什么。approved spec 不注入（由生成的 plan 接管，避免双重注入）。

5. **重依赖懒加载**：`rag.py` 的 faiss/numpy/sentence_transformers 不在顶层 import，避免没配 embedding 的用户被 torch 的 Windows 页面文件问题拖垮启动。

6. **globals() 避免闭包遮蔽**：`feedback.py` 和 `download.py` 的工具闭包内用 `globals()["submit_feedback"]` / `globals()["download_asset"]` 显式取模块级函数，避免闭包同名函数遮蔽。

8. **provider 槽位注入**：`session._teammates_provider`、`_plan_provider`、`_spec_provider`、`_ltm_static_provider`、`_ltm_episodic_provider`、`_time_provider`、`_system_extra_provider`、`_state_provider`——所有运行时上下文都通过 provider 回调注入，Agent 与 Session 深度绑定但解耦：Session 组装上下文时调 provider 要运行时信息，Agent 切换 Session 时重挂所有 provider。

9. **全异步 Agent 协作**：`agent_prompt` 永远异步 + 自动 `caller_id` 绑定。子 Agent 完成后按 caller_id 路由 answer 入 inbox（复用现有 inbox → work_q → run 链路），不需要任何新基础设施。recap（每轮一句话总结）用 daemon 线程异步生成，不阻塞主循环，不进入自己上下文但队友可见。
