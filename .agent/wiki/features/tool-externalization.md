# 工具外置 · tools/builtin 脚本工具体系

> 把工具从框架代码外置为「一个 .py 文件」——写一个文件即得新工具，零框架改动。与[节点插件化](../architecture/node-plugins.md)同构的扫描/装配模式，但更轻：只有 .py，无前端件。

## 目录与装配

| 位置 | 角色 |
|------|------|
| `tools/builtin/*.py` | 工具源文件（开发处，现 12 个：fs/str/list/misc/kv/diff/wiki/rag/ltm/download/team/**cache**） |
| `src/assets/tools_builtin/*.py` | 随包副本（pip 安装即有，与 nodes_builtin 同思路；现 11 个——kv/diff 为 2026-08 纯函数批新增（commit 17312eb）；team_tools.py 2026-09-02 已写源文件、随包副本仍待同步；cache_tools.py 2026-09-02 新建**随包副本已同步**） |

约定：模块暴露 `agt_register(ctx=None)` 返回工具描述符列表，`src/script_tools.py` 扫描注册（`rglob("*.py")` 支持子目录组织、`_` 开头跳过、mtime 缓存）。**改完必须同步随包副本**。

## ctx 通用上下文注入（2026-08，commit fd06c48）

外置件以前只能靠 `Path.cwd()`（import 时捕获）猜 workspace——引擎侧 `os.chdir` 等场景会漂移。现引擎扫描时构造**通用上下文**传给外置件：

```python
# src/script_tools.py · scan_script_tools
ctx = {"cwd": str(WORKSPACE), "version": 1}   # 引擎视角的真实 workspace 绝对路径
_invoke_agt_register(mod, ctx)
```

**签名兼容**（`_invoke_agt_register`，inspect 检查形参）：有 `*args`/`**kwargs` 或名字为 `ctx`/`context`/`args` → **按位置**传 ctx；无参 → 原样调用（向后兼容）。外置件标准写法：

```python
_WORKSPACE = Path.cwd()          # import 时兜底（直接 import 也 work）

def agt_register(ctx=None):
    global _WORKSPACE
    if ctx and ctx.get("cwd"):
        _WORKSPACE = Path(ctx["cwd"])   # 引擎视角的真实 workspace 覆盖
    return [{"name": ..., "func": ..., "group": ...}, ...]
```

- 向后兼容已验证：glob / contains / list_append / sleep / rag_query 等**无参存量外置件原样工作**
- 以后通用依赖状态（session 目录、repos 根等）都往 ctx 加字段，外置件按需声明接收（`version` 留协议演进）
- ⚠️ ctx 只作用于**扫描器加载的那个模块实例**（`_import_fresh`）；别处再直接 `import wiki_tools` 拿到的是新实例，仍是 import 时的 `Path.cwd()`。验证时以扫描注册出的**工具行为**为准（实测：os.chdir 到临时目录后扫描，wiki_tree 仍返回真 workspace 的 346 行树）——别拿直连 import 的模块状态断言（开发期两次误报皆源于此）

## 热加载

改完 .py 用 `/reload tools` 即生效，**不需要重启**——比 src 内注册的工具（需 `/restart`，见 [diff-files](diff-files.md)/[get-list-item](get-list-item.md) 注意事项）轻一档。

## 外置件清单（12 文件；真限界上下文四组 + 纯函数批 + 团队管理组 + 缓存分析组）

| 外置件 | 注册的工具 | 形态 | 要点 |
|---|---|---|---|
| `fs_tools.py` | glob_files | 纯函数整体外置 | 首例（commit eafed25），实现+注册都在外置件，见 [glob-files](glob-files.md) |
| `str_tools.py` | split 等 + **length / to_uppercase / to_lowercase**（2026-08 追加） | 纯函数零状态 | 纯函数批（commit 17312eb）——LIGHT_TOOLS 纯函数迁出 |
| `list_tools.py` / `misc_tools.py` | 早期纯函数批（v0.19.0，15 个纯函数的一部分） | 纯函数零状态 | |
| `kv_tools.py`（新） | kv_cache_read / kv_cache_write | 纯函数整体外置（**状态随外置件走**） | **`_KV_CACHE` 进程级自写自读 dict**——同轮多 before_turn 工作流对同输入 LLM 调用（如关键词提取）做 memoization；缓存键 = namespace + 内容 sha1，namespace 兼作版本号（改提示词/换模型换 namespace 即整体失效）；重启清空 = 结果缓存语义（丢失=重算，无正确性影响）；outputs 声明 `hit:boolean` |
| `diff_tools.py`（新） | diff_lines | **算法副本**（复制实现而非 import 框架） | Myers 三件套与框架 diff_files 同源副本，随机重放 200/200 回归；双份注释互指路、改动两处同步，见 [diff-lines](diff-lines.md) |
| `wiki_tools.py` | wiki 十件套 | 纯函数整体外置 | `.agent/wiki/*.md` 自写自读；`agt_register(ctx)` 覆盖 `_WORKSPACE`；**2026-08（commit fe590a3）六件套扩十件套——章节级维护四件套（add/update/remove/move_chapter，章节=标题+全部子树），支撑 wiki-updater 增量维护**，见 [wiki-tools](wiki-tools.md) |
| `rag_tools.py` | rag_query + **cosine_sim / emb_probe**（2026-08 迁入注册） | 注册外置 + 实现留框架 | agt_register 触发 `preload_async` 预热；本体留 `src/rag.py` 共享 embedder 单例——cosine_sim/emb_probe 本体 2026-08 从 real_tools 迁入 rag.py（语义归属 RAG 组），见 [rag](rag.md)、[cosine-sim](cosine-sim.md) |
| `ltm_tools.py` | ltm 五件套 | 注册外置 + 实现留框架 | 本体留 `src/longterm_memory.py`，经 **ensure_ltm 模块级单例**与 Agent 注入 provider 共享同一实例（内存缓存不分裂）；origin_session 由 provider 每轮刷新，见 [longterm-memory](longterm-memory.md) |
| `download_tools.py` | list_downloadable / download_asset | 纯函数（调框架实现） | 资产目录自写自读；框架 `src/download.py` 保留 `list_assets`/`download_asset` 供 `/download` 命令（commands.py）；`agt_register(ctx)` 覆盖 `_WORKSPACE` |
| `team_tools.py`（新，2026-09-02） | team_up / team_status | 纯函数（编排引擎 remote_* 子进程/HTTP） | 团队管理：按清单启动成员 agt-web → 等端口就绪 → remote_connect 组网 → 恢复指定 session；dry_run 默认先行 + team_status 总览（POST /api/status 逐一探测）；⚠️ 随包副本待同步，见 [team-tools](team-tools.md) |
| `cache_tools.py`（新，2026-09-02，commit 8f9a6c6） | cache_breakpoint | 纯函数整体外置（只读分析存档） | **缓存断点分析**：对比两次连续 LLM 调用的投影 dump（`projections/t{N}_s{M}_{ts}.json`），定位缓存前缀断裂处——段位（SYSTEM/折叠摘要/历史档位/当前轮步骤）+ 消息索引 + 字符位置 + 前后对比窗口；agt_register **无参**（`Path.home()` 全局扫最近活跃 session，无需 ctx）；随包副本已同步；见 [cache-tools](cache-tools.md) |

工厂清理：`make_ltm_tools` / `make_download_tools` 已删，chat.py 装配线同步清理（ltm/download 改由 attach_script_tools 扫描注册）。

## 与节点插件化对照

| | 节点插件（nodes_builtin） | 工具外置（tools_builtin） |
|---|---|---|
| 文件 | `.py` + `.js` 配对（后端逻辑 + EdFW 前端表单） | 仅 `.py` |
| 注册入口 | SDK（`workflow_node_api.py`）+ 节点目录三级扫描 | `agt_register(ctx)` 描述符列表 + script_tools.py 扫描 |
| 生效方式 | 节点热加载 | `/reload tools` |

## 迁移收官（判别标准驱动；真限界上下文四组 + 纯函数批双收官）

哪些工具能外置、哪些不能——判别标准见 [tool-externalization-criteria](../architecture/tool-externalization-criteria.md)（一句话：外置的是"拥有自己数据的工具"，不是"读得到数据的工具"）：

1. **真限界上下文 4/4 全部外置 ✅**：wiki（第二批）/ rag（第三批）/ ltm + download（第四批，commit fd06c48）——文件由工具组自己写自己读，数据主权在本组；此后这批外置件的改动都走 `/reload tools` 秒级热加载
2. **纯函数批 ✅（第五批，2026-08 commit 17312eb）**：real_tools 再外置 8 工具（length/to_uppercase/to_lowercase → str_tools；kv_cache_read/write → kv_tools，`_KV_CACHE` 状态随外置件走；diff_lines → diff_tools 算法副本；cosine_sim/emb_probe 本体迁 rag.py、注册并入 rag_tools）——**LIGHT_TOOLS 13→5，剩余全是框架状态型**（ReAct 原语三件套 `_WF_CTX` 注入 + dir_outline/concat_files `_resolve` 沙箱），判别标准全量过筛收官
3. factory kind 机制：D 类（进程内状态组）外置也甩不掉 agent 注入，但描述热改收益仍在
4. memory_tools / toollog **不迁**——events.jsonl/toollog.jsonl 是引擎写的，它们是引擎的可观测性出口（重放拿到数据 ≠ 独立，格式契约耦合更危险）

## 相关页面

- [glob_files](glob-files.md) —— 首个外置工具（纯函数整体外置）
- [wiki 工具集](wiki-tools.md) —— 十件套（页面级六件套 + 章节级四件套）与增量维护优先约定
- [rag](rag.md) —— 混合形态首例（注册外置 + 实现留框架）
- [longterm-memory](longterm-memory.md) —— ltm 五件套外置 + ensure_ltm 共享单例
- [工具外置判别标准](../architecture/tool-externalization-criteria.md) —— 哪些能迁哪些不能（四象限盘点 + rag/ltm 边界裁剪）
- [节点插件化](../architecture/node-plugins.md) —— 同构模式（节点侧，更完整的三级目录/覆盖机制）
