// Aggregator 聚合节点插件（type 32）：多分支汇合取值。
// params：groups 用框架级结构化组件（renderAggrGroups：分组/变量/类型编辑器）。
// defaults.outputs 的 index 标记 fixed:true —— syncPluginOutputs 据此给存量节点补协议端口
// （handler 固定返回；分组输出由 mergeGroups 驱动，不在此列）。
EdFW.register({
  type: "32", label: "聚合",     icon: "🗂️", category: "logic",
  section: "聚合分组",
  defaults: {nodeMeta: {title: "变量聚合"},
             inputs: {mergeGroups: [{name: "Group1", variables: []}]},
             outputs: [{name: "Group1", type: "string"},
                       {name: "index", type: "integer", fixed: true,
                        description: "贡献值的变量序号(组内0起，全空=-1。调试用：观测哪个分支端口拿到值)"}]},
  params: [
    { key: "groups", label: "分组", widget: "groups",
      get(n) { return String((n.data.inputs?.mergeGroups || []).length); }, set() {} },
  ],
});
