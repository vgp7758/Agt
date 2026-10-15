/* screenshot —— 插件前端（html2canvas 截图 → POST 落盘 → 卡片通知）。
 * 「以前端为主」范式示例的重活端：后端命令只 emit(screenshot_request)，
 * 真正的截图在这里做。库随包 vendor/ 分发（/plugins-assets/...，离线可用）。
 * 改完刷新页面即生效。
 */
(function () {
  let _h2c = null;   // 懒加载 html2canvas（首次截图才加载，~194KB）
  function loadH2C() {
    if (window.html2canvas) return Promise.resolve();
    if (_h2c) return _h2c;
    _h2c = new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = '/plugins-assets/screenshot/vendor/html2canvas.min.js';
      s.onload = res;
      s.onerror = () => { _h2c = null; rej(new Error('html2canvas 加载失败（/plugins-assets/screenshot/vendor/）')); };
      document.head.appendChild(s);
    });
    return _h2c;
  }

  async function shoot(path) {
    await loadH2C();
    const canvas = await window.html2canvas(document.body, {
      backgroundColor: '#eef1f5',   // 与页面底色一致，避免透明底
      scale: window.devicePixelRatio > 1 ? 1.5 : 1,   // 高分屏适度提清晰度
      logging: false,
    });
    const blob = await new Promise((r) => canvas.toBlob(r, 'image/png'));
    if (!blob) throw new Error('canvas.toBlob 返回空');
    const resp = await fetch('/api/screenshot/save?path=' + encodeURIComponent(path), {
      method: 'POST', body: blob,
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || ('HTTP ' + resp.status));
    return j;   // {ok, path, size, abs}
  }

  // ① 命令触发链：后端 emit(screenshot_request) → 截图 → 落盘（结果经 screenshot_done 卡片反馈）
  AgtPlugin.ws.on('screenshot_request', (m) => {
    shoot(m.path).catch((e) => console.error('[screenshot] 截图失败：', e));
  });

  // ② 完成通知：落盘成功 → 后端 emit(screenshot_done) → 聊天卡片
  AgtPlugin.ui.addChatCard('screenshot_done', (b, m) => {
    const kb = ((m.size || 0) / 1024).toFixed(1);
    b.innerHTML =
      '<div style="border:1px solid #99f6e4;background:#f0fdfa;border-radius:8px;padding:8px 12px">' +
      '<div style="font-weight:600;color:#0f766e">📸 截图已保存</div>' +
      '<div style="color:#134e4a;font-size:13px;margin-top:4px">' + AgtPlugin.util.esc(m.path) +
      '（' + kb + ' KB）</div>' +
      '<div style="color:#6b7280;font-size:11px;margin-top:2px">' + AgtPlugin.util.esc(m.abs || '') + '</div>' +
      '</div>';
  });

  // ③ dock 直达按钮（免命令，点就截）：自动命名 captures/shot_<时间戳>.png
  AgtPlugin.ui.addButton({
    icon: '📸', label: '页面截图', background: '#0f766e',
    onClick: () => {
      const p = 'captures/shot_' + new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19) + '.png';
      shoot(p).catch((e) => console.error('[screenshot]', e));
    },
  });

  console.info('[screenshot] 前端已装配（请求订阅/完成卡片/dock按钮）');
})();
