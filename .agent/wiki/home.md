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

- **v0.31.4 发布（2026-10，发布提交 `ba2f4e6` + tag `v0.31.4`）**：自 0.31.3 以来 6 个 feature 提交一次打包，主打 **WebUI i18n 双语体系**（双向字典引擎 zh/en/bi 三模式 · 8 页共享引擎零漂移 · 字典 765 条/翻 491 条 · bi 三处体验修复），同批：钩子开关进设置面板「其它」页签 + repo 级持久化（`.agent/hooks_state.json`）/ 团队看板远程实例添加弹窗化 + 列表顺序换位 / ⭐ 收藏按钮挪右上 / answer 引用 workspace 外绝对路径渲染（读侧对齐 t1195）。PyPI 已上线，桌面版 CI 巡检确认。详见 [v0.31.4 发布记录](releases/v0.31.4.md)、[i18n](features/i18n.md)

## 快速事实增补（2026-10-04 · 四 · autonomous 融合进 schedule：code/deadline/mode 三参数，纯自主模式退役）

- **autonomous → schedule 融合收官（2026-10-04，用户提案，commits `0716fc0` + `01edc18`，净 -318 行）**：早期「纯自主模式」（打断 answer 续跑当前轮）实际应用中渐渐被 schedule 替代，用户裁定整体融合——`add_schedule` 新增三参数：`code`（触发时跑 Python，`result`+stdout 尾部为消息，**空产物该次静默**=自主循环「有话才说」）、`deadline`（过期自动删除，取代 end_time）、`mode`（busy 时注入三分岔：`immediate` 步边界插话打断 / `idle` 排队等空闲（默认）/ `skip` 放弃）。自主循环退化为「一个带 code+immediate 的循环任务」。autonomous 全家退役：五工具 + `/autonomous` 命令组 + agent 状态机 + WebUI 开关（7 文件，pending_messages 保留与 immediate 共用）。七项语义单测 + 持久化往返 + L2 隔离实跑全绿；重启后生效，历史 meta.json 的 `autonomous_*` 键变无害冗余。详见 [background-scheduler · autonomous 融合](features/background-scheduler.md)

## 快速事实增补（2026-10-05 · schedule 推送主从语义：code/action 主通道 + message 附言，主通道全空=全静默）

- **schedule 推送语义：互斥三选一 → 三通道组合 → 主从收敛（2026-10-05，用户问询触发 + 两轮裁定，commits `458475c` + `6c3f770`）**：用户问「同时传 message / code / tool 是都执行并注入吗」问出旧实现两暗坑——互斥三选一（`code > action > message` 只执行第一个，其余忽略）+ code 静默连 message/action 一起吞（「心跳 + 有事才说话」做不到）。v1 初版改三通道独立拼接；用户随即裁定 v2 **主从语义**：code/action 是主通道、message 是附言——**主通道产物全空 → 全静默（message 不单独发）**，有产物 → 按 code → action → message 拼接注入；只传 message 正常发。七场景验证全过；site-packages 已同步，重启生效，**已存在任务无需重建**。同日 · 三：用户实锤 run_python/run_shell 空输出返回占位符**「(无输出)」**（非空串）击穿 tool 静默——tool 产物归一化补占位符识别（commit `9d6cadd`），三通道静默语义闭环（code 空 / tool 空串或占位符 / 主通道全空）。详见 [background-scheduler · 三通道主从语义](features/background-scheduler.md)

## 快速事实增补（2026-10 · 回溯快照开关 repo 级化：设置「其它」页签 + .agent/snapshots_state.json）

- **回溯快照开关 repo 级化（2026-10，用户提案「跟着 repo 设置」，commit `04c37d6`）**：enable_snapshots 主源从 settings.json 迁到 `<cwd>/.agent/snapshots_state.json`（`{"enabled": bool}`）——快照开销是 per-repo 属性，跟着工作区走；设置控件从「模型」页签挪到「其它」页签（即时保存，`GET/POST /api/snapshots/setting`）。刻意不进 repo 级 `.agent/settings.json`：那是文件级整体覆盖语义，单键写入会遮蔽全局 settings 其余全部键——照 `.agent/hooks_state.json` 范式用独立文件。读取三源优先级（snapshots_state > settings 旧键 > 默认 True），写侧 `save_enable_snapshots()` 唯一入口并顺手清理旧键，老配置零迁移。详见 [snapshot-rewind · 开关 repo 级化](features/snapshot-rewind.md)

## 快速事实增补（2026-10-05 · i18n 第四轮补批 71 条：设置页 / agents 长段 / 模型卡片 tooltips）

- **WebUI i18n 第四轮补批 71 条（2026-10-05，用户实锤切 en 后仍有大段中文，commit `3610714`）**：字典 **765 → 836 条 · 已翻 562**，覆盖六区域——设置页三区块（钩子开关组+回溯快照组 / 模型与回退+**模型卡片全部字段长 tooltips** / MCP 配置+热重载说明）、agents 管理页（🤖 子 Agent 管理标题+「段名/注入姿势」完整长段）、团队抽屉（👥 Agent 团队等）、零散按钮与错误前缀（▶ Agent执行 / 💭回答推理 / ✅⚠️✓ 系列完整串）。site-packages 已同步，刷新即见。留白 ~26 条 **JS 模板拼装串**（`✓ 已发送×`+变量、`」卡片`等）——全串匹配引擎够不着，归渲染处 `_t()` 渐进改造（与 `_needs_t.json` 97 条同批）。详见 [i18n](features/i18n.md)

## 快速事实增补（2026-10-05 · 二 · i18n 第五轮补批 7 条 + 浏览器缓存误报甄别：Ctrl+F5 即消）

- **WebUI i18n 第五轮补批 7 条 + 浏览器缓存误报甄别（2026-10-05 · 二，用户实锤，commit `5333f98`）**：用户再报「还有一些」中文列 ~20 条——逐条对照字典发现**绝大多数第 4 轮已入库且 key 与页面原文逐字一致**，真根因是浏览器缓存旧 index.html/dict.json，**Ctrl+F5 即消**（排障口诀：报「还有中文」先 grep 字典，key 命中即缓存假阳性）。真补 7 条（字典 **836 → 843 · 已翻 569**）：agents 钩子 chip 三条长说明（模型 chip 增删回退链 / 工具 chip 增删 / 位置 before_turn/after_tool/before_answer/turn_end）+ 模型卡片「设默认」「补reasoning占位」+ MCP「未连接」两形态。新增留白实例：「已连接 · 25工具」（数字运行时拼接）。详见 [i18n](features/i18n.md)

## 快速事实增补（2026-10-05 · 三 · i18n 第六轮：空壳 value 根因修复，274 条空壳填平 248）

- **WebUI i18n 第六轮·空壳 value 根因修复（2026-10-05 · 三，用户实锤，commit `2ff2be8`）**：用户三批点名「还有中文」（MCP 动态注入区组头 / Runtime 页签保存按钮等）——排障**推翻第五轮「浏览器缓存」甄别结论**：真根因是第 2 步首批全量翻译为半成品，**274 条 key 已录入、value 为空字符串**，引擎 `|| null` 静默跳过 = 从未翻译。本轮：①假入库 key 排查出 325 条手打走样，改「**raw 文案从源码正则抓取**（零手打）」姿势补正 5 条（两个组头 + 💾 保存模型配置/运行时/MCP 三按钮，emoji/标点变体）；②全量扫空值 274 条 → **填平 248 条**（✅❌⚠️ 状态串 ~70 + 设置页全部组名与 tooltips + 团队/看板/审批/问卷等面板 ~100），仍空 23 条模板碎片并入 `_t()` 改造。字典 843 条仅剩 23 空值，site-packages 已同步，Ctrl+F5 后 en 基本全覆盖。排障口诀升级：grep 字典须**核对 value 非空**。详见 [i18n · 第六轮](features/i18n.md)

## 快速事实增补（2026-10-05 · 四 · ask_user 问卷宽容归一：[object Object] + 百词选项双症状根治）

- **ask_user 问卷宽容归一（2026-10-05 · 四，20048 实例实锤）**：跨实例问卷两处渲染翻车——agent 把 options 传成**对象数组**（前端 `String(对象)` → 全部 `[object Object]`）+ **整句逐词拆成上百个单词选项**，旧版 ask_user 对 options 零校验直接透传。修复三件：①后端 `_normalize_survey` 宽容归一（对象项提 text/label/value 等 7 键转字符串；整段字符串按分隔符拆；>10 项且均长<6 判疑似逐词拆分；缺题面补占位）；②前端 `renderSurveyBubble` 对象选项直接 JSON.stringify（永不再 [object Object]，双保险）；③归一警告附在 ask_user 返回值尾部——agent 答完卷即看到，下次调用自愈。四态测试全绿；site-packages 已同步（20048 重启生效后端，前端刷新即见）。详见 [ask_user](features/ask-user.md)

## 快速事实增补（2026-10-05 · 五 · ask_user 归一提升模块级：重启恢复路径也过归一，存档自愈闭环）

- **ask_user 归一提升模块级 + 恢复路径补刀（2026-10-05 · 五，用户追问「重启再加载能渲染吗」暴露）**：归一原只挂 `ask_user` 入口，pending 问卷是**存档态**——旧坏问卷重启后 `check_pending_survey` re-emit 时绕过归一原样渲染。两处闭合：①`_normalize_survey` 从 `make_survey_tools` 嵌套提升**模块级**（src/survey_tools.py L30）；②恢复路径 re-emit 前过归一并**写回** extra_state（坏形态只修一次，此后读档即净）。重启后两题命运对照：对象选项题**存档自愈**；百词选项题存档里已是合法字符串数组，归一不猜意图合并、需 agent 重发——分界线：归一救「形态错」不救「语义错」。site-packages 已同步。详见 [ask_user · 修复三](features/ask-user.md)

## 快速事实增补（2026-10-06 · desktop-operator 桌面操作 Agent 全链路：image_feed 实时画面段 + services 依赖声明）

- **desktop-operator 桌面操作 Agent 全链路（2026-10-06，深夜四连发）**：①**image_feed 装配段**——assembly 新动作项，每步从画面服务取最新帧挂投影末尾（vision 门控/失联降级不炸轮/瞬态不落档，`@@IMGFEED@@` 哨兵抽取进图片桶、不进文本统计）；服务端 `tools/image_feed_poc.py`（PIL.ImageGrab 全屏 → 1280 宽 JPEG q70 内存缓存，0.5s 节流零落盘）；②**desktop-operator 三件套实测全过**（看帧/工具/剪切板，glm-official-flash）；③**声明目录化配套**——agents 页 persona 正文改从 file: 项 md 直读（剥 frontmatter、读写对称）+ ACT_TYPES 增 image_feed + kill_agent 删声明整目录移除；④**services 依赖声明**（commit 2b621b4）——yml `services: [{名: 命令}]`，实例化幂等拉起（同名在跑跳过、失败 warning 不阻断），kill_agent 同步停服（治了 desktop-frame rc=1 崩溃后忘手工拉起的「睁眼瞎」）。详见 [image_feed](features/image-feed.md) + [services 章节](architecture/multi-agent.md)

- **编辑页两洞补齐（2026-10-06 · 二，commit 2c63133，用户实锤）**：用户发现 `/agents#edit=` 保存有两个洞——①声明**仍落平铺**（目录化只改了读侧/扫描侧，写侧 `_dump_agent_yml` 漏网）；②页面无 services 字段，`services:` 声明**一保存就丢**。修复：保存写入路径改目录形态 `<name>/<name>.yml + md`；file 项改**扫描原位替换**（首项 seg:system 式声明不再长出双 file）；编辑页新增 services textarea（每行 `名: 命令`，GET 行式回显 / PUT 解析回列表）；POST 判重认目录形态。冒烟五场景全绿，site-packages 已同步，/restart 生效。详见 [agents-admin](features/agents-admin.md)

## 快速事实增补（2026-10-08 · v0.33.0 发布：10-07 全天 41 笔提交打包上线）

- **v0.33.0 发布（2026-10-08，版本提交 `371b18b` + tag）**：版本 0.32.1 → 0.33.0，10-07 全天 41 笔提交一次打包。五大块：①**服务系统协议化**（`repl:` 前缀 MCP 式请求-响应 + 每步投影自动 /status + watch_tail 日志尾投影 + service_stdin expect + start 覆盖已退出同名）；②**Agent 体系增强**（coder-py/coder-cs 语言变体、子 Agent 步数三级取值 max_steps、agent_ask 纯问答、pv-producer 制片 Agent、agent_watch 单例接管）；③**交互修复·target 路由三部曲**（模型下拉/斜杠命令/文本插话全按本页签交互对象路由 + 插话死信兜底 + plan 面板刷新恢复）；④**上下文工程**（生成图自动可视伪造 read_file 对、before_turn 钩子只服务人类直输轮）；⑤**WebUI**（服务看板四按钮、定时任务 CRUD 弹窗、_main_ 三态保存、human_step/survey 只读化）。发布轮收编 pv-producer 自改 yml（`85ecf45`——它自己把模型切 proxy 避 glm-official 配额窗口，子 Agent 声明自治首例）；twine 一次 rc=0（对照 v0.30.1 TLS 阻断轮）。详见 [v0.33.0 发布记录](releases/v0.33.0.md)

## 快速事实增补（2026-10-09 · v0.34.1 发布：分支 v3 平铺 + 共享 toollog + 启动性能根治打包上线）

- **v0.34.1 发布（2026-10-09，版本提交 `720f64a`，whl + sdist 双文件就位）**：0.34.0 → 0.34.1 patch，10-09 分支二轮重构主线一次打包。五大块：①**分支 v3 目录平铺化**（`sessions/b/` 与主线平级 + `branch_of` 单段父引用 + `/merge` 合回根主线 + 失链自愈 + 越界 tier 状态修剪）；②**toollog 改 repo 共享单文件**（`sessions/toollog.jsonl` + `call_id=<session_id>-N` 层级前缀，40MB×N 分支拷贝清零，旧 per-session 文件 load 时自动并入）；③**启动性能根治**（`Session.load` 188s → 2.8s：收敛期 0 渲染 + sos 预检跳过注定无效的 LLM 浓缩）；④**修复**（reset 回退策略空壳补实现 / 历史展开卡死三连修 / `/resume <目录名>` 直查）；⑤**健壮性**（rewind 不再重写共享 toollog、llm_calls 基底合载保留）。多实例部署（如 20048）务必同步升级——toollog 落盘路径已迁移，旧代码按 per-session 路径找不到新文件。详见 [v0.34.1 发布记录](releases/v0.34.1.md)、[session 分支机制](features/session-branching.md)、[toollog](features/toollog.md)

## 快速事实增补（2026-10-09 · 二 · scheduler 切会话残留：restore_state 追加→全量替换）

- **定时任务切会话残留修复（2026-10-09，用户实锤，commit `f168e4c`）**：UI 下拉框切 session 后旧 session 的 schedule 还在跑、新 session 再建同名任务 → 双投 + 消息串台。根因：`Scheduler.restore_state` 是**追加语义**——`restore_runtime_state` 恢复链上 plan/spec/background_tasks/remote_servers 全是替换语义，唯独 scheduler 只添不清。修复：先 clear `_schedules`/`_by_name` 再恢复（**空列表 = 纯清空**，`if not items: return` 早退删除）；相位重算与幂等语义不变。四场景验证全绿，`/restart` 后生效。详见 [background-scheduler · 后记二](features/background-scheduler.md)

## 快速事实增补（2026-10-09 · 三 · before_turn 钩子与投影装配并行：投影不计时，90s 从投影后起算）

- **before_turn 钩子与投影装配并行（2026-10-09，用户提案，commit `5591648`）**：此前串行 = 投影装配完 → 再跑检索钩子 → 再请求，钩子耗时全叠加在用户首 token 等待里。`_run_hooks` 拆两段：`_start_hooks` 启动钩子批次立即返回句柄（实测 0.003s），投影装配并行进行；渲染到 user 注入点才 `_collect_hooks` 惰性收割，**90s（hook_timeout_before_turn）从投影完成后起算**——首请求等待从 `投影+钩子` 变 `max(投影, 钩子+90s 封顶)`。其它钩子位置（before_answer/turn_end/before_tool/after_tool）走同步组合语义不变；顺带修复 `extra_timeout` 传参被忽略。e2e 四场景全绿（6s 钩子 + 2s 投影：总 6.00s 而非串行 8s）。详见 [workflow-hooks · before_turn 钩子与投影装配并行](architecture/workflow-hooks.md)

## 快速事实增补（2026-10-10 · 系统提示气泡同轮相同提示合并计数：×N 徽标）

- **系统气泡相同提示合并计数（2026-10-10，用户提案，commit `3b378ac`）**：用户提案「同一轮下相同的提示信息合并为一条加计数」——tail ambient / 钩子注入每步重复的同一段提示此前每步落一个紫色系统气泡，同轮内刷屏。修法：`addRow('sys', text)` 入口判 `msgArea` 末行是 `.sys` 且 `_sig===text` → 计数 +1 更新 `×N` 徽标，否则新开一条（**只合并相邻**，中间隔开则各一条；不相邻即不合并）；**徽标挂 row 级**（bubble 的兄弟）——系统气泡 innerHTML 会被折叠/展开/markdown 重写反复刷，挂 row 级才不被冲掉（同「控件挂不被重写的祖先」范式）。playwright 真页面实测：连调 5 次只落 3 行 ✓。**纯前端，Ctrl+F5 即生效**。详见 [气泡交互 · 同轮相同系统提示合并计数](features/bubble-interaction.md#同轮相同系统提示合并计数n-徽标2026-10-10用户提案commit-3b378ac)

## 快速事实增补（2026-10-10 · v0.34.4 发布：steps 分档模式 + RAG 首启冻结修复打包上线）

- **v0.34.4 发布（2026-10-10，发布提交 `77c5536`，whl + sdist 双文件就位）**：0.34.3 → 0.34.4 patch。主打 **steps 分档模式**（settings `"tiering_mode": "steps"` 显式开启，**默认空 = 现行算法不受影响**）——轮间分档改按步数的确定性阶梯（五档 500/600/1200/2400/4800 可配，档位边界由步数守恒唯一决定、与 token 估算无关，1500 轮对拍增量≡重算）；三笔演进：本体 `ed6ab2d` → 顶窗三级级联 `08b4758`（fc→sos → 工具折叠档→fc → 档2/3/4→工具折叠档，每级下压一格）→ fc→sos 三段并行 `5d3f3df`（0.46s vs 串行 1.2s，失败前缀落账 + 每段实例级回退链）。同批 **RAG 首启冻结修复** `ac753c0`（USE_TORCH=1 阻断 transformers TF 连带 import + 预热单飞/延迟 15s，fresh 实例页面加载冻死根治、对照实测秒开）。镜像同步滞后 10~30 分钟，急用加 `-i https://pypi.org/simple`。详见 [v0.34.4 发布记录](releases/v0.34.4.md)、[context-engine · steps 分档模式](architecture/context-engine.md)、[rag](features/rag.md)

## 快速事实增补（2026-10-10 · 三 · schedule 编辑弹窗两问双修：tool/args 不丢实证 + add_schedule 参数描述补全）

- **schedule 编辑弹窗两问双修（2026-10-10 · 三，用户问诊，commit `e758270`）**：①「编辑弹窗保存后 tool 和 args 是不是就没了？」——实证**没丢**：`reschedule` 部分更新只碰表单字段（every_seconds / at / deadline / repeat / message），action(tool+args) / code / mode / daily 一概不动，持久化 `export_state` 全字段落盘；编辑弹窗对带工具调用的任务补一行只读提示「🔧 该任务携带工具调用：tool(args)——编辑不会丢失，保存后保留」（纯前端，刷新即见）。②「mode 字段是干嘛的？看起来没有 description」——mode = busy 时注入三分岔（`idle` 排队默认 / `immediate` 插话打断 / `skip` 放弃），语义原本只在 docstring（LLM 看得到）而参数级 schema 为空（🔧 工具表单弹窗 placeholder 读的正是后者）；`add_schedule` 十参补全 `param_descriptions` + `mode` 加 enum 三值（弹窗渲染为下拉框），`/restart` 后生效。详见 [background-scheduler](features/background-scheduler.md)

