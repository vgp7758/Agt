// Selector 选择器节点插件（type 8）：条件分支路由。
// params：branches 用框架级结构化组件（renderSelectorBranches：左值/运算符/右值的三段条件编辑器）。
EdFW.register({
  type: "8",  label: "选择器",   icon: "🔀", category: "logic",
  section: "选择器分支",
  params: [
    { key: "branches", label: "分支", widget: "branches",
      get(n) { return String((n.data.inputs?.branches || []).length); }, set() {} },
  ],
});
