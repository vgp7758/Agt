# Agt 手机版（Android · Termux）

手机本地跑 Agt Agent：LLM 走手机流量（云端 API），工具在手机本地执行，WebUI 手机浏览器访问。
与电脑版同构（session/记忆/工作流），数据目录 `~/.agt`。

## 一、装 Termux（唯一前置）

Termux 不在应用商店，从 **F-Droid** 或 **GitHub Releases** 下载安装：
- F-Droid: https://f-droid.org/packages/com.termux/
- GitHub: https://github.com/termux/termux-app/releases

（建议顺手装 **Termux:Widget**——同源下载，用于桌面一键启动图标）

## 二、安装 agt（两种方式）

### 方式 A：在线安装（有网环境）

把本目录的 `install.sh` 传到手机（微信/USB 均可），在 Termux 里：

```sh
termux-setup-storage          # 一次性授权存储（从 /sdcard/Download 拷贝用）
cp /sdcard/Download/install.sh ~/
sh ~/install.sh
```

约 5-15 分钟（pydantic-core 需 rust 本地编译，取决于手机性能）。

### 方式 B：离线包安装（别人给你打好的包，零编译零下载）

在装好 agt 的 Termux（母本机）里跑 `make-offline-bundle.sh` 产出
`agt-offline-<版本>-<日期>.tgz`，传到目标手机后：

```sh
termux-setup-storage
mkdir -p ~/pkg && tar -xzf /sdcard/Download/agt-offline-*.tgz -C ~/pkg
cd ~/pkg/agt-offline-*/ && sh restore-offline.sh
```

## 三、启动

```sh
~/start-agt.sh        # 起服务 + 自动弹浏览器
```

或桌面 **Termux:Widget 小部件 →「Agt演示」**（真·一键启动）。

## 四、首次配置（1 分钟）

浏览器打开 `http://127.0.0.1:8000` → 右上角 ⚙ 设置 → 模型 → preset 列表选一个
→ 填 api_token → 保存。之后就能对话了。

## 演示话术建议

- 「介绍一下你现在的运行环境」——它会说明自己跑在手机 Termux 里
- 「在 workspace 里写一个 hello.py 然后运行它」——展示本地工具执行
- 「记住：这台手机的主人是小明」→ 新会话再问——展示长期记忆

## 已知限制

- iOS 无方案（沙盒禁止常驻服务端）
- LSP / MCP / 桌面窗口（pywebview）不可用（可选依赖，手机场景不需要）
- `run_python` 里跑的脚本受 Termux 环境（无 Windows API）
- 手机息屏后 Termux 可能被系统冻结（演示时保持亮屏，或在通知栏 Acquire wakelock）
