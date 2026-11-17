// 任務卡前端。狀態一律以伺服器回傳為準，畫面只負責呈現。
let CFG = null, S = null, flash = {}, seen = null;
const STAMP = {1: '距離', 2: '迷思', 3: '跑友', 4: '試喝', 5: '提問'};
const app = document.getElementById('app');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const params = new URLSearchParams(location.search);

async function api(path, data, method) {
  const r = await fetch(path, {
    method: method || (data === undefined ? 'GET' : 'POST'),
    headers: {'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data),
    credentials: 'same-origin',
  });
  let j = {};
  try { j = await r.json(); } catch (e) {}
  if (!r.ok || j.ok === false) {
    const err = new Error(j.error || '連線發生問題，請稍後再試');
    err.status = r.status;
    throw err;
  }
  return j;
}

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = () => rej(new Error('LINE 元件載入失敗'));
    document.head.appendChild(s);
  });
}

const addFriendUrl = () => 'https://line.me/R/ti/p/' + encodeURIComponent(CFG.oa_id);

// 官方帳號模式下還沒有身分的人：請他加好友，任務卡連結會由官方帳號傳給他
function landing() {
  const invited = params.get('ref') || CFG.has_pending_ref;
  app.innerHTML = `<section class="panel alert">
      <h3>${invited ? '朋友邀你一起跑補給站' : '加入好友，領你的任務卡'}</h3>
      <p>加入 PulsarPump 的 LINE 好友，我們會馬上把你專屬的任務卡傳到聊天室，點開就能開始。${invited ? '你和朋友會自動算成一隊。' : ''}</p>
      <a class="btn primary" href="${addFriendUrl()}">加入 PulsarPump 好友</a>
      <p class="note">已經是好友？在 PulsarPump 的聊天室輸入「${esc(CFG.keyword)}」，就會收到你的任務卡。</p>
    </section>`;
}

async function openSession() {
  const payload = {ref: params.get('ref') || ''};
  const useDev = CFG.dev && (params.get('dev_user') || CFG.identity !== 'oa');
  if (CFG.identity === 'oa' && !useDev) {
    try {
      S = (await api('/api/me')).state;
      return true;
    } catch (e) {
      if (e.status === 401) { landing(); return false; }
      throw e;
    }
  }
  if (CFG.liff_id) {
    if (!window.liff) await loadScript('https://static.line-scdn.net/liff/edge/2/sdk.js');
    await liff.init({liffId: CFG.liff_id});
    if (!liff.isLoggedIn()) { liff.login({redirectUri: location.href}); return false; }
    payload.id_token = liff.getIDToken();
    payload.access_token = liff.getAccessToken();
  } else if (useDev) {
    payload.dev_user = params.get('dev_user') || 'tester';
    payload.dev_friend = params.get('friend') !== '0';
  } else {
    throw new Error('任務卡尚未開放');
  }
  try {
    S = (await api('/api/session', payload)).state;
  } catch (e) {
    // ID token 過期時重新登入一次
    if (e.status === 401 && CFG.liff_id && !params.get('relogin')) {
      liff.logout();
      const u = new URL(location.href); u.searchParams.set('relogin', '1');
      liff.login({redirectUri: u.toString()});
      return false;
    }
    throw e;
  }
  return true;
}

async function boot() {
  try {
    CFG = await api('/api/config');
    const ev = CFG.event;
    document.getElementById('facts').innerHTML =
      [['博覽會', ev.name], ['日期', '12/17（四）–12/19（六）'], ['地點', ev.venue], ['攤位', ev.booth]]
        .map(f => `<div class="fact"><span>${esc(f[0])}</span><b>${esc(f[1])}</b></div>`).join('');
    document.getElementById('hours').textContent = ev.hours;
    document.getElementById('foot').innerHTML = esc(CFG.disclaimer) + '<br>© 2020–2026 PulsarPump 脈衝星 ｜ service@pulsarpump.com';
    if (await openSession()) render();
  } catch (e) {
    app.innerHTML = `<section class="panel alert"><h3>打不開任務卡</h3><p class="msg bad">${esc(e.message)}</p><button class="primary" onclick="location.reload()">重新整理</button></section>`;
  }
}

// ---- 動作 ----

async function act(key, fn) {
  flash = {};
  try {
    const r = await fn();
    if (r && r.state) S = r.state;
    if (r && r.note) flash[key] = {text: r.note, ok: !r.bad};
  } catch (e) {
    flash[key] = {text: e.message, ok: false};
  }
  render();
}
const msg = key => flash[key] ? `<p class="msg ${flash[key].ok ? 'ok' : 'bad'}">${esc(flash[key].text)}</p>` : '';

const inviteUrl = () => CFG.liff_id
  ? `https://liff.line.me/${CFG.liff_id}?ref=${S.code}`
  : `${location.origin}/?ref=${S.code}`;

async function invite() {
  const text = CFG.invite_text + inviteUrl();
  if (!CFG.demo && !window.liff && /Android|iPhone|iPad|Line\//i.test(navigator.userAgent)) {
    // 不在 LIFF 裡時，用 LINE 的分享連結開啟選擇聊天室的畫面
    location.href = 'https://line.me/R/share?text=' + encodeURIComponent(text);
    return {note: '朋友加入好友並完成第 1 站後，這一站就會完成。'};
  }
  if (window.liff && liff.isApiAvailable && liff.isApiAvailable('shareTargetPicker')) {
    try {
      const res = await liff.shareTargetPicker([{type: 'text', text}]);
      if (res) return {note: '邀請已送出。朋友加入好友並完成第 1 站後，這一站就會完成。'};
      return {};
    } catch (e) { /* 改用複製 */ }
  }
  try {
    await navigator.clipboard.writeText(text);
    return {note: '邀請文字已複製，貼給朋友就可以。'};
  } catch (e) {
    return {note: text};
  }
}

// ---- 畫面 ----

function rewardGummy() {
  const r = S.rewards, t = S.today;
  const fallbackBtn = r.fallback === 'available'
    ? `<button data-act="fallback">改領官網 100 元折扣券</button><p class="note">改領後就不能再領軟糖。</p>` : '';
  const map = {
    locked: ['', '完成 3 站', `加入好友並完成任 3 站，到 ${esc(CFG.event.booth)} 攤位領能量軟糖 2 條。`],
    ready: ['on', '可以領軟糖了', t.is_event_day
      ? `到 ${esc(CFG.event.booth)} 攤位出示你的代碼。今日還有 ${t.gummy_left} 份。`
      : `資格已達成。12/17–12/19 到 ${esc(CFG.event.booth)} 攤位出示代碼領取，每日限量。`],
    out_today: ['warn', '今日軟糖已送完', t.over ? '' : '明天還有新的份數，也可以改領折扣券。'],
    ended: ['warn', '博覽會已結束', '實體獎勵已停止發放。'],
    claimed: ['on', '軟糖已領取', '謝謝你來攤位。'],
    switched: ['on', '已改領官網折扣券', `<span class="coupon">${esc(r.fallback_code)}</span>結帳時輸入，可與新會員優惠併用。`],
  }[r.gummy];
  return `<div class="reward ${map[0]}"><b>${map[1]}</b><span>${map[2]}</span>${r.gummy === 'out_today' || r.gummy === 'ended' ? fallbackBtn : ''}${msg('fallback')}</div>`;
}

function rewardCrampeace() {
  const t = S.today;
  const map = {
    locked: ['', '兩人組隊', '揪一位跑友，兩個人都到攤位完成第 4 站，各送勁速補 1 包。'],
    waiting_partner: ['warn', '等隊友到攤位', '你這邊完成了，等隊友也完成第 4 站就能領勁速補。'],
    ready: ['on', '可以領勁速補了', t.is_event_day ? `到攤位出示代碼。今日還有 ${t.crampeace_left} 份。` : '博覽會期間到攤位出示代碼領取。'],
    out_today: ['warn', '今日勁速補已送完', t.over ? '' : '明天還有新的份數。'],
    ended: ['warn', '博覽會已結束', '實體獎勵已停止發放。'],
    claimed: ['on', '勁速補已領取', '謝謝你們一起來。'],
  }[S.rewards.crampeace];
  return `<div class="reward ${map[0]}"><b>${map[1]}</b><span>${map[2]}</span></div>`;
}

function rewardCoupon5() {
  const r = S.rewards;
  return r.coupon5
    ? `<div class="reward on"><b>5 站全部完成</b><span><span class="coupon">${esc(r.coupon5_code)}</span>官網 100 元折扣券，可與新會員優惠併用。</span></div>`
    : `<div class="reward"><b>完成 5 站</b><span>官網 100 元折扣券，可與新會員優惠併用。</span></div>`;
}

function station(k, tags, title, body) {
  const done = S.stations[k];
  const tagHtml = tags.map(t => `<span class="tag ${t[0]}">${t[1]}</span>`).join('') + (done ? '<span class="tag ok">已完成</span>' : '');
  return `<li class="stn ${done ? 'done' : ''}"><div class="mark">0${k}</div>
    <div class="card"><div class="tags">${tagHtml}</div><h3>${title}</h3>${body}</div></li>`;
}

function render() {
  if (!S.consent) {
    app.innerHTML = `<section class="panel alert">
      <h3>開始前，先說明資料怎麼用</h3>
      <p>${esc(CFG.privacy)}</p>
      ${CFG.privacy_url ? `<a href="${esc(CFG.privacy_url)}" target="_blank" rel="noopener">隱私權條款</a>` : ''}
      <button class="primary" data-act="consent">同意並開始</button>${msg('consent')}
    </section>`;
    return bind();
  }
  const draft = CFG.reviewed ? '' : '<p class="note">內容為草稿，待黎醫師確認。</p>';
  const friend = S.is_friend ? '' : `<section class="panel alert">
      <h3>${CFG.friend_check === 'staff' ? '領獎前，請攤位人員確認好友' : '先加入 LINE 好友'}</h3>
      <p class="muted">${CFG.friend_check === 'staff'
        ? '進度已經幫你記錄。加入好友後，到攤位把 LINE 畫面給工作人員看一下，就能領獎勵。'
        : '進度已經幫你記錄。加入好友後才能領獎勵，博覽會前一天也會提醒你。'}</p>
      <a class="btn primary" href="${addFriendUrl()}" target="_blank" rel="noopener">加入 PulsarPump 好友</a>
      <button data-act="refresh">${CFG.friend_check === 'staff' ? '重新整理' : '我加好了，重新整理'}</button>${msg('refresh')}
    </section>`;

  const plan = S.distance ? '<table>' + CFG.plans[S.distance].map(r => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join('') + '</table>' : '';
  const s1 = `<div class="row">${[['half', '半馬'], ['full', '全馬'], ['cheer', '陪跑加油']].map(d =>
      `<button data-act="dist" data-v="${d[0]}" class="${S.distance === d[0] ? 'sel' : ''}">${d[1]}</button>`).join('')}</div>
    ${plan}${draft}${msg('dist')}<p class="note">到攤位可以請我們依你的配速調整。</p>`;

  const s2 = S.stations['2']
    ? `<p>${flash.myth ? '' : esc(S.myth_explain || '')}</p>${msg('myth')}${draft}`
    : `<div class="row2"><button data-act="myth" data-v="1">真的</button><button data-act="myth" data-v="0">假的</button></div>${msg('myth')}`;

  const s3 = `<p class="muted">${S.stations['3'] ? '你們已經是一隊。兩個人都到攤位完成第 4 站，各送勁速補 1 包。'
      : S.has_partner ? '朋友已經點開邀請。等他加入好友並完成第 1 站，這一站就會完成。'
      : '把任務卡傳給一位要去博覽會的朋友。他加入好友並完成第 1 站，你們就算一隊。'}</p>
    <button class="${S.stations['3'] ? '' : 'primary'}" data-act="invite">${S.stations['3'] ? '再邀請一位' : '傳邀請給跑友'}</button>${msg('invite')}
    <p class="note">你的邀請碼：<b>${esc(S.code)}</b></p>
    ${S.stations['3'] || S.invited ? '' : `<div class="codebox"><input id="refcode" placeholder="朋友的邀請碼" maxlength="5" autocomplete="off" autocapitalize="characters" aria-label="朋友的邀請碼">
      <button data-act="ref">加入他的隊</button></div>${msg('ref')}`}`;

  const s4 = S.stations['4'] ? '<p class="muted">謝謝你來攤位。</p>'
    : `<p class="muted">出示這張任務卡，試喝後工作人員會給你當日通關碼。三天都可以來。</p>
       <div class="codebox"><input id="code4" placeholder="輸入通關碼" autocomplete="off" autocapitalize="characters" aria-label="通關碼">
       <button data-act="code">送出</button></div>${msg('code')}`;

  const s5 = S.stations['5'] ? `<p class="muted">問題收到了。精選題目會拍成「醫師說」影片，被選上會通知你。</p>${msg('ask')}`
    : `<p class="muted">在攤位直接問黎醫師，或寫在這裡。精選題目會拍成「醫師說」影片，被選上會通知你。</p>
       <label for="q">你的問題<textarea id="q" maxlength="200" placeholder="例如：全馬 30K 後胃口很差，還要硬吃嗎？"></textarea></label>
       <button data-act="ask">送出問題</button>${msg('ask')}
       <p class="note">不回答個人病情、用藥與檢查數值，這類狀況請直接就醫。</p>`;

  app.innerHTML = `${friend}
    <section class="progress" aria-live="polite">
      <div class="progress-top"><span class="muted">已蓋章</span><b>${S.count} / 5</b></div>
      <div class="stamps" role="img" aria-label="已完成 ${S.count} 站，共 5 站">${[1, 2, 3, 4, 5].map(k => S.stations[k]
        ? `<div class="slot done ${seen && !seen[k] ? 'new' : ''}">${STAMP[k]}</div>`
        : `<div class="slot">${k}</div>`).join('')}</div>
      <div class="mycode"><span class="muted">出示給攤位人員的代碼</span><b>${esc(S.code)}</b></div>
    </section>
    <section class="rewards">${rewardGummy()}${rewardCrampeace()}${rewardCoupon5()}
      <p class="note" style="padding-top:10px">實體獎勵每人各限領一次，每日限量，送完為止。</p></section>
    <ol class="route">
      ${station('1', [['online', '線上']], '選你的距離，拿一張補給時間表', s1)}
      ${station('2', [['online', '線上']], esc(CFG.myth.question), s2)}
      ${station('3', [['online', '線上']], '揪一位跑友一起來', s3)}
      ${station('4', [['onsite', '現場']], '到攤位喝一杯冰的搖滾話梅', s4)}
      ${station('5', [['onsite', '現場'], ['online', '或線上']], '問黎醫師一題', s5)}
    </ol>
    <p class="note">不去博覽會？活動結束後，完成 3 站的好友可以改領官網折扣券。</p>`;
  seen = {...S.stations};
  bind();
}

function bind() {
  app.querySelectorAll('[data-act]').forEach(el => el.addEventListener('click', () => {
    const a = el.dataset.act, v = el.dataset.v;
    el.disabled = true;
    if (a === 'consent') return act('consent', () => api('/api/consent', {}));
    if (a === 'refresh') return act('refresh', async () => {
      await openSession();
      if (S.is_friend) return {};
      return {note: CFG.friend_check === 'staff' ? '還沒確認。到攤位請工作人員幫你確認。'
        : CFG.friend_check === 'oa' ? `還沒偵測到。加入後在聊天室輸入「${CFG.keyword}」再試一次。`
        : '還沒偵測到好友狀態，加入後再按一次。', bad: true};
    });
    if (a === 'dist') return act('dist', () => api('/api/station/1', {distance: v}));
    if (a === 'myth') return act('myth', async () => {
      const r = await api('/api/station/2', {answer: v === '1'});
      return {state: r.state, note: (r.correct ? '答對了。' : '其實是假的。') + r.explain};
    });
    if (a === 'invite') return act('invite', invite);
    if (a === 'ref') return act('ref', () => api('/api/ref', {code: document.getElementById('refcode').value}));
    if (a === 'code') return act('code', () => api('/api/station/4', {code: document.getElementById('code4').value}));
    if (a === 'ask') return act('ask', () => api('/api/station/5', {text: document.getElementById('q').value}));
    if (a === 'fallback') return act('fallback', () => api('/api/reward/fallback', {}));
  }));
}

boot();
