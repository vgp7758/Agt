// Text 文本节点插件（type 15）：concat（模板渲染）/ split（按分隔符切分）。
// 画布体：大文本框直编 concatResult 模板（{{输入字段}} 占位符）——高度按内容自适应
// （_taH 框架函数，3~16 行——免"大滚动区里套小滚动区"）；oninput 就地增长，blur 重绘对齐连线。
EdFW.register({
  type: "15", label: "Text", icon: "📄", category: "data",
  nodeH(n) { return _taH(n.data.inputs?.concatParams?.[0]?.input?.value?.content); },
  body(n, g) {
    const fo = makeTextArea(n);
    if (fo) {
      const yBase = HDR_H + Math.max(nodeInputs(n).length, nodeOutputs(n).length) * ROW_H + 4;
      fo.setAttribute('x', 6); fo.setAttribute('y', yBase);
      fo.setAttribute('width', n.w - 12);
      fo.setAttribute('height', _taH(n.data.inputs?.concatParams?.[0]?.input?.value?.content) - 2);
      g.appendChild(fo);
    }
  },
});
