const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ATTR_KEYS = ['utm_source','utm_medium','utm_campaign','utm_content','utm_term','ttclid'];
const TITLES = {connect:'店铺连接', create:'内容创作', results:'效果分析', pricing:'套餐与账单'};
const STYLE_LABELS = {ugc:'UGC 实拍', promo:'限时优惠', educational:'科普', unboxing:'开箱'};
const PLATFORM_LABELS = {instagram:'Instagram', tiktok:'TikTok', facebook:'Facebook'};
const styleName = s => STYLE_LABELS[s] || s;
const platformName = p => PLATFORM_LABELS[p] || p;
const TOOL_LABELS = {get_products:'查商品', analyze_reviews:'分析评价', get_post_performance:'查帖子效果', generate_post:'生成帖子', create_tracked_link:'创建追踪链接'};
let ME = null, PRODUCTS = [], busy = false, view = 'create';

async function api(path, opts = {}) {
  const res = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...opts});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || res.statusText), {status: res.status});
  return data;
}
const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
const clientEvent = (name, props) => post('/api/events', {name, props}).catch(() => {});

function toast(msg) {
  const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 2600);
}
function modal(html, w) {
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal" style="${w ? `max-width:${w}px` : ''}"><button class="x" aria-label="关闭">×</button>${html}</div>`;
  bg.onclick = e => { if (e.target === bg || e.target.classList.contains('x')) bg.remove(); };
  document.body.appendChild(bg); return bg;
}
// 只渲染少量 Markdown：加粗、行内代码、换行
function md(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .split(/\n{2,}/).map(p => `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}

/* ---------- 启动：注册 or 进入应用 ---------- */
function captureAttribution() {
  const q = new URLSearchParams(location.search);
  const attr = {};
  ATTR_KEYS.forEach(k => { if (q.get(k)) attr[k] = q.get(k); });
  if (Object.keys(attr).length) {
    attr.landing_page = location.pathname;
    try { localStorage.setItem('lumi_attr', JSON.stringify(attr)); } catch (e) {}
  }
  try { return JSON.parse(localStorage.getItem('lumi_attr') || 'null'); } catch (e) { return null; }
}

async function boot() {
  const attr = captureAttribution();
  try { ME = await api('/api/me'); } catch (e) { ME = null; }
  if (!ME) {
    $('#authView').classList.remove('hidden');
    if (attr) {
      $('#attrNote').classList.remove('hidden');
      $('#attrNote').textContent = '已捕获广告来源：' + ATTR_KEYS.filter(k => attr[k]).map(k => `${k}=${attr[k]}`).join('，') + '（注册时写入账户）';
    }
    return;
  }
  $('#appView').classList.remove('hidden');
  PRODUCTS = await Promise.all(['p1','p2','p3','p4'].map(id => api('/api/products/' + id)));
  renderChrome();
  if (!$('#msgs').children.length) aiNote(`你好！我是 Lumi。告诉我想推哪个商品、发在哪个平台，我会帮你写好帖子${ME.plan === 'free' ? '' : '，并自动挂上追踪链接'}。`);
  const fromHash = location.hash.slice(2);
  go(TITLES[fromHash] ? fromHash : (ME.connected ? 'create' : 'connect'));
}

$('#signupForm').onsubmit = async e => {
  e.preventDefault();
  let client_attribution = null;
  try { client_attribution = JSON.parse(localStorage.getItem('lumi_attr') || 'null'); } catch (err) {}
  await post('/api/signup', {email: $('#email').value, store_name: $('#storeName').value, client_attribution});
  $('#authView').classList.add('hidden');
  boot();
};
$('#logoutBtn').onclick = async () => { await post('/api/logout'); location.href = '/'; };

async function refreshMe() { ME = await api('/api/me'); renderChrome(); }
function renderChrome() {
  const b = $('#planBadge');
  b.textContent = {free: 'Free', pro: 'Pro', business: 'Business'}[ME.plan];
  b.className = 'badge' + (ME.plan === 'free' ? '' : ' pro');
  $('#quotaText').textContent = `本月生成 ${ME.quota_used} / ${ME.quota_limit}`;
  $('#quotaBar').style.width = Math.min(100, ME.quota_used / ME.quota_limit * 100) + '%';
  $('#storeInfo').innerHTML = ME.connected
    ? `<b style="color:var(--text)">${esc(ME.store_name)}</b><br>${esc(ME.store_domain)}`
    : `<b style="color:var(--text)">${esc(ME.store_name)}</b><br>尚未接入店铺`;
  $('#productList').innerHTML = PRODUCTS.map(p => `<div class="product"><i style="background:var(--accent-soft)"></i><div>${esc(p.name)}<div class="muted">$${p.price} · ${p.id}</div></div></div>`).join('');
}

/* ---------- 路由 ---------- */
function go(v) {
  view = TITLES[v] ? v : 'create';
  history.replaceState(null, '', '#/' + view);
  document.querySelectorAll('[data-view]').forEach(s => s.classList.toggle('hidden', s.dataset.view !== view));
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('on', a.dataset.nav === view));
  $('#title').textContent = TITLES[view];
  if (view === 'results') renderResults();
  if (view === 'pricing') $('#pricingPlans').innerHTML = plansHTML();
  if (view === 'connect' && ME.connected) $('#connectResult').innerHTML = `<p class="hint">✓ 已接入 ${esc(ME.store_domain)}。<a href="#/create">去生成帖子 →</a></p>`;
}
document.addEventListener('click', e => {
  const a = e.target.closest('a[href^="#/"]');
  if (a) { e.preventDefault(); go(a.getAttribute('href').slice(2)); }
});

/* ---------- 接入店铺 ---------- */
$('#connectForm').onsubmit = async e => {
  e.preventDefault();
  try {
    ME = await post('/api/store/connect', {domain: $('#storeInput').value});
    renderChrome();
    $('#connectResult').innerHTML = `<p class="hint">✓ 已接入 ${esc(ME.store_domain)}：导入 4 个商品、5 条历史帖子。</p>
      <button class="btn primary" onclick="go('create')">下一步：生成带追踪的帖子 →</button>`;
  } catch (err) { $('#connectResult').innerHTML = `<p class="hint" style="color:var(--warn)">${esc(err.message)}</p>`; }
};

/* ---------- AI 对话 ---------- */
function addMsg(cls, html) {
  const d = document.createElement('div'); d.className = 'msg ' + cls; d.innerHTML = html;
  $('#msgs').appendChild(d); $('#msgs').scrollTop = 1e9; return d;
}
function aiNote(text) { addMsg('ai', `<div class="bubble">${md(text)}</div>`); }

async function send(text) {
  if (busy || !text.trim()) return;
  busy = true; $('#sendBtn').disabled = true;
  addMsg('user', esc(text));
  const box = addMsg('ai', '');
  let bubble = null, buf = '';
  const tools = {};
  const ensureBubble = () => {
    if (!bubble) { bubble = document.createElement('div'); bubble.className = 'bubble typing'; box.appendChild(bubble); buf = ''; }
    return bubble;
  };
  const closeBubble = () => { if (bubble) bubble.classList.remove('typing'); bubble = null; };
  const handle = ev => {
    if (ev.type === 'text') { ensureBubble(); buf += ev.delta; bubble.innerHTML = md(buf); }
    else if (ev.type === 'tool_start') {
      closeBubble();
      const t = document.createElement('div'); t.className = 'tools';
      t.innerHTML = `<div class="tool run">${esc(TOOL_LABELS[ev.name] || ev.name)} <span class="args">${esc(ev.name)}(${esc(ev.args)})</span></div>`;
      box.appendChild(t); tools[ev.name] = t.firstChild;
    } else if (ev.type === 'tool_end') {
      const t = tools[ev.name];
      if (t) { t.className = 'tool ' + (ev.ok ? 'done' : ''); t.insertAdjacentHTML('beforeend', ` → <span class="sum">${esc(ev.summary)}</span>`); }
    } else if (ev.type === 'card') {
      closeBubble();
      box.appendChild(ev.kind === 'post' ? postCard(ev.data) : perfCard(ev.data));
    } else if (ev.type === 'paywall') { openPaywall(ev.trigger); }
    else if (ev.type === 'error') {
      closeBubble();
      box.insertAdjacentHTML('beforeend', `<div class="bubble err">${esc(ev.message)}</div>`);
    }
    $('#msgs').scrollTop = 1e9;
  };
  try {
    const res = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: text})});
    const reader = res.body.getReader(), dec = new TextDecoder();
    let pending = '';
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      pending += dec.decode(value, {stream: true});
      const parts = pending.split('\n\n'); pending = parts.pop();
      parts.forEach(p => { if (p.startsWith('data: ')) handle(JSON.parse(p.slice(6))); });
    }
  } catch (err) {
    handle({type: 'error', message: '网络连接中断，请重试。'});
  }
  closeBubble();
  busy = false; $('#sendBtn').disabled = false;
  refreshMe();
}
$('#composer').onsubmit = e => { e.preventDefault(); const v = $('#chatInput').value; $('#chatInput').value = ''; send(v); };
$('#chips').onclick = e => { if (e.target.classList.contains('chip')) send(e.target.textContent); };
$('#resetBtn').onclick = async () => { await post('/api/chat/reset'); $('#msgs').innerHTML = ''; aiNote('对话已清空。'); };

function postCard(d) {
  const el = document.createElement('div'); el.className = 'post-card';
  const track = d.lock === 'free'
    ? `<div class="track locked">想知道这条帖子带来了多少订单？升级 Pro 后自动挂上追踪短链和专属优惠码。 <button class="btn sm" data-act="unlock">了解 Pro</button></div>`
    : d.lock === 'not_connected'
    ? `<div class="track locked">还没接入店铺，这条帖子暂时无法追踪订单。 <a href="#/connect">去接入 Shopify →</a></div>`
    : `<div class="track"><b style="color:var(--good)">${d.reused_tracking ? '沿用原追踪' : '已开启追踪'}</b> · 订单会自动对应回这条帖子<br>短链 <code class="short">${esc(d.short_url)}</code><br>专属优惠码 <code>${esc(d.discount_code)}</code></div>`;
  el.innerHTML = `
    <div class="post-head"><i>${esc((ME.store_name || 'L').trim()[0].toUpperCase())}</i><div><b>${esc(ME.store_name)}</b><div class="muted">${esc(platformName(d.platform))} · ${d.status === 'published' ? '已发布' : '草稿'}</div></div></div>
    <div class="post-img">${esc(d.title)}</div>
    <div class="post-body"><p>${esc(d.caption)}</p><div class="tags">${d.hashtags.map(t => '#' + esc(t)).join(' ')}</div>
      <div class="hint">${esc(d.product)} · ${esc(platformName(d.platform))} · ${esc(d.post_id)}</div></div>
    ${track}
    <div class="post-actions">
      ${d.lock ? '' : `<button class="btn primary sm" data-act="publish" ${d.status === 'published' ? 'disabled' : ''}>${d.status === 'published' ? '已发布' : '发布到 ' + esc(platformName(d.platform))}</button>`}
      <button class="btn sm" data-act="copy">复制文案</button>
      <button class="btn sm" data-act="regen">重新生成（消耗 1 次）</button>
    </div>`;
  el.querySelector('[data-act="copy"]').onclick = () => {
    navigator.clipboard?.writeText(d.caption + '\n' + d.hashtags.map(t => '#' + t).join(' ')).catch(() => {});
    toast('文案已复制');
  };
  el.querySelector('[data-act="regen"]').onclick = () => send(`重新生成这条帖子（post_id: ${d.post_id}），换个开头`);
  const u = el.querySelector('[data-act="unlock"]'); if (u) u.onclick = () => openPaywall('feature_tracking');
  const pb = el.querySelector('[data-act="publish"]');
  if (pb) pb.onclick = async () => {
    pb.disabled = true;
    try {
      const r = await post(`/api/posts/${d.post_id}/publish`, {channel: d.platform});
      pb.textContent = '已发布';
      el.insertAdjacentHTML('beforeend', `<div class="hint" style="padding:0 14px 14px">模拟顾客：<a href="${esc(r.short_url)}" target="_blank">打开短链</a> → 在商品页下单，订单会出现在「效果分析」里。</div>`);
    } catch (err) { pb.disabled = false; toast(err.message); }
  };
  return el;
}

function perfCard(d) {
  const el = document.createElement('div'); el.className = 'card'; el.style.padding = '12px';
  el.innerHTML = `<div style="font-size:12px;color:var(--muted)">最近 ${d.days} 天 · ${d.totals.orders} 单 · $${d.totals.gmv}</div>
    <table class="mini-table"><tr><th>帖子</th><th>风格</th><th>点击</th><th>订单</th><th>营业额</th></tr>
    ${d.posts.map(p => `<tr><td>${esc(p.title)}</td><td>${esc(styleName(p.style))}</td><td>${p.clicks}</td><td>${p.orders}</td><td>$${p.gmv}</td></tr>`).join('')}</table>`;
  return el;
}

/* ---------- 效果面板 ---------- */
async function renderResults() {
  const wrap = $('#resultsWrap');
  wrap.innerHTML = '<p class="muted">加载中…</p>';
  const d = await api('/api/dashboard');
  if (d.locked) {
    wrap.innerHTML = `<div class="card" style="max-width:420px;margin:40px auto;text-align:center">
      <div class="eyebrow">Pro</div><h3 style="margin:8px 0 6px">看到每条帖子带来的订单</h3>
      <p class="muted">效果追踪是 Pro 功能。升级后每条帖子都会自动挂上追踪链接，订单对应回帖子。</p>
      <button class="btn primary" id="unlockResults">升级 Pro · $29/月</button></div>`;
    $('#unlockResults').onclick = () => openPaywall('results_locked');
    clientEvent('paywall_viewed', {trigger: 'results_locked'});
    return;
  }
  if (!d.connected) { wrap.innerHTML = `<div class="card">还没接入店铺。<a href="#/connect">去接入 Shopify →</a></div>`; return; }
  const max = Math.max(1, ...d.weekly.map(w => w.orders));
  const s = d.suggestion;
  wrap.innerHTML = `
    <div class="hero">
      <div><div class="eyebrow">最近 ${d.days} 天 · Lumi 帖子带来的订单</div>
        <div class="big">${d.totals.orders} 单 · $${d.totals.gmv.toLocaleString()}</div>
        <div class="muted" style="font-size:13px">营业额是 Pro 月费（$29）的 ${Math.round(d.totals.gmv / 29)} 倍</div></div>
      <button class="btn sm" id="refreshBtn">刷新数据</button>
    </div>
    <div class="kpis">
      <div class="kpi"><div class="label">帖子点击</div><div class="val">${d.totals.clicks.toLocaleString()}</div></div>
      <div class="kpi"><div class="label">归因订单</div><div class="val">${d.totals.orders}</div></div>
      <div class="kpi"><div class="label">点击 → 下单</div><div class="val">${d.totals.clicks ? (d.totals.orders / d.totals.clicks * 100).toFixed(2) : 0}%</div></div>
      <div class="kpi"><div class="label">带追踪的帖子</div><div class="val">${d.posts.length}</div></div>
    </div>
    <div class="grid2">
      <div class="card">
        <h3 style="margin:0 0 8px;font-size:15px">每条帖子带来了什么</h3>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>帖子</th><th>短链 / 优惠码</th><th class="num">点击</th><th class="num">订单</th><th class="num">营业额</th></tr></thead>
          <tbody>${d.posts.map(p => `<tr><td>${esc(p.title)}<div class="muted" style="font-size:11px">${esc(platformName(p.platform))} · ${esc(styleName(p.style))} · ${p.published_at.slice(5, 10)}${p.fresh ? ' · 刚发布，等待数据' : ''}</div></td>
            <td style="font-size:12px">${p.short_url ? `<a href="${esc(p.short_url)}" target="_blank">/l/${esc(p.link_code)}</a><br><code>${esc(p.discount_code)}</code>` : '—'}</td>
            <td class="num">${p.clicks}</td><td class="num"><b>${p.orders}</b></td><td class="num">$${p.gmv}</td></tr>`).join('')}</tbody>
        </table></div>
        <p class="hint">测试归因：点任意短链 → 在商品页“模拟下单”（可填优惠码）→ 回来点「刷新数据」。</p>
      </div>
      <div>
        <div class="card"><h3 style="margin:0;font-size:15px">每周归因订单</h3>
          <div class="bars">${d.weekly.map(w => `<div><b>${w.orders || ''}</b><span style="height:${w.orders / max * 90}%"></span>${w.week}</div>`).join('')}</div></div>
        ${s ? `<div class="card suggest" style="margin-top:16px">
          <div class="eyebrow">下一条建议</div>
          <p style="margin:8px 0 12px">「${esc(styleName(s.best.style))}」风格的点击 → 下单转化（${s.best.conversion}%）是「${esc(styleName(s.worst.style))}」（${s.worst.conversion}%）的 ${s.ratio} 倍，下一条建议继续用「${esc(styleName(s.best.style))}」风格。</p>
          <button class="btn primary sm" id="genFromSuggest">让 Lumi 按这个写</button></div>` : ''}
      </div>
    </div>`;
  $('#refreshBtn').onclick = renderResults;
  const g = $('#genFromSuggest');
  if (g) g.onclick = () => { go('create'); send(`下一条用「${styleName(s.best.style)}」风格，帮我给防晒霜写一条 Instagram 帖子`); };
  clientEvent('dashboard_viewed', {period_days: d.days, attributed_orders_count: d.totals.orders, entry: 'nav'});
}

/* ---------- 付费墙 ---------- */
function plansHTML(trigger) {
  const p = (key, name, price, items, rec) => `
    <div class="plan ${rec ? 'rec' : ''}">
      ${rec ? '<span class="badge pro" style="align-self:flex-start">推荐</span>' : ''}
      <b>${name}</b><div class="price">$${price}<small>${price ? ' / 月' : ''}</small></div>
      <ul>${items.map(i => `<li class="${i.startsWith('★') ? 'key' : ''}">${i.replace('★', '')}</li>`).join('')}</ul>
      ${ME.plan === key ? '<button class="btn" disabled>当前套餐</button>'
        : key === 'free' ? '<span></span>'
        : `<button class="btn ${rec ? 'primary' : ''}" data-upgrade="${key}" data-trigger="${trigger || 'pricing_page'}">升级 ${name}</button>`}
    </div>`;
  return `<div class="plans">
    ${p('free', 'Free', 0, ['每月 10 次生成', '基础文案与标签'])}
    ${p('pro', 'Pro', 29, ['每月 500 次生成', '★每条帖子自动追踪订单', '★效果面板', 'AI 按效果推荐下一条'], true)}
    ${p('business', 'Business', 79, ['每月 2,000 次生成', 'Pro 全部功能', '3 个团队席位'])}
  </div>`;
}
function openPaywall(trigger) {
  clientEvent('paywall_viewed', {trigger});
  const head = trigger === 'quota'
    ? `<h2 style="margin:0">本月 ${ME.quota_limit} 次${ME.plan === 'free' ? '免费' : ''}生成额度已用完</h2><p class="muted" style="margin:6px 0 0">额度将在下月 1 日重置。升级 Pro，不只是多生成，还能看到每条帖子带来了多少订单。</p>`
    : `<h2 style="margin:0">看到每条帖子带来了多少订单</h2><p class="muted" style="margin:6px 0 0">Pro 会给每条帖子自动挂上追踪短链和专属优惠码，订单对应回具体帖子。</p>`;
  modal(head + plansHTML(trigger));
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-upgrade]');
  if (!b) return;
  b.disabled = true;
  ME = await post('/api/subscribe', {plan: b.dataset.upgrade, trigger: b.dataset.trigger});
  document.querySelectorAll('.modal-bg').forEach(m => m.remove());
  renderChrome(); go(view);
  toast(`已升级 ${ME.plan === 'pro' ? 'Pro' : 'Business'}（演示环境，未扣款）`);
  aiNote('升级成功。从现在起，已接入店铺的帖子都会自动挂上追踪链接。');
});

boot();
