# 贡献指南

感谢想为 Agt 出一份力。两类贡献，走两条路：

- **代码 bug 修复 / 框架改动** —— 直接开 PR（参见下方「通用约定」）。
- **新资产**（workflow / 节点插件 / skill / MCP 配置）—— 走 `community/` 流程（见下）。这是让社区资产可被他人 `/download` 取用的正式入口。

---

## 提交一个社区资产

### 四步

1. **Fork** 本仓库，新建分支 `community/<type>-<name>`（如 `community-workflow-slack-notify`）
2. 在 `community/<type>/<name>/` 放**实体文件 + `entry.yaml`**（目录约定见下）
3. 开 PR，标题 `[community] add <type>: <name>`
4. CI 自动校验格式 → 作者 review → merge；merge 后资产通过 GitHub raw 立即可用（约 5 分钟 CDN 生效），下次发版随包进入 `/download` 清单

### 目录约定

```
community/
  workflows/<name>/   # 工作流
    workflow.json     或 workflow.xml    # Coze 画布（推荐 XML，写作方便）
    entry.yaml
  nodes/<name>/        # 节点插件
    <name>.py          # def agt_node() -> {...}
    <name>.js          # EdFW.register({...})（须与 .py 同名配对）
    entry.yaml
  skills/<name>/       # 技能
    SKILL.md           # YAML frontmatter + 正文
    entry.yaml
  mcp/<name>/          # MCP 配置
    mcp.json           # {"mcpServers": {...}}
    entry.yaml
```

`name` 须匹配 `^[A-Za-z0-9_-]+$`，且不与内置资产、其它社区条目重名。

### entry.yaml 字段

```yaml
name: my-flow            # 必填，唯一取用名
type: workflow           # 必填：workflow / node / skill / mcp
desc: "一句话描述"        # 必填
author: "名字 <github>"   # 可选
source: "https://..."     # 可选，原始出处
license: MIT             # 可选，缺省继承仓库 MIT
date_added: "2026-09-03" # 可选，rebuild 时自动补
```

### 四类资产格式要点

| 类型 | 关键约定 | 校验 |
|---|---|---|
| workflow | 开始节点 id `100001`、结束 `900001`；XML 根属性 `hidden` 默认 true（要注册为 `wf_` 工具须显式 `hidden="false"`） | 复用框架 `scan_workflows` |
| node | `.py` 内 `def agt_node()` 返回 `{type, label, handler}`；type 用自编段（`N1`/`N2`…），不可覆盖调度器核心 type | 复用 `scan_node_plugins` |
| skill | `SKILL.md` frontmatter 三字段 `name`/`description`/`when_to_use` | 复用 frontmatter 解析 |
| mcp | `mcpServers` 对象，每项 `command`+`args`（stdio）或 `url`（http/sse） | schema 校验 |

参考活样本：`community/workflows/example-flow/`。

### CI 校验项

PR 会跑 `.github/workflows/validate-contributions.yml`，对改动的条目做：格式完整性、entry.yaml 必填字段、name 规则与冲突检测、四类实体文件的对应校验。本地自测：

```bash
python tools/validate_contributions.py
```

---

## 通用约定

- **提交信息用中文**（本项目惯例，简短主题，偶带 `docs:` 前缀）
- **文档用中文**（对外 README 除外）
- 许可证 **MIT**
- 改动前先读相关代码；不可逆操作（删文件、覆盖）先确认
- 外部反馈可走 /feedback 或 GitHub Issues

谢谢。