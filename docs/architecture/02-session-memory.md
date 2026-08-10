# 会话与记忆模块架构设计

> 对应源码：`src/session.py` · `src/longterm_memory.py` · `src/session_vec.py` · `src/cache_sim.py` · `src/snapshots.py` · `src/toollog.py`

---

## 1. 模块职责

| 文件 | 行数 | 职责 |
|------|------|------|
| `session.py` | 1572 | **核心**：分层上下文引擎——Turn>Step>ToolCall 三级结构、分档投影、毕业压缩、折叠召回、存档/恢复 |
| `longterm_memory.py` | 321 | 跨 session 长期记忆：semantic/episodic/procedural 三层、注入策略 |
| `session_vec.py` | 264 | 会话向量检索：embed 索引 + 语义召回 |
| `cache_sim.py` | 145 | 前缀缓存模拟/估算 |
| `snapshots.py` | 71 | 工作区快照管理（检查点回溯） |
| `toollog.py` | 210 | 工具调用完整详情库 + 步距衰减摘要 |

---

## 2. 核心数据结构

### 2.1 三级层次：Turn > Step > ToolCall

```
Session
  └── turns: list[Turn]          # 每轮用户请求
        ├── user_message          # 用户消息
        ├── images                # 用户附图（data URL）
        ├── snapshot_sha          # 该轮发送前的工作区快照
        ├── steps: list[Step]     # 每步 = 一次 LLM 调用
        │     ├── reasoning       # 推理过程（reasoning_content）
        │     ├── tool_calls: list[ToolCall]
        │     │     └── call_id   # 只存 id，详情在 toollog
        │     ├── preceding_hint  # 该步前插入的"用户中途补充"
        │     └── file_snapshots  # {call_id: {path,version,text}} 运行时填充
        ├── answer                # 最终回答
        ├── answer_reasoning      # 回答那步的 reasoning
        └── summary               # 该轮一句话摘要
```

**关键设计**：`ToolCall` 只存 `call_id`（如 "c1"、"c2"），工具名/入参/完整结果存在 `toollog` 里。组装上下文时按 id 召回——这样 Step 序列化极轻量，且工具结果可按步距独立摘要。

### 2.2 ToolLog：工具调用详情库

```python
class ToolLog:
    _data: dict[str, tuple]  # call_id → (name, arguments, result)
    _counter: int            # 自增 id 生成器

    def record(cid, name, args, result)   # 记录
    def view(cid) -> (name, args, result) # 召回
    def search(keywords) -> list          # 关键词搜索
```

### 2.3 步距衰减摘要

工具结果按**距当前步的距离**差异化摘要（`toollog.detail_limit`）：

```python
DETAIL_BASE = 1500   # 初始摘要字数上限（当前步 d=0）
DETAIL_FLOOR = 20    # 下限
# detail_limit(d) = max(DETAIL_BASE - d * DETAIL_STEP, DETAIL_FLOOR)
#   d=0 → 1500 字（完整披露）
#   d=1 → 1485 字
#   d=10 → 1350 字
#   d=50 → 750 字
#   d=100 → 20 字（下限）
```

截断处标注 `call_id`，提示模型用 `get_tool_detail("c7")` 拉取完整内容。

---

## 3. 分档上下文投影（核心算法）

### 3.1 问题

长对话的上下文会超出模型窗口。传统方案是滑动窗口（丢弃旧轮）或全局摘要（有损压缩），各有缺陷：
- 滑动窗口：早期细节永久丢失
- 全局摘要：压缩质量不稳定，且每次都要 LLM 调用

### 3.2 分档投影方案

**核心思想**：已完成的老轮按"档位"递进压缩，近期轮保持全量。每个档位的压缩级别不同，越老的档位压缩越狠。

```
时间轴 ──────────────────────────────────────────────→

  档位4(最老)    档位3      档位2      档位1     当前段(最新)
  ┌─────┐    ┌─────┐    ┌─────┐    ┌─────┐    ┌─────────┐
  │摘要 375字│  │摘要 750字│  │摘要 1500│  │全量披露│  │全量+实时│
  │/轮     │  │/轮     │  │/轮     │  │/轮   │  │       │
  └─────┘    └─────┘    └─────┘    └─────┘    └─────────┘
                                                 ↑
                                           _tier_boundaries = [5, 10, 15]
                                           （已毕业的 turn 索引边界）
```

### 3.3 档位级别计算

```python
def _tier_level(self, turn_idx: int) -> int:
    # level = 1 + count(boundaries 中 >= turn_idx)
    # 封顶 max_level（默认 4）
    #
    # 示例：boundaries = [5, 10]
    #   turn 0-4  → level 3（跨了 2 个边界）
    #   turn 5-9  → level 2（跨了 1 个边界）
    #   turn 10+  → level 1（当前段，无压缩）
```

### 3.4 档位 base 字数

```python
base = max(DETAIL_BASE >> (level - 1), DETAIL_FLOOR)
#   level 1 → 1500 字（当前段，全量）
#   level 2 → 750 字
#   level 3 → 375 字
#   level 4 → 187 字
```

### 3.5 冻结渲染（Frozen Render）

每个已完成 turn 按其档位级别渲染一次后**冻结缓存**：

```python
_frozen_renders: dict[int, tuple[int, list]]  # turn_idx → (level, msgs)

def _render_turn_frozen(self, turn_idx):
    level = self._tier_level(turn_idx)
    cached = self._frozen_renders.get(turn_idx)
    if cached and cached[0] == level:
        return cached[1]          # ← 同 level 直接复用，byte-stable
    # level 变了才重算
    msgs = self._render_turn(turn_idx, base=...)
    self._frozen_renders[turn_idx] = (level, msgs)
    return msgs
```

**为什么 byte-stable 重要**：前缀缓存（prefix cache）要求消息序列的 bytes 完全一致。如果每次渲染结果有微小差异（如时间戳变化），缓存就失效了。冻结渲染保证同一档位的 turn 渲染结果永远不变。

### 3.6 毕业机制

当上下文超出 `max_effective_context_window` 时，触发"毕业"：

```python
def _graduate_once(self) -> bool:
    # 把最后完成 turn 升档：append 其索引到 _tier_boundaries
    # 其后所有档 level+1 = 顺移
    # 清掉 level 变了的冻结缓存让其按新级别重渲染
```

毕业流程：

```
初始状态（5 轮，都在当前段）：
  turns: [0] [1] [2] [3] [4]    boundaries: []
  所有 turn level=1（全量）

第 6 轮完成，超窗口 → 毕业 turn 5：
  turns: [0] [1] [2] [3] [4] [5]  boundaries: [5]
  turn 0-4 → level 2（压缩到 750 字/轮）
  turn 5+ → level 1（全量）

继续对话，再超窗口 → 毕业 turn 10：
  boundaries: [5, 10]
  turn 0-4 → level 3（375 字/轮）
  turn 5-9 → level 2（750 字/轮）
  turn 10+ → level 1（全量）
```

### 3.7 折叠召回

当毕业也压不动了（所有档位都到 max_level），把最前档折叠成摘要不逐条渲染：

```python
def _folded_summary(self, fold_count: int) -> str:
    # 每轮：user_message[:80] + (已折叠N次工具调用) + answer摘要
    # 纯结构信息、无需 LLM
    # 逐字原文用 recall_turn 工具按需召回
```

**关键**：fold_count 是每次 build 派生的、不持久化——窗口变大/对话变短时会自动回退（折叠的轮重回渲染）。

### 3.8 完整投影流程

```python
def _build_tiered_messages(self, msgs):
    # msgs = [system, task_guidance, ltm_static]
    prefix_len = len(msgs)
    win = self.max_effective_context_window

    fold_count = 0
    for _ in range(len(self.turns) + self.max_level + 4):  # 安全上限
        body = self._render_tiered_body(fold_count)
        if self._estimate_tokens(msgs[:prefix_len] + body) <= win:
            break                           # 进窗口了
        if self._graduate_once():            # 先压缩：升一档
            continue
        nxt = self._next_fold_target(fold_count)
        if nxt is not None:                 # 压不动了：折叠最前档
            fold_count = nxt
            continue
        break                               # 既压不动也折不动，放弃
    return msgs[:prefix_len] + self._render_tiered_body(fold_count)
```

---

## 4. 消息组装：messages_for_llm()

喂给 LLM 的完整消息序列：

```
┌─────────────────────────────────────────────────────────┐
│ system（人设，纯指令，不包裹）                              │
│ system（任务指引：AGENTS.md/rules/skills/子Agent，每轮重读）│
│ system（长期记忆·静态层：semantic 事实 + procedural 标题）   │
├─────────────────────────────────────────────────────────┤
│ 分档 body：                                               │
│   system（折叠摘要，如有）                                 │
│   [档位N的老轮] user → assistant(tool_calls) → tool → ... │
│   [档位2的老轮] user → ...（压缩到 750 字/轮）             │
│   [档位1的近期轮] user → ...（全量）                       │
│   [当前进行中轮] user → steps → pending_hint              │
├─────────────────────────────────────────────────────────┤
│ system <system-reminder>（tail 合并：                     │
│   时间 + 后台服务状态 + 计划 + spec + 情境记忆）            │
└─────────────────────────────────────────────────────────┘
```

### tail ambient 合并

易变块（时间/后台/计划/spec/情境记忆）合并成**一组** `<system-reminder>`，放在 user 消息之后——保前缀缓存稳定：

```python
@staticmethod
def _ambient_group(blocks: list[str]) -> str:
    return "<system-reminder>\n" + "\n\n".join(blocks) + "\n</system-reminder>"
```

### 图片投影

`<img>name</img>` 占位标签按当前模型能力投影：
- **视觉模型**：`[text块 + image_url块]`（读 repo images/ 转 data URL）
- **非视觉模型**：文字占位 `'[图片 name，你无法直接查看；如需理解请委托视觉子 agent]'`

---

## 5. 长期记忆（LongTermMemory）

### 三层模型

| 层 | 类型 | 注入策略 | 存储 |
|----|------|----------|------|
| **semantic** | 事实/偏好 | 每轮**始终注入** | `~/.agt/repos/<hash>/memories/semantic.json` |
| **episodic** | 情境经历 | 按当前问题**自动召回**注入 | `~/.agt/repos/<hash>/memories/episodic.json` |
| **procedural** | 流程经验/how-to | system 里只列**标题**，需要时 `read_procedure(id)` 取详情 | `~/.agt/repos/<hash>/memories/procedural.json` |

### 注入位置

```
messages_for_llm()
  ├── system（人设）
  ├── system（任务指引）
  ├── system（← semantic 静态层：事实 + procedural 标题清单）  ← 始终注入
  ├── ... 分档 body ...
  └── system <system-reminder>（← episodic 情境层：按 user_message 召回）  ← 按需注入
```

### 召回策略

```python
def episodic_block(self, query: str) -> str:
    # 1. 关键词初筛（子串匹配）
    # 2. 语义精排（如有 embed 模型）
    # 3. 取 top-3 相关条目
    # 4. 格式化成 <system-reminder> 注入
```

---

## 6. 存档与恢复

### 文件夹结构

```
~/.agt/repos/<repo-hash>/sessions/
  └── 20250811_034344/           ← 时间戳文件夹
        ├── meta.json             ← 元信息（name/created_at/system/extra_state/tier_boundaries）
        ├── events.jsonl          ← 事件流（turn_start/step/turn_end/snapshot/restore）
        ├── toollog.jsonl         ← 工具调用完整详情
        └── llm_calls.jsonl      ← LLM 调用流水（可观测性）
```

### 事件流持久化

不把 turns 直接序列化到 meta.json，而是用 **append-only 事件流**：

```jsonl
{"event": "turn_start", "user": "你好", "images": []}
{"event": "snapshot", "sha": "abc123"}
{"event": "step", "reasoning": "...", "call_ids": ["c1"]}
{"event": "turn_end", "answer": "...", "answer_reasoning": "...", "summary": "..."}
```

**好处**：
- 增量写入（每轮只 append，不重写整个文件）
- 回溯只需截断（`restore` 事件标记保留前 N 轮）
- 重放重建（`_replay_events` 从事件流还原 turns 树）

### 检查点回溯

```python
def restore_to_snapshot(self, sha: str):
    # 找到 snapshot_sha==sha 的那轮
    # 截断它及之后的轮
    # 重写 events/toollog 文件（含 restore 标记）
    # 清空冻结缓存 + 分档边界
```

### 自动命名

两阶段命名（通过 `_name_lock` 互斥）：
1. **_ensure_name_early**：首次工具调用前，用 LLM 思考 + 工具名推断主题（daemon 线程，不阻塞）
2. **_ensure_name**：首轮完成后，用 user_message + answer 生成标题（兜底）

### 异步落盘

每轮 `finish_turn` 后触发 `_autosave()`（daemon 线程），不阻塞主循环。原子写（先 .tmp 再 os.replace）。

---

## 7. 设计亮点总结

1. **完整原文永不丢**：turns 永不截断，超出窗口的轮只改变喂给模型时的形态（摘要/折叠），原文完整保留在内存和存档里
2. **分档冻结利于前缀缓存**：同档位渲染结果 byte-stable，provider 的 prefix cache 命中率极高
3. **步距衰减**：工具结果按距当前步的距离差异化摘要，近的详细、远的简略，截断处标注 call_id 可按需拉取完整
4. **事件流持久化**：append-only，增量写入，回溯只需截断，重放重建完整状态
5. **两阶段命名**：工具调用前就异步命名+落盘，不等首轮完成
6. **tail 合并**：所有易变块合并成一组 `<system-reminder>` 放 user 后，保前缀缓存
