# 手机版 · Android（Termux）安装与离线分发（packaging/android/）

## 职责

把 Agt 带到 Android 手机：Termux 里本地跑完整 Agt Agent——LLM 走手机流量调云端 API，工具（文件读写/代码执行）在手机本地执行，WebUI 用手机浏览器访问本地端口。与电脑版同构（session/记忆/工作流，数据目录 `~/.agt`）。它是「桌面版」之外的第二条分发形态：不打包二进制，而是 Termux 内的 shell 安装/离线分发脚本三件套。

## 交付三件套（packaging/android/）

| 文件 | 职责 |
|---|---|
| `install.sh` | 一键在线安装（有网环境，75 行） |
| `make-offline-bundle.sh` | 母本机打离线包，产出零编译零下载分发物（47 行） |
| `README.md` | 两种安装方式 / 启动 / 首次配置 / 演示话术 / 已知限制 |

## install.sh：一键在线安装五步

shebang `/data/data/com.termux/files/usr/bin/sh` + `set -e`，五步：

1. **环境自检**：`[ -d /data/data/com.termux ]` 判真 Termux——防止在普通 Linux shell 误跑（不是 Termux 直接 exit）
2. **pkg update** 更新包源
3. **pkg install python rust binutils libffi openssl**——rust 供 `pydantic-core` 本地编译（几分钟属正常）
4. **pip 清华镜像装 agt-agent**：`CARGO_NET_GIT_FETCH_WITH_CLI=true` + `-i https://pypi.tuna.tsinghua.edu.cn/simple`；失败提示「最常见原因：pydantic-core 编译缺 rust——确认第 2 步 rust 装成功后重跑」
5. **初始化演示 workspace + 启动脚本**：写 `~/agt-demo/AGENTS.md`（手机演示 persona）+ `~/start-agt.sh`（pkill 旧 agt 进程 → `nohup agt-web 8000` → `termux-open-url http://127.0.0.1:8000`，失败则提示手动开浏览器）+ 桌面快捷方式 `~/.shortcuts/Agt演示`（需另装 Termux:Widget——同源 F-Droid）

## make-offline-bundle.sh：离线母本打包

在**已装好 agt 的 Termux**（母本机）跑，产出 `~/agt-offline-<版本>-<日期>.tgz` 单文件：

- 定位 `site.getsitepackages()[0]` → 全量打包 site-packages（约几百 MB，免编译的元凶）
- 生成 `restore-offline.sh`：目标机只需 `command -v python || pkg install python` → 解包 site-packages → 安装 `start-agt.sh` / `AGENTS.md` / `~/.shortcuts/Agt演示`——**零编译零下载**
- 归一成单文件 tgz 便于微信/USB/网盘传输

## 启动与首次配置

`~/start-agt.sh` 或桌面 Termux:Widget「Agt演示」→ `http://127.0.0.1:8000` → 首次在右上角 ⚙ 设置里选 preset 模型 + 填 api_token 保存（走手机流量）。演示话术：`介绍一下你现在的运行环境`（跑在手机 Termux）/ `在 workspace 写 hello.py 并运行`（本地工具执行）/ `记住主人叫小明 → 新会话再问`（长期记忆）。

## 与其它形态的关系

- **同构数据根 `~/.agt`**：与 CLI/pip/桌面版共用同一数据根语义（差异在 workspace / 运行形态，不在数据根）——见 [桌面版 · 数据目录唯一真源](desktop-mode.md#数据目录唯一真源paths-py单一数据根用户裁定-2026-09-10)
- **非 PyInstaller 打包**：桌面版是 onedir exe（自带 Python 运行时），Android 版是 Termux 内 pip 安装 / site-packages tgz 分发——两条独立分发通道
- **可选依赖缺位**：LSP / MCP / pywebview 桌面窗口在手机不可用（场景不需要）

## 已知限制

- 无 iOS 方案（沙盒禁止常驻服务端）
- 息屏后 Termux 可能被系统冻结（演示时保持亮屏 / 通知栏 Acquire wakelock）
- `run_python` 内脚本受 Termux 环境限制（无 Windows API）

## 相关页面

- [桌面版（--desktop + PyInstaller）](desktop-mode.md)——另一条「超 pip CLI」分发形态
- [配置体系与模型调优](../guides/config-and-models.md)——首次配置 preset 模型
- [运维与排障](../guides/ops.md)——数据根 `~/.agt` / 存档布局