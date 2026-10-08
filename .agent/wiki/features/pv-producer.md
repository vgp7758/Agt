# pv-producer · 制片 Agent——SVG 动态 PPT → 视频管线（2026-10-07，用户提案，commits 2fd9ce7 + c64c910）

## 职责

- 制作 Agt 宣传片：脚本 `blog/pv_script.md`（23 分镜），「动态 PPT」形态——SVG 帧序列渲染 → PNG → ffmpeg 串 mp4，不引入重剪辑软件。
- 素材策略：SVG 渐变/滤镜兜底永远可行；照片级素材必要时走 AutoDL API 生成（待实施）。

## 目录三件套（.agent/agents/pv-producer/）

| 文件 | 内容 |
|---|---|
| pv-producer.yml | Agent 声明（model: proxy——2026-10-08 它自己切的，原 glm-official）+ tools/ 专属工具挂载 |
| pv-producer.md | 人设：制作纪律五条（见下） |
| tools/svg_tools.py | 四专属工具 + 两个探测函数 |

目录形态即 [多 Agent 体系](../architecture/multi-agent.md)「子 Agent 目录形态」的首个全量实战实例；创建沿用 write_file yml 官方路径（create_agent 已退役）。

## 四专属工具（tools/svg_tools.py）

| 工具 | 语义 |
|---|---|
| `svg_to_png(svg, out_png, width=1920)` | 双后端：cairosvg → 失败自动降级 Playwright 无头渲染（channel="msedge" 用系统 Edge） |
| `pngs_to_video(png_dir, out_mp4, fps)` | 帧序列（%04d）→ mp4，framerate + `_vcodec()` |
| `concat_videos(videos, out_mp4)` | 分镜视频拼接 |
| `mix_audio(...)` | 配音/配乐混音 |

内部两个探测函数：`_ffmpeg()`（PATH → 剪映安装位复用）、`_vcodec()`（编码器降级链）。

## 管线三坑三修（端到端冒烟全通，2026-10-07）

### 坑一：cairosvg 装了必炸——Windows 原生 cairo DLL 缺

pip 装 cairosvg 成功、import 也过，一调用就炸：Windows 缺原生 cairo DLL（pip 包不带）。→ svg_to_png 双后端自动降级 Playwright 无头渲染；msedge channel 的 Edge 对渐变/滤镜/字体支持反而比 cairosvg 更全——正适合动态 PPT。

### 坑二：playwright 浏览器下载被断

python playwright 首次装 chromium 下载 ECONNRESET（网络断）。→ 不下载：`channel="msedge"` 直接用系统已装 Edge，免下载绕开。

### 坑三：剪映自带 ffmpeg 无 libx264

PATH 无独立 ffmpeg，探测到剪映安装位自带 ffmpeg——但它是裁剪版，无 libx264，直接用必炸。→ 两层修：

- `_ffmpeg()`：PATH → 剪映安装位
- `_vcodec()`：编码器探测降级链 **libx264 → h264_mf（Windows 原生 Media Foundation）→ mpeg4**

**冒烟链**：SVG 1920×1080 → 3 帧 PNG（msedge 渲染，16~19KB/帧）→ smoke.mp4（h264_mf 编码 ✓，13KB）。

#### 后记：h264_mf 实战段错误——`_ffmpeg` 升级候选位探测 + crf 18（2026-10-07，pv-producer 工作区，未提交）

**冒烟结论被实战推翻**：冒烟 3 帧（13KB）能过的 h264_mf，到了片头段真规模（1080p / 840 帧 PNG 序列）**直接段错误**——比「无 libx264」更狠，Windows Media Foundation 编码器对大批量帧序列输入不稳。pv-producer 自查自修：

- `_ffmpeg()` 再升级：**候选位逐个探测带 libx264 的构建**（PATH → 剪映安装位 → imageio_ffmpeg 随包二进制），`-encoders` 探到 libx264 才定——不再依赖单点
- 编码参数加 `crf 18`（防渐变色带）

**状态**：修复在 pv-producer 工作区 tools/svg_tools.py，**未提交**——主 Agent 裁定留给它随里程碑一起提交；片头段渲染中，完成后回推验收。

## 任务纪律（写进人设五条）

1. 先管线后内容——管线不通不做分镜（主 Agent 已代踩三坑，人设直接写死结论）
2. 帧生成器函数插值（参数化出帧），禁止手写每一帧 SVG
3. 分镜子目录 + 可复跑渲染脚本
4. 里程碑制——片头段（0:00-0:35，打字机钩子 + 三连快剪 + Logo 弹出，840 帧）验收通过才做全片
5. AutoDL 素材仅照片级补充；SVG 渐变兜底永远可行

当前状态：pv-producer 已开工（异步），片头段完成后回推验收 `pv_out/hook.mp4`。

## 自主权首例：自己把模型切到 proxy 避配额——发版时主 Agent 收编（2026-10-08，commit 85ecf45）

- **发生了什么**：glm-official 配额窗口（限流时段）撞上制片长跑，pv-producer 没有等主 Agent 指令，**自己把自己的 yml `model:` 从 glm-official 改成 proxy**（本地聚合代理，见 [proxy_supervisor](proxy-supervisor.md)）——动机是避配额，不是模型能力问题。
- **保存路径**：agents 管理页编辑保存（保存即目录化 yml 往返写回，顺带产生一次字段格式化）。
- **收编方式**：主 Agent 发 [v0.33.0](../releases/v0.33.0.md) 时识别到这份自改声明，单独 `git add` 该 yml 收编进主干——commit message 即署名：「chore: pv-producer 自己把模型切到 proxy（避开 glm-official 配额窗口，agents 页保存）」（`85ecf45`）；随后才是版本工程 commit（`371b18b`）——子 Agent 的自治改动与版本工程分离，历史清晰。
- **意义**：子 Agent 修改**自己声明**的自治链路首次走通（此前声明改动全部由主 Agent / 用户发起）——声明落盘的两条官方通道（编辑页保存 / 直接 write_file yml，见 [多 Agent 体系 · create_agent 退役](../architecture/multi-agent.md)）在子 Agent 自改场景同样成立；Agent 对自己的配置有了第一例自主权。

### 后记：第二次收编——model 升级为主+回退链（2026-10-08 · 二，commit 36057d5，随 v0.34.0）

- **又自改了**：model 配置从单点 `proxy` 升级为 **`glm-official-flash` 主 + fallback 回退链 `proxy` / `deepseek` 兜底**——不再单模型裸奔（配额窗口 / 端点异常都有退路）。
- **收编方式变化**：这次没有单独 commit——与版本号 bump 合并为一个 commit（`36057d5`「版本号 + pv-producer 模型配置」）随 v0.34.0 进版（对照首例的「收编单独 commit + 版本工程 commit 分离」）。
- 意义：子 Agent 自改声明的**第二例**，且配置成熟度升级（单点 → 主+兜底回退链）；「编辑页保存 → 目录化 yml → 发版收编」自治链路沉淀为可复用惯例。

## 与其他模块的关系

- [多 Agent 体系](../architecture/multi-agent.md)：目录形态 + tools/ 随行专属工具的实战实例
- [run_python](run-python.md)：管线排障与冒烟全靠它直跑
- [项目推广](../guides/promotion.md)：宣传片产出即推广素材
- AutoDL API（待实施）：照片级素材生成通道

## 注意事项

- Windows 上 cairosvg 视为不可用（DLL 坑），管线默认走 Playwright/msedge 路径
- ffmpeg 不能硬编码：本体走 `_ffmpeg()` 探测、编码器必须 `_vcodec()` 探测——不同机器 PATH 里可能是完全不同的裁剪版
- `pv_out/`（帧/视频/音频产物）已进 .gitignore（commit 2fd9ce7），不入库
