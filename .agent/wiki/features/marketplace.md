# 插件市场（marketplace）· 官方精选索引 + /install 命令 + WebUI 市场页（2026-10-17，用户提案，commit 312599a）

## 职责

把随包分发的资产（工作流/子Agent声明/插件/技能/工具）做成【显式可控】的市场：

- **看得见清单**：策展索引 `src/assets/market/index.json`（repo 内维护，随 wheel 分发），34 条含 name/type/desc/version/tags/source/落位路径
- **装得上**：CLI `/install <name> [--force]`、HTTP `POST /api/marketplace/install`、WebUI `/market` 页安装按钮
- **卸得掉**：`/uninstall <name>` / `POST /api/marketplace/uninstall` / WebUI 卸载按钮——只删索引声明的落位路径，不碰用户其它文件

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
/marketplace [--type T]     全部条目按类型分组 + ✓已装标记
/install <name> [--force]   安装（已存在不覆盖；--force 覆盖重装）
/uninstall <name>           卸载
```

### HTTP API（src/server.py）

| 端点 | 说明 |
|---|---|
| `GET /api/marketplace?type=` | 索引 + installed/available 运行时态，返回 `{updated,total,installed_count,items[]}` |
| `POST /api/marketplace/install` | `{name, force?}` → `{ok,name,message}`；`[未找到]/[缺失]` 失败文案 → `ok:false` |
| `POST /api/marketplace/uninstall` | `{name}` → 同上 |
| `GET /market` | WebUI 市场页（market.html） |

幂等语义：重复安装返回 `ok:true` + 「⏭ 已安装（未动）」（幂等成功，不报错）。

### WebUI（src/static/market.html，路由 /market）

分类 tab（全部/工作流/子Agent/插件/技能/工具）+ 搜索框（名称/描述/标签）+ 条目卡片（类型徽章/版本/标签/落位路径）+ 动作按钮：未装→「安装」；已装→「重装(confirm)/卸载(confirm)」+ ✓已安装徽章；toast 反馈 + `load()` 刷新重渲染。

## 验证（2026-10-17 施工轮，plan p_1945c5a0 步骤 5）

- **单测**：test/test_market.py 15 例全过（索引 schema/装态检测/双通道安装/幂等/force/卸载/越界防护/API 三端点）
- **CLI e2e**（临时 workspace）：文件型 download_asset 通道、目录型复制、多文件型 md+yml、重复装跳过、force 覆盖（篡改后被还原）、卸载后 installed 全翻回 False、install_dir 空壳保留、install_dir 外用户文件绝不被碰
- **HTTP e2e**（真 server + Playwright 页面）：GET 34 条 + type 过滤；POST install 落位+装态翻转；WebUI 点「安装」→ toast + 徽章 + 按钮态翻转 + 文件落位（rerank_topk 实测）；点「卸载」→ 还原；未知名/缺参 `ok:false`
- **重启往返**：stop+start server 后 GET installed 态与重启前完全一致（installed 以文件存在性为源，天然持久）

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
