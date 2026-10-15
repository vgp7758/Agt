/* record —— 前端状态机：idle → recording ⇄ paused → blobReady（save 可多次）→ flush 清。
 * 三桥范式的进阶示例（比 screenshot 复杂）：长生命周期 MediaRecorder 跨命令存活、
 * getDisplayMedia 的 user-activation 限制处理（WS 回调不算激活 → 弹确认浮条补）、
 * 大 blob 回传、容器（webm/mp4）按浏览器能力自适应。
 * 改完刷新页面即生效。测试模式：URL 加 ?record-test=1 用 canvas 动画流替代选屏
 * （无头/自动化验证用，不弹授权框）。
 */
(function () {
  const S = { rec: null, stream: null, chunks: [], state: 'idle', fmt: 'webm', blob: null, t0: 0 };
  const TEST = new URLSearchParams(location.search).get('record-test') === '1';

  // —— 自绘 toast（SDK 未暴露——顺便演示前端自由度；不依赖聊天流内部结构）——
  function toast(msg, ms) {
    let host = document.getElementById('_rec_toast');
    if (!host) {
      host = document.createElement('div'); host.id = '_rec_toast';
      host.style.cssText = 'position:fixed;right:16px;bottom:70px;z-index:9999;display:flex;flex-direction:column;gap:6px;pointer-events:none';
      document.body.appendChild(host);
    }
    const d = document.createElement('div');
    d.style.cssText = 'background:#111827;color:#fff;padding:8px 14px;border-radius:8px;font-size:13px;box-shadow:0 4px 14px #0006;max-width:340px';
    d.textContent = msg;
    host.appendChild(d);
    setTimeout(() => d.remove(), ms || 3500);
  }

  function pickMime() {
    if (!window.MediaRecorder) return '';
    for (const m of ['video/mp4;codecs=avc1', 'video/webm;codecs=vp9', 'video/webm'])
      if (MediaRecorder.isTypeSupported(m)) return m;
    return '';
  }
  function fmtOf(mime) { return (mime || '').includes('mp4') ? 'mp4' : 'webm'; }

  // —— 流来源：测试模式=canvas 动画流（免授权）；真实=getDisplayMedia（需用户激活+选屏授权）——
  async function getStream() {
    if (TEST) {
      const c = document.createElement('canvas'); c.width = 640; c.height = 360;
      const g = c.getContext('2d'); let f = 0;
      (function draw() {
        g.fillStyle = '#' + ((f * 37) % 4096).toString(16).padStart(3, '0');
        g.fillRect(0, 0, 640, 360);
        g.fillStyle = '#fff'; g.font = 'bold 30px monospace';
        g.fillText('REC TEST frame ' + (f++), 24, 190);
        requestAnimationFrame(draw);
      })();
      return c.captureStream(10);
    }
    return navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: false });
  }

  function _begin(stream) {
    S.stream = stream; S.chunks = []; S.blob = null; S.t0 = Date.now();
    const mime = pickMime(); S.fmt = fmtOf(mime);
    S.rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    S.rec.ondataavailable = (e) => { if (e.data && e.data.size) S.chunks.push(e.data); };
    S.rec.onstop = () => {
      S.blob = new Blob(S.chunks, { type: S.rec.mimeType || 'video/webm' });
      S.state = 'blobReady'; S.rec = null;
      const sec = ((Date.now() - S.t0) / 1000).toFixed(1), mb = (S.blob.size / 1048576).toFixed(2);
      toast(`⏹ 录制结束：${sec}s / ${mb}MB（${S.fmt}）——用 /record save <路径> 落盘`, 6000);
    };
    S.rec.start(1000);   // timeslice=1s：chunks 分片到达（防长时间录制内存尖峰一次性 onstop）
    S.state = 'recording';
    toast(TEST ? '● 测试流录制中（?record-test=1）' : '● 录制中——/record pause|stop 控制', 5000);
  }

  function start() {
    if (S.state === 'recording' || S.state === 'paused') { toast('已在录制中（' + S.state + '）'); return; }
    if (!window.MediaRecorder) { toast('此浏览器不支持 MediaRecorder', 5000); return; }
    if (TEST || (navigator.userActivation && navigator.userActivation.isActive)) {
      getStream().then(_begin).catch((e) => toast('获取屏幕流失败：' + e.message, 5000));
      return;
    }
    // 激活限制 workaround：getDisplayMedia 需 user activation，WS 事件回调不算——
    // 弹一个真按钮让用户点一下（点击=激活）再唤起选屏授权框。
    const b = document.createElement('button');
    b.textContent = '🎬 点击开始录制（浏览器将请求选屏授权）';
    b.style.cssText = 'position:fixed;right:16px;bottom:70px;z-index:9999;background:#dc2626;color:#fff;border:none;border-radius:10px;padding:12px 18px;font-size:14px;cursor:pointer;box-shadow:0 6px 18px #0007';
    b.onclick = () => { b.remove(); getStream().then(_begin).catch((e) => toast('获取屏幕流失败/已取消：' + e.message, 5000)); };
    document.body.appendChild(b);
    setTimeout(() => b.remove(), 30000);   // 30s 不点自动消失
  }

  const ctl = {
    start,
    pause()  { if (S.state !== 'recording') return toast('当前不可暂停（' + S.state + '）'); S.rec.pause(); S.state = 'paused'; toast('⏸ 已暂停（/record resume 恢复）'); },
    resume() { if (S.state !== 'paused') return toast('当前不可恢复（' + S.state + '）'); S.rec.resume(); S.state = 'recording'; toast('▶ 已恢复录制'); },
    stop()   { if (S.state !== 'recording' && S.state !== 'paused') return toast('当前不在录制（' + S.state + '）');
               try { S.rec.stop(); } catch (e) { toast('stop 异常：' + e); }
               if (S.stream) S.stream.getTracks().forEach((t) => t.stop()); S.stream = null; S.state = 'stopping'; },
    async save(path) {
      if (!S.blob) return toast('无暂存录制：先 /record stop（当前 ' + S.state + '）', 5000);
      try {
        const r = await fetch('/api/record/save?path=' + encodeURIComponent(path) + '&fmt=' + S.fmt, { method: 'POST', body: S.blob });
        const j = await r.json();
        toast(j.ok ? '💾 已上传落盘 → ' + j.path + '（' + (j.size / 1048576).toFixed(2) + 'MB）' : '保存失败：' + j.error, 6000);
      } catch (e) { toast('上传失败：' + e, 5000); }
    },
    flush()  { if (!S.blob && !S.chunks.length) return toast('无暂存内容可丢弃');
               S.blob = null; S.chunks = []; S.state = 'idle'; toast('🗑 已丢弃暂存录制'); },
    status() { toast('录制状态：' + S.state + (S.blob ? '｜暂存 ' + (S.blob.size / 1048576).toFixed(2) + 'MB（' + S.fmt + '）待 save' : '') + (TEST ? '｜测试流模式' : ''), 5000); },
  };

  AgtPlugin.ws.on('record_request', (m) => {
    const fn = ctl[m.action];
    if (fn) fn(m.path); else toast('未知子命令：' + m.action);
  });

  AgtPlugin.ui.addChatCard('record_done', (b, m) => {
    b.innerHTML =
      '<div style="border:1px solid #fde68a;background:#fffbeb;border-radius:8px;padding:8px 12px">' +
      '<div style="font-weight:600;color:#b45309">🎥 录制已保存</div>' +
      '<div style="color:#78350f;font-size:13px;margin-top:4px">' + AgtPlugin.util.esc(m.path) +
      '（' + ((m.size || 0) / 1048576).toFixed(2) + ' MB）</div>' +
      '<div style="color:#92400e;font-size:11px;margin-top:2px">' + AgtPlugin.util.esc(m.abs || '') + '</div>' +
      '</div>';
  });

  // dock 直达：idle→开始（含激活浮条）；录制中→停止（blob 暂存，toast 提示 save）
  AgtPlugin.ui.addButton({
    icon: '🔴', label: '页面录屏', background: '#b91c1c',
    onClick: () => (S.state === 'recording' || S.state === 'paused') ? ctl.stop() : start(),
  });

  console.info('[record] 前端已装配（状态机/卡片/dock按钮' + (TEST ? '/测试流模式' : '') + '）');
})();
