# 工具外置 · 判别标准与分层（2026-08-25 定稿；四组真限界上下文已全部外置）

> 外置的不是"读得到数据的工具"，而是**拥有自己数据的工具**。

## 背景

工具外置第一步（v0.19.0，spec s_25352f88）把 15 个纯函数工具迁到了 `tools/builtin/`。后续讨论"还有哪些能迁"时，最初的口径是"能从磁盘重建状态的 = 低依赖"——后来被修正：**数据的写者才是归属的决定因素**。

## 判别标准（一句话）

> **如果文件系统的某些文件本身就是由这组工具写的、又由这组工具读的 → 对引擎依赖低，可外置；
> 如果文件是引擎写的、工具只是读 → 仍然是对引擎有依赖的工具，外置是伪外置。**

关键：重放能拿到数据 ≠ 独立。外置后 import 依赖没了，但换成**文件格式依赖**——引擎改事件结构（step 加字段、turn_end 改语义）时，外置工具的解析静默失效（解析不出→返回空→上游无察觉）。耦合没消除，只是从显式（import）变成隐式（格式契约），反而更危险。

## 四象限盘点（2026-08-25 全量 21 组）

### ✅ 自写自读——真限界上下文（外置完全自洽；**四组已全部外置，2026-08 收官**）

| 工具组 | 数据文件 | 说明 |
|---|---|---|
| **wiki 六件套** | `.agent/wiki/*.md` | wiki_write 自己写、wiki_read 自己读——引擎从头到尾没碰过。**已外置 ✅（wiki_tools.py）** |
| **ltm 五件套** | `repos/<key>/memories/*.jsonl` | add/update/delete_memory 自己写。**已外置 ✅——ltm_tools.py 注册外置（ensure_ltm 单例共享），见下节** |
| **download** | 随包资产 → manifest 声明的 default_dir（`.agent/workflows/` 等） | download_asset 自己写、exists 检查自己读。**已外置 ✅（download_tools.py）** |
| **rag**（大部分） | 向量 `.db` | 索引构建自己写（配置读 config，属轻依赖）。**已外置 ✅——rag_tools.py 混合形态，见下节** |

性质：**文件格式契约归工具组自己**——外置后想改存储结构（wiki 加 frontmatter、ltm 换分段键），工具组自己说了算，引擎升级永远不破坏它。

### ❌ 引擎写、工具读——可观测性出口（永远内置）

| 工具组 | 数据文件 | 写者 |
|---|---|---|
| memory_tools (recall) | `events.jsonl` | 引擎 `_emit` 落盘 |
| toollog (list/get_detail) | `toollog.jsonl` | 引擎工具调度器写 |
| session_tools (history) | `meta.json` + events | 引擎 Session 写 |

它们是引擎的日志消费者，跟着引擎走才是正确位置。

### ⚠️ 半依赖——文件自写但流程归引擎

| 工具组 | 情况 |
|---|---|
| plan/spec CRUD | `plans|specs/*.json` 是工具写的，但 draft→committed→approved 状态机 + threading.Event 阻塞批阅是引擎流程；CRUD 半边可外置，流转半边永远引擎 |
| agent_config | settings.json 是全框架公共契约，不算工具组自有数据 |
| background 查询 | `/api/status` 快照由引擎序列化 |

### 🔒 纯进程内状态——永远内置

restart（work_q 退出哨兵+看门狗协议）/ spec+survey（threading.Event 交互阻塞）/ multiagent 全家（registry 活引用、inbox、push_message、bg 线程）/ wf_*（_emit、utility_client、动态注册）/ workflow_debug（_debug_ctx）。

## A 类：零引擎依赖（连 factory 都不需要）

feedback（纯 HTTP 上报）、agent_config（settings.json 约定读写）、rag 大部分。

## rag 外置的边界裁剪（混合形态，2026-08 commit 71e0b90）

rag 组数据主权（向量 `.db` 自写自读）达标可外置，但 **embedder 是被 cosine_sim/session_vec/emb_probe 共享的进程内单例**——按本标准属 🔒"纯进程内状态"。落地取折中：**外置注册与预热触发（`tools/builtin/rag_tools.py` 的 `agt_register` 触发 `preload_async` + 注册 rag_query），函数本体留在框架 `src/rag.py`**——外置件里 `import rag` 主进程零成本，复制实现反而破坏共享。标准在边界情况下的正确裁剪：数据主权侧外置、共享态留框架。详见 [rag](../features/rag.md)。

## ltm 外置的边界裁剪（2026-08，commit fd06c48，rag 同款）

ltm 数据主权（`memories/*.jsonl` 自写自读）达标，但 **LongTermMemory 是带内存缓存的实例、Agent 的两个注入 provider 也持有它**——各持一份会缓存分裂（工具写一条，provider 下一轮看不到）。落地同 rag：**外置的只有注册（`tools/builtin/ltm_tools.py` 五件套），函数本体留在框架 `src/longterm_memory.py`，经 `ensure_ltm` per-workspace 模块级单例共享同一实例**；`origin_session` 元数据由 provider（`_ltm_static_block`，每轮必被调）刷新到单例上，外置 add_memory 读取。详见 [longterm-memory](../features/longterm-memory.md)。

外置的通用配套：**ctx 通用上下文注入**——引擎扫描时把 `{"cwd": workspace 绝对路径, "version": 1}` 签名兼容传给 `agt_register`（外置件不再依赖 import 时 `Path.cwd()` 猜 workspace，os.chdir 等场景不漂移；无参存量外置件原样兼容），见 [tool-externalization · ctx](../features/tool-externalization.md#ctx-通用上下文注入2026-08commit-fd06c48)。

## 纯函数批：real_tools 再外置 8 工具（2026-08，commit 17312eb）

判别标准的第二次全量应用（用户提议：real_tools 里蛮多工具是纯函数，似乎可以外置）——这次过筛的不是"数据主权"，而是**把 LIGHT_TOOLS 里所有非框架状态的工具清出去**，13→5：

| 工具 | 去向 | 判定 |
|---|---|---|
| length / to_uppercase / to_lowercase | `str_tools.py` 追加 | 零状态纯函数 |
| kv_cache_read / kv_cache_write | `kv_tools.py` 新建 | **`_KV_CACHE` 状态随外置件走**——进程级 dict 自写自读（"自写自读的文件"退化为内存态）；用途是同输入结果确定的 LLM 调用 memoization（同轮多个 before_turn 工作流共用一次提取），namespace 兼作版本号；重启清空=结果缓存语义（丢失=下次重算，无正确性影响） |
| diff_lines | `diff_tools.py` 新建 | **算法副本**形态：Myers 三件套复制实现而非 import 框架（外置件零框架依赖约定）——Myers 是稳定经典算法，双份各自带回归（随机重放 200/200），注释互指路、改动两处同步 |
| cosine_sim / emb_probe | 本体迁 `src/rag.py` + 注册 `rag_tools.py` | 语义归属 RAG 组：与 embedder 单例共生，rag_query 同款「本体在框架、注册在外置」 |

**LIGHT_TOOLS 剩余 5 件全是框架状态型**（判别标准下不可外置）：ReAct 原语三件套（`_WF_CTX` 注入型，执行时由 workflow 引擎注入 llm/tools 上下文）+ dir_outline/concat_files（`_resolve` 沙箱型）。验证：外置扫描全量 43 工具、kv 读写往返、diff 输出对拍、outputs 声明（kv `hit:boolean` / cosine `raw:number`）全过。详见 [tool-externalization · 外置件清单](../features/tool-externalization.md)。

## 运行时管理器的替代边界（background/lsp/mcp/reload_hot）

- **查询**有替代：`/api/status` HTTP 快照（仅 WebUI 模式可用）
- **操作**（start/stop/send/注册/挂载）无替代——工具箱本身就是引擎器官，但都是薄注入（一个 agent 参数）

## 双模式先例（代码库里已有三个）

`format_team(session_dir=...)` 磁盘兜底、`agent_query_events` lazy-load `Session.load`、`_restore_subagents` 扫 `agents/` 目录——**有 agent 走活视图，没有走磁盘约定**。外置工具可继承此模式（描述符 make / make_standalone 双工厂）。

## 迁移进度（判别标准驱动；真限界上下文四组 + 纯函数批双收官）

1. wiki 六件套 ✅ + rag ✅ + ltm 五件套 ✅ + download ✅（第四批 commit fd06c48 收官）——数据主权本来就在工具组；**此后这四组的改动走 `/reload tools` 秒级热加载**
2. **纯函数批 ✅（第五批，2026-08 commit 17312eb）**：real_tools LIGHT_TOOLS 再外置 8 工具（见上节）——**LIGHT_TOOLS 13→5，剩余全是框架状态型**，判别标准对 real_tools 全量过筛收官
3. factory kind 机制（D 类描述热改收益仍在：工具 docstring 就是 LLM 看的 schema 描述）
4. plan/spec CRUD 半边
5. memory_tools/toollog **不迁**（判别标准下从旧名单划掉）

## 相关页面

- features/tool-externalization.md（外置体系：目录/装配/ctx 注入/热加载/**外置件 10 文件清单**）
- features/rag.md（rag 外置混合形态：注册外置 + 实现留框架）
- features/longterm-memory.md（ltm 外置 + ensure_ltm 共享单例）
- features/glob-files.md（外置首例演示：纯函数整体外置）
- features/diff-lines.md（算法副本形态：diff_tools.py）
- architecture/node-plugins.md（节点插件化——同一哲学 + catalog_entries 动态聚合同款"元信息跟实现走"）

