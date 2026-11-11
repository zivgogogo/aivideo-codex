(() => {
  'use strict';
  const button = document.getElementById('app-update-button');
  if (!button) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'app-update-dialog';
  dialog.setAttribute('aria-label', '程序更新');
  dialog.innerHTML = `<div class="app-update-heading"><div><small>映序 · 程序更新</small><h2>保持程序为最新版本</h2></div><button type="button" class="app-update-close" aria-label="关闭更新窗口">×</button></div>
    <p class="app-update-message" role="status">正在读取版本…</p>
    <div class="app-update-versions"><span>当前版本 <b class="app-update-current">—</b></span><span>最新版本 <b class="app-update-latest">—</b></span></div>
    <ul class="app-update-commits"></ul>
    <p class="app-update-note">更新只替换程序代码，保留密钥、剧本、素材和项目数据。更新前请保存其他窗口的编辑，服务会短暂重启。</p>
    <div class="app-update-actions"><a href="https://github.com/zivgogogo/aivideo-codex" target="_blank" rel="noreferrer">查看 GitHub</a><button type="button" class="app-update-check">重新检查</button><button type="button" class="app-update-apply">更新并重启</button></div>`;
  document.body.append(dialog);
  const find = name => dialog.querySelector('.app-update-' + name);
  let state = {}, checking = false, applying = false, checkedAt = 0, operation = '', target = '';
  const active = new Set(['preparing','stopping','fetching','backing_up','updating','dependencies','restarting','rolling_back']);
  const short = value => value ? value.slice(0, 8) : '未安装 Git 版本';
  const open = () => { if (!dialog.open) dialog.showModal(); };
  function render() {
    const working = applying || active.has(state.phase);
    button.classList.toggle('has-update', !!state.available);
    button.textContent = working ? '正在更新…' : state.available ? '发现新版本' : '检查更新';
    button.title = state.message || '检查 GitHub 上的程序更新';
    find('current').textContent = short(state.running_sha);
    find('latest').textContent = state.target_sha ? short(state.target_sha) : '尚未检查';
    find('message').textContent = checking ? '正在连接 GitHub，检查最新版本…' : state.message || '点击检查，查看是否有新版本。';
    find('message').classList.toggle('is-error', state.phase === 'failed');
    find('check').disabled = checking || working;
    find('check').textContent = checking ? '正在检查…' : '重新检查';
    find('apply').hidden = !state.available && !working;
    find('apply').disabled = checking || working || !state.can_update;
    find('apply').textContent = working ? '更新进行中…' : '更新并重启';
    find('close').disabled = working;
    find('commits').replaceChildren(...(state.commits || []).map(message => {
      const item = document.createElement('li'); item.textContent = message; return item;
    }));
  }
  async function request(path, body) {
    const response = await fetch('/api/system/update' + path, {
      method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '更新请求未成功');
    return data;
  }
  async function check(show = false) {
    if (show) open();
    if (checking || applying || active.has(state.phase)) return;
    checking = true; render();
    try { state = await request('/check', {}); checkedAt = Date.now(); }
    catch (error) { state = { ...state, can_update: false, phase: 'failed', message: '无法检查更新：' + error.message }; }
    finally { checking = false; render(); }
  }
  async function waitForRestart() {
    const deadline = Date.now() + 25 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1200));
      try {
        const next = await request('');
        if (next.operation !== operation) continue;
        state = next; render();
        if (next.phase === 'complete' && next.running_sha === target) { location.reload(); return; }
        if (next.phase === 'failed') { applying = false; render(); return; }
      } catch (_) {
        find('message').textContent = '正在更新或重启服务，连接恢复后会自动加载新版本…';
      }
    }
    applying = false;
    state = { ...state, phase: 'failed', can_update: false, message: '暂时无法确认服务状态，请稍后刷新页面或查看本机更新记录。' };
    render();
  }
  async function apply() {
    if (applying || !state.can_update) return;
    find('apply').disabled = true;
    try {
      if (typeof window.AIVideoBeforeUpdate === 'function') await window.AIVideoBeforeUpdate();
      target = state.target_sha;
      const accepted = await request('/apply', { target_sha: target });
      state = accepted; operation = accepted.operation; applying = true;
      render(); await waitForRestart();
    } catch (error) {
      applying = false;
      state = { ...state, phase: 'failed', message: error.message };
      render();
    }
  }
  button.addEventListener('click', () => {
    open();
    if (active.has(state.phase) && !applying) { operation = state.operation; target = state.target_sha; applying = true; render(); waitForRestart(); }
    else check();
  });
  find('check').addEventListener('click', () => check());
  find('apply').addEventListener('click', apply);
  find('close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (applying || active.has(state.phase)) event.preventDefault(); });
  window.addEventListener('focus', () => { if (Date.now() - checkedAt > 60 * 1000) check(); });
  setInterval(() => { if (!document.hidden) check(); }, 5 * 60 * 1000);
  request('').then(value => {
    state = value; render();
    if (active.has(value.phase)) { open(); operation = value.operation; target = value.target_sha; applying = true; render(); waitForRestart(); }
    else if (value.supported) check();
  }).catch(() => { state.message = '当前服务尚未启用更新接口，请先重启服务。'; render(); });
})();
