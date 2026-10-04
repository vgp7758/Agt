## 快速事实增补（2026-10-03 · 二 · awesome-openrouter PR 被关复盘 + 反馈章节诚实化）

- **awesome-openrouter PR #132 被关（2026-10-03）**：维护者两理由——「no traction」（硬事实，awesome 列表=热度认证，无法速成）+「docs link invalid」（实锤：`docs/` 只有散文件无入口 README，PR 链接 404）。当日两修复：①新建 `docs/README.md` 索引页（六篇架构导航 + 规范 + 指引）根治死链；②README 反馈章节撤过时承诺「直达作者手机/飞书实时推送」（随包 webhook 已于 v0.31.2 撤销，详见 [feedback](features/feedback.md#随包-webhook-撤销v0312--readme-反馈章节诚实化2026-10-03)），GitHub Issues 置顶推荐。**策略：现在不重提**——PR 下礼貌回复留好印象，攒 traction（知乎 blog 系列 06 篇待发 + PyPI 版本节奏），star ≈50+ 再提新 PR；复盘与策略见 [promotion 推广页](guides/promotion.md)

## 快速事实增补（2026-10 · 求职投递管线启动：岗位雷达 + playwright 代投 + 邮件互动通道）

- **求职投递管线（2026-10，用户委托）**：用户把「投递 + 与招聘方邮件互动」全权委托给 Agent，只在真有机会时出面细聊。进展：①**首封已投**——Enveritas（Python 后端 $135-155k Worldwide）官方邮箱直投 jobs@enveritas.org（resume_en.docx + 个性化 cover letter），IMAP 盯 bounce 中；②**Tether 视频题攻坚完成**——用户照逐字稿自录三段（53s/54s/51s，首录即控时），经 ffmpeg 校验 + 抽帧 + YuNet 人脸检测兜底（`<img>` 发 vision 注入失败）+ 720p 压缩（100MB→2.2MB），Ziggeo 上传转码全 READY，**唯一卡点剩 Send 前组件状态机**（逐题 Trim/Skip 确认）；③岗位雷达每 2h 巡逻（162→158 条地理过滤后可行）；④IMAP 读信通道已验证，回复跟踪待挂。详情见 [job-hunt](features/job-hunt.md)

## 快速事实增补（2026-10-04 · WebUI 界面国际化 i18n 第 1 步：中英双向字典引擎）


## 快速事实增补（2026-10-04 · WebUI 界面国际化 i18n 第 1 步：中英双向字典引擎）

- **WebUI i18n 全链路落地（2026-10-04，用户提案「中英互为 key」，commit 9a982e7）**：字典单文件 `src/static/i18n/dict.json`（中文 key → 英文 value），引擎运行时建 zh2en/en2zh **双向索引**——en 模式中文→英文、zh 模式英文→中文（Agent 双语环境写代码，UI 文案写哪侧都能被归一），缺翻译 fallback 原文；语言链 `?lang=` > localStorage > 浏览器语言，切换即 reload。交付四件：index.html 引擎（TreeWalker 文本节点/属性扫描 + MutationObserver 动态区 150ms 节流 + `_t()` 渐进包裹）/ `GET /api/i18n` 端点 / 设置·其它页语言下拉 / 字典 349 条骨架 + PoC 26 条（Playwright `?lang=en` 实测全绿：Send 按钮 / placeholder / en2zh 互查 ✓）。待办：323 条待翻 + 97 条拼接串 `_t()` 包裹。详见 [i18n](features/i18n.md)

