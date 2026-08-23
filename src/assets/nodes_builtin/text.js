// Text 文本节点插件（type 15）：concat（模板渲染）/ split（按分隔符切分）。
// 画布体：大文本框直编 concatResult 模板（{{输入字段}} 占位符）——与内置兜底逻辑一致，
// 迁出为插件后内置分支仅作插件未注入时的兜底。
EdFW.register({
  type: "15", label: "Text", icon: "📄", category: "data",
  nodeH(n) { return TEXTAREA_H; },
  body(n, g) {
    const fo = makeTextArea(n);
    if (fo) {
      const yBase = HDR_H + Math.max(nodeInputs(n).length, nodeOutputs(n).length) * ROW_H + 4;
      fo.setAttribute('x', 6); fo.setAttribute('y', yBase);
      fo.setAttribute('width', n.w - 12); fo.setAttribute('height', TEXTAREA_H - 8);
      g.appendChild(fo);
    }
  },
});
