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
    "event": content.EVENT, "plans": content.PLANS,
    "myth": {"question": content.MYTH["question"]},
    "invite_text": content.INVITE_TEXT, "privacy": content.PRIVACY,
    "privacy_url": "", "disclaimer": content.DISCLAIMER, "reviewed": content.REVIEWED_BY_DOCTOR,
    "caps": {d: {"gummy": v["gummy"], "crampeace": v["crampeace"]} for d, v in rules.EVENT_DAYS.items()},
}

MOCK = """
const DEMO = {cfg: %s, myth: %s, code: 'PULSAR'};
const D = {consent: false, distance: null, st: {1: false, 2: false, 3: false, 4: false, 5: false},
           partner: false, partner4: false, eventDay: false, sent: false};
function demoState() {
  const n = Object.values(D.st).filter(Boolean).length, all = n === 5;
  return {name: '示範', code: 'PUL5R', is_friend: true, consent: D.consent, distance: D.distance,
    stations: {...D.st}, count: n, has_partner: D.partner, invited: false,
    myth_explain: D.st[2] ? DEMO.myth.explain : undefined,
    rewards: {gummy: n >= 3 ? 'ready' : 'locked',
      crampeace: !(D.st[3] && D.st[4]) ? 'locked' : D.partner4 ? 'ready' : 'waiting_partner',
      fallback: 'none', fallback_code: '', coupon5: all, coupon5_code: all ? 'DEMO100' : ''},
    today: {is_event_day: D.eventDay, over: false, gummy_left: 150, crampeace_left: 80}};
}
async function api(path, data) {
  const fail = m => { const e = new Error(m); e.status = 400; throw e; };
  const ok = extra => ({ok: true, state: demoState(), ...extra});
  if (path === '/api/config') return DEMO.cfg;
  if (path === '/api/consent') D.consent = true;
  if (path === '/api/station/1') { D.distance = data.distance; D.st[1] = true; }
  if (path === '/api/station/2') { D.st[2] = true; return ok({correct: data.answer === DEMO.myth.answer, explain: DEMO.myth.explain}); }
  if (path === '/api/station/4') {
    if (!D.eventDay) fail('通關碼只在博覽會期間（12/17–12/19）有效');
    if (String(data.code || '').trim().toUpperCase() !== DEMO.code) fail('通關碼不對，請再向攤位人員確認一次');
    D.st[4] = true;
  }
  if (path === '/api/station/5') { if (!String(data.text || '').trim()) fail('請先寫下你的問題'); D.st[5] = true; }
  if (path === '/api/ref') fail('示範版沒有其他參加者');
  return ok();
}
function demoDo(a) {
  const tip = document.getElementById('demoTip');
  tip.textContent = '';
  if (a === 'friend') {
    if (!D.sent) { tip.textContent = '先在第 3 站按「傳邀請給跑友」，再按這個按鈕。'; return; }
    D.partner = true; D.st[3] = true;
  }
  if (a === 'day') D.eventDay = !D.eventDay;
  if (a === 'mate') {
    if (!D.st[3] || !D.st[4]) { tip.textContent = '要先組隊（第 3 站）並完成第 4 站，隊友到攤位才有意義。'; return; }
    D.partner4 = true;
  }
  if (a === 'reset') { location.reload(); return; }
  if (a === 'landing') { landing(); return; }
  document.getElementById('demoDay').textContent = D.eventDay ? '2. 改回活動前' : '2. 切到博覽會當天';
  S = demoState(); flash = {}; if (CFG) render();
}
""" % (json.dumps(cfg, ensure_ascii=False),
       json.dumps({"answer": content.MYTH["answer"], "explain": content.MYTH["explain"]}, ensure_ascii=False))

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
    先照著任務卡自己做。下面的按鈕只用來模擬「別人」或「時間」：朋友接受邀請、博覽會開始、隊友到攤位。第 4 站的示範通關碼是 PULSAR。
    <div><button onclick="demoDo('landing')">0. 看加好友引導頁</button>
    <button onclick="demoDo('friend')">1. 朋友接受了邀請</button>
    <button onclick="demoDo('day')" id="demoDay">2. 切到博覽會當天</button>
    <button onclick="demoDo('mate')">3. 隊友也到攤位</button>
    <button onclick="demoDo('reset')">重來</button></div>
    <p id="demoTip" style="color:#ffb199;margin-top:8px"></p></div>""", 1)
html = html.replace('<script src="/static/app.js"></script>', "<script>\n" + js + "\n</script>")
html = html.replace("<title>", "<title>示範｜", 1)
open(sys.argv[1], "w", encoding="utf-8").write(html)
print("ok", len(html))
