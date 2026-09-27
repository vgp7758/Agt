# nodes/ — 节点插件目录（工作流自定义节点）

一个节点类型 = 一对文件：`<type>.py`（执行 handler，后端）+ `<type>.js`（编辑器渲染，前端）。
两边共享同一个 type 名。`.agent/nodes/` 同语义（私有层）。

## 要点

- `.py`：实现节点执行入口，框架按 type 名分发；输出通过返回 dict 的 key 暴露给下游引用
- `.js`：在画布上渲染节点外观/端口/参数控件（跟 `src/static/workflow_editor.html` 的组件约定对接）
- 放好后 `/reload nodes` 热生效（或重启）；**改名/删文件后用 /reload nodes 摘除旧注册**
- 内置对照实现：随包 `nodes_builtin/`（AND/OR / intent_nano / jev_batch_judge 等，可照抄骨架）

## 深入

- 节点插件 SDK 与协议：`src/node_plugins.py`
- 编辑器组件协议：`src/static/nodes_builtin/*.js` 与 `workflow_editor.html`
