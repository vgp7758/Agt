// Timestamp 节点插件（type N1）：输出当前时间字符串。
// defaults 定义建节点模板（无输入、单输出）——编辑器浮窗「节点插件」组可创建、可连线。
EdFW.register({
  type: "N1", label: "Timestamp", icon: "🕐", category: "data",
  defaults: {nodeMeta: {title: "时间戳"},
             inputs: {inputParameters: []},
             outputs: [{name: "output", type: "string", description: "当前时间 YYYY-MM-DD HH:MM:SS"}]},
});
