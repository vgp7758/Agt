# 工具外置 · tools/builtin 脚本工具体系

> 把工具从框架代码外置为「一个 .py 文件」——写一个文件即得新工具，零框架改动。与[节点插件化](../architecture/node-plugins.md)同构的扫描/装配模式，但更轻：只有 .py，无前端件。

## 目录与装配

| 位置 | 角色 |
|------|------|
| `tools/builtin/*.py` | 工具源文件（开发处） |
| `src/assets/tools_builtin/*.py` | 随包副本（pip 安装即有，与 nodes_builtin 同思路） |

约定：模块暴露 `agt_register()` 返回工具描述符列表，`src/script_tools.py` 按此扫描注册（约定记载于 fs_tools.py 文件头注释）。

## 热加载

改完 .py 用 `/reload tools` 即生效，**不需要重启**——比 src 内注册的工具（需 `/restart`，见 [diff-files](diff-files.md)/[get-list-item](get-list-item.md) 注意事项）轻一档。

## 首例：fs_tools.py（glob_files）

2026-08，commit eafed25。`glob_files(pattern, path)` 文件名模式查找，详见 [glob-files](glob-files.md)。完整路径演示：写 `tools/builtin/fs_tools.py` → `/reload tools` 热加载 → 拷贝随包副本进 `src/assets/tools_builtin/`。

## 与节点插件化对照

| | 节点插件（nodes_builtin） | 工具外置（tools_builtin） |
|---|---|---|
| 文件 | `.py` + `.js` 配对（后端逻辑 + EdFW 前端表单） | 仅 `.py` |
| 注册入口 | SDK（`workflow_node_api.py`）+ 节点目录三级扫描 | `agt_register()` 描述符列表 + script_tools.py 扫描 |
| 生效方式 | 节点热加载 | `/reload tools` |

## 相关页面

- [glob_files](glob-files.md) —— 首个外置工具
- [节点插件化](../architecture/node-plugins.md) —— 同构模式（节点侧，更完整的三级目录/覆盖机制）
