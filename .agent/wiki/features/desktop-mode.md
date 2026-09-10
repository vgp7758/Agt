# 桌面版 · --desktop 窗口模式 + 数据目录统一 + PyInstaller 打包（spec s_d53311f8，2026-09-10，commit 6c2efce）

## 职责

把 Agt 从「浏览器交互的 Web 服务」升级为「双击即用的桌面应用」（spec s_d53311f8 · 四步全交付）：`agt-web --desktop` 用 pywebview 弹系统 WebView 窗口（Win: WebView2 / mac: WKWebView / Linux: gtkwebkit，非 Electron）；配套数据目录迁移（桌面模式进 Windows AppData 惯例位置）、PyInstaller 打包基建（onedir 自带 Python 运行时，下载即用）、**首启向导 + 应用内更新检查**（无 provider 配置自动弹 onboarding + GitHub Releases 版本横幅）、**图标 / Windows 版本资源 / SmartScreen 教学文档**。pip 用户走 Step 1（窗口模式）；分发走 Step 2-4（打包 + 首启体验 + 发版物料）。

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

- 桌面打包形态由入口设 `AGT_DESKTOP=1` → 数据进 AppData（Windows 惯例）；测试/多实例用 `AGT_HOME` env 覆盖
- **存量迁移**：首次桌面启动新目录为空且旧 `~/.agt` 存在 → 整体复制（旧目录保留不删，回滚不丢数据）
- **七处独立定义收编**：此前 `Path.home()/".agt"` 散落在 config / session / lsp_manager / restart_watchdog / spec_tools / updater / feedback 各自定义——桌面模式数据目录迁移前**必须**统一，否则数据分裂三处。现全部改 `from paths import AGT_DIR`（或 `AGT_DIR / "..."` 拼子目录）
- config.py 的迁移逻辑也移入 paths.py：config 不再自持目录解析，**避免 session→config 循环 import**
- 外置工具（`assets/tools_builtin`，无 src 可 import）各自轻量复制三级逻辑（同款语义），如 cache_tools.py
- **版本号唯一真源同在此文件**（2026-09-10 二轮，平铺打包）：`VERSION = "0.26.4"`，`src/__init__.py` 反向 `from paths import VERSION as __version__` 保持 pip 侧一致；`server.py /api/latest` 也从 `import src` 改 `from paths import VERSION`（桌面平铺形态无 src 包）

## 打包基建：packaging/Agt.spec + desktop_entry.py（Step 2）

### Agt.spec（PyInstaller onedir）

- 构建：`pyinstaller packaging/Agt.spec --noconfirm` → 产物 `dist/Agt/`
- **平铺形态（spec 修 #1）**：`pathex=[str(SRC)]`（src/）→ 模块以**顶层名**收集（config/session/chat…），与 pip 运行时 `src/__init__.py` 的 sys.path hack（把 src/ 塞进 sys.path）同构——运行时代码里的裸 `import config` 才能在 PYZ 命中。首版 `pathex=仓库根` 是坑：模块以 `src.config` 命名空间收集，裸 import 找不到（见 [施工中排掉的两个坑 · 3](#施工中排掉的两个坑)）
- datas 同样**平铺到 `_internal/` 根**：`src/static → static`、`src/assets → assets`——`config.py` 的 `Path(__file__).parent/"assets"` 在 `_internal/assets` 命中
- 排除 tkinter / matplotlib / pytest / pip，隐藏 imports 收集子模块（uvicorn / webview / anyio + encodings 补全 + web_desktop）
- 发布：`release.py --desktop`（打包 → zip → GitHub Releases upload）

### desktop_entry.py（PyInstaller Analysis 入口）

- **裸名导入**：平铺形态无 `src` 包 → `from chat import web_main`（不是 `from src.chat import`），与收集形态同构
- **workspace 锚定 exe 旁**：`Path(sys.executable).parent` 作为 cwd 基线——防快捷方式启动时 cwd 歧视（工作区相对路径解析不到）；首启自动创建 workspace/ 并 chdir
- **`--pyrun` 子进程分流**：PyInstaller 下 `sys.executable` = Agt.exe 本体，直接 spawn Python 子进程会 **GUI 套娃**（Agt.exe 再拉 Agt.exe）——real_tools 三处 spawn 点（run_python / 相关子进程工具）已接 `_py_child_cmd`：打包形态用 `sys.executable --pyrun <file>` 分流到纯子进程 stub（runpy 直接执行目标文件，env 打 `AGT_FROZEN_CHILD=1`），源码形态直接 `sys.executable`
- **`--selftest` 产物自检**：`Agt.exe --selftest` 验证 import 链（config/paths/session/web_desktop/chat/server，逐个 `__import__`）+ 资源就位（static/index.html + assets/models.preset.json）→ 逐项 ✅/❌ 打印 + `SELFTEST_PASS/FAIL` 退出码——打包后跑一条命令替代难自动化的 GUI 冒烟（CI/发布前自动验证）

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

### 施工中排掉的两个坑

1. **pathlib backport 冲突**：环境里装的 Python 2 时代 `pathlib` backport 包与 PyInstaller 冲突 → 卸载后打包即通
2. **冻结环境 GUI 套娃**：PyInstaller 下 `sys.executable` = `Agt.exe` 本体，run_python 直接 spawn 会 Agt.exe 再拉 Agt.exe → `--pyrun` 入口分流（见 [打包基建 · desktop_entry.py](#打包基建packagingagtspec--desktopentrypy-step-2)），冻结形态实测跑通
3. **模块收集形态错位（打包产物启动即崩，2026-09-10 二轮，用户实测）**：首版 `pathex=仓库根` → 模块以 `src.config` 命名空间收集；运行时代码裸 `import config` 找顶层 → PYZ 里只有 `src.config` → `ModuleNotFoundError: No module named 'config'`。修复 = **平铺同构**（spec 修 #1）：pathex=src/ + datas 平铺 `_internal/` 根 + desktop_entry 裸名导入 + 版本号唯一真源收 `paths.py`（`src/__init__.py` 反向 `from paths import VERSION`，pip 侧 `__version__` 保持单源一致）

## 验证状态

四步全部施工完成并推送（commit 6c2efce + f634d0d）；PyInstaller 全量打包 **84MB**（自带 Python 3.13 运行时 + 全部依赖）构建成功，最终冻结冒烟 ✓——代码/文件双模式 run_python 走 `--pyrun` 子进程分流实测跑通（FROZEN_OK）。

**打包形态修复（2026-09-10 二轮）**：用户实测 `desktop_entry.py → src.chat` 链报 `ModuleNotFoundError: No module named 'config'` → 定位为收集形态错位，spec 改平铺同构（[施工中排掉的两个坑 · 3](#施工中排掉的两个坑)）。本地平铺 import 链全通 + `src.__version__` 与 `paths.VERSION` 单源一致已验；后台重打包完成后跑 `Agt.exe --selftest` 收口（期待 `SELFTEST_PASS`）。

交付验收（GUI 只能人工验）：`packaging/dist/Agt/Agt.exe` 双击 → 首启向导 → 配 token → 对话一轮 → 关窗重开（session 恢复）。验收通过后 `python release.py --desktop` 一键出 zip 上 GitHub Releases。

## 相关页面

- [系统总览](../architecture/overview.md) — 模块地图（服务层 chat.py / 配置层 config.py 的桌面配套）
- [运维 · 存档布局](../guides/ops.md#存档布局paths-py-三级解析--默认-agt-repos) — 数据目录三级解析落地后存档根随 AGT_DIR 走
- [用户交互 · /restart 重启双坑](user-interaction.md#restart-重启双坑电脑无端多开-tab--早连页签空白2026-08commit-7ca6cfc) — 重启不开新窗口同款语义
- [配置体系 · 配置文件解析 config_file](../guides/config-and-models.md) — repo 级覆盖与数据目录正交（路径解析归 paths.py）

