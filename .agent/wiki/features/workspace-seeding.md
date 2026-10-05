# workspace 播种 · 约定目录 README + 示例技能 + 自包含 agents（seed_dir_docs / seed_default_skills / seed_default_agents）

> 挂点：`src/chat.py build_agent()` 播种序列——`seed_default_agents`（bundled agents 目录播种；2026-10-06 起 desktop-operator 以自包含四件加入，见下文专节）→ `seed_dir_docs`（commit 0ec4ec7）→ `seed_default_skills`（commit 16aede0）。随每次启动检查，幂等，让每个 repo（含新装用户）的约定目录「开箱即懂」。

## seed_dir_docs · 约定目录 README（用户提案：把框架那一页文档以 README.md 播种到目录位置）

- **文档源**：`src/dir_docs/`（随包分发，pip 用户同样生效）——`tools_builtin.md`（脚本工具 agt_register 约定 + [任务脚本防御](tool-externalization.md)）、`agent_tools.md`、`workflows.md`（XML 骨架 / exec_workflow·debug·钩子）、`agents.md`（含子 Agent 目录形态）、`skills.md`（技能包结构 / 双层激活 / 七件套）、`nodes.md`（节点插件 .py+.js）
- **播种目标（7 个约定目录）**：`tools/builtin/`、`.agent/tools/`、`.agent/workflows/`、`.agent/agents/`、`.agent/skills/`、`nodes/`、`.agent/nodes/`
- **语义**：目录不存在 → 创建目录 + 播种（引导价值：一眼看到全部可扩展点）；`README.md` 已存在 → 跳过——**用户改动永不覆盖**
- **写入**：字节级 + `seed_state` 基线——接入 /update-assets 三方 hash 判定管道（框架升级后文档可安全更新）
- **`.agent/agents/README.md` 的消费端闭环**（2026-10-06 · 三，commit e0d0060）：该目录说明此前会被 [/agents 管理页](agents-admin.md)当 name=README 的伪 agent 列出（平铺 `*.md` 扫描）——现 `load_agents_index` 显式跳过，页面改提供「📖 目录说明」只读渲染入口（`GET /api/agents/readme-doc`，python-markdown 渲染）

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

- **源 → 目标**：`src/agents/<name>/`（随包 bundled）→ `.agent/agents/<name>/`；判存在跳过 + seed_state 基线。当前 6 个：coder / desktop-operator / explorer / reviewer / vision / wiki-updater
- **desktop-operator 四件（自包含）**：`desktop-operator.yml`（services 已改自包含路径）+ `desktop-operator.md` 人设 + `tools/desktop_tools.py`（10 个键鼠/剪切板/窗口专属工具）+ `tools/image_feed_poc.py`（桌面画面服务）——[目录形态](../architecture/multi-agent.md)整目录随包，新 repo 开箱即得「拉起 Agent 即自带眼睛」（[image_feed](image-feed.md)）
- **services 命令自包含化**：通用 repo 没有本 repo 的 `tools/image_feed_poc.py`，声明命令改为 `.agent/agents/desktop-operator/tools/image_feed_poc.py`（workspace 相对路径，[services 拉起 cwd=workspace](../architecture/multi-agent.md)直接可用）
- **顺带两修（不做这轮就等于白播）**：
  1. **pyproject 打包 glob 失配**：`agents/*.md` / `agents/*.yml`（平铺 glob）在源目录化后匹配不到任何文件——目录化后 6 个 agent 全都不会进 wheel；改 `agents/*/*.md` + `agents/*/*.yml` + `agents/*/tools/*.py`
  2. **seed 只扫一层**：原实现 `sub.iterdir()` 只收 `.yml`/`.md`，自包含 agent 的 `tools/*.py` 不随行（首轮验证 12 文件断言 False 抓到）；改 `rglob` 递归 → 重播 14 文件、desktop-operator 四件自包含 ✅
- **通用环境两个前提**（播出去≠能用）：① 需**视觉模型**——无 glm key 回退主模型，非 vision 则 image_feed 门控静默跳过（agent 可起但看不到桌面）；② `pip install pyautogui pyperclip`——缺依赖 import 失败仅 warning 空工具箱，**不炸实例化**
- **验证**：重播 14 文件（coder / explorer / reviewer / vision / wiki-updater 各 2 件 + desktop-operator 4 件）；site-packages 已同步，本机其它 repo 下次播种即得

## 验证

- 临时 workspace：首次播种 7/7 → 幂等（二次 0）→ 手改 README 后 seed 不覆盖
- test-svc 九项全绿：read / navigate（树+标题大纲）/ run_code（greet 两行问候）/ evaluate（EVAL PASS）/ equip（能力清单）/ use `add 5`→`add 3` = count 8（**跨调用状态保持**）
- 本 repo 实测：6 份 README + test-svc 落位

## 注意事项

- **加调用必须同步查顶部 import 行**（commit ed26ac0 教训）：`seed_default_skills` 曾漏加 `chat.py` 顶部 import → `build_agent` NameError **启动即炸**（build_agent 在 web_main 早期，服务都起不来）；且函数级子进程直测验不出模块 import 链——**验证必须含 `import chat`**，语法检查不查运行时 NameError
- 播种一律「已存在跳过」，不做内容更新；框架文档升级走 /update-assets 管道，不走重复播种

## 相关页面

- [技能体系](skills.md) —— 七件套与 skill_equip/use 协议（test-svc 是它的活教材）
- [工具外置体系](tool-externalization.md) —— tools/builtin 目录收敛与任务脚本防御（README 内容来源之一）
- [多 Agent 体系](../architecture/multi-agent.md) —— 子 Agent 目录形态（agents.md 播种内容之一）
- [节点插件化](../architecture/node-plugins.md) —— nodes/ 目录约定（nodes.md 播种内容来源）
