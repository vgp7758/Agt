# job-hunt · 岗位雷达 + 投递自动化 + 邮件互动通道（2026-10，用户委托）

> 用户把**投递和与对方的邮件互动**全权委托给 Agent；用户只在「确实有机会」时出面细聊。本页记录这条管线的现状、关卡与待办。

## 委托分工（用户裁定）

- **Agent 代管**：岗位巡逻、表单投递、与招聘方邮件互动（读 + 起草回复）
- **用户出面**：只在实际约到面试/细聊时登场
- **约束**：用户多年没说英语，需要适应过程 → 真到面试前 Agent 可陪做**模拟英文面试**（出题、逐轮纠表达）
- **薪资口径**：Tether 表单暂填 **$100,000**，待用户确认；确认后后续批次全部沿用新数

## 岗位雷达（job-radar 定时任务）

- 每 **2 小时**自动巡逻一轮，有新匹配才发邮件通知（复用后台定时任务机制，见 [定时/到点任务调度](background-scheduler.md)）
- 首批 **162 条**岗位已进用户邮箱
- **地理过滤**：162 → **158 条**对中国候选人可行——US-only 排除，Anywhere/Worldwide 优先
- **回复跟踪（下一步）**：`job_radar.py` 加 IMAP 检查已投公司的回信 → 翻译摘要 + 建议回复草稿 → 通知用户

## 邮件通道：IMAP 已验证

- Agent **能读用户收件箱**（实测读最近 8 封，岗位雷达的通知邮件就在最上面）
- 这是「代管邮件互动」的前提能力；起草回复走「翻译摘要 + 建议草稿 → 用户过目」的口径

## 投递管线（playwright 代投）

### Tether（AI Harness，Worldwide）——视频题已过，卡在 Ziggeo 组件 Send

表单内容 100% 就绪：姓名 / 邮箱 / +86 手机 / 简历 docx 已上传 / cover letter / 技术问答 ×2 / GitHub+PyPI 链接 / 薪资 $100k / China / Q9 来源。

**视频题（原硬关卡，2026-10 已过）**：用户照逐字稿手机自录三段——Q1 Why Tether（53.83s）/ Q2 P2P 经历（54.60s）/ Q3 紧急交付（51.10s），首录即全部压进 120s 限。校验与上传链见下节 [视频校验管线](#视频校验管线ffmpeg-探测--抽帧--yunet-人脸检测兜底2026-10)。三段经 playwright 上传 Ziggeo 后**服务端转码全部 READY ✓**。

**⛔ 当前卡点（Send 前）**：点 Send 表单没有发出 POST——Ziggeo 视频组件的前端状态机没走完：`{{videoselectnotification}}` 模板未渲染 + 每题裁剪界面的 Trim/Skip 确认待正确处理。下轮攻坚路径：逐题打开裁剪界面走完确认流程 → 再点 Send 观察网络面板。

### Enveritas（Python 后端，$135-155k，Global 远程）——已投（首封正式投递，2026-10）

**2026-10 已投——投递管线首封正式投递**。该公司申请不走 Greenhouse 表单而是官方邮箱直投：`To: jobs@enveritas.org`，主题 `Application for Backend Software Engineer (Remote Global) — Jianqiang Ma`，附件 resume_en.docx + 个性化 cover letter（Python/PostgreSQL/AWS 直接匹配 + GPU 容器编排实战背书）。

选它先投的理由：$135-155k、Worldwide Remote、Python 后端 9 年经验直接对口、YC 非营利（使命感叙事加分）。

**盯回执**：jobs@ 若不存在会 bounce——下轮 IMAP 检查确认送达。

### 视频校验管线：ffmpeg 探测 + 抽帧 + YuNet 人脸检测兜底（2026-10）

**全链**（2026-10 首跑即闭环）：用户手机自录 → 下载 `D:\vgp77\Pictures` → ① ffmpeg（imageio_ffmpeg）探测时长/音频流 → ② 第 5 秒抽帧 `probe_qN.png` → ③ 出镜质量校验 → ④ 压缩（1080p ≈100MB → 720p ≈2.2MB，Ziggeo 上传无压力）→ ⑤ 复制进 workspace（playwright 上传可达路径）→ ⑥ 探针临时文件清理。

**出镜质量校验的兜底**：vision 子 Agent 侧 `<img>` 插话注入失败（三占位均报「读取失败」，未看到像素，根因见 [image-input · 实战翻车](image-input.md)）——当轮改**程序化检测**：YuNet 人脸检测（yunet.onnx）+ 亮度/相关性像素统计，结论三段均有人出镜（conf 0.88-0.92）、同人同场景大特写、光线良好、无遮挡。该兜底不依赖看图通道，可复用。

**卫生纪律**：探针产物（probe_*.png / _face_check.py / yunet.onnx / 表单状态截图）用完即清；workspace 只留投递仍要用的材料（三段视频副本保留——Tether 提交可能还用）。

## 物料

| 物料 | 位置 | 状态 |
|---|---|---|
| 英文简历 | `D:\AI_Usings\Agt\resume_en.docx`（源：`D:\AI_Usings\resume\resume_en.docx`，已复制进 workspace） | 用户已确认定稿 ✓ |
| Cover letters 首批两封 | `D:\AI_Usings\resume\cover_letters_batch1.md` | Enveritas + Tether ✓ |

简历复制进 Agt workspace 的意义：投递管线（playwright 上传附件）可直接从 workspace 取文件，不必跨盘找源。

## 待办

- [ ] Tether 收尾：Ziggeo 组件逐题 Trim/Skip 确认后 Send（唯一剩余关卡）
- [ ] Enveritas 回执确认（IMAP 盯 bounce）
- [ ] `job_radar.py` 回复跟踪（IMAP → 摘要 + 草稿 → 通知）
- [ ] 薪资数字用户确认（当前 $100,000）
- [ ] 低优：`<img>` 插话发 vision 子 Agent 注入失败根因实锤（疑似 repo_images_dir 落点契约，见 [image-input](image-input.md)）

## 相关页面

- [定时/到点任务调度](background-scheduler.md)——job-radar 挂靠的调度机制
- [agent_watch](agent-watch.md)——同为「邮件通知型」后台监视器，邮件触发纪律（真有事才打扰）的先例
- [run_python](run-python.md)——playwright 自动化（表单投递/视频上传/ffmpeg 校验）的执行通道
- [image-input](image-input.md)——`<img>` 发 vision 子 Agent 注入失败翻车（视频校验兜底改 YuNet 的缘由）

