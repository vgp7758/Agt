# 桌面版 · --desktop 窗口模式 + 数据目录统一 + PyInstaller 打包（spec s_d53311f8，2026-09-10，commit 6c2efce）

## 职责

把 Agt 从「浏览器交互的 Web 服务」升级为「双击即用的桌面应用」：`agt-web --desktop` 用 pywebview 弹系统 WebView 窗口（Win: WebView2 / mac: WKWebView / Linux: gtkwebkit，非 Electron）；配套数据目录迁移（桌面模式进 Windows AppData 惯例位置）与 PyInstaller 打包基建（onedir 自带 Python 运行时，下载即用）。pip 用户走 Step 1（窗口模式）；分发走 Step 2（打包）。

## 用法

```bash
pip install agt-agent[desktop]   # 装 pywebview extras（pyproject: desktop = ["pywebview>=5.0"]）
agt-web --desktop                # 桌面窗口模式（非浏览器）
```

pywebview 缺失时 web_desktop 给出安装指引后退出；`is_desktop()` 检测当前是否桌面模式（env `AGT_DESKTOP=1` 或 pywebview 已装 + `--desktop` 分支进入）。

## 零侵入接入：chat.py 两个分支点（web_desktop.py）

web_main 原有两条路径（`open_browser(port)` / `_render_loop(...)`）各加一个桌面分支，其余全部复用：

| 原有 | 桌面分支 | 语义 |
|---|---|---|
| `open_browser(port)` | `web_desktop.open_window(port)` | 注册窗口（立即返回），WebView 异步加载 |
| `_render_loop(agent, event_q, worker, state, work_q, interactive=False)` | `web_desktop.run_loop()` | `webview.start()` 阻塞至窗口关闭 |

**窗口关闭 = 优雅退出零新代码**：run_loop 返回后走 web_main 现有 finally 链（work_q None → worker join → stop_server → agent.shutdown → mcp shutdown）；正在跑的轮由 session 中断轮防御（t150/t272）在读档时兜底恢复。

**/restart 看门狗重启分支**：desktop 模式端口退让 `pick_port(port)`（占用→+1）后，`AGT_RESTART_SESSION/MESSAGE` env 存在时**不开新窗口**（用户已有窗口会自动重连——与 Web 模式跳过 open_browser 同款语义，见 [user-interaction · /restart 重启双坑](user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc)）；非重启场景才 open_window。

实现细节：开窗与运行分离（open_window 注册 → run_loop 阻塞），窗口关闭事件接回主循环；单实例锁（pid 存活 + 进程名比对防 pid 复用）。

## 数据目录唯一真源：paths.py（Step 2，七处收编）

`src/paths.py` 提供全局解析（唯一真源），三级：

```
AGT_HOME env（测试/多实例） > 桌面模式 %APPDATA%\Agt > ~/.agt（默认）
```

- 桌面打包形态由入口设 `AGT_DESKTOP=1` → 数据进 AppData（Windows 惯例）；测试/多实例用 `AGT_HOME` env 覆盖
- **存量迁移**：首次桌面启动新目录为空且旧 `~/.agt` 存在 → 整体复制（旧目录保留不删，回滚不丢数据）
- **七处独立定义收编**：此前 `Path.home()/".agt"` 散落在 config / session / lsp_manager / restart_watchdog / spec_tools / updater / feedback 各自定义——桌面模式数据目录迁移前**必须**统一，否则数据分裂三处。现全部改 `from paths import AGT_DIR`（或 `AGT_DIR / "..."` 拼子目录）
- config.py 的迁移逻辑也移入 paths.py：config 不再自持目录解析，**避免 session→config 循环 import**
- 外置工具（`assets/tools_builtin`，无 src 可 import）各自轻量复制三级逻辑（同款语义），如 cache_tools.py

## 打包基建：packaging/Agt.spec + desktop_entry.py（Step 2）

### Agt.spec（PyInstaller onedir）

- 构建：`pyinstaller packaging/Agt.spec --noconfirm` → 产物 `dist/Agt/`
- datas：`src/static`（WebUI 全套 html）+ `src/assets`（装配/workflows/agents/nodes_builtin/presets）
- 排除 tkinter / matplotlib / upx，隐藏 imports 收集子模块
- 发布：`release.py --desktop`（打包 → zip → GitHub Releases upload）

### desktop_entry.py（PyInstaller Analysis 入口）

- **workspace 锚定 exe 旁**：`Path(sys.executable).parent` 作为 cwd 基线——防快捷方式启动时 cwd 歧视（工作区相对路径解析不到）
- **`--pyrun` 子进程分流**：PyInstaller 下 `sys.executable` = Agt.exe 本体，直接 spawn Python 子进程会 **GUI 套娃**（Agt.exe 再拉 Agt.exe）——real_tools 三处 spawn 点（run_python / 相关子进程工具）已接 `_py_child_cmd`：打包形态用 `sys.executable --pyrun` 分流到一个纯子进程 stub，源码形态直接 `sys.executable`

## 验证状态

Step 1+2 施工完成并推送（commit 6c2efce）；PyInstaller 全量打包后台进行中，完成后做干净目录验证。Step 3（首启向导 + 应用内更新检查）、Step 4（图标/文档/SmartScreen 教学）待打包验证通过后继续。

## 相关页面

- [系统总览](../architecture/overview.md) — 模块地图（服务层 chat.py / 配置层 config.py 的桌面配套）
- [运维 · 存档布局](../guides/ops.md#存档布局paths-py-三级解析--默认-agt-repos) — 数据目录三级解析落地后存档根随 AGT_DIR 走
- [用户交互 · /restart 重启双坑](user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc) — 重启不开新窗口同款语义
- [配置体系 · 配置文件解析 config_file](../guides/config-and-models.md) — repo 级覆盖与数据目录正交（路径解析归 paths.py）

