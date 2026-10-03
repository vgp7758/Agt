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

### Tether（AI Harness，Worldwide）——90%，卡在视频题

已填完：姓名 / 邮箱 / +86 手机 / 简历 docx 已上传 / cover letter / 技术问答 ×2 / GitHub+PyPI 链接 / 薪资 $100k / China / Q9 来源。

**⛔ 硬关卡**：3 个**必填视频题**——Why Tether / P2P 经历 / 紧急交付经历。表单 DOM 只提供**录制/上传视频**入口，**没有文本替代通道**。三段英文逐字稿已写好（每段 60-90 词，30-45 秒）。两条出路：

1. **用户自录**：逐字稿发到用户邮箱，照稿念录三段（手机竖屏即可）→ 文件路径给 Agent → Agent 上传提交
2. **跳过 Tether**：先投 Enveritas，Tether 等用户想录再说

### Enveritas（Python 后端，$135-155k，Global 远程）——下一目标

- 匹配度最高的一单；Greenhouse 标准表单，预计无视频题
- playwright 投递进行中（预计下轮闭环）

## 物料

| 物料 | 位置 | 状态 |
|---|---|---|
| 英文简历 | `D:\AI_Usings\Agt\resume_en.docx`（源：`D:\AI_Usings\resume\resume_en.docx`，已复制进 workspace） | 用户已确认定稿 ✓ |
| Cover letters 首批两封 | `D:\AI_Usings\resume\cover_letters_batch1.md` | Enveritas + Tether ✓ |

简历复制进 Agt workspace 的意义：投递管线（playwright 上传附件）可直接从 workspace 取文件，不必跨盘找源。

## 待办

- [ ] Enveritas 投递闭环（playwright）
- [ ] Tether 视频题：等用户二选一（自录 / 跳过）
- [ ] `job_radar.py` 回复跟踪（IMAP → 摘要 + 草稿 → 通知）
- [ ] 薪资数字用户确认（当前 $100,000）

## 相关页面

- [定时/到点任务调度](background-scheduler.md)——job-radar 挂靠的调度机制
- [agent_watch](agent-watch.md)——同为「邮件通知型」后台监视器，邮件触发纪律（真有事才打扰）的先例
- [run_python](run-python.md)——playwright 自动化的执行通道
