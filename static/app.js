// 兌換券前端。狀態一律以伺服器回傳為準，畫面只負責呈現。
let CFG = null, S = null, flash = {}, editDistance = false, planOpen = false, askCode = false, justDrew = false;
const DIST = {half: '半馬', full: '全馬', cheer: '陪跑加油'};
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
      <h3>${invited ? '朋友邀你一起去攤位' : '加入好友，領你的兌換券'}</h3>
      <p>加入 PulsarPump 的 LINE 好友，我們會馬上把你專屬的兌換券傳到聊天室。選好距離，就幫你在攤位保留一份。${invited ? '你和朋友會自動算成一隊。' : ''}</p>
      <a class="btn primary" href="${addFriendUrl()}">加入 PulsarPump 好友</a>
      <p class="note">已經是好友？在 PulsarPump 的聊天室輸入「${esc(CFG.keyword)}」，就會收到你的兌換券。</p>
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
    payload.dev_new = params.get('new') !== '0';
  } else {
    throw new Error('兌換券尚未開放');
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
    document.getElementById('facts').innerHTML = ev.facts
      .map(([k, v]) => `<div class="fact"><span>${esc(k)}</span><b>${esc(v).replace(/\n/g, '<br>')}</b></div>`).join('');
    document.getElementById('foot').innerHTML = esc(CFG.disclaimer) + '<br>© 2020–2026 PulsarPump 脈衝星 ｜ service@pulsarpump.com';
    if (await openSession()) render();
  } catch (e) {
    app.innerHTML = `<section class="panel alert"><h3>打不開兌換券</h3><p class="msg bad">${esc(e.message)}</p><button class="primary" onclick="location.reload()">重新整理</button></section>`;
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
  : `${location.origin}${location.pathname}?ref=${S.code}`;

async function invite() {
  const text = CFG.invite_text + inviteUrl();
  if (!CFG.demo && !window.liff && /Android|iPhone|iPad|Line\//i.test(navigator.userAgent)) {
    // 不在 LIFF 裡時，用 LINE 的分享連結開啟選擇聊天室的畫面
    location.href = 'https://line.me/R/share?text=' + encodeURIComponent(text);
    return {note: '朋友加入好友並領券後，你們就算一隊。'};
  }
  if (window.liff && liff.isApiAvailable && liff.isApiAvailable('shareTargetPicker')) {
    try {
      const res = await liff.shareTargetPicker([{type: 'text', text}]);
      if (res) return {note: '邀請已送出。朋友加入好友並領券後，你們就算一隊。'};
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

// 贈品一列：左邊品名，右邊狀態章
function item(name, sub, status) {
  const label = {claimed: '已領取', ready: '已保留', out_today: '今日送完', ended: '已結束',
                 waiting_partner: '等隊友', locked: '未達成'}[status];
  const cls = status === 'claimed' ? 'done' : status === 'ready' ? 'on' : '';
  return `<li class="item ${cls}"><div><b>${name}</b>${sub ? `<span>${sub}</span>` : ''}</div><i>${label}</i></li>`;
}

function pickup() {
  const t = S.today, booth = esc(CFG.event.booth);
  if (S.arrived) return '<p class="muted">謝謝你來攤位。還有加碼沒領的話，達成後再給工作人員看一次代碼。</p>';
  if (t.over) return '<p class="muted">博覽會已經結束，現場贈品停止發放。</p>';
  if (t.is_event_day) return `<p><b>現在就可以領。</b>到 ${booth} 攤位，把下面的代碼給工作人員看。今日軟糖還有 ${t.left.gummy} 份。</p>`;
  return `<p>12/17–12/19 到 ${booth} 攤位，把下面的代碼給工作人員看。每日限量，三天都可以來。</p>`;
}

function quizBlock() {
  const r = S.rewards, total = S.quiz.length, done = S.quiz.filter(q => q.answered).length;
  const cur = S.quiz.find(q => !q.answered);
  const draft = CFG.reviewed ? '' : '<p class="note">題目與解說為草稿，待黎醫師確認。</p>';
  const answered = S.quiz.filter(q => q.answered).map(q => `
    <div class="qa"><p class="q">${esc(q.text)}</p>
      <p class="a"><b>${q.correct ? '答對了' : '答錯了'}，這句話是${q.truth ? '真的' : '假的'}。</b>${esc(q.explain)}</p></div>`).join('');
  const ask = cur ? `<div class="qa now"><p class="note">第 ${done + 1} 題，共 ${total} 題</p><p class="q">${esc(cur.text)}</p>
      <div class="row2"><button data-act="quiz" data-q="${cur.id}" data-v="1">真的</button><button data-act="quiz" data-q="${cur.id}" data-v="0">假的</button></div>${msg('quiz')}</div>` : '';
  const end = cur ? '' : `<p class="msg ok">${total} 題都答完了，${r.bonus === 'claimed' ? '加碼軟糖已領取。' : '到攤位多拿 1 條軟糖。'}${esc(CFG.quiz_after)}</p>`;
  return `<section class="extra" id="quiz">
      <div class="extra-head"><span class="plus">加碼一</span><h3>答 ${total} 題真的假的，多拿 1 條軟糖</h3></div>
      <p class="muted">不管答對答錯都算，答完看解說。</p>
      ${answered}${ask}${end}${draft}
    </section>`;
}

function teamBlock() {
  const st = S.rewards.crampeace;
  const status = st === 'claimed' ? '勁速補已領取，謝謝你們一起來。'
    : st === 'ready' ? '你們兩個都到了，給工作人員看代碼就能領勁速補。'
    : st === 'out_today' ? '今日勁速補已送完，明天還有新的份數。'
    : st === 'ended' ? '博覽會已經結束。'
    : S.team_qualified ? (S.arrived ? '你到了，等隊友也到攤位，兩個人就各拿一包。' : '已經組隊。兩個人都到攤位，各拿勁速補 1 包。')
    : S.has_partner ? '你們已經連上了，但對方原本就是 PulsarPump 的好友，這個加碼要揪新朋友才算。可以再邀一位。'
    : '把邀請傳給一位還不是 PulsarPump 好友的跑友。他加入好友、領了券，你們就算一隊。';
  const canInvite = !['claimed', 'ended'].includes(st);
  return `<section class="extra" id="team">
      <div class="extra-head"><span class="plus">加碼二</span><h3>揪一位新朋友，兩人各拿勁速補 1 包</h3></div>
      <p class="muted">${status}</p>
      ${canInvite ? `<button class="${S.team_qualified ? '' : 'primary'}" data-act="invite">${S.has_partner ? '再邀請一位' : '傳邀請給跑友'}</button>${msg('invite')}
      <p class="note">朋友點你傳的連結、加好友領券，就會自動跟你一隊，不用輸入任何代碼。</p>` : ''}
      ${canInvite && !S.invited && !S.has_partner ? (askCode || flash.ref
        ? `<p class="note">正常情況不用輸入：點朋友傳來的連結領券，就會自動配對。沒配對到時，請朋友把他的兌換代碼告訴你，填在這裡。</p>
        <div class="codebox"><input id="refcode" placeholder="朋友的兌換代碼" maxlength="5" autocomplete="off" autocapitalize="characters" aria-label="朋友的兌換代碼">
        <button data-act="ref">跟他一隊</button></div>${msg('ref')}`
        : `<p class="note"><button class="link" data-act="askcode">是朋友邀你來的，但沒有顯示組隊？</button></p>`) : ''}
    </section>`;
}

function onsiteBlock() {
  const r = S.rewards;
  const head = '<div class="extra-head"><span class="plus">現場限定</span><h3>現場優惠</h3></div>';
  if (r.onsite === 'locked') return '';
  const p = r.prize ? CFG.prizes[r.prize] : null;
  if (!p) {
    if (r.onsite === 'ended') return '';
    // 四張牌是同一次抽獎：結果由主機決定，選哪一張都一樣，所以翻開後不顯示其他三張
    return `<section class="extra" id="draw">${head}
      <p>選一張翻開，看你在攤位能用什麼優惠。每人抽一次。</p>
      <div class="cards">${[1, 2, 3, 4].map(k => `<button class="cardback" data-act="draw" aria-label="第 ${k} 張">?</button>`).join('')}</div>${msg('draw')}
      <p class="note">${esc(CFG.prize_base)}</p></section>`;
  }
  const how = r.onsite === 'ended' ? '博覽會已結束' : S.today.is_event_day ? '結帳時把這個畫面給工作人員看' : `12/17–12/19 在 ${esc(CFG.event.booth)} 攤位結帳時出示`;
  return `<section class="extra" id="draw">${head}
      <div class="coupon ${r.onsite === 'ended' ? 'used' : ''} ${justDrew ? 'flip' : ''}">${esc(p.title)}<small>${how}</small></div>
      <p class="muted">${esc(p.detail)}</p>
      <p class="note">${esc(CFG.prize_base)}${r.onsite_uses ? ` 已使用 ${r.onsite_uses} 次，謝謝你的支持。` : ''}</p></section>`;
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
  const friend = S.is_friend ? '' : `<section class="panel alert">
      <h3>${CFG.friend_check === 'staff' ? '領取前，請攤位人員確認好友' : '先加入 LINE 好友'}</h3>
      <p class="muted">${CFG.friend_check === 'staff'
        ? '加入好友後，到攤位把 LINE 畫面給工作人員看一下，就能領取。'
        : '加入好友後才能領取，博覽會前一天也會提醒你。'}</p>
      <a class="btn primary" href="${addFriendUrl()}" target="_blank" rel="noopener">加入 PulsarPump 好友</a>
      <button data-act="refresh">${CFG.friend_check === 'staff' ? '重新整理' : '我加好了，重新整理'}</button>${msg('refresh')}
    </section>`;
  const distButtons = `<div class="row">${Object.entries(DIST).map(([k, v]) =>
      `<button data-act="dist" data-v="${k}" class="${S.distance === k ? 'sel' : ''}">${v}</button>`).join('')}</div>${msg('dist')}`;

  // 第一步：還沒選距離
  if (!S.distance) {
    app.innerHTML = `${friend}<section class="panel alert">
        <h3>你這次跑什麼距離？</h3>
        <p class="muted">選好之後，我們照你的距離準備一張補給時間表，連同試喝和軟糖，在 ${esc(CFG.event.booth)} 攤位幫你留一份。</p>
        ${distButtons}
      </section>`;
    return bind();
  }

  if (S.rewards.absent) {
    app.innerHTML = `<section class="panel alert">
        <h3>這次沒遇到你，送你一張官網折扣券</h3>
        <span class="coupon">${esc(S.rewards.absent_code)}<small>${esc(S.rewards.absent_text)}</small></span>
        <p class="muted">到 www.pulsarpump.com 結帳時輸入。</p>
      </section>`;
    return bind();
  }

  const r = S.rewards;
  const plan = '<table>' + CFG.plans[S.distance].map(x => `<tr><td>${esc(x[0])}</td><td>${esc(x[1])}</td></tr>`).join('') + '</table>';
  const got = r.gummy === 'claimed';
  app.innerHTML = `${friend}
    <section class="ticket" aria-live="polite">
      <p class="for">${esc(S.name) ? esc(S.name) + '，' : ''}已經幫你保留</p>
      <ul class="items">
        ${item(`${DIST[S.distance]}補給時間表`, '現場依你的配速調整', S.arrived ? 'claimed' : r.gummy === 'locked' ? 'locked' : S.today.over ? 'ended' : 'ready')}
        ${item('冰的搖滾話梅 1 杯', '現場試喝', S.arrived ? 'claimed' : r.gummy === 'locked' ? 'locked' : S.today.over ? 'ended' : 'ready')}
        ${item('能量軟糖 2 條', '', r.gummy)}
        ${r.bonus !== 'locked' ? item('加碼：能量軟糖 1 條', '答題加碼', r.bonus) : ''}
        ${!['locked', 'waiting_partner'].includes(r.crampeace) ? item('加碼：勁速補 1 包', '揪新朋友加碼', r.crampeace) : ''}
      </ul>
      ${pickup()}
      <div class="mycode"><span class="muted">兌換代碼</span><b>${esc(S.code)}</b></div>
      <p class="note">每人各限領一次，每日限量，送完為止。</p>
    </section>
    <details class="plan"${got ? '' : ''}>
      <summary>先看你的${DIST[S.distance]}補給時間表</summary>
      ${plan}
      ${CFG.reviewed ? '' : '<p class="note">內容為草稿，待黎醫師確認。</p>'}
      ${S.plan_sent ? `<p class="note">這張表也傳到你的 LINE 了。之後在 PulsarPump 聊天室輸入「補給表」，隨時可以再拿一次。</p>` : CFG.identity === 'oa' ? `<p class="note">在 PulsarPump 聊天室輸入「補給表」，就會把這張表傳給你。</p>` : ''}
      <p class="note">到攤位可以請我們依你的配速調整。${S.arrived ? '' : ' <button class="link" data-act="redist">改距離</button>'}</p>
      ${editDistance ? distButtons : msg('dist')}
    </details>
    ${onsiteBlock()}
    ${quizBlock()}
    ${teamBlock()}
    <section class="extra">
      <div class="extra-head"><span class="plus">問黎醫師</span><h3>補給、訓練的問題，先寫下來</h3></div>
      <p class="muted">${S.asked ? '問題收到了。' : ''}黎醫師會在活動後，從 PulsarPump 的 LINE 官方帳號逐一回覆。想當面問，也歡迎到攤位。</p>${S.asked ? msg('ask') : ''}
      <label for="q">${S.asked ? '還想再問一題' : '你的問題'}<textarea id="q" maxlength="200" placeholder="例如：全馬 30K 後胃口很差，還要硬吃嗎？"></textarea></label>
      <button data-act="ask">送出問題</button>${S.asked ? '' : msg('ask')}
      <p class="note">不回答個人病情、用藥與檢查數值，這類狀況請直接就醫。</p>
    </section>
    <p class="note">不能來博覽會？活動結束後，這裡會出現一張官網折扣券。</p>`;
  bind();
}

function bind() {
  app.querySelectorAll('[data-act]').forEach(el => el.addEventListener('click', () => {
    const a = el.dataset.act, v = el.dataset.v;
    if (a === 'redist') { editDistance = !editDistance; planOpen = true; return render(); }
    if (a === 'askcode') { askCode = !askCode; return render(); }
    el.disabled = true;
    if (a === 'consent') return act('consent', () => api('/api/consent', {}));
    if (a === 'refresh') return act('refresh', async () => {
      await openSession();
      if (S.is_friend) return {};
      return {note: CFG.friend_check === 'staff' ? '還沒確認。到攤位請工作人員幫你確認。'
        : CFG.friend_check === 'oa' ? `還沒偵測到。加入後在聊天室輸入「${CFG.keyword}」再試一次。`
        : '還沒偵測到好友狀態，加入後再按一次。', bad: true};
    });
    if (a === 'dist') {
      // 第一次選：回到券的開頭看保留清單。之後改距離：留在原地、時間表保持展開
      const first = !S.distance;
      editDistance = false;
      if (!first) planOpen = true;
      return act('dist', async () => {
        const r = await api('/api/distance', {distance: v});
        return first ? r : {state: r.state, note: `已改成${DIST[v]}，時間表也換好了。`};
      }).then(() => { if (first) app.scrollIntoView({block: 'start'}); });
    }
    if (a === 'draw') { justDrew = true; return act('draw', () => api('/api/draw', {})).then(() => { justDrew = false; }); }
    if (a === 'quiz') return act('quiz', () => api('/api/quiz', {q: Number(el.dataset.q), answer: v === '1'}));
    if (a === 'invite') return act('invite', invite);
    if (a === 'ref') return act('ref', () => api('/api/ref', {code: document.getElementById('refcode').value}));
    if (a === 'ask') return act('ask', async () => {
      const r = await api('/api/ask', {text: document.getElementById('q').value});
      return {state: r.state, note: '已送出。'};
    });
  }));
  // 改距離時時間表要保持展開
  const d = app.querySelector('details.plan');
  if (d) { d.open = planOpen; d.addEventListener('toggle', () => { planOpen = d.open; }); }
}

boot();
