# release_publish · 一键发布工具（`.agent/tools/release_tools.py`，本 repo 自用）

> 用户提问触发（2026-10-12）：「我们现在发布补丁的时候好像需要进行好几步 run_python 什么的是为啥，我记得做成本地工具了吧」——记忆偏差澄清：主体 `release.py` 早已一步化（bump + build + upload + push 一条命令），但**外围三步**（工作区检查 / version_file 同步提交 / PyPI 上线验证）此前一直靠 run_shell / run_python 手工串联。本工具把外围三步与 release.py 主体串成**一次工具调用**（commit `a70b991`）。

## 职责与四步语义

| 步骤 | 做什么 | 失败语义 |
|---|---|---|
| ① 工作区检查 | `git status --short`，脏则停并列出改动（截前 10 行） | ❌ 中断（dry_run 豁免——预览允许脏） |
| ② release.py 主体 | `python release.py {part} [-m msg] -y`：版本 bump（src/paths.py）+ build + upload + push | ❌ 中断，输出尾部 8 行带回，后续步骤不执行 |
| ③ version_file.txt 同步提交 | `git add packaging/version_file.txt` + commit + push | ⚠️ 只警告不中断（rc 非 0 时附输出前 200 字） |
| ④ PyPI 上线验证 | GET `pypi.org/pypi/agt-agent/{ver}/json`，报告上线件数与文件名 | 20s × 3 重试，三连失败 ⚠️ 提示「索引可能仍在同步，稍后手动确认」 |

- **失败即中断、如实报告**：②（含 upload）失败时 ③④ 不执行
- **版本号解析**：从 release.py 输出「📦 版本：X → Y」正则取 Y；解析不到则 ④ 跳过并注明「⏭ 跳过（版本号未解析到）」
- **④ 的 20s×3 重试**：PyPI 索引生效有 ~20s 延迟，上传完立即查必 404

## 签名与 dry_run

```python
release_publish(part="patch", message="", dry_run=False) -> str
```

| 参数 | 说明 |
|---|---|
| `part` | patch（默认）/ minor / major，或具体版本号（如 `0.35.0`） |
| `message` | 发布 commit 信息（缺省由 release.py 自动生成） |
| `dry_run` | True = 只预览 + build，不上传不 commit/push（release.py `--dry-run`，版本号与暂存自动还原） |

dry_run 分支：跳过 ①（允许脏工作区）、② 传 `--dry-run`、③④ 不执行，返回带「✅ dry-run 完成（未 commit/upload/push）」前缀的尾部 8 行。

## 实现要点

- 纯 subprocess + urllib，**零框架依赖**——与外置件「不 import 框架」约定一致（同 [diff_tools 算法副本](diff-lines.md)思路）
- `Path(__file__).resolve().parent.parent.parent` 反推 repo 根（`.agent/tools/` 上三级；旧 `tools/builtin/` 同为三级，2026-10-15 迁移未影响此逻辑）；`_sh()` 统一封装 subprocess（cwd=repo 根、utf-8 + errors=replace，stdout+stderr 合流判错）
- `agt_register()` **无参**——本 repo 自用工具，操作对象是 repo 自身而非 workspace 数据，无需 ctx（同 [agentid_tools](agentid-tools.md) 形态）

## 注册形态：README 标准协议活范例

描述符即 `{"name", "func", "group", "params"}`——`func` 函数引用、schema 从签名+类型注解自动推断、`params` 提供参数级描述、描述从 docstring 读取。正是 [播种 README 协议纠错](tool-externalization.md)（同日 commit `aae2f69`）修正后的标准写法，**可作为从零写工具的照抄范例**。

## 验证（dry-run 实测，2026-10-12 开发轮）

```
✅ dry-run 完成（未 commit/upload/push）：
📦 版本：0.34.7 → 0.34.8
🗂  暂存 1 个文件：
      src/paths.py
🧪 dry-run：仅构建不上传。产物：agt_agent-0.34.8-py3-none-any.whl, agt_agent-0.34.8.tar.gz
   （版本号与暂存已还原，未提交任何东西）
```

正式发布入口：`/reload tools` 确认工具在列后，一句话「发吧」→ Agent 一次 `release_publish` 调用 + 贴结果，不再四连手工。

## 首次实战发版：v0.35.0（2026-10-16，part=minor）——② 半成功处置 + version_file 结构坑

dry-run 之后的首个生产运行，一次调用走完 ①，卡在 ② 内部末段——工具的失败报告按设计如实带回内部三段状态：

- **✅ 构建完成**：`agt_agent-0.35.0` whl + tar.gz；**✅ 已上传 PyPI**（人工端点三连验证过：`pypi.org/pypi/agt-agent/0.35.0/json`；国内镜像滞后 10~30 分钟属正常）
- **❌ git push 失败**：`ssh.github.com:443` 当日持续超时 → ② 整体判失败，**③④ 未执行**（符合「失败即中断」语义，输出明示「后续步骤未执行」）
- **部分成功粒度**：② 内部是 bump→build→upload→push 串联，push 挂在末段时 PyPI 其实已上线、bump commit（`897556c`）已落本地——按失败报告逐项补尾即可，**不要误判发布失败而重发**（PyPI 同版本号重复 upload 会被拒）
- **② 半成功的收尾清单**：人工验证 PyPI 端点 → 本地 commit+tag 重试 push（本次挂后台每 10 分钟重试；tag 上去自动触发 GitHub Actions 桌面版云构建）→ 补 ③ 的 version_file 提交

### 坑：version_file.txt 是 VSVersionInfo 结构文件，不是裸版本号（本次先踩后恢复）

- `packaging/version_file.txt` 是 pyinstaller `--version-file` 消费的 **Python VSVersionInfo 字面量**——版本号要在 **filevers/prodvers 元组 + FileVersion/ProductVersion 字符串共四处**字段级同步；整文件覆写成 `"0.35.0\n"` 会炸掉结构（本次实操先踩：原结构从调用记录原文捞回，字段级重写 + 语法校验过）
- **它长期无人编辑**：③ 的「同步」只是 git 层面的提交搬运、从不改文件内容，版本字段更新历来靠人工——而自 v0.26.4 后没人做过，其间所有版本的桌面包 Windows 资源元数据都停在 0.26.4（仅影响文件属性显示，不影响功能）
- **改进方向**（未做）：release.py bump 时顺带字段级同步 version_file.txt，或 ③ 改为「版本一致才提交」的校验式提交

## 第二次实战：v0.35.1（2026-10-16，part=patch）——主链路一次全绿，但 tag 不在链路里（手工补打）

dry-run、v0.35.0 之后的第二个生产运行：**一次调用走完四步、零手工补尾**（对照上文 v0.35.0 的 ② 半成功），用户侧只有一句「发补丁吧」。

| 步骤 | 结果 |
|---|---|
| ① 工作区检查 | ✅ 干净 |
| ② release.py 主体 | ✅ 0.35.0 → 0.35.1；产物 `agt_agent-0.35.1-py3-none-any.whl` + `.tar.gz`；upload ✓；push ✓ `origin main (2f03ccc..2b6f7e6)` |
| ③ version_file.txt 同步 | ✅ 字段级同步 0.35.1 并提交推送（FileVersion / ProductVersion 四处，见上文结构坑） |
| ④ PyPI 上线验证 | ✅ 两件就位 <https://pypi.org/project/agt-agent/0.35.1/> |

### 缺口：release.py 只推分支、不打 tag——桌面版 CI 不会触发

发布完成后核对远端 tag，结果**只有 `v0.29.0` / `v0.35.0`，没有 `v0.35.1`**——② 的 push 只推 **main 分支**，**tag 从来不在链路里**（v0.35.0 的 tag 也是当日 push 失败后人工补的，见上文 ② 半成功收尾清单：**同款缺口第二次出现**）。

- **危害不在「发布失败」，而在「桌面版静默不发」**：Windows Release 由 tag 触发 GitHub Actions 云构建（见 [desktop-mode · 云构建](desktop-mode.md)）——不补 tag 这版桌面版永远不会出，且没有任何报错提示
- **本次手工补法**：`git push origin <含 version_file 同步的 commit>:refs/tags/v0.35.1` → `v0.35.1 -> a68a9fb`（打在含 version_file 同步的那个 commit 上，正是 CI 构建需要的状态）
- **改进方向（未做）**：把「打 tag + 推 tag + 远端核验」纳入链路——或 release_publish 在 ④ 之后加一步 ⑤（`git ls-remote --tags` 核验，缺则 `git push origin HEAD:refs/tags/v{ver}`），或 release.py 内部直接打 tag。在此之前，**正式发布收尾必须手工核验远端 tag**——口诀：**PyPI 上线 ≠ 桌面版会出包**（`git ls-remote --tags origin` 一眼见分晓）

### 本版收录清单（自 v0.35.0 的四处修复）

| 修复 | 详情页 |
|---|---|
| serve-web 缓存半残致无限 panic 三层根治（`_dir_ok` 完整性判据 / `_drop_dir` 先 rename 探测占用 / `_prefetch` 单例锁 + 全路径直起 + `--port 0`）+ 救场脚本 `tools/_fix_serveweb_cache.py` | [webide](webide.md) |
| `_probe` 4xx/5xx 分支读 body 判特征——serve-web「启动中」的 503 不再误报「被其他程序占用」 | [webide](webide.md) |
| 章节工具内标题自动降级（content 里写 `##` 不再与父级撞层级） | [wiki 工具集](wiki-tools.md) |
| 写 wiki 即消费攒批队列（pending → events 轮转）+ 消费冷却窗（`last_consumed.json` 距上次消费 < 600s 走「入队不派活」） | [wiki 工具集](wiki-tools.md) · [wiki_auto_maintenance](wiki-auto-maintenance.md) |

**生效提示**：pip 索引约 20s 延迟，`pip install -U agt-agent` 后 `/restart` 生效；`pip install -e .` 的本机生产实例改的就是本 repo 代码，配合 `/reload tools`（wiki 工具）/ `/reload plugins`（webide 插件）即可免重启吃到修复。

## 与其他模块的关系

- **release.py**（repo 根）：发布主体（bump/build/upload/push），本工具是它的编排外壳；release.py 版本真源迁移史见 [desktop-mode · 发布链修复](desktop-mode.md)
- **packaging/version_file.txt**：桌面版云构建的 CI 版本戳源（`tools/ci_stamp_version.py` 消费），见 [desktop-mode · 云构建](desktop-mode.md)——所以 ③ 必须同步提交推送，否则 GitHub Actions 打出的桌面包版本号落后
- [tool-externalization](tool-externalization.md)：外置工具体系清单行（发布组）

## 注意事项

- **不随包播种**（本地私有工具层，同 team_tools / agentid_tools）——发版编排只属于本 repo，src/assets/tools_builtin/ 不放副本
- ① 的脏检查只在非 dry_run 生效：正式发版前保持工作区干净是硬约束（release.py 会 bump src/paths.py，混合未提交改动发出去不可控）
- ③ 失败只警告不阻断（⚠️）：version_file 推送异常需人工补，否则下次桌面版云构建用旧版本号
- ④ 三连失败大概率只是索引同步延迟——按提示稍后手动确认即可，不代表发布失败
- **tag 不归本工具管**（v0.35.0 / v0.35.1 两次实战实证）：② 只推 **main 分支**，tag 需事后手工补打 → **正式发布收尾必须核验远端 tag**（`git ls-remote --tags origin`）——缺 tag = 桌面版 CI 不触发、包不出且无报错（口诀：PyPI 上线 ≠ 桌面版会出包）。补法与改进方向见上文 [v0.35.1 缺口一节](#第二次实战v03512026-10-16partpatch主链路一次全绿但-tag-不在链路里手工补打)

