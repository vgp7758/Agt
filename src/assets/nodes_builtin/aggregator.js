// Aggregator 聚合节点插件（type 32）：多分支汇合取值。
// params：groups 用框架级结构化组件（renderAggrGroups：分组/变量/类型编辑器）。
EdFW.register({
  type: "32", label: "聚合",     icon: "🗂️", category: "logic",
  section: "聚合分组",
  params: [
    { key: "groups", label: "分组", widget: "groups",
      get(n) { return String((n.data.inputs?.mergeGroups || []).length); }, set() {} },
  ],
});
