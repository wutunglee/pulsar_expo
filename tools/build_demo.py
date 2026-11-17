"""把任務卡頁打包成單一 HTML 的示範版（不需要後端，資料只存在記憶體）。
用法：python tools/build_demo.py 輸出路徑
給內部看畫面與流程用，不是正式站。"""
import base64
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import content  # noqa: E402
import rules  # noqa: E402

read = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()
logo = base64.b64encode(open(os.path.join(ROOT, "static/logo_black.png"), "rb").read()).decode()

cfg = {
    "liff_id": "", "dev": True, "demo": True, "oa_id": "@201nygdf", "identity": "demo",
    "friend_check": "oa", "keyword": "任務卡", "has_pending_ref": False,
    "event": content.EVENT, "plans": content.PLANS, "quiz_after": content.QUIZ_AFTER,
    "invite_text": content.INVITE_TEXT, "privacy": content.PRIVACY,
    "privacy_url": "", "disclaimer": content.DISCLAIMER, "reviewed": content.REVIEWED_BY_DOCTOR,
    "caps": {d: {r: v[r] for r in rules.PHYSICAL} for d, v in rules.EVENT_DAYS.items()},
}

MOCK = """
const DEMO = {cfg: %s, pool: %s};
const D = {consent: false, distance: null, ans: {}, asked: false, sent: false, partner: false,
           partnerNew: true, partnerArrived: false, arrived: false, got: {}, eventDay: false, over: false,
           ids: [...DEMO.pool.keys()].sort(() => Math.random() - .5).slice(0, 3)};
function demoState() {
  const quizDone = D.ids.every(i => i in D.ans), qualified = D.partner && D.partnerNew;
  const stock = D.over ? 'ended' : 'ready';
  const absent = D.over && !!D.distance && !D.arrived;
  return {name: '示範', code: 'PUL5R', is_friend: true, is_new: true, consent: D.consent,
    distance: D.distance, arrived: D.arrived, asked: D.asked, quiz_done: quizDone,
    quiz: D.ids.map(i => i in D.ans
      ? {id: i, text: DEMO.pool[i].text, answered: true, correct: D.ans[i] === DEMO.pool[i].answer, truth: DEMO.pool[i].answer, explain: DEMO.pool[i].explain}
      : {id: i, text: DEMO.pool[i].text, answered: false}),
    has_partner: D.partner, team_qualified: qualified, partner_arrived: D.partnerArrived, invited: false,
    rewards: {
      gummy: D.got.gummy ? 'claimed' : !D.distance ? 'locked' : stock,
      bonus: D.got.bonus ? 'claimed' : !quizDone ? 'locked' : stock,
      crampeace: D.got.crampeace ? 'claimed' : !qualified ? 'locked' : !(D.arrived && D.partnerArrived) ? 'waiting_partner' : stock,
      onsite: D.got.onsite ? 'used' : D.arrived ? 'ready' : 'locked', onsite_text: '現場優惠（內容待定）',
      absent: absent, absent_code: absent ? 'DEMO100' : ''},
    today: {is_event_day: D.eventDay, over: D.over, left: D.eventDay ? {gummy: 150, bonus: 150, crampeace: 80} : null}};
}
async function api(path, data) {
  const fail = m => { const e = new Error(m); e.status = 400; throw e; };
  if (path === '/api/config') return DEMO.cfg;
  if (path === '/api/consent') D.consent = true;
  if (path === '/api/distance') D.distance = data.distance;
  if (path === '/api/quiz' && !(data.q in D.ans)) D.ans[data.q] = data.answer;
  if (path === '/api/ask') { if (!String(data.text || '').trim()) fail('請先寫下你的問題'); D.asked = true; }
  if (path === '/api/ref') fail('示範版沒有其他參加者');
  return {ok: true, state: demoState()};
}
function demoDo(a) {
  const tip = document.getElementById('demoTip');
  tip.textContent = '';
  if (a === 'reset') { location.reload(); return; }
  if (a === 'landing') { landing(); return; }
  if (!S || !S.distance) { tip.textContent = '先在下面的兌換券按「同意並開始」、選距離。'; return; }
  if (a === 'friend' || a === 'oldfriend') {
    if (!D.sent) { tip.textContent = '先在「加碼二」按「傳邀請給跑友」，再按這個按鈕。'; return; }
    D.partner = true; D.partnerNew = a === 'friend';
  }
  if (a === 'day') { D.eventDay = true; D.over = false; }
  if (a === 'checkin') {
    if (!D.eventDay) { tip.textContent = '要先切到博覽會當天，攤位才有人。'; return; }
    const before = Object.keys(D.got).length, first = !D.arrived;
    D.arrived = true;
    const s = demoState().rewards;
    ['gummy', 'bonus', 'crampeace'].forEach(k => { if (s[k] === 'ready') D.got[k] = true; });
    tip.textContent = first ? '工作人員按了「報到並發放」。' : Object.keys(D.got).length > before ? '工作人員發了新達成的加碼。' : '目前沒有新的東西可以領。';
  }
  if (a === 'mate') {
    if (!(D.partner && D.partnerNew)) { tip.textContent = '要先有一位新朋友接受邀請。'; return; }
    D.partnerArrived = true;
  }
  if (a === 'pay') {
    if (!D.arrived) { tip.textContent = '要先到攤位報到，才有現場優惠券。'; return; }
    D.got.onsite = true;
  }
  if (a === 'over') { D.eventDay = false; D.over = true; }
  S = demoState(); flash = {}; render();
}
""" % (json.dumps(cfg, ensure_ascii=False), json.dumps(content.QUIZ, ensure_ascii=False))

js = read("static/app.js")
js, n = re.subn(r"async function api\(path, data, method\) \{.*?\n\}\n", lambda m: MOCK, js, count=1, flags=re.S)
assert n == 1
js, n = re.subn(r"async function invite\(\) \{", "async function invite() { D.sent = true;", js, count=1)
assert n == 1

html = read("templates/index.html")
html = html.replace('<link rel="stylesheet" href="/static/card.css">',
                    "<style>" + read("static/card.css") + """
.demo{background:#1f1b16;color:#f8f3e8;padding:12px 14px;margin-bottom:12px;font-size:15px;line-height:1.6}
.demo b{display:block;margin-bottom:6px}
.demo div{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.demo button{color:#f8f3e8;border-color:#f8f3e8;font-size:15px;padding:6px 10px}
</style>""")
html = html.replace('src="/static/logo_black.png"', f'src="data:image/png;base64,{logo}"')
html = html.replace('<div class="wrap">', """<div class="wrap">
  <div class="demo"><b>示範版：資料不會儲存，重新整理就歸零</b>
    先照著下面的兌換券自己做。這些按鈕只用來模擬「別人」和「時間」。
    <div><button onclick="demoDo('landing')">看加好友引導頁</button>
    <button onclick="demoDo('friend')">新朋友接受邀請</button>
    <button onclick="demoDo('oldfriend')">舊好友接受邀請</button>
    <button onclick="demoDo('day')">切到博覽會當天</button>
    <button onclick="demoDo('checkin')">攤位人員幫我報到</button>
    <button onclick="demoDo('mate')">隊友也到攤位</button>
    <button onclick="demoDo('pay')">結帳用了現場優惠</button>
    <button onclick="demoDo('over')">活動結束</button>
    <button onclick="demoDo('reset')">重來</button></div>
    <p id="demoTip" style="color:#ffb199;margin-top:8px"></p></div>""", 1)
html = html.replace('<script src="/static/app.js"></script>', "<script>\n" + js + "\n</script>")
html = html.replace("<title>", "<title>示範｜", 1)
open(sys.argv[1], "w", encoding="utf-8").write(html)
print("ok", len(html))
