// AND 逻辑节点插件（type AND）：多组条件全部满足 → result=true。
// 条件编辑复用 selector 的 branches 组件（props 面板）；画布节点下方渲染各组条件摘要行。
EdFW.register({
  type: "AND", label: "AND", icon: "⛓", category: "logic",
  section: "条件组（全部满足 → true）",
  defaults: {nodeMeta: {title: "AND"},
             inputs: {inputParameters: [],
                      branches: [{condition: {logic: 2, conditions: []}}]},
             outputs: [{name: "result", type: "boolean", description: "全部条件组满足时为 true", fixed: true},
                       {name: "results", type: "list", itemType: "boolean", description: "每组条件的判定结果", fixed: true}]},
  params: [
    { key: "branches", label: "条件组", widget: "branches",
      get(n) { return String((n.data.inputs?.branches || []).length); }, set() {} },
  ],
  nodeH(n) {
    const brs = n.data.inputs?.branches || [];
    return Math.max(1, brs.length) * 16 + 6;   // 每组一行摘要
  },
  body(n, g) {
    // 各组条件摘要（左侧逻辑标记 && ：组内条件表达式拼接，未设置的条件显示 …）
    const brs = n.data.inputs?.branches || [];
    const yBase = HDR_H + Math.max(nodeInputs(n).length, nodeOutputs(n).length) * ROW_H + 4;
    const brs2 = brs.length ? brs : [null];
    brs2.forEach((br, i) => {
      const conds = (br?.condition?.conditions) || [];
      const parts = conds.map(c => {
        const l = c.left?.input?.value?.content;
        const lname = l ? (l.blockID ? `${l.blockID}.${l.name || ''}` : String(l.content ?? '')) : '…';
        const op = (OPERATORS.find(o => o.v === c.operator) || {}).t || '?';
        const r = c.right?.input?.value?.content;
        const rname = r ? (r.blockID ? `${r.blockID}.${r.name || ''}` : String(r.content ?? '')) : '';
        return `${lname} ${op} ${rname}`;
      });
      const txt = parts.length ? parts.join(br?.condition?.logic === 1 ? ' && ' : ' || ') : '（空）';
      const t = document.createElementNS(NS, 'text');
      t.setAttribute('x', 14); t.setAttribute('y', yBase + 11 + i * 16);
      t.setAttribute('class', 'node-field-name');
      t.setAttribute('fill', '#8fa3c8'); t.setAttribute('font-size', '9');
      t.textContent = (i > 0 ? '&& ' : '') + txt.slice(0, 60);
      g.appendChild(t);
    });
  },
});
