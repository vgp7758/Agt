# voice-input · 语音输入插件（全局层自用：服务端录音 → ASR 真流式 → 文字边说边填进输入框；2026-11-24，用户提案）

> 用户提案：「添加一个插件吧，我通过语音服务流式把我的实时语音转成文字添加到输入框里」。
> **位置：`~/.agt/plugins/voice-input/`（全局层，本机自用）——不在 agt 仓库内、不进随包种源**；本机所有 repo 实例共享（两层发现 / 同名覆盖见 [插件 SDK](../architecture/plugin-sdk.md)）。进仓库的只有三件引擎侧配套（见文末）。

## 职责与用法

输入框上方按钮排（`#controls`：模型 / 📁 加图 / 🔧 工具 / 会话 / Agent 同排）里的 **🎤 语音输入**（`AgtPlugin.ui.addButton` 的 `position:'input'` 席位；2026-11-25 起该席位并入 `#controls`，见 [fabDock · 后记二](fab-dock.md)）。

两种触发方式（2026-11-25 起）：

- **点一下 🎤** → 对麦克风说话 → 文字**边说边追加**进输入框（保留已有文字）→ 停顿 1.5 秒自动结束（或再点 ⏹）
- **按住空格键说话、松手结束**（焦点不在输入框时；详见下节）

转写完成后自己编辑再发送——**不自动发送**。

## 2026-11-25 二轮：按钮并入 #controls + 按住空格说话（松手结束）

用户两条反馈：① 🎤 按钮所在的「输入框上方工具条」看着像新开了一个固定区域，希望并进上面那排按钮；② 希望支持**按住空格键说话、松手结束**。

**① 按钮并入 `#controls`**（src/static/index.html，repo，commit `aa8249e`）：插件 SDK `position:'input'` 席位渲染目标由独立横排 `#pluginInputBar` 改为 `#controls`（容器保留闲置，兼容旧页面缓存）——SDK 层变化，详见 [插件 SDK · position:'input' 后记](../architecture/plugin-sdk.md)。

**② 按住空格说话**（插件前端 `frontend.js`，全局层 `~/.agt/plugins/voice-input/`，不入仓库）：

| 规则 | 行为 |
|---|---|
| 焦点不在输入框时按住空格 | 开始录音（🎤 变 ⏹ + 计时）；**松手结束** → 文字填入输入框（不自动发送） |
| 焦点在输入框时 | **不触发**——空格要正常打字。页面加载后输入框自动聚焦，想用空格说话先点一下页面空白处让焦点离开 |
| 录音中重复按键 | 忽略 |
| 录音中窗口失焦 | 只复位按键态；录音由静音自动结束兜底 |
| 点击 🎤 | 老交互保留，与空格键并存 |

**验证（Playwright 实测）**：按钮在 `#controls` 内且旧横排无按钮、按住空格触发（后端 active=true）、松手复位、焦点在输入框按空格不触发、点击开始/停止回归、无 JS 错误——全过。

**生效**：两处均为前端（index.html 磁盘 serve + 插件 frontend.js no-cache），**刷新页面即生效**，无需 /restart。

## 链路

```
浏览器（🎤 按钮，无需麦克风权限）
  → 插件路由（开始 / 停止 / 状态；录音会话带 sid）
    → 插件 backend 订阅 ASR SSE：http://127.0.0.1:8095/asr/stream
      → WS 事件 voice_partial / voice_final / voice_error 广播
        → frontend.js 只在发起页签落字
```

- **录音发生在服务端**（跑 agt 那台机器的麦克风）：用 `http://192.168.x.x:9000` 从别的设备打开也能用，浏览器不需要麦克风权限
- **必须经后端中继**：ASR 不发 CORS 头，浏览器直连读不到 SSE
- **页面级事件**：事件按约定 `agent_id=""` 系统级广播（发起页签可能正停在某个子 Agent 对话上），页签归属靠事件里的 `client_id`——约定见 [插件 SDK · emit 目标路由](../architecture/plugin-sdk.md)

## 关键设计

| 环节 | 说明 |
|---|---|
| 礼貌停止 | 先等服务端把最后一块转完（≤2.7s）再断开——立即断开会丢掉没凑满一块的尾音 |
| 防呆 | 环境音转出的「。」不落框；转写无内容则恢复原文；刷新页面自动停录；录音中每 0.5s 计时 |
| 麦克风独占 | 同一时刻只允许一个录音；第二个页签点会收到提示 |
| 参数 | 插件目录 `config.json`：`threshold` / `silence_seconds` / `chunk_seconds` / `max_seconds`——改完下次点击即生效，无需 reload |

## ASR 上游（8090 / 8095，repo 外依赖）

- **服务**：`asr_server`（脚本 `E:\Programs\asr\asr_server.py`），直连口 `http://127.0.0.1:8095/asr/stream`（SSE：录音中推 `partial`，结束推 `final`）；经 8090 聚合口 `POST /run/asr-local?wait=1` 会被缓冲成一次性返回（= `mode=stream`），所以**真流式必须直连 8095**
- **8090 三种模式**（转写结果在响应 `result` 字段）：`mode=file`（传路径）/ `mode=record`（`max_seconds` + `silence_seconds` 静音自动结束）/ `mode=stream`（`chunk_seconds` 分块，返回 `parts` + 拼接 `text`）
- **8090 结果缓存坑（已修）**：录音类请求每次请求体相同 → 第二次直接命中 8090 的结果缓存、返回第一次的旧转写（实测 0.0s 返回）——8090 侧加了**服务级 `no_cache` 开关**并给 `asr-local` 打开（`D:\Programs\env` 仓 commit `ca372dba`）
- **⚠️ 不自启**：`asr_server` 是以 8090 侧后台服务方式常驻的，agt 重启后不会自己起来，需重新拉起（冷启约 40 秒）；点 🎤 报 ASR 不可达时先查它
- **给 8090 的反馈（待对方处理）**：流式录音没有 `lead_timeout`（点了不说话会录满 `max_seconds` 才停）、无显式 stop 端点（已用礼貌停止缓解）；`partial` 是分块独立转写，块边界可能切词——`chunk_seconds` 越大越准但延迟越高

## 验证

| 层 | 结果 |
|---|---|
| 后端进程内冒烟 | 7 场景：正常流 / 中途停止 / 并发拒绝 / 错 sid / ASR 不可达 / 参数钳制 / status |
| 单测 | 59 passed（含引擎侧路由接管 3 例） |
| 9000 真页面（Playwright） | 按钮就位、开始计时、停止复位、输入框保护、无 JS 报错 |
| 9621 隔离实例（L2 旁路，已清理） | 子 Agent 目标页签能收到事件 |
| ASR 通路（8090 侧实测） | 文件转写 4.4s wav → 0.32s、RTF 0.07、文本全对；录音 / 分块模式通路正常 |

**未验证：真人说话效果**（Agent 出不了声）。默认静音阈值 `threshold=350` 偏灵敏（环境音被当成语音）；出现截断或提前结束，调 `threshold` 与 `silence_seconds`。

## 引擎侧配套（进仓库，commit e870092）

1. **插件路由 reload 接管**：调试中实锤 `/reload plugins` 后路由仍绑旧 handler（改了两版都没接管）→ `_attach_one` 对同 path 同 method 的旧插件路由先摘再挂，详见 [插件 SDK · 后记](../architecture/plugin-sdk.md)
2. **`emit` 页面级事件约定**：docstring 写明默认 `agent_id=_main_`、全页签广播须显式 `agent_id=""`（src/plugins.py `PluginAPI.emit`）
3. **i18n 5 条**：按钮 / tooltip 文案进 `src/static/i18n/dict.json`（见 [i18n · 后记](i18n.md)）

生效：主视图刷新即可；子 Agent 页签上的语音输入需实例 `/restart` 一次（路由接管修复属引擎侧 plugins.py）。

## 注意事项

- 全局层插件 = 本机自用，**别写进随包种源 `src/assets/plugins/`**（依赖本机 8095 ASR，换机器即不可用）；其它机器要让 🎤 可用，先起 ASR 并把 `asr-local` 挂上
- 录音端是「跑 agt 的那台机器」——远程页面点 🎤，录的是服务器所在机器的麦克风，不是你面前的
- ASR 是 CPU 常驻模型；开发轮开头 `before_turn_retrieval` 钩子报过一次 `MemoryError`——根因当日（2026-11-24）查清：**宿主机提交内存（commit）额度耗尽**，非物理内存（asr_server 占提交 2.40GB，本机第 2 大）——语音功能常开时留意；是否挂 8090 托管空闲卸载待裁定，详见 [workflow-hooks · WinError 1455 诊断](../architecture/workflow-hooks.md)

## 相关页面

- [插件 SDK](../architecture/plugin-sdk.md) — 插件包形态 / `position:'input'` 席位（2026-11-25 起并入 `#controls`）/ 路由 reload 接管后记 / emit 目标路由约定
- [fabDock](fab-dock.md) — 按钮席位（`position:'input'`，2026-11-25 起并入 `#controls` 按钮排）

