// Code 节点插件（type 5）：async def main(args) 沙箱代码。
// params：code（唯一参数）用 code widget（等宽字体 + 行数自适应的 textarea）。
EdFW.register({
  type: "5",  label: "Code",      icon: "🐍", category: "logic",
  section: "Python 代码",
  params: [
    { key: "code", label: "代码", widget: "code", rows: 10,
      tip: "async def main(args): return {...}",
      get(n) { return n.data.inputs?.code || ""; },
      set(n, v) { n.data.inputs.code = v; } },
  ],
});
