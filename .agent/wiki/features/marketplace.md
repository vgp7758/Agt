# 插件市场（marketplace）· 官方精选索引 + /install 命令 + WebUI 市场页（2026-10-17，用户提案，commit 312599a）

## 职责

把随包分发的资产（工作流/子Agent声明/插件/技能/工具）做成【显式可控】的市场：

- **看得见清单**：策展索引 `src/assets/market/index.json`（repo 内维护，随 wheel 分发），35 条含 name/type/desc/version/tags/source/落位路径
- **装得上**：CLI `/install <name> [--force] [--global]`、HTTP `POST /api/marketplace/install`（`scope` 参数）、WebUI `/market` 页「安装」/「安装到全局」按钮
- **卸得掉**：`/uninstall <name>` / `POST /api/marketplace/uninstall` / WebUI 卸载按钮——只删索引声明的落位路径，不碰用户其它文件
- **两层可选**（2026-10-17 增补）：`workflow/skill/plugin` 支持 **repo 层**（`.agent/<dir>/`，随项目走）与 **全局层**（`~/.agt/<dir>/`，跨项目生效，与 982/985 轮 `seed_scope="global"` 播种位置同源）；`agent_decl/tool` 无全局约定（repo-only）
- **搬得动**（2026-11-27 增补）：已装插件 **repo ⇄ 全局**层间转移（移动非复制，`market.move_plugin`）——市场页「⇪ 移到全局」/「⇩ 移到项目」、`/plugins move <name> [--to repo] [--force]`、`agt plugin move …`；不在索引的自建插件以「本地」条目入清单（仅可转移）

与 seed（asset_sync / seed_default_skills）的分工：seed 是【隐式自动】首启播种；market 是【显式可控】市场。两者都"已存在不覆盖"。

## 索引条目 schema（index.json）

```json
{
  "name": "ccs", "type": "tool",
  "desc": "…", "version": "1.0", "tags": ["…"],
  "source": "builtin",
  "file": "tools/ccs.py",          // 随包源（相对 site-packages/src/，str|list）
  "dir": null,                      // 目录型资产（插件/技能）与 file 二选一
  "install_dir": ".agent/tools",   // workspace 相对落位目录
  "installed": ".agent/tools/ccs.py" // 已装检测路径（str|list，全部存在才算已装）
}
```

收录 35 条：17 工作流 + 9 子Agent（coder/coder-py/coder-cs/worker/explorer/reviewer/vision/desktop-operator/wiki-updater）+ 6 插件（cmd/feedback/open-folder/record/screenshot/webide）+ 2 技能（test-svc / desktop-ops）+ 1 工具（ccs）。

## 安装通道（src/market.py）

双通道按条目形态自动分流：

- **文件型且 manifest.json 唯一名**（17 条工作流、ccs）→ 复用 `download.download_asset`，落位/"已存在不覆盖"/`--force` 语义与 `/download` 完全一致
- **多文件（子Agent md+yml）与目录型（插件/技能）**→ market 模块统一复制，同语义

卸载清理**止步 install_dir 根目录**：删除落位文件/目录后，父级变空的嵌套目录一并清理，但 install_dir 本身（如 `.agent/tools/`）保留空壳——不越界删可能装有其它东西的落位根。

## 三个消费面

### CLI（src/commands.py）

```
/marketplace [--type T]              全部条目按类型分组 + 已装徽章（✅已装·repo / 已装·全局 / 已装·双层）
/install <name> [--force] [--global] 安装（已存在不覆盖；--force 覆盖重装；--global 落 ~/.agt 全局层）
/uninstall <name>                    卸载（卸 installed_scope 实际存在的层；both 两层都卸）
/plugins move <name> [--to repo] [--force]  已装插件层间转移（默认 → 全局；成功后自动热重载）
```

- `--global` 仅对 workflow/skill/plugin 有效，其余类型返回 `[不支持] …（无全局层约定，仅支持安装到 repo）`
- 全局层落位：workflow→`~/.agt/workflows/`、skill→`~/.agt/skills/<name>/`、plugin→`~/.agt/plugins/<name>/`（目录型整目录复制，`install_dir` 在全局语义下忽略；全局层根实为 `paths.AGT_DIR`，缺省 `~/.agt`）
- `/marketplace` 分组标题对支持全局的类型标注「（支持 --global）」；清单末尾含不在索引的**本地插件**，用法行提示 `/plugins move` 转移入口（2026-11-27）

### HTTP API（src/server.py）

| 端点 | 说明 |
|---|---|
| `GET /api/marketplace?type=` | 索引 + installed/available 运行时态，返回 `{updated,total,installed_count,local_count,items[]}`；每项带 `installed_scope`（`repo`/`global`/`both`/`""`）；末尾合并不在索引的本地插件项（`local:true`，计入 `total` 与 `local_count`，2026-11-27） |
| `POST /api/marketplace/install` | `{name, force?, scope?}`（scope 默认 `repo`，可 `global`）→ `{ok,name,message}`；失败文案前缀 `[未找到]/[缺失]/[不支持]/[参数]` → `ok:false` |
| `POST /api/marketplace/uninstall` | `{name, scope?}`（省略/空 = 卸实际已装层；`repo`/`global` 只卸指定层）→ 同上 |
| `POST /api/marketplace/move` | `{name, to?, force?}`（to 默认 `global`，可 `repo`）→ `{ok,name,message}`；失败前缀 `[未找到]/[缺失]/[不支持]/[参数]/[已存在]/[失败]` → `ok:false`（`[已存在]` 另带 `conflict:true`）；成功且 `_agent` 在场 → 自动热重载，摘要首行以 `🔁 ` 附进 message（2026-11-27） |
| `GET /market` | WebUI 市场页（market.html） |

幂等语义：重复安装返回 `ok:true` + 「⏭ 已安装（未动）」（幂等成功，不报错）；转移的「无需转移」同样以 `⏭ ` 开头按成功处理。

### WebUI（src/static/market.html，路由 /market）

分类 tab（全部/工作流/子Agent/插件/技能/工具）+ 搜索框（名称/描述/标签）+ 条目卡片（类型徽章/版本/标签/落位路径）+ 动作按钮；toast 反馈 + `load()` 刷新重渲染。计数行「官方精选 N 项 · 本地 M 个 · 已装 K」（M=0 隐藏本地段，2026-11-27）。

装态徽章按 `installed_scope` 三态着色：`✓ 已装·repo`（绿）/`✓ 已装·全局`（蓝）/`✓ 已装·双层`（紫）；落位路径行对全局态追加「｜已装于 ~/.agt」（both 为「｜repo + ~/.agt」）。

动作按钮随装态展开（`actions(x)`，`GLOBAL_TYPES = [workflow, skill, plugin]`；`⇪/⇩` 为 2026-11-27 增补的层间转移按钮 `moveBtns(x)`，仅 `type=plugin` 且非双层时出现）：

| 装态 | 按钮 |
|---|---|
| 未装（支持全局类型） | 「安装」（repo，主色）+「安装到全局」 |
| 未装（tool/agent_decl） | 「安装」 |
| 已装·repo | 「重装」+「+ 全局」+「⇪ 移到全局」（plugin）+「卸载」 |
| 已装·全局 | 「重装」+「+ 项目」+「⇩ 移到项目」（plugin）+「卸载」 |
| 已装·双层 | 「重装」（作用于 repo 层）+「卸载」（两层都卸）；不给转移按钮 |
| 本地插件（不在索引） | 仅转移按钮；双层时显示「双层已装（无需转移）」——无安装/重装/卸载 |

「重装/卸载」均带 confirm，文案标明作用层（repo 工作区 / 全局层 ~/.agt / 两层）；覆盖重装提示"可能丢失本地修改"。转移按钮的 confirm 写明「这是移动：源目录将被移除」；目标层同名 → conflict 二次确认 → 带 `force` 重发（`movePlugin()`）。「+ 全局」与「⇪ 移到全局」的区别靠 title 说明：前者装随包官方版（不含 repo 修改），后者把 repo 这份整体搬过去。

## 验证（2026-10-17 施工轮，plan p_1945c5a0 步骤 5）

- **单测**：test/test_market.py 15 例全过（索引 schema/装态检测/双通道安装/幂等/force/卸载/越界防护/API 三端点）
- **CLI e2e**（临时 workspace）：文件型 download_asset 通道、目录型复制、多文件型 md+yml、重复装跳过、force 覆盖（篡改后被还原）、卸载后 installed 全翻回 False、install_dir 空壳保留、install_dir 外用户文件绝不被碰
- **HTTP e2e**（真 server + Playwright 页面）：GET 34 条 + type 过滤；POST install 落位+装态翻转；WebUI 点「安装」→ toast + 徽章 + 按钮态翻转 + 文件落位（rerank_topk 实测）；点「卸载」→ 还原；未知名/缺参 `ok:false`
- **重启往返**：stop+start server 后 GET installed 态与重启前完全一致（installed 以文件存在性为源，天然持久）

## 后记：主 WebUI 入口缺失——fabDock 🛒 补位（2026-10-16，用户实锤，commit 2a93c96）

**用户实锤**：施工交付了完整的 `/market` 独立页（路由 + market.html + API 三件），但主 WebUI（index.html）里**一个引用都没有**——入口断裂，用户只能手敲 URL。

**修复（src/static/index.html，commit `2a93c96`）**：`fabDockMenu` **首位**插入 🛒 按钮（`#marketFab`，`fabItem` 类与既有 dock 按钮同款）——`onclick` 新标签打开 `/market`；纯 HTML 静态插入，零 JS 依赖、零 SDK 时序问题，分列随 [fabDock 自动分列](fab-dock.md) 自收纳。

**生效两段式**：按钮改的是 index.html——mtime 热更，**浏览器刷新即见**；`/market` 路由在引擎侧（server.py）——**随 `/restart` 生效**（此前点击 404）。

**教训**：独立页交付 ≠ 可达——「入口在主界面」是交付验收的一部分，新页面落成时应同步检查主 WebUI 的入口面（fabDock / 抽屉 / 命令）。

## 后记二：双层装态（repo + ~/.agt 全局层）——检测漏全局 & 无全局通道（2026-10-17，用户实锤，commit 8e30b3f）


**用户实锤的两个缺口**（首版交付后一天）：①市场把 11 个全局类工作流全显示"未安装"——它们由 `seed_scope="global"` 播种在 `~/.agt/workflows/`，但 installed 检测只扫 repo 层（`.agent/...`），点"安装"还会重复装一份到 repo；②根本没有"安装到全局"的通道——`install_dir` 是 workspace 相对路径，安装只会落 repo。

**改法（三通道同步）**：

- **src/market.py**：新增 `_GLOBAL_DIRS = {"workflow": "workflows", "skill": "skills", "plugin": "plugins"}` 与 `_GLOBAL_ROOT = Path.home()/".agt"`（可注入，测试用；2026-11-27 起改为 `None` + `_global_root()` → `paths.AGT_DIR`，见后记三）；`installed_scope(item, workspace)` 双层检测返回 `repo/global/both/""`，`item` 透出该字段；`install(name, force, workspace, scope="repo")` 全局分支统一由本模块 `shutil.copytree/copy2` 复制（**不走 download_asset**——它固定落 workspace）；`uninstall` 按 `installed_scope` 对实际存在的层逐个清理（both 两层都卸），嵌套空目录清理**止步 `~/.agt/<dir>`**（不越界删 `~/.agt`）
- **src/commands.py**：`/install <name> [--force] [--global]`；`/marketplace` 徽章三态 + 分组标题标「（支持 --global）」
- **src/server.py**：install API 接 `scope`（默认 repo）；uninstall API 接 `scope`（省略=卸实际已装层）；`ok` 判据扩到 `[未找到]/[缺失]/[不支持]/[参数]` 四前缀
- **src/static/market.html**：徽章三态着色 + 按钮组随装态展开（见上节）
- **test/test_market.py**：+9 例 → 24 例（双层检测 / scope 映射 / 全局文件装卸往返 / 目录型全局装卸 + 嵌套清理 / `--global` 拒装无全局约定类型 / API scope 传参与非法 scope / 未装条目不误报）

**验证（2026-10-17 增补轮）**：

- 单测 24/24 过（含原 15 例回归）
- **现实核对（缺口① 直接验证）**：真实环境跑 `marketplace_items()`，本机 `~/.agt/workflows/` 的 11 个播种工作流全部识别 `installed_scope="global"`（此前全显示未装）
- **CLI 往返**（真实 `~/.agt` + 临时 workspace 隔离 repo 层）：`/install custom_tool_demo --global` → 落 `~/.agt/workflows/` → scope=global → `--force` 覆盖重装 → `/uninstall` → 文件与 scope 双双还原，`~/.agt/workflows/` 回到初始 11 个；目录型 `test-svc --global` → `~/.agt/skills/test-svc/SKILL.md` → 卸载后已有全局技能（pbridge/yangzi-aistudio）不受影响
- **WebUI e2e**（真 server + Playwright，临时 workspace 构造未装态）：徽章/按钮四态全对 → 点「安装到全局」→ toast + 徽章翻「已装·全局」+ 真实文件落位 → 点「+ 全局」（repo→both）→ 徽章「已装·双层」+ 路径注「repo + ~/.agt」→ 点「卸载」→ 两层全清 + 卡片回未装态（[安装][安装到全局]）；tool 型（ccs）只出「安装」一个按钮

**生效（两段式，同 🛒 先例）**：`market.html` 是静态文件——浏览器刷新即见；`market.py`/`commands.py`/`server.py` 在引擎侧——**需 /restart 生效**（未重启的常驻实例跑旧代码，响应无 `installed_scope` 字段，市场页仍是旧行为）。

**设计取舍**：全局层不做 `install_dir` 映射（按类型定目录，简单可预期）；不引入"全局优先/遮蔽"语义——两层同时存在即 both，卸载一并清；`agent_decl/tool` 明确 repo-only（避免为两种类型发明未经验证的全局约定）。

**教训**：资产有多个落位层时，**检测/安装/卸载三处必须对同一份层映射表取真**——首版只实现 repo 层就让整个市场对全局播种资产"瞎"了，而这种"瞎"在只看 repo 的测试里完全测不出来（需要拿真实 `~/.agt` 或注入全局根核对）。

## 后记三：已装插件层间转移（repo ⇄ 全局）+ 本地插件入清单（2026-11-27，施工牌 p_a8823fda，commit cc9ed46）

**缺口**：后记二补齐了「两层各装一份」，但插件装错了层只能手动拷目录——引擎发现本就支持两层（repo 同名覆盖全局），缺的只是「把已装这份搬过去」的入口；且市场只列索引条目，自己写进 `.agent/plugins/` 的插件（不在索引）在市场上**根本看不到**。

### 三入口（同一实现 `market.move_plugin`）

| 入口 | 用法 | 热重载 |
|---|---|---|
| WebUI `/market` | 已装·repo 卡片「⇪ 移到全局」/ 已装·全局「⇩ 移到项目」；双层不给 | 端点成功后 `reload_plugins`，摘要首行以 `🔁 ` 附进 message |
| 会话命令 | `/plugins move <名> [--to repo] [--force]`（默认 → 全局） | 返回 `✅` 才重载；无 agent 提示「重启或 /reload plugins」 |
| CLI | `agt plugin move <名> [--to repo] [--force]` | 无 agent 不重载（提示重启 / `/reload plugins`）；`✅`/`⏭` 且无 ⚠️ → 退出码 0，否则 1 |

- **移动不是复制**：源目录移除，repo 里的本地修改随行带到目标层——与「+ 全局」的区别：后者从**随包官方版**安装、不含 repo 修改（按钮 title 已注明）
- **冲突**：目标层同名且非 force → `[已存在]`；端点返回 `conflict:true`，页面二次确认后带 `force` 重发

### 安全移动流程（src/market.py `move_plugin`）

1. `_scan_plugin_dirs` 两层扫描定位源：两层都没有 → `[未找到]`；源层没有 → `⏭ 无需转移`
2. **两层同一目录拒绝**（`AGT_HOME` 落在 `<ws>/.agt` 这类配置下 repo 层 = 全局层，移动 = 自删）→ `⏭`
3. 清同名历史残留 `.<名>.moving-*`（进程曾中断在复制与就位之间）
4. `copytree` → **目标层落位目录下的临时名** `.<名>.moving-<pid>`（与落位同盘 `os.replace` 才原子——跨盘会 EXDEV；忽略 `__pycache__`/`*.pyc`）→ 校验 `manifest.yml` 存在且 `_plugins._parse_manifest` 可解析
5. force：旧目标**原子挪开**到 `.<名>.moving-old-<pid>`（不直接删——删一半失败会留残缺目录）→ `os.replace` 新副本就位 → 清旧
6. 就位 → 删源；删源失败如实提示 ⚠️（此时两层都在，repo 级仍生效，手动删）

失败恢复：就位前任一步异常 → 清临时目录、`[失败]` 前缀、**源保持原样**；旧目标已挪开而新副本就位失败 → 把旧的放回去。`move_plugin` 是纯文件操作不 reload（CLI 无 agent 也用它），热重载归调用方（端点 / 会话命令）。

### 本地插件入清单

`marketplace_items()` 末尾合并 `_scan_plugin_dirs` 里**不在索引**的插件：`source=local`、`local:true`、`tags:["本地"]`、`installed_scope` 按层；与索引同名的去重（索引条目已表达）。`GET /api/marketplace` 新增 `local_count`（`total` 含本地项）；页面计数「官方精选 N 项 · 本地 M 个 · 已装 K」（M=0 隐藏本地段）。本地插件卡片**只有转移按钮**（无随包源：无安装/重装/卸载——卸载通道依赖索引声明路径，删除请手动删目录），双层时显示「双层已装（无需转移）」。`_scan_plugin_dirs` 有意不调 `plugins.discover`——后者只返回覆盖后的一份，市场要两层完整视图。

### 全局层根收编 paths.AGT_DIR

- `market._GLOBAL_ROOT`：后记二的 `Path.home()/".agt"` 常量 → `None` + `_global_root()`（测试注入优先，否则 `paths.AGT_DIR`），模块内引用全替换
- `plugins.global_plugins_dir()`（`paths.AGT_DIR / "plugins"`）成为全局插件目录**唯一真源**：`discover()` 与 CLI `install` 的硬编码改用它
- 效果：`AGT_HOME` 隔离环境（测试 / 桌面版数据根）下全局层自动跟随，不再写死 `~/.agt`——[数据目录唯一真源 paths.py](desktop-mode.md)那次收编的补漏

### 交付复核：worker 主流程 + 主 Agent 加固 4 处

worker（施工牌 p_a8823fda）交付三入口主流程 + 8 例单测 + 隔离实例 / Playwright 实测；主 Agent 读 diff 复核后加固（各配单测）：① force 覆盖不直接删旧目标（流程第 5 步）② 两层同目录拒绝（第 2 步）③ 中断残留 `.<名>.moving-*` 不被当作插件——否则市场误报「双层已装」（`_scan_plugin_dirs` 跳过点开头目录，第 3 步顺手清）④ 就位后删源失败如实报告（此前笼统报 `[失败] 源保持原样`，实际目标层已就位）。

### 验证

- `test/test_market.py` **36 例**全过（原 24 + worker 8 + 加固 4：同层拒绝 / 残留跳过与清理 / 就位失败恢复旧目标 / 删源失败报告），本机真实文件系统，含 TestClient 直打端点与热重载分支（FakeAgent，`🔁` 断言）
- worker：隔离冒烟四链路（`global_plugins_dir` / `discover` / `_global_root()` / `agt plugin move`）+ L2 隔离实例 + Playwright 点按钮（含冲突二次确认）+ CLI 实测
- 加固后 force 路径**未再跑浏览器**——由单测覆盖（端点 force 重发、就位失败恢复旧目标）

### 注意事项

- **生效 `/restart`**：`market.html` 静态刷新即见新按钮，但 move 端点在引擎侧——未重启点按钮报「转移失败」
- 双层同名（both）不给转移按钮——repo 覆盖全局，先卸一层
- 已提交进 git 的插件转移后仓库显示为删除（不再随这个 repo 走）——预期行为
- 施工牌写的 `test_plugins` 并不存在（全 repo 无此文件）——插件回归靠函数级冒烟 + test_market 内 `_parse_manifest` 依赖用例
- 失败前缀比 install/uninstall 多 `[已存在]`（冲突）与 `[失败]`（中断）——改文案表时同步 server.py `fail_prefixes`

引擎侧落点（`global_plugins_dir()` 单源 + CLI `move` 子命令）见 [插件 SDK](../architecture/plugin-sdk.md)。

## 注意事项

- e2e 期间抓出一个 API 缺陷并当场修复：install/uninstall handler 起初对 `[未找到]` 失败文案也返回 `ok:true`，前端红 toast 路径死路——已按文案前缀判 ok
- `market.install` 的失败约定：`[未找到]`（索引无名）/`[缺失]`（随包源文件缺，wheel 安装不全可诊断）；改动文案表时同步 server.py 的 startswith 判据
- installed 检测 = 声明的全部路径存在（目录型指向主文件更稳，如 `.agent/agents/coder/coder.yml`）
- 卸载只信索引声明的路径；索引里写错 installed 路径 = 装态永远 False / 卸载删错目标，改索引需连带核对 file 与 installed 的一致性
- pyproject package-data 已含 `assets/market/*.json`、`assets/tools/*.py`（ccs 随包源）——新策展条目若引入新的 assets 子目录记得同步打包清单

## 相关页面

- [施工委托](construction-delegation.md)——本特性是委托施工线案例（plan p_1945c5a0；层间转移为 plan p_a8823fda）
- [download 通道](tool-externalization.md)——文件型安装复用 download_asset 落位语义（`download_tools.py` 外置件 / 框架 `src/download.py`）
- 插件播种家族：[record](record-plugin.md)、[feedback](feedback.md)、[cmd](cmd-command.md)——市场收录的随包插件
- [插件 SDK](../architecture/plugin-sdk.md)——全局插件目录单源 `global_plugins_dir()` + CLI `move` 子命令（层间转移的引擎侧落点，2026-11-27）
- [数据目录唯一真源 paths.py](desktop-mode.md)——`AGT_DIR` 两级解析；市场全局层根现同源

