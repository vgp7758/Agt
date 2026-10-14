# .agent/nodes/ — 节点插件目录（工作流自定义节点）

一个节点类型 = 一对文件：`<name>.py`（执行 handler，后端）+ `<name>.js`（编辑器渲染，前端）。
两边共享同一个 type 名。放这里即得全新节点类型——本地自定义统一收进 `.agent/`（2026-10-15）。

## 要点

- `.py`：`def agt_node() -> 描述符`（type/label/handler/xml），框架按 type 分发；输出通过返回 dict 的 key 暴露给下游引用
- `.js`：在画布上渲染节点外观/端口/参数控件（跟 `workflow_editor.html` 的组件约定对接）
- 放好后 `/reload nodes` 热生效（或重启）；**改名/删文件后用 /reload nodes 摘除旧注册**
- 内置对照实现：随包 `src/assets/nodes_builtin/`（AND/OR / intent_nano / jev_batch_judge 等，可照抄骨架）
- 旧 `nodes/` 目录（workspace 根）仍兼容扫描——存量 repo 不炸；同名节点本目录覆盖它（建议逐步迁入）

## 深入

- 节点插件 SDK 与协议：`src/node_plugins.py`
- 编辑器组件协议：`src/static/nodes_builtin/*.js` 与 `workflow_editor.html`
