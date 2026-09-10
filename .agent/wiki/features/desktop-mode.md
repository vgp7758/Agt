# 桌面版 · --desktop 窗口模式 + 数据目录统一 + PyInstaller 打包（spec s_d53311f8，2026-09-10，commit 6c2efce）

## 职责

把 Agt 从「浏览器交互的 Web 服务」升级为「双击即用的桌面应用」（spec s_d53311f8 · 四步全交付）：`agt-web --desktop` 用 pywebview 弹系统 WebView 窗口（Win: WebView2 / mac: WKWebView / Linux: gtkwebkit，非 Electron）；配套 PyInstaller 打包基建（onedir 自带 Python 运行时，下载即用）、**首启向导 + 应用内更新检查**（无 provider 配置自动弹 onboarding + GitHub Releases 版本横幅）、**图标 / Windows 版本资源 / SmartScreen 教学文档**。pip 用户走 Step 1（窗口模式）；分发走 Step 2-4（打包 + 首启体验 + 发版物料）。

**数据根：单一 `~/.agt`（用户裁定 2026-09-10，见 [数据目录唯一真源](#数据目录唯一真源paths-py单一数据根用户裁定-2026-09-10)）**——桌面版与 CLI/pip 版、多桌面实例、Launcher 打开的不同 workspace 全部读写同一份数据，与 VS Code 插件/CLI 共用 `~/.claude` 同构。曾按桌面惯例迁 `%APPDATA%\Agt` 并全量拷贝，已回退。

**瘦启动器（spec s_37494daf，2026-09-10 续）**：在四步之上加一层「先选工作区再拉起主程序」的入口——`Launcher.exe`（11.3MB 独立 onefile）双击选目录 → 以该目录为 workspace 启动 `Agt.exe`；直接双击 `Agt.exe` 仍进默认 workspace（两入口共存）。见 [瘦启动器 Launcher.exe](#瘦启动器-launcherexe先选工作区再拉起主程序spec-s_37494daf2026-09-10)。

**可多开（2026-09-10，用户裁定）**：单实例锁已退化为「窗口去重提示」——双击第二个 exe 会照常开窗（端口自动退让 8001/8002…），每个窗口各自 workspace / 规则 / 上下文隔离，仅共享 `~/.agt`。见 [多开支持](#多开支持单实例锁退化为窗口去重2026-09-10用户裁定)。

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

**端口选定必须在 `start_server` 之前（2026-09-10 六轮，用户双击实测 ERR_CONNECTION_REFUSED 修复）**：desktop 模式 `pick_port(port)`（占用→+1）**必须前置到 `start_server` 调用之前**——服务与窗口共用同一端口。若放在服务之后，`pick_port` 的 connect 探测会把**已监听的自己**判为"被占用" → 退让到 +1 端口 → 服务在 8000 而窗口指向 8001（无监听）→ `ERR_CONNECTION_REFUSED`（双击必现）。见 [施工中排掉的坑 · 8](#施工中排掉的坑)。

**/restart 看门狗重启分支（2026-09-10 六轮修订）**：

| 形态 | 重启后行为 | 理由 |
|---|---|---|
| 浏览器模式 | **不开新页签**（`AGT_RESTART_SESSION/MESSAGE` env 存在时跳过 open_browser） | 用户已有页签自动重连——手机触发重启时电脑端无端多开 tab 正是用户报告的困扰（见 [user-interaction · /restart 重启双坑](user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc)） |
| 桌面模式 | **必须 `open_window(port)` 重开窗口** | 旧窗口已随旧进程关闭，不重开则应用消失（此前误按浏览器语义跳过，属连带 bug） |

条件形态：`if _desk or not (AGT_RESTART_SESSION or AGT_RESTART_MESSAGE): open_window/open_browser`——env 在 `_recover_restart_env` 才 pop，此处仍在。

实现细节：开窗与运行分离（open_window 注册 → run_loop 阻塞），窗口关闭事件接回主循环；单实例锁（pid 存活 + 进程名比对防 pid 复用）——**2026-09-10 起语义改为「多开提示」不再 `sys.exit`**，见 [多开支持](#多开支持单实例锁退化为窗口去重2026-09-10用户裁定)。

## 数据目录唯一真源：paths.py · 单一数据根（用户裁定 2026-09-10）

**单一数据根：始终 `~/.agt`**（用户裁定 2026-09-10，回退桌面 AppData 方案）：

| 优先级 | 来源 | 用途 |
|---|---|---|
| 1 | `AGT_HOME` env | 测试 / 多实例隔离 |
| 2 | `~/.agt`（默认，**含桌面模式**） | 全部形态共用一份数据 |

- **为什么统一**（用户判断，与 Claude Code 同构）：VS Code 插件 / CLI / 各项目共用 `~/.claude`——差异在 **workspace**（打开哪个目录），不在数据根。桌面版若另立 `%APPDATA%\Agt`：① 每台把 Agt 装到别处的机器、**每次 CI 云构建**都要全量复制 GB 级存档；② 与 CLI 版数据**分叉**（在哪边干活，另一边的记忆/session 就"丢"）——`~/.agt` 下模型配置 / settings / 长期记忆 / 跨 repo 的 wiki·RAG 只需一份
- **回退与回收**：`resolve_agt_home()` 桌面分支不再返回 AppData，改为调 `_reclaim_legacy_appdata()`——把此前被迁出去的产物**搬回** `~/.agt`：**只搬缺失项、从不覆盖、`OSError` 静默**（回收失败不阻塞启动），覆盖 `models.json / settings.json / mcp.json / main.yml / models.py / repos / memories / logs / remote_instances.json` 九类，成功打印「📦 数据根统一：回收 … → `~/.agt`」
- **`%APPDATA%\Agt` 保留为系统侧产物**（不进 git 数据根）：Launcher 的 `recent_workspaces.json`（`packaging/launcher.py` `_data_dir()`）、桌面 `logs/desktop.log`（`desktop_entry._redirect_stdio()`）、`web_desktop` 单实例锁 `instance.lock`
- **七处独立定义收编**：此前 `Path.home()/".agt"` 散落在 config / session / lsp_manager / restart_watchdog / spec_tools / updater / feedback 各自定义——现全部改 `from paths import AGT_DIR`（或 `AGT_DIR / "..."` 拼子目录）。外置工具（`assets/tools_builtin`，无 src 可 import）各自轻量复制同款逻辑
- config.py 的目录解析也移入 paths.py：config 不再自持目录解析，**避免 session→config 循环 import**
- **版本号唯一真源同在此文件**（2026-09-10 二轮，平铺打包）：`VERSION = "0.26.4"`，`src/__init__.py` 反向 `from paths import VERSION as __version__` 保持 pip 侧一致；`server.py /api/latest` 也从 `import src` 改 `from paths import VERSION`（桌面平铺形态无 src 包）

**单测（四场景全过）**：桌面根=`~/.agt` 且不再外迁 ✅ / 回收搬回 models+repos ✅ / 目标已有不覆盖 ✅ / CLI 分支不触发回收 ✅。

**与 CLI 版可同时开**：同一 `~/.agt`、不同 workspace / 实例互不干扰；同 repo 同 session 才会撞（与多实例组网约束一致）。

**历史（已废弃方案，留作教训）**：曾按桌面惯例 `%APPDATA%\Agt` + 首启全量迁移 `~/.agt`，并因此踩过 [迁移判定被 launcher 预建目录短路](#迁移判定-bug目录存在--已初始化launcher-预建目录短路迁移2026-09-10--七轮)（迁移判定改看「用户数据存在性」）——该轮修复的 `_USER_DATA` 判据随本轮回退一并作废，但「目录存在 ≠ 已初始化」的通用教训仍成立。

## 打包基建：packaging/Agt.spec + desktop_entry.py（Step 2）

### Agt.spec（PyInstaller onedir）

- 构建：`pyinstaller packaging/Agt.spec --noconfirm` → 产物 `dist/Agt/`
- **平铺形态（spec 修 #1）**：`pathex=[str(SRC)]`（src/）→ 模块以**顶层名**收集（config/session/chat…），与 pip 运行时 `src/__init__.py` 的 sys.path hack（把 src/ 塞进 sys.path）同构——运行时代码里的裸 `import config` 才能在 PYZ 命中。首版 `pathex=仓库根` 是坑：模块以 `src.config` 命名空间收集，裸 import 找不到（见 [施工中排掉的坑 · 3](#施工中排掉的坑)）
- datas 同样**平铺到 `_internal/` 根**：`src/static → static`、`src/assets → assets`——`config.py` 的 `Path(__file__).parent/"assets"` 在 `_internal/assets` 命中
- 排除 tkinter / matplotlib / pytest / pip；隐藏 imports 收集子模块（uvicorn / webview / anyio + encodings 补全 + web_desktop + **workflow_node_api**——节点插件 `assets/nodes_builtin/*.py` 运行时 `_import_fresh` 动态加载、静态分析看不到其 import，须显式收集，spec 修 #3）
- **hookspath 覆盖社区 hook（spec 修 #2，2026-09-10 三轮）**：`hookspath=[ROOT/packaging/hooks]` 放空操作 `hook-workflow.py`（`hiddenimports = []`）——覆盖 pyinstaller-hooks-contrib 给**同名 PyPI 包 workflow** 准备的 hook（顶层模块 `workflow` 与本项目 `src/workflow.py` 平铺后同名冲突，社区 hook 的 import 会失败）；project hookspath 优先于社区 hooks
- 发布：`release.py --desktop`（打包 → zip → GitHub Releases upload）

### desktop_entry.py（PyInstaller Analysis 入口）

- **裸名导入**：平铺形态无 `src` 包 → `from chat import web_main`（不是 `from src.chat import`），与收集形态同构
- **workspace 锚定 exe 旁**：`Path(sys.executable).parent` 作为 cwd 基线——防快捷方式启动时 cwd 歧视（工作区相对路径解析不到）；首启自动创建 workspace/ 并 chdir
- **`--pyrun` 子进程分流**：PyInstaller 下 `sys.executable` = Agt.exe 本体，直接 spawn Python 子进程会 **GUI 套娃**（Agt.exe 再拉 Agt.exe）——real_tools 三处 spawn 点（run_python / 相关子进程工具）已接 `_py_child_cmd`：打包形态用 `sys.executable --pyrun <file>` 分流到纯子进程 stub（runpy 直接执行目标文件，env 打 `AGT_FROZEN_CHILD=1`），源码形态直接 `sys.executable`
- **`--selftest` 产物自检（用途 = CI/发布前门禁，2026-09-10 十六轮去 GUI 化）**：`Agt.exe --selftest` ①**全模块 import 链**（config / paths / session / chat / server / workflow_node_api / **workflow / multiagent / llm_client / tools**，逐个 `__import__`，逐个 try）②**资源就位 6 项**（static/index.html + agents.html + assets/models.preset.json + nodes_builtin + tools_builtin + manifest.json）③**节点插件动态加载自检**（走 `_import_fresh` 真实加载 `assets/nodes_builtin` 全部插件——判定 **`n_ok > 0`**，**逐个 try** 单个插件炸不再淹没整组）④**外置工具脚本 `py_compile` 校验**（`assets/tools_builtin/*.py`，与节点插件同属「随包动态加载」一类）→ 逐项 ✅/❌ 打印 + `SELFTEST_PASS/FAIL` 退出码（所有 print 包 try，输出编码问题不再让自检本身崩）+ **明细落文件 `cwd/selftest_result.txt`**（GUI 子系统 exe 在 CI 管道下 stdout 曾只剩末行，文件是确定性通道——2026-09-10 · 十七轮，见 [selftest 明细落文件](#selftest-明细落文件--门禁改-start-process-重定向2026-09-10--十七轮commit-待推)）。
  - **不 import 任何 GUI 依赖模块**（关键约束）：`web_desktop`（pywebview）已从清单移除——CI runner 无桌面会话时 pythonnet/.NET 初始化会**挂住不返回**，导致 selftest 步骤永不结束 → job 被取消。真产物的问题（模块漏收集 / 资源缺 / 插件加载失败）照样全暴露，但**永远不会吊死流水线**。详见 [selftest 去 GUI 化](#selftest-去-gui-化ci-挂死根因与双层修复2026-09-10-十六轮)
- **`_redirect_stdio()`（2026-09-10 五轮，commit c9bd925）**：GUI（`console=False`）无控制台时 `sys.stdout/stderr` 为 None——uvicorn `DefaultFormatter.__init__` 调 `isatty()` 启动即崩（`Unable to configure formatter 'default'`）。进 `web_main` 前把 None 的 stdio **重定向到数据目录 `logs/desktop.log`**（spec「日志写文件」落地 + 桌面版排障第一现场），stdin 兜 devnull（见 [施工中排掉的坑 · 7](#施工中排掉的坑)）

## Step 3：首启向导 + 应用内更新检查（server.py / index.html）

### first_run 自动弹 onboarding（server.py + index.html）

- `_first_run = not config.MODELS`：WS 连接时无任何 provider 配置 → true（首启向导触发条件）。连接/重连 system 消息携带 `first_run` 字段（与 `models` / `preset` / `current_model` 四字段同时补全，两条路径一致）
- 前端：`m.first_run && !m.transient` → 自动弹 `showPresetOnboard('qwen')`（预选 modelscope qwen——免费额度、国内可达、对非技术用户最友好）——**复用既有 onboarding 弹窗**（[config-and-models · 预设 provider 模板](../guides/config-and-models.md)），仅是新增「无配置自动触发」入口，非新弹窗
- 触发后用户照常走 onboarding 全链路（注册 → 拿 token → 粘贴 → 落地 → 刷新下拉），落地完成即有模型可对话

### /api/latest 应用内更新检查

GitHub Releases latest 比对版本号（`paths.VERSION`——版本唯一真源）的版本检查端点：**24h 缓存 + 3s 超时 + 失败静默**（`update_available=null` 时前端不渲染横幅）。

- `desktop` 字段 = env `AGT_DESKTOP` 布尔（`_os.environ.get(...)` 读取，区分运行形态）
- 前端按形态给指引：桌面版 → 下载 zip 覆盖指引；pip → `pip install -U agt-agent`
- 实现：模块级 `_LATEST_CACHE = {"ts", "data"}` + 端点内局部惰性 `import urllib.request / time as _t / os as _os / from paths import VERSION as _VER`（不污染模块顶部导入）——版本号来源改 `paths`（平铺形态无 src 包可 import，且与桌面/pip 双形态共用唯一真源）

## Step 4：图标（PIL 生成）+ Windows 版本资源 + packaging README

### 图标：PIL 运行时生成

深蓝渐变圆角方块 + 白色 "A" + 终端点（简洁可辨识），多尺寸 ico（256/128/64/48/32/16）——`run_python` + PIL 生成（`packaging/` 构建脚本内），exe 文件图标直接可见。

### Windows 版本资源

版本资源编译器注入中文产品名等版本信息（右键 exe → 属性 → 详细信息可见），走 PyInstaller `version` 资源文件。

### packaging/README.md（分发说明）

含 **SmartScreen 教学**——Windows 无签名 exe 首次运行的「更多信息 → 仍要运行」步骤图文指引（本机自签名的桌面应用必遇，写进文档降低用户门槛）。

### 构建产物

重打包 `dist/Agt/Agt.exe` **84MB**（自带 Python 3.13 运行时 + 全部依赖，解压即用）；发布链 `python release.py --desktop`（打包 → zip → GitHub Releases）。

### 施工中排掉的坑

1. **pathlib backport 冲突**：环境里装的 Python 2 时代 `pathlib` backport 包与 PyInstaller 冲突 → 卸载后打包即通
2. **冻结环境 GUI 套娃**：PyInstaller 下 `sys.executable` = `Agt.exe` 本体，run_python 直接 spawn 会 Agt.exe 再拉 Agt.exe → `--pyrun` 入口分流（见 [打包基建 · desktop_entry.py](#打包基建packagingagtspec--desktopentrypy-step-2)），冻结形态实测跑通
3. **模块收集形态错位（打包产物启动即崩，2026-09-10 二轮，用户实测）**：首版 `pathex=仓库根` → 模块以 `src.config` 命名空间收集；运行时代码裸 `import config` 找顶层 → PYZ 里只有 `src.config` → `ModuleNotFoundError: No module named 'config'`。修复 = **平铺同构**（spec 修 #1）：pathex=src/ + datas 平铺 `_internal/` 根 + desktop_entry 裸名导入 + 版本号唯一真源收 `paths.py`（`src/__init__.py` 反向 `from paths import VERSION`，pip 侧 `__version__` 保持单源一致）
4. **hook-workflow 同名冲突（2026-09-10 三轮，平铺后暴露，spec 修 #2）**：顶层模块 `workflow`（`src/workflow.py` 平铺收集）撞上 pyinstaller-hooks-contrib 给**同名 PyPI 包 workflow** 准备的 `hook-workflow.py`（其 import 必然失败）→ 仓库内 `packaging/hooks/hook-workflow.py` 放**空操作 hook**（`hiddenimports = []`）覆盖（project `hookspath` 优先于社区 hooks），见 [打包基建 · Agt.spec](#打包基建packagingagtspec--desktopentrypy-step-2)
5. **节点插件 `No module named 'workflow_node_api'`（2026-09-10 三轮，spec 修 #3）**：节点插件（`assets/nodes_builtin/*.py`）由 `_import_fresh` 运行时**动态加载**，其 `import workflow_node_api` 静态分析看不见 → `hiddenimports` 显式补 `workflow_node_api`；`--selftest` 加节点插件动态加载自检（漏收集即暴露，实测 ok=12 fail=0）
6. **PyInstaller 增量缓存复用（重打包必须 `--clean`，2026-09-10 四轮）**：重打包默认复用 `--workpath packaging/build` 的 Analysis 缓存，`desktop_entry.py` 刚做的 selftest 扩展（查 `assets/nodes_builtin` + workflow_node_api 收集）**没真正进产物**——exe 里嵌的还是旧字节码 → 产物 `--selftest` 报 `SELFTEST_FAIL`（仍在查老路径 `assets/nodes`、节点插件动态加载 0 个）。修复 = **全量 `--clean` 重打包**（清 Analysis 缓存，产物才会带当前 desktop_entry.py）；教训「PyInstaller 重打包须 --clean」已记长期记忆。同轮把节点插件自检判定从恒 `True` 收紧为 `n_ok > 0`（0 个=目录缺失/放错路径提示），避免空载被掩盖
8. **端口错位 → 窗口指向无监听端口 `ERR_CONNECTION_REFUSED`（2026-09-10 六轮，commit ac9a79a，用户双击实测）**：用户双击 exe 后浏览器/窗口显示「127.0.0.1 拒绝连接 / ERR_CONNECTION_REFUSED」。根因 = **`pick_port` 被放在 `start_server` 之后**——`start_server(port=8000)` 同步等 uvicorn started（8000 已在监听）→ 随后 `pick_port(8000)` 的 connect 探测**把自己的服务判为"被占用"** → 退让到 8001 → `open_window(8001)` 指向无监听端口。**前两轮端到端漏检原因**：只查了 `logs/desktop.log`（服务在 8000 正常）和进程存活，**没验证窗口实际加载的端口**。修复 = `pick_port` 前置到 `start_server` 之前（服务与窗口同端口）；单测实锤：8000 无人听时 `pick_port→8000`，8000 被自己监听时 `→8001`（旧 bug 的窗口端口正是后者）。连带修：桌面模式 `/restart` 后必须重开窗口（见 [零侵入接入](#零侵入接入chatpy-两个分支点web_desktoppy)）。教训：**端口探测类操作必须在被探测服务启动之前**；GUI 形态的端到端验证须覆盖「窗口实际加载的 URL 可连」，而非只看服务端日志。
7. **GUI 无控制台 → `sys.stdout/stderr` 为 None → uvicorn formatter 启动即崩（2026-09-10 五轮，commit c9bd925，用户双击实测）**：`console=False` 的 GUI 程序（explorer 双击启动）**没有控制台**，`sys.stdout`/`sys.stderr` 是 `None`——`server.start_server()` → uvicorn `configure_logging` → `DefaultFormatter.__init__` 调 `sys.stdout.isatty()` 直接崩 `AttributeError: 'NoneType' object has no attribute 'isatty'` → `ValueError: Unable to configure formatter 'default'` → 启动即崩。**selftest 抓不到的盲区**：只做 import 链不启动 uvicorn，且 cmd 下跑 selftest 继承控制台（stdio 非 None）——**只有真·双击才触发**。修复 = `desktop_entry._redirect_stdio()`：进 `web_main` 前检测 stdio 为 None 则**重定向到数据目录 `logs/desktop.log`**（落实 spec「日志写文件」约定，成为桌面版排障第一现场），stdin 兜 devnull；**pythonw 无控制台模拟验证全过**（REDIRECT_STDOUT_OK isatty=False / REDIRECT_STDERR_OK / **UVICORN_FORMATTER_OK use_colors=False**——原崩溃的那一行现在构造成功）。修复后 `--clean` 全量重打包，完成后再 selftest 回归 + `AGT_HOME` 临时目录无控制台拉起 exe 端到端验证

## 验证状态

四步全部施工完成并推送（commit 6c2efce + f634d0d）；PyInstaller 全量打包 **84MB**（自带 Python 3.13 运行时 + 全部依赖）构建成功，最终冻结冒烟 ✓——代码/文件双模式 run_python 走 `--pyrun` 子进程分流实测跑通（FROZEN_OK）。

**打包形态修复（2026-09-10 二轮 + 三轮，commit c41d169 已推送）**：用户实测 `desktop_entry.py → src.chat` 链报 `ModuleNotFoundError: No module named 'config'` → 定位为收集形态错位，spec 改**平铺同构**（[施工中排掉的坑 · 3](#施工中排掉的坑)）。平铺后三连坑一次收口：config 错位（坑 3）+ **hook-workflow 同名冲突**（坑 4）+ **workflow_node_api 漏收集**（坑 5，见 [施工中排掉的坑](#施工中排掉的坑)）。重打包产物 `Agt.exe --selftest` **完整自检通过**：全模块 import 链 ✅（含 workflow_node_api）、三资源就位 ✅、**节点插件动态加载 ok=12 fail=0**（workflow_node_api 依赖闭环）→ `SELFTEST_PASS`。

**增量缓存坑（2026-09-10 四轮，commit c41d169 之后）**：两轮 `run_shell` 打包任务正常结束后，`Agt.exe --selftest` 却报 `SELFTEST_FAIL`——产物嵌的是**旧版 desktop_entry.py**（还在查老路径 `assets/nodes`、节点插件动态加载 0 个）：PyInstaller 未 `--clean` 时复用 `--workpath packaging/build` 的 Analysis 缓存，新 selftest 扩展根本没进产物（见 [施工中排掉的坑 · 6](#施工中排掉的坑)）。处理：发起**全量 `--clean` 重打包**（后台任务），并把「重打包须 --clean」记入长期记忆；同时节点插件自检判定收紧为 `n_ok > 0`。`--clean` 产物完成后再次 `--selftest` 收尾验证。

**uvicorn formatter 崩溃（2026-09-10 五轮，commit c9bd925，用户双击实测）**：`AttributeError: 'NoneType' object has no attribute 'isatty'` → `Unable to configure formatter 'default'` 启动即崩——GUI 程序（`console=False`）无控制台，`sys.stdout/stderr` 为 None，uvicorn `DefaultFormatter` 调 `isatty()` 崩溃（selftest 覆盖不到：不启动 uvicorn + cmd 下继承控制台）。修复 = `desktop_entry._redirect_stdio()` 重定向 stdio 到数据目录 `logs/desktop.log`（spec「日志写文件」落地），pythonw 无控制台模拟验证全过（见 [施工中排掉的坑 · 7](#施工中排掉的坑)）；后续 `--clean` 重打包完成后 `AGT_HOME` 临时目录无控制台拉起 exe 端到端验证。

交付验收（GUI 只能人工验）：`packaging/dist/Agt/Agt.exe` 双击 → 首启向导 → 配 token → 对话一轮 → 关窗重开（session 恢复）——自检覆盖不到 GUI 交互层。验收通过后 `python release.py --desktop` 一键出 zip 上 GitHub Releases。

### 端到端验证通过：stdio 修复闭环 + 顺手抓到 assembly 段校验残坑（2026-09-10）

**模拟双击启动端到端验证通过**（`AGT_HOME` 临时目录 + 无控制台拉起打包 exe——替代真双击的自动化验证）：

| 验证点 | 结果 |
|---|---|
| 进程存活（不再启动即崩） | ✅ |
| `logs/desktop.log` 写入（stdio 重定向生效） | ✅ |
| **uvicorn 启动 @ 0.0.0.0:8000**（原崩溃点 `Unable to configure formatter` 已过） | ✅ |
| Agent 装配（模型 glm / 工具 133 个） | ✅ |
| 桌面窗口模式运行 | ✅（验证完已 taskkill） |

**顺手抓到第二个 bug（commit 1c4aaa7）**：`logs/desktop.log` 里一行被掩盖的警告 `assembly 含未知段名 'recent_file'`——`src/multiagent.py` 的 `_ASSEMBLY_SEGS` 校验集合漏加 recent_file 段（session.py 投影层 / 管理页编辑器都认识它，唯独 DSL 解析漏了）→ 声明清单里的改文件快照段被静默丢弃。修复 + 单测验证 dict/str 两路径解析全过、无告警，详见 [context-engine · 修复八后记](../architecture/context-engine.md)。

第三次 `--clean` 全量重打包进行中（后台任务）——完成后跑 selftest 回归 + 再拉一次端到端确认警告消失，桌面版即收官。

### 窗口端口错位修复：ERR_CONNECTION_REFUSED（2026-09-10 六轮，commit ac9a79a，用户双击实测）

**用户报告**：双击 `Agt.exe` 启动后页面显示「无法访问此页面 / 127.0.0.1 拒绝连接 / ERR_CONNECTION_REFUSED」。

**根因**（时序 bug，见 [施工中排掉的坑 · 8](#施工中排掉的坑)）：

```
start_server(port=8000)   ← uvicorn 监听 8000（同步等 started ✅）
pick_port(8000)           ← 探测 8000：自己的服务在监听 → connect 成功 → 判"占用" → 退让 8001 ❌
open_window(8001)         ← 窗口加载 http://127.0.0.1:8001/ → 无人监听 → ERR_CONNECTION_REFUSED
```

**修复（两处，src/chat.py）**：

1. **`pick_port` 前置**——在 `start_server` 之前选好端口，服务与窗口同端口；单测实锤两种情形（8000 无人听 → `pick_port→8000`；8000 被自己监听 → `→8001`，即旧 bug 的窗口端口）
2. **桌面模式 `/restart` 必须重开窗口**——旧窗口已随旧进程关闭；浏览器模式维持「重启不重开页签」不变（见 [零侵入接入](#零侵入接入chatpy-两个分支点web_desktoppy)）

**验证方法（本轮补上的盲区）**：前两轮端到端只查 `desktop.log`（服务在 8000 正常）+ 进程存活，**未验证窗口实际加载的 URL 可连**——修复后改为对**窗口端口**发 HTTP 请求确认可连再收尾。`--clean` 重打包（约 6 分钟）后拉起 exe 复验。

**教训**：端口探测类操作必须在被探测服务启动之前；GUI 形态的端到端验证须覆盖「窗口实际加载的 URL 可连」，而非只看服务端日志。

## 多开支持：单实例锁退化为窗口去重（2026-09-10，用户裁定）

**用户判断**：「大概同开不能，因为端口有竞争」——端口确实会竞争，但**真正的拦路虎不是端口**。

**两个拦截点，实际先动手的是单实例锁**：

| 拦截点 | 位置 | 行为 |
|---|---|---|
| 单实例锁（先动手） | `web_desktop.open_window` → `_acquire_single_instance()` | 已有活实例 → 原逻辑 `sys.exit(3)` **挡掉第二个窗口** |
| 端口占用（早已解决） | `chat.py` → `pick_port(port)` | 占用即退让 8000→8001→8002…（上限 +50，全占随机高位），**不是障碍** |

**改动（`src/web_desktop.py` `open_window`）**：单实例锁语义从「禁止第二个实例」→「窗口去重提示」——`_acquire_single_instance()` 返回 False 时不再 `sys.exit`，改为打印 `ℹ️ 已有 Agt 桌面实例在运行——本窗口改为多开模式（服务 http://127.0.0.1:8001/）` 后**照常开窗**。锁文件 `instance.lock` 仍写（记录当前 pid），`run_loop` 退出时释放；强杀由 `_pid_alive` 自愈。

**多开的价值 = 规则隔离**（不只是多一个窗口）：

- 第二个实例的**引擎 / 端口 / workspace 都是新的**——`AGENTS.md`、`.agent/rules/`、`.agent/mcp.json` 按各自 workspace 加载
- 所以「两个窗口」= **两套项目规则、两份上下文**，互不干扰
- 仅共享 `~/.agt`（模型配置 / settings / 长期记忆 / 跨 repo 存档）——与 [数据目录唯一真源](#数据目录唯一真源paths-py单一数据根用户裁定-2026-09-10) 一致（数据根统一，workspace 隔离）

**边界（诚实说明）**：

| 场景 | 结果 |
|---|---|
| 两个窗口 → 不同 workspace | ✅ 完全隔离，推荐用法 |
| 两个窗口 → **同一 repo 的同一 session** | ⚠️ 不建议：同一份 session 文件被两个进程写（与多实例组网里「同 repo 同 session」约束同源） |
| 端口 | ✅ 自动退让：8000 → 8001 → 8002… |

**与 `/restart` 的关系**：桌面模式 `/restart` 仍必须重开窗口（`open_window(port)`，旧窗口随旧进程关闭）；重开后 `_acquire_single_instance()` 读到的是**旧进程已死**（pid 不存活）→ 正常获取，不误判多开。

**验证**：重打包（后台 `bg_1789038011675`）后双击第二个 Launcher/Agt.exe 即可开出第二个窗口——`pick_port` 退让 + 锁提示 + 窗口正常打开。commit 已就位（push 遇 GitHub SSH 超时，19:06 自动重试）。

**教训**：多实例/多开的「拦路虎」要分清是**资源竞争**（端口，已有退让机制）还是**显式互斥逻辑**（单实例锁，会直接 `sys.exit`）——排查时先看谁先动手。

## 迁移判定 bug：目录存在 ≠ 已初始化（launcher 预建目录短路迁移，2026-09-10 · 七轮）

**用户报告（三问，同一根因）**：① 桌面版看起来读的是 `models.py` / `config.py` 而不是 `~/.agt/`——是被打进包了吗？② 从 `Launcher.exe` 选了一个已有 repo，打开后 **session 下拉框是空的**——桌面端存档目录和正常版有区别吗？③ 正常 web 端有没有回归？

> **后续（2026-09-10 同日，用户裁定）**：本节根因所在的「桌面版独立数据目录 `%APPDATA%\Agt`」方案**已被整体回退**——数据根统一 `~/.agt`，迁移逻辑随之作废（见 [数据目录唯一真源](#数据目录唯一真源paths-py单一数据根用户裁定-2026-09-10)）。本节保留为**「目录存在 ≠ 已初始化」这一通用教训**的原始案例。

**逐问结论**：

1. **`models.py`/`config.py` 没被打进包**——模型来自**用户所选 repo 目录里的 `models.py`**（config 的向后兼容回退链：`models.json` 不存在 → 读 workspace 的 `models.py`）。真问题是 `%APPDATA%\Agt\models.json` 为什么不存在 → 见 2
2. **桌面端存档目录当时确实不同**（当时设计）：桌面版 = `%APPDATA%\Agt`，正常版 = `~/.agt`；首次桌面启动会**一次性全量迁移** `~/.agt` → `%APPDATA%\Agt`（session / 模型配置全跟过来）。但迁移被 **launcher 预建目录短路**（见下），故桌面版空配置 + 空存档 → session 空 + 模型回退到 workspace 的 `models.py`
3. **web 端无回归 ✅**（实测）：pip 态起 9001 web 服务 → HTTP 200（18.6KB 页面正常返回）；chat.py 两处改动（`pick_port` 前置、开窗条件）都只在桌面分支生效

**根因（时序 bug，`src/paths.py` `resolve_agt_home`）**：

```
launcher 先写 %APPDATA%\Agt\recent_workspaces.json（_save_recent）
→ 目录已存在
→ 迁移判定是「not new.exists()」→ 判"已初始化" → 跳过迁移 ❌
→ 桌面版：空配置 + 空存档 → fallback 读到 workspace 的 models.py
```

**当时的修复（`src/paths.py`）**：迁移判定从「**目录存在**」改为「**有无用户数据**」——定义 `_USER_DATA = ("models.json", "settings.json", "mcp.json", "main.yml", "repos", "remote_instances.json")`，任一存在才算已初始化；`shutil.copytree(old, new, dirs_exist_ok=True)` 兼容 launcher 预建目录（logs/ 亦系统产物，目录存在 ≠ 已初始化）。迁移成功写 `.migrated-from` 留痕；`OSError` 静默。首启打印「📦 首次桌面启动：迁移 …」。

**验证（单测四场景全过）**：预建目录（仅 recent/logs）仍迁移 ✅ / 有用户数据不迁移 ✅ / 非 desktop 回默认 `~/.agt` ✅ / 干净启动正常迁移 ✅。

**教训（本轮最有价值的部分，与方案回退无关）**：**「目录存在」不是「已初始化」的判据**——任何「首次运行才执行」的初始化/迁移逻辑，判定条件必须基于**业务数据的存在性**（models.json / repos 等），而非目录/文件系统层面存在性；有别的组件（launcher / 日志 / 缓存）会预先创建目录，用 `exists()` 判据必被短路。

**同轮暴露的下一步**：用户随即质疑「为什么桌面版要复制一份数据？」→ 触发数据根统一（[见上](#数据目录唯一真源paths-py单一数据根用户裁定-2026-09-10)），全量拷贝的卡顿问题与「与 CLI 版分叉」一并消失。

## 数据根统一回退：桌面版不再迁 %APPDATA%（2026-09-10 · 十四轮，用户裁定，commit 8ed30f6）

**数据根统一 `~/.agt`（2026-09-10 · 十四轮，用户裁定，commit 8ed30f6）**

**用户提问**：「是要把存档 copy 到 APPDATA? 我觉得保持在 `~/.agt/` 里读写就行吧？claude code 的 vs 插件和 cli 端读写的也是同一个位置的东西吧」——判断正确，`%APPDATA%\Agt` 迁移方案整体回退。

| 反直觉坏处 | 说明 |
|---|---|
| 云构建/多机成本 | 每台把 Agt 装到别处的机器、**每次 CI 云构建**都要复制 GB 级存档 |
| 数据分叉 | 桌面版与 CLI/pip 版各持一份——在哪边干活，另一边的记忆 / session 就"丢" |

**改动**：`resolve_agt_home()` 桌面分支不再返回 AppData（两级：`AGT_HOME` env > `~/.agt`）；新增 `_reclaim_legacy_appdata()` 把此前被迁出去的产物**搬回** `~/.agt`（只搬缺失项、从不覆盖、失败静默），覆盖 `models.json / settings.json / mcp.json / main.yml / models.py / repos / memories / logs / remote_instances.json` 九类；`%APPDATA%\Agt` 保留为系统侧产物（Launcher recent 列表 / 桌面日志 / 实例锁）。详见 [数据目录唯一真源](#数据目录唯一真源paths-py单一数据根用户裁定-2026-09-10)。

**验证**：单测四场景全过（桌面根=`~/.agt` 且不再外迁 / 回收搬回 models+repos / 目标已有不覆盖 / CLI 分支不触发回收）；`--clean` 重打包后台进行（`bg_1789037352009`），完成后新产物启动会打印一次「📦 数据根统一：回收 … → `~/.agt`」（若此前试装迁走过数据），随后与 CLI 版完全共用一份数据（session 下拉框直接看到全部历史）。

**顺带**：桌面版与 CLI 版可同时运行（同一 `~/.agt`、不同 workspace/实例互不干扰；同 repo 同 session 才会撞，与多实例组网约束一致）。

**遗留（代码注释未同步，非功能性）**：`src/desktop_entry.py` 模块 docstring 第 1 条仍写「数据目录迁 `%APPDATA%\Agt`」、`src/config.py` L21 注释仍写「AGT_HOME env > 桌面 %APPDATA%\Agt > ~/.agt」——实际行为已统一 `~/.agt`，注释待顺手清理。

## selftest 去 GUI 化：CI 挂死根因与双层修复（2026-09-10 · 十六轮）

**症状**：GitHub Actions 的 selftest 门禁步骤**永远不结束**——`$out = & packaging/dist/Agt/Agt.exe --selftest 2>&1 | Out-String` 挂住，job 拖到 45min 超时被 `Error: The operation was canceled.` 取消（日志里只有一句 Error，看不出崩在哪）。

**根因**：selftest 的 import 清单里有 **`web_desktop`（pywebview）**。pywebview 的 pythonnet/.NET 初始化在**无桌面会话的 runner** 上会挂住不返回；本机有 WebView2 环境，所以本地跑得好好的——**「本地能跑」再次不构成「CI 能跑」的证据**（同 [编码坑](#️-编码windows-runner-的-python-stdout-默认-cp12522026-09-10--十五轮必修) 的教训）。

**修复一：selftest 本身去 GUI 化**（`src/desktop_entry.py`）

| 旧清单 | 新清单 |
|---|---|
| config / paths / session / **web_desktop** / chat / server / workflow_node_api | config / paths / session / chat / server / workflow_node_api + **workflow / multiagent / llm_client / tools**（引擎核心反而补全） |

- 资源检查扩到 6 项（+ agents.html / tools_builtin / manifest.json）
- 节点插件加载**逐个 try**（单个炸不再淹没整组）
- 新增外置工具脚本 `py_compile` 校验（与节点插件同属「随包动态加载」一类）
- 所有 print 包 try——输出编码问题不再让自检本身崩

**修复二：workflow 门禁加 180s 超时兜底**（`.github/workflows/desktop-release.yml`）

```yaml
- name: Selftest gate (禁止坏产物出门)
  shell: pwsh
  run: |
    $job = Start-Job -ScriptBlock { & packaging/dist/Agt/Agt.exe --selftest 2>&1 | Out-String }
    if (-not (Wait-Job $job -Timeout 180)) { Stop-Job $job; Write-Error "selftest 180s 未结束（疑似挂起）"; exit 1 }
    $out = Receive-Job $job; Remove-Job $job -Force
    Write-Host $out
    if ($out -notmatch "SELFTEST_PASS") { Write-Error "selftest 未通过"; exit 1 }
```

万一再有东西吊死，**3 分钟就明确失败**（报「疑似挂起」），不再拖到 job 级 45min 被取消。

**净效果**：坏产物的检测能力**不降反升**（多查 4 个核心模块 + 3 类资源 + 工具脚本编译），但 selftest 从此不可能因环境问题吊死流水线。

**验证**：`--clean` 重打包（验证新清单里 workflow / multiagent / llm_client / tools 在产物里真的可 import——此前没在 selftest 里查过）→ 新 selftest 回归 → 推送后再触发一次 Actions。

**教训（通用）**：**CI 门禁脚本必须「确定性退出」**——凡是可能因环境差异阻塞的调用（GUI / .NET / 网络 / 交互式输入）都不能进门禁路径；同时门禁步骤本身要有**超时兜底**，否则「挂住」比「失败」更难诊断（日志里只有一个 canceled）。

### selftest 明细落文件 + 门禁改 Start-Process 重定向（2026-09-10 · 十七轮，commit 待推）

**症状（用户贴 Actions 日志）**：门禁步骤**正常结束**（不再是挂起），但只报：

```
SELFTEST_FAIL
Write-Error: selftest 未通过
Error: Process completed with exit code 1.
```

——`SELFTEST_PASS` 判定失败，可是**看不到任何一条 ❌**，无从知道是哪个检查项挂了。

**根因：GUI 子系统 exe（`console=False`）在 `Start-Job` 的管道传递下 stdout 句柄继承不完整**——实测 `Write-Host $out` 只出最后一行（全部 ✅/❌ 明细丢失，只剩末行的 `SELFTEST_FAIL`）。**不是 selftest 没跑完，是明细被丢了**——与 [十六轮](#selftest-去-gui-化ci-挂死根因与双层修复2026-09-10-十六轮) 同族的「GUI 形态 × CI 管道」交互坑。

**修复一：selftest 明细落文件（`src/desktop_entry.py`）**——逐项结果同时写 `cwd/selftest_result.txt`：

```python
lines = [("✅" if good else "❌", name, err) for name, good, err in checks]
lines.append(("SUMMARY", "SELFTEST_" + ("PASS" if ok else "FAIL"), ""))
try:
    with open(os.path.join(os.getcwd(), "selftest_result.txt"), "w", encoding="utf-8") as f:
        for a, b, c in lines:
            f.write(f"{a} {b} {c}\n".rstrip() + "\n")
except Exception:
    pass   # 落文件失败不影响退出码判定
```

**文件是确定性通道**——print 通道再怎么丢，CI 门禁 `cat` 它就能拿完整明细。

**修复二：门禁改 `Start-Process` 显式重定向（`.github/workflows/desktop-release.yml`）**——`Start-Job` 管道 → `Start-Process` + 三个显式文件：

```yaml
- name: Selftest gate (禁止坏产物出门)
  shell: pwsh
  run: |
    $outF = "selftest_out.txt"; $errF = "selftest_err.txt"
    $p = Start-Process -FilePath "packaging/dist/Agt/Agt.exe" -ArgumentList "--selftest" `
        -RedirectStandardOutput $outF -RedirectStandardError $errF -PassThru
    if (-not $p.WaitForExit(180000)) { try { $p.Kill() } catch {}; Write-Error "selftest 180s 未结束（疑似挂起）"; exit 1 }
    # 三份输出全部 cat 出来再判定，失败时明细不再丢
```

180s 超时语义保留（`WaitForExit(180000)` + `Kill`）——挂起仍 3 分钟明确失败。

**拿到明细后的头号嫌疑（本机绿、CI 红的经典模式）**：

| 环境 | Analysis 收集范围 | 后果 |
|---|---|---|
| 本机构建 | 全家桶（torch / playwright / modelscope… 都装了） | 全收集，产物齐全 |
| CI 构建 | 只有 requirements.txt + pyinstaller + pywebview | 引擎某模块顶层 import 了 requirements 外的库 → **不收集** → 产物运行时 `ModuleNotFoundError` |

新 selftest 恰好把 `workflow / multiagent / llm_client / tools` 加进了清单——若 ❌ 挂在它们身上，即此「环境遮蔽」问题（解法：把缺的库补进 CI 安装清单或 spec 的 `hiddenimports`）。

**下一步**：本地模拟 CI 形态（`Start-Process` 重定向）验证输出通道 → 重试 push（遇 GitHub SSH 抖动）→ 用户再触发一次 Actions，即可看到具体 ❌ 是谁。

**教训（通用）**：**CI 里「拿到完整输出」和「拿到正确退出码」是两件事**——GUI 子系统 exe / 重定向管道下 stdout 可能只剩末行；门禁脚本除退出码外必须有一条**文件通道**承载明细，否则失败时只有一句「未通过」，等于没有诊断信息。

## 云构建 + Release：GitHub Actions 流水线（2026-09-10 · 十三轮，spec 决策：云构建为主）

**决策**：桌面版发布走**云构建为主**（GitHub Actions `windows-latest`，公开仓库免费无限额），本地 `python release.py --desktop` 降为**兜底**（离线 / 应急 / 无网时用）。理由：本机打包约 6 分钟且需 `--clean` 全量、环境坑多（pathlib backport / 缓存复用），云端干净环境 + 门禁更可靠。

**文件**：`.github/workflows/desktop-release.yml` + `tools/ci_stamp_version.py`（仅 CI 调用）

### 触发与权限

| 触发 | 行为 |
|---|---|
| `push: tags: ["v*"]` | 构建 + **自动发 GitHub Release**（附 zip） |
| `workflow_dispatch`（Actions 页面手动） | 只出 **artifact**（`retention-days: 14`）供试装，不动 Release |

```yaml
permissions: { contents: write }        # softprops/action-gh-release 需要
concurrency:
  group: desktop-release-${{ github.ref }}
  cancel-in-progress: false             # 发布任务排队，不互相打断
jobs.build: { runs-on: windows-latest, timeout-minutes: 45 }
```

### 流水线 11 步

7. **🔒 selftest 门禁（含 180s 超时兜底 + 明细文件通道）**：`packaging/dist/Agt/Agt.exe --selftest` 输出不含 `SELFTEST_PASS` 即 `exit 1`——本 session 的**缓存旧字节码 / 漏收集 workflow_node_api** 这类坏产物从此出不了门。用 `Start-Process -RedirectStandardOutput/-RedirectStandardError` + `WaitForExit(180000)` 包住：selftest 万一挂起（历史坑：pywebview 在无桌面会话 runner 上挂死 → job 被 45min 取消）**3 分钟即明确失败**；三份输出（stdout / stderr / `selftest_result.txt`）全部 `cat` 出来再判定——GUI 子系统 exe 在管道下 stdout 曾只剩末行（`SELFTEST_FAIL`），明细落文件是确定性通道。详见 [selftest 去 GUI 化](#selftest-去-gui-化ci-挂死根因与双层修复2026-09-10-十六轮) 与 [selftest 明细落文件](#selftest-明细落文件--门禁改-start-process-重定向2026-09-10--十七轮commit-待推)

### ⚠️ 编码：Windows runner 的 Python stdout 默认 cp1252（2026-09-10 · 十五轮，必修）

**症状（用户贴 Actions 日志）**：`python tools/ci_stamp_version.py "branch" "main"` 在 runner 上崩：

```
UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-4
  File "C:\hostedtoolcache\windows\Python\3.13.15\x64\Lib\encodings\cp1252.py", line 19, in encode
  print(f"手动触发（ref_type={ref_type}）——沿用仓库版本号 …")
```

**根因**：GitHub Windows runner 上 Python（3.12+ 非 UTF-8 模式）stdout 默认 **cp1252**，中文 `print` 直接编码失败。**本地不复现**——本机 Windows 终端是 GBK/UTF-8，字符集能编，只有 runner 严格 cp1252 才炸。

**双层兜底（都做，不只修一个脚本）**：

| 层 | 改动 | 保护范围 |
|---|---|---|
| workflow 顶层 `env` | `PYTHONIOENCODING: utf-8` + `PYTHONUTF8: "1"` | 之后**所有** python 步骤的输出（不只 ci_stamp_version.py） |
| 脚本自身 | `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` | 本地 / 其他环境直跑也不崩 |

顺带：所有跑 python 的步骤统一 `shell: pwsh`（cmd 代码页是另一坑）。

**教训（通用）**：CI 脚本里**不要裸 `print` 非 ASCII**——要么 reconfigure stdio，要么在 workflow 层设 `PYTHONIOENCODING`；「本地能跑」不构成「CI 能跑」的证据（终端编码与 runner 默认编码不同源）。

### tools/ci_stamp_version.py：CI 版本戳

保证**产物版本号 == tag 版本号**。语义（`main()`）：

- `ref_type != "tag"`（手动触发时 GitHub 给 `ref_type=branch`）→ **不改**，打印仓库当前 `paths.VERSION` 后返回 0
- tag 名非 `x.y.z` 形态（如 `vNext`）→ 跳过并告警（用仓库当前值）
- tag 正常 → 改两处：`src/paths.py` 的 `VERSION = "x.y.z"`（**唯一真源**，`src/__init__.py` 反向 `from paths import VERSION as __version__` 随其同步）+ `packaging/version_file.txt`（exe 版本资源，正则 `0\.\d+\.\d+` 替换，2 处）
- `paths.py` 未匹配到 VERSION → 返回 1（CI 失败，不静默）

**踩坑（当场修）**：首版还去改 `src/__init__.py` 的 `__version__`——平铺打包后该文件是**反向导入**（无字面量），正则匹配 0 处直接 `AssertionError`；收敛为「只改唯一真源 paths.VERSION + version_file.txt」。手动分支的「沿用仓库版本号」打印也一度写成 `from paths import VERSION`（CI 里 src 不在 sys.path，不可靠）→ 改回对 `paths.py` 正则解析。

**验证（单测，已还原）**：tag `v9.9.9` → `paths.VERSION` 与 `version_file.txt` 双同步 ✅（新进程 import 实测 `VERSION = 9.9.9`）；手动触发（`ref_type=branch`）沿用仓库版本 ✅；非 x.y.z tag（`vNext`）跳过 ✅。

### 与本地发布链的关系

| 通道 | 命令 | 用途 |
|---|---|---|
| 云构建（主） | push tag `v*` / Actions 手动 | 正式 Release + 试装 artifact |
| 本地兜底 | `python release.py --desktop`（PyInstaller onedir → zip → 有 `gh` 则 `gh release upload --clobber`，无则给手动上传路径） | 离线 / 应急 |

**注意**：`release.py --desktop` **不 bump 版本、不上传 PyPI**（桌面 zip 与 pip 包两条独立通道）；本机未装 `gh`，正式 Release 由 Actions 自己创建，不需要 gh。

### 用户侧下一步（需 GitHub 账号，Agent 无法代做）

**排障入口**：Actions 页面看 job 日志——崩在版本戳步骤先查编码（`PYTHONIOENCODING` 是否就位）；崩在 selftest 门禁即产物坏（漏收集 / 缓存旧字节码），本地 `--clean` 重打包复现；**selftest 步骤被 canceled / 超时**先查是否又引入了 GUI 依赖（见 [selftest 去 GUI 化](#selftest-去-gui-化ci-挂死根因与双层修复2026-09-10-十六轮)）；**门禁只报 `SELFTEST_FAIL` 看不到 ❌ 明细**→ 看 `selftest_result.txt`（门禁已 cat，见 [十七轮](#selftest-明细落文件--门禁改-start-process-重定向2026-09-10--十七轮commit-待推)），再按「本机绿 CI 红 = 环境遮蔽（CI 缺依赖 → 漏收集）」排查。

## 瘦启动器 Launcher.exe：先选工作区再拉起主程序（spec s_37494daf，2026-09-10）

用户提案：「launcher.exe 可能是个瘦启动器，在窗口选一个 workspace 以后才去对应的目录启动真正的桌面应用」——VSCode / JetBrains 式「先选项目再开应用」。**两入口共存**：双击 `Launcher.exe` 选工作区；双击 `Agt.exe` 直接进默认 workspace（exe 旁 `workspace/`）兜底。

## 交付四步

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | `desktop_entry._pick_workspace`：workspace 三级解析 | ✅ 单测三级优先级全过 |
| 2 | `packaging/launcher.py`：tkinter 瘦启动器（176 行纯标准库） | ✅ 单测全过 |
| 3 | `packaging/Launcher.spec`：onefile 独立打包 | ✅ 构建成功 `Launcher.exe` **11.3MB** |
| 4 | 主程序 `--clean` 重打包 → launcher 端到端验证 | 进行中（后台） |

## 发布布局（打完即生效）

```
Agt/                        ← 解压即用的发行包
├── Launcher.exe   11.3MB   ← 双击这个：选工作区 → 拉起主程序（VSCode 式）
├── Agt.exe       107MB     ← 或直接双击：进默认 workspace（兜底入口）
└── _internal/
```

## desktop_entry：workspace 三级解析（`_pick_workspace`）

优先级 **`AGT_WORKSPACE` env（launcher / 看门狗传）> `--workspace <path>` 参数 > exe 旁默认 `workspace/`**；`chdir` 必须在 `import 引擎之前`（锚定机制），目录不存在自动创建。

- **env 不 pop**：`/restart` 继承 env 时新进程保持同 workspace——**重启不换区语义**（若 pop 掉，重启会掉回 exe 旁默认区，用户会话/记忆全丢）
- launcher 传 `AGT_WORKSPACE` + `cwd=选定目录` 双保险

## packaging/launcher.py：瘦启动器（纯标准库）

**零引擎依赖**（tkinter + subprocess + json，不 import paths / config）——独立 onefile 打包，与主程序互不牵连。

| 函数 | 职责 |
|---|---|
| `_data_dir()` / `_recent_file()` | recent 列表落 `%APPDATA%\Agt\recent_workspaces.json`（与 `paths.resolve_agt_home` 桌面分支同语义，launcher 不 import paths 保持零依赖） |
| `_load_recent()` | 读列表 + **消失目录静默滤除** + 截断 `RECENT_MAX=8` |
| `_save_recent(ws)` | 置顶去重（`lower()` 大小写不敏感）+ 写失败静默（不阻塞启动） |
| `_main_exe()` | 定位主程序三级：`AGT_MAIN_EXE` env（测试）> frozen 同目录 `Agt.exe` > 源码态 `<repo>/dist/Agt/Agt.exe`（开发验证路径） |
| `_launch(ws)` | 存 recent → `AGT_WORKSPACE` + `AGT_DESKTOP=1` env → `Popen(cwd=ws, DETACHED_PROCESS)` → **launcher 自退**（`sys.exit(0)`，主程序独立存活） |
| `LauncherUI` | Treeview 最近列表（首项 `★` 前缀）+ 双击直开 + 浏览按钮；**空列表自动弹目录选择**（`root.after(150, self._browse)`）；无 recent 且取消 → 退出 |
| `main()` | `--auto <dir>` 无 UI 直启（自动化验证 / 脚本用法，走 `return` 而非 `sys.exit` 供脚本断言输出） |

## Launcher.spec：onefile 独立打包

- 构建：`pyinstaller packaging/Launcher.spec --noconfirm --distpath packaging/dist`
- `excludes` 大名单瘦身（numpy / PIL / uvicorn / webview / requests / pydantic / yaml / httpx / anyio…）——引擎模块本不在 pathex，此处兜底防意外收集 → 产物仅 11.3MB
- `console=False`（GUI 启动器无控制台）+ `upx=False`（与主 spec 一致，upx 误杀率高且 SmartScreen 雪上加霜）+ 复用 `agt.ico` / `version_file.txt`

**spec 三连坑（当场修掉）**：① `SPECPATH` 是 **str 不是 Path** → `SPECPATH / "launcher.py"` 报 `TypeError` → 先 `_SPEC = Path(SPECPATH)`；② 忘 `from pathlib import Path` → `NameError`；③ 另两处 `SPECPATH` 引用（icon / version）一并改 `_SPEC`。三次构建全绿。

## 注意事项

- launcher **必须与 `Agt.exe` 同目录分发**（`_main_exe` 靠同目录定位）；找不到主程序时弹 `messagebox` 提示而非静默失败
- `AGT_MAIN_EXE` env 是测试/开发钩子（源码态验证指向 `dist/Agt/Agt.exe`），非用户面配置
- `--auto` 模式 stdout 在 `console=False` 下仍可用（重定向），但 `print` 输出不保证可见——脚本断言以退出码为准
- 主程序侧 workspace 语义变更集中在 `desktop_entry._pick_workspace` 一处，launcher 只负责传 env
- **多开允许但同 repo 同 session 不要开两个窗口**（同一 session 文件被两进程写）——不同 workspace 才是推荐用法；端口冲突由 `pick_port` 自动退让，无需人工干预
- **端口探测类操作必须在被探测服务启动之前**（`pick_port` 前置到 `start_server` 之前），否则会把自己的服务判为「被占用」
- GUI 形态端到端验证须覆盖「**窗口实际加载的 URL 可连**」，而非只看服务端日志 / 进程存活
- **selftest 里不要 import GUI 依赖模块**（pywebview / web_desktop）——CI runner 上会挂住不返回；门禁脚本必须确定性退出，且步骤本身要有超时兜底
- **CI 门禁除退出码外必须有文件通道承载明细**：GUI 子系统 exe（`console=False`）在 `Start-Job` 管道下 stdout 只剩末行——selftest 写 `selftest_result.txt`，门禁用 `Start-Process -RedirectStandardOutput` 三份输出全 cat
- **「本机绿、CI 红」先怀疑环境遮蔽**：本机装了全家桶（torch/playwright…）→ Analysis 全收集；CI 只有 requirements.txt → 顶层 import 缺库的模块不收集 → 产物运行时 `ModuleNotFoundError`（解法：补 CI 安装清单或 spec `hiddenimports`）

## 相关页面

- [系统总览](../architecture/overview.md) — 模块地图（服务层 chat.py / 配置层 config.py 的桌面配套）
- [运维 · 存档布局](../guides/ops.md#存档布局paths-py-三级解析--默认-agt-repos) — 数据目录三级解析落地后存档根随 AGT_DIR 走
- [用户交互 · /restart 重启双坑](user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc) — 重启不开新窗口同款语义
- [配置体系 · 配置文件解析 config_file](../guides/config-and-models.md) — repo 级覆盖与数据目录正交（路径解析归 paths.py）

