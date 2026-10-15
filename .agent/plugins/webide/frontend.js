/* webide —— 插件前端：dock VS Code 图标（原 index.html 内置 btnIde 插件化，2026-10-15）。
 * 12s 超时 + 潜规则口乐观退回，逻辑照搬 openWebIde()。图标资产走 /plugins-assets/webide/vscode.png。 */
(function () {
  AgtPlugin.ui.addButton({
    icon: '<img src="/plugins-assets/webide/vscode.png" alt="VS Code" style="width:22px;height:22px;display:block">',
    label: 'WebIDE (VS Code)', background: '#fff',
    onClick: async (ev) => {
      const b = (ev && ev.currentTarget) || null;
      if (b) { b.disabled = true; b.classList.add('loading'); }
      const curPort = parseInt(location.port || '8000', 10);
      const fallbackUrl = `http://${location.hostname}:${curPort + 30000}/`;
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 12000);
      try {
        // 原生 fetch（非 AgtPlugin.api.post）：AbortController 12s 中断必须走 signal——
        // SDK post 签名 (path, body) 不透传 signal（v1 不为此扩签名，插件自取更直接）
        const r = await fetch('/api/webide/open', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                    body: '{}', signal: ctl.signal });
        const d = await r.json();
        clearTimeout(timer);
        if (d.ok) {
          window.open(`http://${location.hostname}:${d.port}/`, '_blank');
          AgtPlugin.util.toast(d.ready ? '📝 已打开 WebIDE（新页签）。界面文字/图标若显示异常：Ctrl+Shift+P → Configure Display Language → English（一次即好）'
                                       : '⏳ ' + (d.hint || 'VS Code Server 组件初始化中，页签会自动刷新'));
        } else {
          AgtPlugin.util.toast('❌ ' + (d.error || '无法启动 WebIDE'));
        }
      } catch (e) {
        clearTimeout(timer);
        window.open(fallbackUrl, '_blank');   // 超时/异常：退回潜规则口乐观打开
        AgtPlugin.util.toast('⏳ WebIDE 正在后台启动（超时乐观打开）——若页签空白请稍后刷新');
      }
      if (b) { b.classList.remove('loading'); b.disabled = false; }
    },
  });
  console.info('[webide] 前端已装配（按钮1）');
})();
