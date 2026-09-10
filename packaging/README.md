# Agt 桌面版（Windows）

> 下载 → 解压 → 双击 `Agt.exe` → 开始用。不需要安装 Python 或任何其它东西。

## 下载与安装

1. 到 [GitHub Releases](https://github.com/vgp7758/Agt/releases/latest) 下载
   `Agt-Desktop-<版本>-win64.zip`
2. 解压到任意位置（建议 `D:\Agt`，**避免 Program Files**——程序会在自己旁边建
   `workspace\` 工作目录，Program Files 下写文件需要管理员权限）
3. 双击 `Agt.exe`

### 首次启动：配置第一个模型

首次启动会自动弹出模型引导（ModelScope 免费额度最适合起步）：

1. 引导框里点 **ModelScope 的领取链接**（[modelscope.cn/my/accessToken](https://modelscope.cn/my/accessToken)）
2. 注册/登录（手机号即可），复制页面上显示的 **访问令牌（Access Token）**
3. 粘贴到引导框，点确定——完成，直接开始对话

不需要免费额度的，也可以在引导框左上角换任意 provider（OpenRouter / SiliconFlow /
DeepSeek 官方 / Z.AI 等），流程相同：注册 → 拿 API Key → 粘贴。

## 日常使用

| 事项 | 说明 |
|---|---|
| 工作目录 | exe 旁边的 `workspace\`——你要 Agent 处理的文件放这里 |
| 数据目录 | `%APPDATA%\Agt`（会话存档/模型配置/长期记忆），换电脑迁移这个文件夹即可 |
| 更新 | 应用内顶部会自动提示新版本 → 下载新 zip → 解压**覆盖**旧目录（workspace 和 AppData 数据不受影响） |
| 升级失败/回滚 | 直接跑旧目录备份即可——数据都在 AppData，不随程序目录走 |

## 常见问题

**双击后弹出蓝色窗口"Windows 已保护你的电脑"（SmartScreen）**
这是无签名应用的正常提示（开源免费应用常见）。点 **「更多信息」** → 出现
**「仍要运行」** 按钮 → 点击即可。只有第一次会出现。

**双击后没反应 / 闪退**
- 看任务管理器是否有 `Agt.exe` 残留进程（等 10 秒再启动一次）
- 旧的第二次双击会提示"已在运行"——本程序同时只允许开一个
- 日志在 `%APPDATA%\Agt\` 下，提 issue 时附上

**想在局域网用手机访问**
桌面窗口内照样用；同时它也在本地起了 HTTP 服务——窗口标题栏下方（或日志）找
`http://<你的IP>:<端口>`，手机浏览器打开（仅可信网络）。

**和 pip 版的区别**
同一套引擎。pip 版（`pip install agt-agent`）适合开发者；桌面版自带 Python 运行时，
适合不想碰终端的用户。数据目录互通（首次桌面启动会自动迁移 pip 版的 `~/.agt`）。

## 开发者：构建桌面版

```bash
pip install pyinstaller pywebview
python release.py --desktop        # 打包 → zip →（有 gh CLI 时）附到 GitHub Release
# 产物：packaging/dist/Agt/（目录）与 Agt-Desktop-<ver>-win64.zip
```

spec 见 [`packaging/Agt.spec`](Agt.spec)；入口 `src/desktop_entry.py`
（`--pyrun` 子进程分流，供 Agent 的 run_python 工具在打包环境下执行脚本）。
