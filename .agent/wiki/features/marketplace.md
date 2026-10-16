# 插件市场（marketplace）· 官方精选索引 + /install 命令 + WebUI 市场页（2026-10-17，用户提案，commit 312599a）

## 职责


把随包分发的资产（工作流/子Agent声明/插件/技能/工具）做成【显式可控】的市场：

- **看得见清单**：策展索引 `src/assets/market/index.json`（repo 内维护，随 wheel 分发），34 条含 name/type/desc/version/tags/source/落位路径
- **装得上**：CLI `/install <name> [--force] [--global]`、HTTP `POST /api/marketplace/install`（`scope` 参数）、WebUI `/market` 页「安装」/「安装到全局」按钮
- **卸得掉**：`/uninstall <name>` / `POST /api/marketplace/uninstall` / WebUI 卸载按钮——只删索引声明的落位路径，不碰用户其它文件
- **两层可选**（2026-10-17 增补）：`workflow/skill/plugin` 支持 **repo 层**（`.agent/<dir>/`，随项目走）与 **全局层**（`~/.agt/<dir>/`，跨项目生效，与 982/985 轮 `seed_scope="global"` 播种位置同源）；`agent_decl/tool` 无全局约定（repo-only）

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

收录 34 条：17 工作流 + 9 子Agent（coder/coder-py/coder-cs/worker/explorer/reviewer/vision/desktop-operator/wiki-updater）+ 6 插件（cmd/feedback/open-folder/record/screenshot/webide）+ 1 技能（test-svc）+ 1 工具（ccs）。

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
```

- `--global` 仅对 workflow/skill/plugin 有效，其余类型返回 `[不支持] …（无全局层约定，仅支持安装到 repo）`
- 全局层落位：workflow→`~/.agt/workflows/`、skill→`~/.agt/skills/<name>/`、plugin→`~/.agt/plugins/<name>/`（目录型整目录复制，`install_dir` 在全局语义下忽略）
- `/marketplace` 分组标题对支持全局的类型标注「（支持 --global）」

### HTTP API（src/server.py）


| 端点 | 说明 |
|---|---|
| `GET /api/marketplace?type=` | 索引 + installed/available 运行时态，返回 `{updated,total,installed_count,items[]}`；每项带 `installed_scope`（`repo`/`global`/`both`/`""`） |
| `POST /api/marketplace/install` | `{name, force?, scope?}`（scope 默认 `repo`，可 `global`）→ `{ok,name,message}`；失败文案前缀 `[未找到]/[缺失]/[不支持]/[参数]` → `ok:false` |
| `POST /api/marketplace/uninstall` | `{name, scope?}`（省略/空 = 卸实际已装层；`repo`/`global` 只卸指定层）→ 同上 |
| `GET /market` | WebUI 市场页（market.html） |

幂等语义：重复安装返回 `ok:true` + 「⏭ 已安装（未动）」（幂等成功，不报错）。

### WebUI（src/static/market.html，路由 /market）


分类 tab（全部/工作流/子Agent/插件/技能/工具）+ 搜索框（名称/描述/标签）+ 条目卡片（类型徽章/版本/标签/落位路径）+ 动作按钮；toast 反馈 + `load()` 刷新重渲染。

装态徽章按 `installed_scope` 三态着色：`✓ 已装·repo`（绿）/`✓ 已装·全局`（蓝）/`✓ 已装·双层`（紫）；落位路径行对全局态追加「｜已装于 ~/.agt」（both 为「｜repo + ~/.agt」）。

动作按钮随装态展开（`actions(x)`，`GLOBAL_TYPES = [workflow, skill, plugin]`）：

| 装态 | 按钮 |
|---|---|
| 未装（支持全局类型） | 「安装」（repo，主色）+「安装到全局」 |
| 未装（tool/agent_decl） | 「安装」 |
| 已装·repo | 「重装」+「+ 全局」+「卸载」 |
| 已装·全局 | 「重装」+「+ 项目」+「卸载」 |
| 已装·双层 | 「重装」（作用于 repo 层）+「卸载」（两层都卸） |

「重装/卸载」均带 confirm，文案标明作用层（repo 工作区 / 全局层 ~/.agt / 两层）；覆盖重装提示"可能丢失本地修改"。

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

- **src/market.py**：新增 `_GLOBAL_DIRS = {"workflow": "workflows", "skill": "skills", "plugin": "plugins"}` 与 `_GLOBAL_ROOT = Path.home()/".agt"`（可注入，测试用）；`installed_scope(item, workspace)` 双层检测返回 `repo/global/both/""`，`item` 透出该字段；`install(name, force, workspace, scope="repo")` 全局分支统一由本模块 `shutil.copytree/copy2` 复制（**不走 download_asset**——它固定落 workspace）；`uninstall` 按 `installed_scope` 对实际存在的层逐个清理（both 两层都卸），嵌套空目录清理**止步 `~/.agt/<dir>`**（不越界删 `~/.agt`）
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

## 注意事项

- e2e 期间抓出一个 API 缺陷并当场修复：install/uninstall handler 起初对 `[未找到]` 失败文案也返回 `ok:true`，前端红 toast 路径死路——已按文案前缀判 ok
- `market.install` 的失败约定：`[未找到]`（索引无名）/`[缺失]`（随包源文件缺，wheel 安装不全可诊断）；改动文案表时同步 server.py 的 startswith 判据
- installed 检测 = 声明的全部路径存在（目录型指向主文件更稳，如 `.agent/agents/coder/coder.yml`）
- 卸载只信索引声明的路径；索引里写错 installed 路径 = 装态永远 False / 卸载删错目标，改索引需连带核对 file 与 installed 的一致性
- pyproject package-data 已含 `assets/market/*.json`、`assets/tools/*.py`（ccs 随包源）——新策展条目若引入新的 assets 子目录记得同步打包清单

## 相关页面

- [施工委托](features/construction-delegation.md)——本特性是委托施工线案例（plan p_1945c5a0）
- [download 通道](features/agent-watch.md)——文件型安装复用 download_asset 落位语义
- 插件播种家族：[record](features/record-plugin.md)、[feedback](features/feedback.md)、[cmd](features/cmd-command.md)——市场收录的随包插件
