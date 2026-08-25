# wiki_auto_maintenance · wiki 自动维护与提交

> 工作流：`.agent/workflows/wiki_auto_maintenance.xml`
> 职责：主 Agent 完成开发任务后，自动维护 `.agent/wiki/` 知识库页面并**自动 git 提交推送**，形成"改代码 → 更新文档 → 提交"的闭环。
> **v0.18.2 正式发布**。

## 背景：为什么需要 commit_wiki

主 Agent 在开发迭代中会调用 `update_wiki`（wiki-updater 子 Agent）增量更新 wiki 页面，但主 Agent 自身**不提交 wiki 文件**——它只改代码和文档，git add/commit/push 由其他机制负责。这导致 wiki 改动经常滞留在工作区，未被版本控制跟踪，知识库与代码长期脱节。

**commit_wiki 节点**解决了这一矛盾：在 update_wiki 之后自动 `git add .agent/wiki/` 并检测变更，有变更则 commit+push，无变更则静默跳过。

### 提交失败问题（2026-08 修复）

**旧方案根因**：commit_wiki 此前用 `run_shell`（shell 命令）拼接 `git commit -m "<msg>"`。当 commit message 含**多行文本**（update_wiki 报告摘要换行）时，shell 转义会破坏引号/换行，导致 git 提交失败——**run_shell 版从未成功提交过**。

**新方案**：将 commit_wiki 从 `run_shell` 改为 **`git_commit` 节点**，并通过 subprocess **列表参数**传递 commit message（不经过 shell 字符串拼接），彻底规避多行/特殊字符的转义问题。同时自动追加 `Co-authored-by` 署名。**实战验证**：commit `1577693` 是新链路第一次成功提交，`0293eec` 是快照子工作流重构后的提交。

### dict split 报错修复（2026-08，commit edd9851）

**现象**：commit_wiki 节点报错 `'dict' object has no attribute 'split'`。

**根因链**（单元级复现确认）：

```
wf_diff_snapshots 是【工具节点】（plugin, toolName=wf_diff_snapshots）
  → Tool.run() 把 end 的 {files, count, changed} json.dumps 成字符串
  → _handle_plugin 的 _try_parse 又解析回 Python dict
  → 1400227.raw = {files: "...", count: 3, changed: [...]}   ← dict 对象！
  → commit_wiki.files ← 1400227.raw（整个 dict）
  → git_commit 里 files.split(",") 对 dict 调 split → AttributeError
```

**修复**（commit `edd9851`）：

| 改动 | 说明 |
|------|------|
| `commit_wiki.files` 引用 | `1400227.raw` → **`1400227.files`**（dict 字段引用，`_dotted_get` 直接取字段） |
| `1400227` 补 out 声明 | `<out name="files" type="string"/>` + `count`——`_extract_field` 按声明字段填充，编辑器下拉也能选到 files 端口 |

**实测验证**：`raw=dict{files,count,changed}`（根因复现）→ `1400227.files` 解析为 `'.agent/wiki/a.md,.agent/wiki/new.md,.agent/wiki/old.md'` 逗号分隔 string，`git_commit` 直接消费成功。**在编辑器里重新打开此工作流，把 files 输入的连线从 raw 拖到 files 端口即可**（out 声明已在 XML 里）。

**经验**：工具节点（如 wf_diff_snapshots）raw 返回的是 **dict**，消费端必须引用其**具体字段**（`files` / `count` / `changed`，`_dotted_get` 直接取）并**补 `<out>` 声明**；把整个 dict 当字符串 `.split(",")` 会报 `'dict' object has no attribute 'split'`。

### path 字面量保存后重开变空（2026-08 修复，commit 910fc1b）

**现象**：snap_before 的 `path` 字段手动输入字面量 `.agent/wiki/` 保存后，重新打开工作流输入框又变空。

**误诊结论（2026-08 初期）**：后端三层验证正常（磁盘 XML 有值、xml_to_canvas 读回正确、GET /api/wf 返回正确），**初步判断为前端浏览器缓存问题**，建议硬刷新排查。

**实际根因**（2026-08-18 定位）：问题在 **`syncSubworkflowNode`**（`workflow_editor.html` 中 openWf 时对 type 9 节点重跑的 schema 同步）：

```javascript
// 修复前：只保留连线，且默认值造出 blockID='' 的空 ref
const prev={};
(...).forEach(p=>{if(p.input?.value?.content?.blockID)prev[p.name]=...});
n.data.inputs.inputParameters=(...).map(o=>({...value:{type:'ref',content:{blockID:prev[o.name]||'',...}}}));
// ↑ 空 blockID 的 ref——既不是连线（blockID 空）也不是 literal → makeInputControl 的
//   cur=(v?.type==='literal')?v.content:'' 恒空 → 输入框空白

// 修复后（commit 910fc1b）：连线 ref 完整保留 + 非空字面量保留；默认值改空 literal
```

**数据链路验证**：

| 环节 | 结果 |
|------|------|
| 磁盘 XML | ✅ `<in name="path" type="string">.agent/wiki/</in>` 有值 |
| xml_to_canvas | ✅ 读回 `{type:'literal', content:'.agent/wiki/'}` |
| GET /api/wf | ✅ 返回的 canvas 里 path 也是 `.agent/wiki/` |
| **前端渲染（syncSubworkflowNode）** | ✅ **已修复——输入框正确显示** |

**修复范围**：所有 type 9 节点的输入框（画布节点内，非右侧属性面板）。

**生效方式**：改在**服务启动时载入内存的文件**（`src/static/workflow_editor.html`）——`/restart` 后 **Ctrl+F5 强刷编辑器**，重新打开 wiki_auto_maintenance → path 输入框显示 `.agent/wiki/`（不再空）。

## 工作流结构（2026-08-19 更新：新增 changed_calls 直供链）

```
start（user_message + changed_calls ← 引擎透传的本轮变更调用）
  → 判官 llm（needs_maintenance?）
  → snap_before（子工作流 dir_snapshot，path=.agent/wiki/，打【更新前】基线快照）
  → fmt_calls（code 节点：把 changed_calls 渲染为变更调用原文摘要）
  → 拼接模板（text 节点：user_msg + turn_ctx + changed_calls 摘要 + answer 草稿 → summary）
  → update_wiki（plugin 节点，summary 作为任务文本调 wiki-updater 子 Agent，输出报告摘要）
  → diff_wiki（code 节点：本节点执行时拍【更新后】after 快照 + 调 diff_snapshots 子工作流 diff）
  → has_changes?（selector：count>0）
      ├─ true  → build_msg（text，拼 commit message=判官摘要）→ commit_wiki（git_commit 节点）
      └─ false → 静默跳过（end）
  → end
```

| 节点 | 类型 | 职责 |
|------|------|------|
| start.changed_calls | start 输出 | 引擎透传的本轮变更调用列表（`_turn_changed_calls`，数组原样） |
| 判官 llm | llm | 判断本轮是否值得维护；输出 `needs_maintenance` 布尔 + `summary` 摘要（供 commit message 复用） |
| snap_before | **子工作流 `dir_snapshot`** | 对 `.agent/wiki/` 目录打文件快照（mtime 映射，JSON 字符串），作为**更新前基线** |
| fmt_calls | **code** | 将 `changed_calls` 渲染为可读摘要（调用参数原文 + 变更文件 + 结果预览）；空数组/JSON 字符串容错 |
| 拼接模板 | text | 组装 update_wiki 的任务文本：user message + turn context + 变更调用原文 + answer 草稿 |
| update_wiki | **plugin（update_wiki 工具）** | 调用 update_wiki 工具更新/新建受影响 wiki 页面；输出报告摘要 |
| diff_wiki | **code 节点** | 在**本节点执行时**拍 after 快照（必须此时拍，拍早了 diff 不到 update_wiki 的变更），调 `diff_snapshots` 子工作流对比 before/after 生成变更清单；带本地兜底 |
| build_msg | **text** | 接收判官摘要，拼装为语义化 commit message |
| commit_wiki | **git_commit 节点** | 按 diff 变更清单 `git add` 相关文件 → `git commit`（列表参数传 message）→ `git push`；无变更跳过 |

### 推理减负：changed_calls 直供（2026-08-19，commit 16d6832）

**问题**：wiki 维护子 Agent（wiki-updater）此前只收到判官一句摘要，需要**read_file 逐个重读改过的源文件**才能理解改动——一轮改 5 个文件就是 5 次大读 + 上下文膨胀，子工作流整体耗时特别长，推理负担重。

**解决方案**：引擎在工具执行前后本就有 workspace 快照 diff（检测文件变更），把**有文件变更的调用原文**保存在内存（`_turn_changed_calls`）并透传给 before_answer 钩子。工作流内 `fmt_calls` 渲染后拼进 update_wiki 的任务文本，子 Agent 的上下文变成：

```
user message + 有过文件变更的工具调用步骤原文 + answer 草稿
```

任务文本中包含（fmt_calls 渲染格式）：

```
changed tool calls（引擎快照 diff 检出的文件变更调用原文——以此为准，一般无需再 read_file）:
1. edit({"path": "src/agent.py", "old_string": "...", "new_string": "..."})
   变更: src/agent.py(modified)
   结果: ✅ 已替换 1 处
2. write_file({"path": "docs/x.md", "content": "..."})
   变更: docs/x.md(new)
```

**效果**：edit 的 diff（old/new）、write 的内容**直接在任务描述里**——子 Agent 一步看懂改动，直接 wiki_write，省掉整个重读探索阶段。

**引擎侧配套**（`src/agent.py`，同 commit）：
- 快照条件扩展：after_tool **或** before_answer 任一钩子存在即拍快照（之前只有 after_tool 在时才拍——本场景 before_answer 钩子拿不到变更信息）
- 三条执行路径全覆盖：逐 call（钩子模式）/ 单 call / 并行（整批 diff 归属批内每个调用，多报不漏报）
- 详见 [changed_calls 变更调用收集（workflow-hooks）](../architecture/workflow-hooks.md#changed_calls-变更调用收集before_answer-透传2026-08-19)

**验证**：链路（start.changed_calls→fmt_calls→拼接模板→update_wiki.summary）/模板/渲染（变更清单+参数+结果预览）/JSON 字符串容错/往返幂等全过。`/restart` 后生效。

### build_commit_msg：动态 commit message

**新增背景**（2026-08-18）：此前 commit_wiki 使用固定文案，git log 中无法区分每次 wiki 维护改了什么。新增 `build_msg`（text 节点）将判官摘要注入 commit message，使每条提交都带有具体内容摘要。

- **节点类型**：text（静态模板，引用 `{{LLM_5.summary}}`）
- **输出格式**：`docs: wiki maintenance - {{summary}}`
- **与 build_commit_msg 区分**：本节点的 `build_msg` 是 text 节点；`build_commit_msg` 是旧方案（已废弃）

### snap_before / diff_wiki：快照与变更清单（重构为子工作流，2026-08）

**重构背景**（2026-08-18）：原 diff_wiki 内嵌快照逻辑（code 节点里直接调用 `dir_snapshot` 工具 + 手动 diff），耦合度高、不易复用。拆为**通用子工作流**后：

- `dir_snapshot`：独立子工作流，对目录打快照（可复用至其他需要快照的场景）
- `diff_snapshots`：独立子工作流，对比两份快照输出变更清单
- diff_wiki 简化为：先拍 after 快照 → 调 diff_snapshots 对比 before/after

详见 [dir_snapshot / diff_snapshots 通用子工作流](../architecture/snapshot-diff.md)。

### commit_wiki 核心逻辑（git_commit 节点）

```xml
<node id="commit_wiki" type="git_commit" x="1800" y="300">
  <in name="files" source="1400227.files"/>
  <in name="message" source="build_msg"/>
  <config>
    <add_scope>.agent/wiki/</add_scope>
  </config>
</node>
```

- **files**：`1400227.files`（diff_snapshots 的输出，逗号分隔的文件路径）
- **message**：`build_msg`（判官摘要 + Co-authored-by）
- **add_scope**：`git add` 的范围限制（防止误提交其他文件）
- **执行逻辑**：无变更（`files=''` 或 `count=0`）→ 静默跳过；有变更 → `git add` → `git commit` → `git push`

**与子工作流的关系**：`dir_snapshot` / `diff_snapshots` 是通用子工作流（在 `.agent/workflows/`），`commit_wiki` 是主工作流内的节点。重构后主工作流更清晰，子工作流可被其他工作流复用（如代码变更检测、配置变更审计等）。

## 与其他模块的关系

- **工作流引擎**：`src/workflow.py` + `src/workflow_xml.py`，执行子工作流调用（type 9 节点）
- **引擎快照与 changed_calls**：`src/agent.py` `_fs_snap` / `_turn_changed_calls`——before_answer context 透传变更调用原文（本工作流的核心输入之一）
- **Git 集成**：`src/git_utils.py`，`git_commit` 节点的底层实现（subprocess 列表参数）
- **Wiki 更新工具**：`tools/update_wiki.py`，`update_wiki` 插件节点的实现；wiki-updater 子 Agent 用 wiki 十件套写页面（2026-08 起含章节级四件套，【增量维护优先】，见 [wiki-tools](wiki-tools.md)）
- **前端编辑器**：`src/static/workflow_editor.html`，输入框渲染（已修复字面量保存问题）

## 注意事项

- **changed_calls 空值容错**：本轮无文件变更时 `changed_calls` 为空数组，fmt_calls 输出"(本轮无文件变更)"占位——不能假设其恒非空；引擎传参也可能被序列化成 JSON 字符串，fmt_calls 需兼容两种形态
- **工具节点输出是 dict**（2026-08 修复，commit edd9851）：消费端必须引用具体字段（`files` / `count` / `changed`），不能引用整个 dict
- **subworkflow 节点 literal 属性约定**（2026-08）：传字面量参数时，**literal 必须用属性形式 `literal="值"`**，不能用于子元素形式（`<literal>值</literal>`），否则参数传递失败（子工作流收不到字面量）→ path 空 → 快照全盘 → WinError 206
- **前端输入框字面量**（2026-08 修复，commit 910fc1b）：type 9 节点的输入框（画布节点内）字面量保存后重开变空——`syncSubworkflowNode` 只保留连线，默认值造出 `blockID=''` 的空 ref → 输入框空白。修复后连线 ref 完整保留 + 非空字面量保留；默认值改空 literal。**生效需 `/restart` + Ctrl+F5 强刷编辑器**
- **路径字面量**：`path` 参数通常用字面量（如 `.agent/wiki/`），保存在磁盘 XML 中，后端链路正常（xml_to_canvas 读回正确、GET /api/wf 返回正确），前端渲染已修复
- **async 钩子并发写**（2026-08，wiki 四件套调试时实证）：本工作流是 async 钩子，可能与别的任务**并发写同一批 wiki 文件**（wiki-tools 往返测试的一次假阳性即源于此）——对 wiki 文件做测试/断言前先确认无并发写入者

## 相关页面

- [wiki 工具集](wiki-tools.md) —— wiki-updater 消费的 wiki 十件套（页面级六件套 + 章节级四件套，增量维护优先）
- [工作流引擎与钩子](../architecture/workflow-hooks.md)（[changed_calls 变更调用收集](../architecture/workflow-hooks.md#changed_calls-变更调用收集before_answer-透传2026-08-19)）
- [dir_snapshot / diff_snapshots 通用子工作流](../architecture/snapshot-diff.md)
- [配置体系与模型调优](../guides/config-and-models.md)
- [运维、可观测性与排障](../guides/ops.md)
- [v0.18.2 发布记录](../releases/v0.18.2.md)
