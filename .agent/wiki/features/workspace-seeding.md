# workspace 播种 · 约定目录 README + 示例技能 + 自包含 agents（seed_dir_docs / seed_default_skills / seed_default_agents）

> 挂点：`src/chat.py build_agent()` 播种序列——`seed_default_agents`（bundled agents 目录播种；2026-10-06 起 desktop-operator 以自包含四件加入，见下文专节）→ `seed_dir_docs`（commit 0ec4ec7）→ `seed_default_skills`（commit 16aede0）。随每次启动检查，幂等，让每个 repo（含新装用户）的约定目录「开箱即懂」。

## seed_dir_docs · 约定目录 README（用户提案：把框架那一页文档以 README.md 播种到目录位置）


- **文档源**：`src/dir_docs/`（随包分发，pip 用户同样生效，2026-10-16 起 **6 份**）——`tools_builtin.md`（脚本工具 agt_register 约定 + [任务脚本防御](tool-externalization.md)，收编后重写为主目录视角）、`workflows.md`（XML 骨架 / exec_workflow·debug·钩子）、`agents.md`（含子 Agent 目录形态 + [直接写 yml 创建最小模板](../architecture/multi-agent.md#create_agent-退役官方路径直接-write_file-yml2026-10-06用户裁定commit-f3f983c)，2026-10-06）、`skills.md`（技能包结构 / 双层激活 / 七件套）、`nodes.md`（节点插件 .py+.js）、`plugins.md`（引擎级插件开发一页通，2026-10-16）；原 `agent_tools.md` 已删（内容并入 tools_builtin.md）
- **播种目标（6 个约定目录，2026-10-16 起；前 5 个为 2026-10-15 收编形态）**：`.agent/tools/`、`.agent/workflows/`、`.agent/agents/`、`.agent/skills/`、`.agent/nodes/`、`.agent/plugins/`——**旧 `tools/builtin/`、`nodes/` 不再播种/创建**（扫描侧仍兼容存量，见 [默认目录收编](tool-externalization.md#默认目录收编本地自定义统一进-agent2026-10-15用户裁定commit-21351b1)）
- **语义**：目录不存在 → 创建目录 + 播种（引导价值：一眼看到全部可扩展点）；`README.md` 已存在 → 跳过——**用户改动永不覆盖**
- **写入**：字节级 + `seed_state` 基线——接入 /update-assets 三方 hash 判定管道（框架升级后文档可安全更新）
- **plugins.md 种源加入（2026-10-16，用户中途补充提案）**：随[随包播种示例插件](../architecture/plugin-sdk.md)扩散——示例插件只有成品没有开发文档，新 repo 用户/Agent 想自己写插件没有入口。一页通（84 行）覆盖：目录三件结构（manifest.yml 必需 + backend.py / frontend.js / 静态资产可选）、manifest 字段、**backend `setup(api)` 装配协议**（register_tool / register_command / register_route / on_tool_pre·post_execute / emit 全带示例）、**前端 AgtPlugin SDK**（addButton / addPanel / addChatCard / ws.on / api.post）、自定义图标（放插件目录 + `/plugins-assets/<名>/<文件>` 引用）、开发调试（backend `/reload plugins` 热生效、frontend 刷新即生效）、自己的插件进随包分发的路径（`src/assets/plugins`）。接线：src/agent_config.py `seed_dir_docs` 目录循环加 `(".agent/plugins", "plugins")`——六个扩展目录凑齐；明细见 [插件 SDK · 开发文档播种](../architecture/plugin-sdk.md)
- **`.agent/agents/README.md` 的消费端闭环**（2026-10-06 · 三，commit e0d0060）：该目录说明此前会被 [/agents 管理页](agents-admin.md)当 name=README 的伪 agent 列出（平铺 `*.md` 扫描）——现 `load_agents_index` 显式跳过，页面改提供「📖 目录说明」只读渲染入口（`GET /api/agents/readme-doc`，python-markdown 渲染）
- **种源协议纠错（2026-10-12，9013 实例反馈，commit aae2f69）**：`tools_builtin.md` 种源示例此前教的是引擎不认的描述符字段（`parameters` 字段表 + `run` lambda）——真实协议是 `"func": 函数引用`，schema 从签名+类型注解自动推断、描述读 docstring。种源已修正 + 本 repo `tools/builtin/README.md` 同步覆盖（旧版为播种原版、非用户改动，覆盖安全）；其余 5 份 dir_docs 同日扫描无同款问题。**扩散路径**：其它实例删旧 README 重启重播种，或 `/update-assets`（三方 hash：未改过的播种原版被新版覆盖）。明细见 [工具外置 · 播种 README 协议纠错](tool-externalization.md)

## seed_default_skills · test-svc 示例技能（2026-09-28，用户提案「最小实现播种给每一个 repo」）

- **源**：随包 `src/assets/skills/test-svc/`（打 wheel 带上）；**目标** `.agent/skills/test-svc/`
- **判存在**：以 `<技能>/SKILL.md` 为准——已存在则整技能跳过（不覆盖用户修改）；整目录拷贝（排除 `__pycache__`）+ seed_state 基线
- **包内四文件 × [七件套](skills.md)全覆盖**：

| 文件 | 对应工具 | 演示什么 |
|---|---|---|
| `SKILL.md` | read_skill / skill_navigate | SOP 本体 + 章节；内嵌**「文件↔工具」对照表**和全部用法 |
| `server.py` | skill_equip / skill_use | 常驻**有状态**计数器服务（协议两标记 `===CAPS_END===` / `===DONE===` + add/show/echo） |
| `scripts/greet.py` | skill_run_code | 包内脚本一次性执行（CLI 参数 `名字 --times N`） |
| `scripts/evaluate.py` | skill_evaluate | 验收入口：三项自检 → `EVAL PASS` |

Agent 打开 SKILL.md 第一眼就是对照表——每个工具该拿这个包的哪个文件练、怎么调，全在里头。

## seed_default_agents · bundled agents：desktop-operator 自包含四件（2026-10-06，用户提案「把 desktop-operator 也添加到播种吧」，commit 789d88e）

- **源 → 目标**：`src/agents/<name>/`（随包 bundled）→ `.agent/agents/<name>/`；判存在跳过 + seed_state 基线。当前 9 个：coder / **coder-py** / **coder-cs** / desktop-operator / explorer / reviewer / vision / wiki-updater / **worker**（2026-10-07 增两个 coder 语言变体，commit `55abf87`——各挂语言专属 after_tool 诊断钩子 + LSP 工具，见 [coder 语言变体](../architecture/multi-agent.md#coder-语言变体coder-py--coder-cs按语言挂检查钩子--配-lsp-工具2026-10-07用户提案commit-55abf87)；**2026-10-15 增施工 worker**，commit `3dc3f7b`——施工委托的执行者（[施工委托](construction-delegation.md)）；两批均 yml+md 两件、无 tools/；本 repo 两侧直接落盘，其它 repo 升级后下次播种即得）
- **desktop-operator 四件（自包含）**：`desktop-operator.yml`（services 已改自包含路径）+ `desktop-operator.md` 人设 + `tools/desktop_tools.py`（10 个键鼠/剪切板/窗口专属工具）+ `tools/image_feed_poc.py`（桌面画面服务）——[目录形态](../architecture/multi-agent.md)整目录随包，新 repo 开箱即得「拉起 Agent 即自带眼睛」（[image_feed](image-feed.md)）
- **services 命令自包含化**：通用 repo 没有本 repo 的 `tools/image_feed_poc.py`，声明命令改为 `.agent/agents/desktop-operator/tools/image_feed_poc.py`（workspace 相对路径，[services 拉起 cwd=workspace](../architecture/multi-agent.md)直接可用）
- **顺带两修（不做这轮就等于白播）**：
  1. **pyproject 打包 glob 失配**：`agents/*.md` / `agents/*.yml`（平铺 glob）在源目录化后匹配不到任何文件——目录化后 6 个 agent 全都不会进 wheel；改 `agents/*/*.md` + `agents/*/*.yml` + `agents/*/tools/*.py`
  2. **seed 只扫一层**：原实现 `sub.iterdir()` 只收 `.yml`/`.md`，自包含 agent 的 `tools/*.py` 不随行（首轮验证 12 文件断言 False 抓到）；改 `rglob` 递归 → 重播 14 文件、desktop-operator 四件自包含 ✅
- **通用环境两个前提**（播出去≠能用）：① 需**视觉模型**——无 glm key 回退主模型，非 vision 则 image_feed 门控静默跳过（agent 可起但看不到桌面）；② `pip install pyautogui pyperclip`——缺依赖 import 失败仅 warning 空工具箱，**不炸实例化**
- **验证**：重播 14 文件（coder / explorer / reviewer / vision / wiki-updater 各 2 件 + desktop-operator 4 件）；site-packages 已同步，本机其它 repo 下次播种即得

## seed_gitignore · 首启 .gitignore 追加清单（`ensure_gitignore` / `_GITIGNORE_SEED`，2026-10-24 提案 → 2026-10-26 事故修正）

- **动机**：新 repo 首启后 `.gitignore` 空白，agt 的自动生成物（快照仓 `.agt/`、`__pycache__`、运行时状态 json……）全冒进 `git status`，用户一上手就是满屏垃圾。首启自动补一份清单，让 status 干净。
- **形态**（`src/agent_config.py`，紧跟 `seed_dir_docs` 之后）：模块级常量 `_GITIGNORE_SEED`（**首元素是分段注释标题行**，其后为条目）+ `ensure_gitignore(workspace) -> str`
- **幂等与边界**：已有条目按 **尾斜杠归一**比对（`.agent` 与 `.agent/` 视为同条）不重复追加；无 `.gitignore` 则创建；**非 git 仓库也写**（将来 `git init` 即生效）；任何异常吞掉返回空——播种链不炸（与其它 seed 同纪律）
- **当前清单（2026-10-26 版，1 行注释 + 12 条）**：`.agt/`、`.playwright-mcp/`、`__pycache__/`、`.pytest_cache/`、`.DS_Store`、`.agent/hooks_state.json`、`.agent/snapshots_state.json`、`.agent/remote_servers.json`、`.agent/wiki_queue/`、`.agent/rag.json`、`.agent/rag/`、`.agent/snapshots/`——**只收运行时状态/缓存，一个种源目录都不收**

### 事故：原版整包追加 `.agent/`，把发版链炸在 ② 里（2026-10-26，release 实锤）

- **原版清单里带一整条 `.agent/`**（整包忽略）——但 `.agent/` 是**工作目录 + 种源目录双身份**：workflows / agents / skills / nodes / plugins / wiki / rules / README 全在里面；本 repo `git ls-files .agent` = **229 个已跟踪文件**
- **炸点不在首启、在下游**：`release.py` 暂存用**字面路径** `git add .agent/workflows`（不带 `-f`）→ `The following paths are ignored by one of your .gitignore files: .agent` → 发版链在 bump 之后中断，留半成品工作区（`src/paths.py` 已 bump、`version_file.txt` 已改）
- **修正**：清单改成**只忽略 `.agent/` 下的运行时状态条目**（hooks_state / snapshots_state / remote_servers.json / wiki_queue / rag / snapshots），种源照常进版本控制；本 repo `.gitignore` 同步删掉那条 `.agent/`（该行正是种子追加的，删后 84 → 83 行）
- **同款教训已是第二次**：[多实例组网](../architecture/multi-instance.md) 记 `remote_servers.json` 落盘时就写明「`.agent/` 不能整目录忽略——workflows/agents/skills/wiki 要提交，精确加单条」；本次是把它**写进种子**放大成全框架默认行为。**口诀：`.agent/` 永不可整包忽略**
- **未收口的残留**：`ensure_gitignore` 只**追加**、不**删改**存量条目——被上一版种子写进 `.agent/` 的其它 repo 升级后不会自愈，需手工删那一行（本 repo 已删，其它实例自检一次）
- **发版视角的连带口径**：发版工具 ① 工作区检查只看「脏不脏」，看不见 ignore 规则——**① 通过 ≠ 链路能跑**，被 ignore 的跟踪目录要到 ② 才炸（详见 [release_publish · 第六次实战](release-publish.md)）

## 验证


- 临时 workspace：首次播种 7/7 → 幂等（二次 0）→ 手改 README 后 seed 不覆盖（**2026-10-15 收编后为 5/5**，**2026-10-16 加 plugins 后为 6/6**：前述 5 目录 + `.agent/plugins`，旧 `tools/builtin`、`nodes` 不再创建）
- test-svc 九项全绿：read / navigate（树+标题大纲）/ run_code（greet 两行问候）/ evaluate（EVAL PASS）/ equip（能力清单）/ use `add 5`→`add 3` = count 8（**跨调用状态保持**）
- 本 repo 实测：6 份 README + test-svc 落位（收编后 5 份；**2026-10-16 播种 `.agent/plugins/README.md` 后本 repo 目录说明为 6 份**，`tools/builtin/README.md`、`nodes/README.md` 已删）

## 注意事项

- **加调用必须同步查顶部 import 行**（commit ed26ac0 教训）：`seed_default_skills` 曾漏加 `chat.py` 顶部 import → `build_agent` NameError **启动即炸**（build_agent 在 web_main 早期，服务都起不来）；且函数级子进程直测验不出模块 import 链——**验证必须含 `import chat`**，语法检查不查运行时 NameError
- 播种一律「已存在跳过」，不做内容更新；框架文档升级走 /update-assets 管道，不走重复播种

## 相关页面

- [技能体系](skills.md) —— 八件套与 skill_equip/use/unequip 协议（test-svc 是它的活教材）
- [工具外置体系](tool-externalization.md) —— tools/builtin 目录收敛与任务脚本防御（README 内容来源之一）
- [多 Agent 体系](../architecture/multi-agent.md) —— 子 Agent 目录形态（agents.md 播种内容之一）
- [节点插件化](../architecture/node-plugins.md) —— `.agent/nodes/` 目录约定（nodes.md 播种内容来源）
