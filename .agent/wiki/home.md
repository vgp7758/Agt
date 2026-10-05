## 快速事实增补（2026-10-03 · 二 · awesome-openrouter PR 被关复盘 + 反馈章节诚实化）

- **awesome-openrouter PR #132 被关（2026-10-03）**：维护者两理由——「no traction」（硬事实，awesome 列表=热度认证，无法速成）+「docs link invalid」（实锤：`docs/` 只有散文件无入口 README，PR 链接 404）。当日两修复：①新建 `docs/README.md` 索引页（六篇架构导航 + 规范 + 指引）根治死链；②README 反馈章节撤过时承诺「直达作者手机/飞书实时推送」（随包 webhook 已于 v0.31.2 撤销，详见 [feedback](features/feedback.md#随包-webhook-撤销v0312--readme-反馈章节诚实化2026-10-03)），GitHub Issues 置顶推荐。**策略：现在不重提**——PR 下礼貌回复留好印象，攒 traction（知乎 blog 系列 06 篇待发 + PyPI 版本节奏），star ≈50+ 再提新 PR；复盘与策略见 [promotion 推广页](guides/promotion.md)

## 快速事实增补（2026-10 · 求职投递管线启动：岗位雷达 + playwright 代投 + 邮件互动通道）

- **求职投递管线（2026-10，用户委托）**：用户把「投递 + 与招聘方邮件互动」全权委托给 Agent，只在真有机会时出面细聊。进展：①**首封已投**——Enveritas（Python 后端 $135-155k Worldwide）官方邮箱直投 jobs@enveritas.org（resume_en.docx + 个性化 cover letter），IMAP 盯 bounce 中；②**Tether 视频题攻坚完成**——用户照逐字稿自录三段（53s/54s/51s，首录即控时），经 ffmpeg 校验 + 抽帧 + YuNet 人脸检测兜底（`<img>` 发 vision 注入失败）+ 720p 压缩（100MB→2.2MB），Ziggeo 上传转码全 READY，**唯一卡点剩 Send 前组件状态机**（逐题 Trim/Skip 确认）；③岗位雷达每 2h 巡逻（162→158 条地理过滤后可行）；④IMAP 读信通道已验证，回复跟踪待挂。详情见 [job-hunt](features/job-hunt.md)

## 快速事实增补（2026-10-04 · WebUI 界面国际化 i18n 第 1 步：中英双向字典引擎）

- **WebUI i18n 全链路落地（2026-10-04，用户提案「中英互为 key」，commit 9a982e7）**：字典单文件 `src/static/i18n/dict.json`（中文 key → 英文 value），引擎运行时建 zh2en/en2zh **双向索引**——en 模式中文→英文、zh 模式英文→中文（Agent 双语环境写代码，UI 文案写哪侧都能被归一），缺翻译 fallback 原文；语言链 `?lang=` > localStorage > 浏览器语言，切换即 reload。交付四件：index.html 引擎（TreeWalker 文本节点/属性扫描 + MutationObserver 动态区 150ms 节流 + `_t()` 渐进包裹）/ `GET /api/i18n` 端点 / 设置·其它页语言下拉 / 字典 349 条骨架 + PoC 26 条（Playwright `?lang=en` 实测全绿：Send 按钮 / placeholder / en2zh 互查 ✓）。待办：323 条待翻 + 97 条拼接串 `_t()` 包裹。详见 [i18n](features/i18n.md)

## 快速事实增补（2026-10-04 · 二 · i18n 第 2 步：全量翻译 8 页铺开 + 引擎共享单源 + bi 双语模式）

- **WebUI i18n 第 2 步收官（2026-10-04 · 二，用户「一次性翻译完」）**：①**引擎共享单源化**——index.html 内嵌块抽为 `src/static/i18n/engine.js`，8 页（index/agents/memory/rag/stats/wf_monitor/workflow_debug/workflow_editor）统一 `<script src="/i18n/engine.js">` 引入，server.py 增 `GET /i18n/engine.js` 引擎端点（与 /api/i18n 双通道 no-store）；②**文案抽取扩到 8 页**——去重 765 条 → 新增待翻 416，分批直译后字典 **759 条 · 已翻 436**（高频 UI 全覆盖，323 条骨架低频空值保留）；③**bi 双语模式**——中文主 + 英文副灰字（下拉「中文 + English（双语）」，`?lang=bi`）；④bi 实测全绿（9635 旁路：按钮/placeholder/下拉双语 + en2zh 反向命中），9000 实例刷新即见。遗留：97 条拼接串 `_t()` 包裹 + 6 条抽取噪音。详见 [i18n](features/i18n.md)

## 快速事实增补（2026-10-04 · 三 · i18n 修复轮三连：看板闪烁 / 按钮 / tooltips）

- **WebUI i18n 修复轮三连（2026-10-04 · 三，用户实锤，commit `6d95611`）**：①**看板闪烁根治**——团队看板 3s 自动重渲染撞 MutationObserver 150ms 节流，每次先闪一帧中文再变双语；防抖窗归零（0ms 立即翻译）后中文中间态消失；②**动态 tooltip 漏翻根治**——observer 加 attributes 监听（title/placeholder/aria-label/data-label），渲染函数事后 `el.title='中文'` 的动态设置即时翻；③**补批 55 条**——fabDock 图标标签 + 顶栏按钮 + 动态 tooltips，字典 **765 条 · 已翻 491**，site-packages 同步、刷新即见。留白：模型卡片 tooltip（`models.preset.json` 数据侧，preset 更新会失效）归下版渲染函数 `_t()` 化。详见 [i18n](features/i18n.md)

## 快速事实增补（2026-10 · v0.31.4 发布：WebUI i18n 双语体系打包上线）

## 快速事实增补（2026-10 · v0.31.4 发布：WebUI i18n 双语体系打包上线）

- **v0.31.4 发布（2026-10，发布提交 `ba2f4e6` + tag `v0.31.4`）**：自 0.31.3 以来 6 个 feature 提交一次打包，主打 **WebUI i18n 双语体系**（双向字典引擎 zh/en/bi 三模式 · 8 页共享引擎零漂移 · 字典 765 条/翻 491 条 · bi 三处体验修复），同批：钩子开关进设置面板「其它」页签 + repo 级持久化（`.agent/hooks_state.json`）/ 团队看板远程实例添加弹窗化 + 列表顺序换位 / ⭐ 收藏按钮挪右上 / answer 引用 workspace 外绝对路径渲染（读侧对齐 t1195）。PyPI 已上线，桌面版 CI 巡检确认。详见 [v0.31.4 发布记录](releases/v0.31.4.md)、[i18n](features/i18n.md)

## 快速事实增补（2026-10-04 · 四 · autonomous 融合进 schedule：code/deadline/mode 三参数，纯自主模式退役）

- **autonomous → schedule 融合收官（2026-10-04，用户提案，commits `0716fc0` + `01edc18`，净 -318 行）**：早期「纯自主模式」（打断 answer 续跑当前轮）实际应用中渐渐被 schedule 替代，用户裁定整体融合——`add_schedule` 新增三参数：`code`（触发时跑 Python，`result`+stdout 尾部为消息，**空产物该次静默**=自主循环「有话才说」）、`deadline`（过期自动删除，取代 end_time）、`mode`（busy 时注入三分岔：`immediate` 步边界插话打断 / `idle` 排队等空闲（默认）/ `skip` 放弃）。自主循环退化为「一个带 code+immediate 的循环任务」。autonomous 全家退役：五工具 + `/autonomous` 命令组 + agent 状态机 + WebUI 开关（7 文件，pending_messages 保留与 immediate 共用）。七项语义单测 + 持久化往返 + L2 隔离实跑全绿；重启后生效，历史 meta.json 的 `autonomous_*` 键变无害冗余。详见 [background-scheduler · autonomous 融合](features/background-scheduler.md)

## 快速事实增补（2026-10-05 · schedule 推送主从语义：code/action 主通道 + message 附言，主通道全空=全静默）

- **schedule 推送语义：互斥三选一 → 三通道组合 → 主从收敛（2026-10-05，用户问询触发 + 两轮裁定，commits `458475c` + `6c3f770`）**：用户问「同时传 message / code / tool 是都执行并注入吗」问出旧实现两暗坑——互斥三选一（`code > action > message` 只执行第一个，其余忽略）+ code 静默连 message/action 一起吞（「心跳 + 有事才说话」做不到）。v1 初版改三通道独立拼接；用户随即裁定 v2 **主从语义**：code/action 是主通道、message 是附言——**主通道产物全空 → 全静默（message 不单独发）**，有产物 → 按 code → action → message 拼接注入；只传 message 正常发。七场景验证全过；site-packages 已同步，重启生效，**已存在任务无需重建**。同日 · 三：用户实锤 run_python/run_shell 空输出返回占位符**「(无输出)」**（非空串）击穿 tool 静默——tool 产物归一化补占位符识别（commit `9d6cadd`），三通道静默语义闭环（code 空 / tool 空串或占位符 / 主通道全空）。详见 [background-scheduler · 三通道主从语义](features/background-scheduler.md)

## 快速事实增补（2026-10 · 回溯快照开关 repo 级化：设置「其它」页签 + .agent/snapshots_state.json）

- **回溯快照开关 repo 级化（2026-10，用户提案「跟着 repo 设置」，commit `04c37d6`）**：enable_snapshots 主源从 settings.json 迁到 `<cwd>/.agent/snapshots_state.json`（`{"enabled": bool}`）——快照开销是 per-repo 属性，跟着工作区走；设置控件从「模型」页签挪到「其它」页签（即时保存，`GET/POST /api/snapshots/setting`）。刻意不进 repo 级 `.agent/settings.json`：那是文件级整体覆盖语义，单键写入会遮蔽全局 settings 其余全部键——照 `.agent/hooks_state.json` 范式用独立文件。读取三源优先级（snapshots_state > settings 旧键 > 默认 True），写侧 `save_enable_snapshots()` 唯一入口并顺手清理旧键，老配置零迁移。详见 [snapshot-rewind · 开关 repo 级化](features/snapshot-rewind.md)

