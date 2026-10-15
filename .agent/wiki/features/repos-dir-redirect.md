# repos 数据根重定向 · settings.json repos_dir——~/.agt/repos/ 整棵数据树可指到其它盘（2026-10-15，用户提案）

## 职责与动机

用户提案（2026-10-15）：可在设置页 / `/config` 把 `~/.agt/repos/`（会话存档、specs、plans、memories、rag 等整棵数据树）重定向到其它目录——**C 盘系统区排除杀毒/索引干扰的根治手段**。默认根在系统区，杀软实时扫描 / 搜索索引器与每轮落盘（meta.json 原子写、jsonl 追加、git 快照）撞锁是长期干扰源（参见 [ops · meta.json 原子写撞锁](../guides/ops.md#metajson-原子写撞锁重试winerror-5-短退避2026-10-11随-v0346-发布)）。

## 实现：paths.repos_root()——启动时求值一次

`src/paths.py` 新增 `repos_root()`：

- settings.json 的 `repos_dir` **绝对路径才认**——相对 / 空 / 损坏值一律回退默认，不猜不兜
- 默认 `AGT_DIR/repos`；**进程启动时求值一次**（`session.REPOS_DIR` import 即快照）
- **运行中改设置不换根**（防半途撕裂：旧轮写旧根、新轮写新根）——**重启后生效**
- **只指路不搬家**：目录迁移由用户自理，函数不搬数据

## 唯一真源与消费方

`src/session.py`：`REPOS_DIR = repos_root()`——**唯一真源**。所有框架内消费方（config / log / rag / session_vec / spec_tools 的 `_specs_dir` 等）都是 `from session import REPOS_DIR`，改一处全跟随。

**外置件例外**：`src/assets/tools_builtin/cache_tools.py`（cache_breakpoint 全局扫最近活跃投影）受「外置件不 import src」约定限制——**同款轻量复制**：读 settings.json 的 repos_dir（绝对路径优先），否则 `_base/repos`，与框架侧语义对齐。

## 入口与命令

| 入口 | 行为 |
|---|---|
| 设置页「repos 数据根重定向」输入框 | 走 `/config` 同一通道 |
| `/config repos_dir D:\agt_data\repos` | 校验绝对路径 → `mkdir(parents=True)` → 写 settings.json → 回执提示重启生效 |
| `/config repos_dir`（查询） | 只读展示当前生效根（get_config 侧只读展示，不可运行时切换） |

`apply_config` 特殊段（src/commands.py，与 dump_projections 同款双写形态）：**相对路径拒绝保存**——回执「❌ repos_dir 需绝对路径（未保存，原值不变）」。

## 迁移流程（用户自理）

1. 停实例
2. robocopy / cp 搬 `repos/` 整棵（sessions / specs / plans / memories / images / rag / render_chars.db 等）
3. 设置新路径（设置页或 `/config repos_dir`）
4. 启动——新根生效；旧目录可留作备份

## 注意事项

- **重启生效**：运行中改设置不换根（启动时求值一次的代价，换防撕裂的确定）
- `AGT_HOME` / `AGT_DESKTOP` 解析的 AGT_DIR 不受影响——重定向只动 `repos/` 子树，models.json / settings.json / mcp.json 等仍住 AGT_DIR
- 本文同轮姊妹修复：dump_projections 子 Agent 跟随，见 [上下文引擎 · 投影转储后记](../architecture/context-engine.md#投影转储文件名与-t/s-标记commit-4aced81)

## 随 v0.35.0 发布（2026-10-16）

repos_dir + 同轮姊妹修复 dump_projections 子 Agent 跟随随 **v0.35.0** 进包（2026-10-16）。同版还有 [dashboard 多实例看板](dashboard.md) 与 [explore 投影层内插/断点续跑](explore-tool.md)。

## 相关页面

- [运维 · 存档布局](../guides/ops.md)——目录树总览（重定向后 `~/.agt/repos/` 读作 `<repos_dir>/`）
- [桌面版 · 数据目录唯一真源](../features/desktop-mode.md)——AGT_DIR 两级解析（本特性只动其下 repos 子树）
- [cache_breakpoint](../features/cache-tools.md)——外置件轻量复制同款读取逻辑
- [配置体系 · settings.json](../guides/config-and-models.md#settingsjson运行时)——repos_dir 键登记
