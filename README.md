# pulsar_expo｜博覽會補給站任務卡

PulsarPump 脈衝星在 LINE 裡的任務卡：線上 3 站＋現場 2 站，完成後到攤位領獎勵。
第一場用在 2026/12/17–12/19 馬拉松運動博覽會（花博爭艷館，攤位 105），之後可改設定用在跑團擺攤或下一場展覽。

狀態（2026-11-17）：後端、三個頁面、自動化測試完成，已用本機開發模式實測。**尚未接上 LINE、尚未部署。**

## 怎麼認出參加者（`IDENTITY`）

| 設定 | 流程 | 需要的 LINE 權限 |
|---|---|---|
| `oa`（預設） | 加官方帳號好友 → 官方帳號自動回一則訊息，裡面是他專屬的任務卡連結 → 點開就是他的卡。已經是好友的人在聊天室輸入「任務卡」也會收到。 | 官方帳號 Messaging API 頻道的 Channel ID、Channel secret，並把 Webhook URL 指到 `/line/webhook` |
| `liff` | 在 LINE 內以 LIFF 開啟，用 LINE Login 認人 | 與官方帳號同一個 Provider 的 LINE Login 頻道＋LIFF |

`oa` 模式下邀請朋友：朋友點 `/?ref=邀請碼` 時先把邀請碼記在 cookie，等他加好友、點開專屬連結時套用。
如果 cookie 沒帶到（例如換了瀏覽器），朋友可以在第 3 站手動輸入邀請碼。

## 怎麼確認「已加好友」（`FRIEND_CHECK`）

| 設定 | 依據 |
|---|---|
| `oa` | webhook 的加入好友／封鎖通知（`IDENTITY=oa` 的預設） |
| `line` | LIFF 開卡時向 LINE 查詢（`IDENTITY=liff` 的預設，需要 LINE Login 頻道已連結官方帳號） |
| `staff` | 攤位人員看過對方手機後，在核銷頁按「確認是好友」。LINE 權限拿不到時的備案 |

## 三個頁面

| 網址 | 給誰 | 用途 |
|---|---|---|
| `/` | 參加者 | 任務卡。沒有身分的人會看到「加入好友」的引導 |
| `/t/<token>` | 參加者 | 官方帳號傳給每個人的專屬連結，點開後轉到 `/` |
| `/line/webhook` | LINE 平台 | 接收加好友、封鎖、訊息事件（會驗證簽章） |
| `/staff` | 攤位人員 | 輸入參加者的 5 碼代碼，發放獎勵並記錄；也可代為完成第 4、5 站 |
| `/admin?token=…` | 行銷與管理 | 開卡人數、各站完成數、每日發放、邀請排行、提問下載 |

## 活動規則（都在 `rules.py`）

- 5 站不強制順序。第 3 站：被邀請的朋友加入好友並完成第 1 站時，邀請雙方同時完成。
- 第 4 站：只在展期當天、輸入當日通關碼才算。每人每天可試 10 次。
- 軟糖：已加好友＋完成任 3 站。每日限量（150／150／300），每人限領一次。
- 勁速補：已組隊，且自己和隊友都完成第 4 站。每日限量（80／80／160），每人限領一次。
- 軟糖當日送完或活動結束後，符合資格的人可改領備案折扣券，與軟糖二選一。
- 5 站全部完成：顯示官網折扣碼。
- 領任何獎勵都必須已加 LINE 好友。

文字內容（補給時間表、迷思題、個資說明）在 `content.py`。黎醫師確認後把 `REVIEWED_BY_DOCTOR` 改成 `True`，頁面上的「草稿」字樣才會消失。

## 本機執行

```bash
pip install -r requirements.txt
python app.py            # 開發模式，http://127.0.0.1:5057
```

開發模式不需要 LINE，用網址參數模擬身分（不帶參數時會看到「加入好友」的引導頁）：

- `/?dev_user=Alice`：以 Alice 開卡（預設已加好友）
- `/?dev_user=Bob&friend=0`：尚未加好友
- `/?dev_user=Bob&ref=ABCDE`：由代碼 ABCDE 的人邀請

測試：

```bash
python -m unittest discover -s tests
```

## 部署（給 Ken）

1. **子網域與 HTTPS**：例如 `expo.pulsarpump.com`。LIFF 的端點網址必須是 HTTPS。
2. **程式**：`git clone` 後 `pip install -r requirements.txt`。進入點是 `passenger_wsgi.py`（Passenger）或 `wsgi.py`（gunicorn）。
3. **設定**：把 `.env.example` 複製成 `.env` 填好。`DEV_MODE` 必須是 `0`；`SECRET_KEY` 沒設定時程式會拒絕啟動。
4. **資料庫**：SQLite，預設在 `data/expo.db`，啟動時自動建表。執行程式的帳號要能寫入該資料夾。請納入主機備份。
5. **LINE（`IDENTITY=oa`）**
   - 把官方帳號 Messaging API 頻道的 Channel ID、Channel secret 填進 `.env`（OA Manager →設定 → Messaging API 可看到）。
   - 把 Webhook URL 設為 `https://<網域>/line/webhook` 並啟用 webhook。這個路徑不能加密碼保護。
   - **改 Webhook URL 之前**先確認：目前設定的網址（`https://www.pulsarpump.com/`）有沒有其他程式在用；
     以及 OA Manager 的「回應設定」——啟用 webhook 後，聊天與自動回應要維持可用。
   - 程式只回應「加好友」和關鍵字「任務卡」，其他訊息一律不回，留給真人客服。
   - 加入好友的歡迎訊息如果已在 OA Manager 設定，使用者會同時收到歡迎訊息與任務卡連結。
   - 改用 `IDENTITY=liff` 時：在官方帳號所在的 Provider 建立 LINE Login 頻道與 LIFF（Scope `openid`、`profile`），
     把官方帳號設為連結的官方帳號，LIFF ID 與頻道 ID 填進 `.env`。
6. **折扣碼**：在官網後台建立兩組 WooCommerce 折扣碼（5 站完成、備案），可與新會員優惠併用，再把代碼填進 `.env`。
7. **通關碼**：三天各一組，填進 `DAY_CODES`。活動前再定，不要進版控。
8. **上線前**：用手機在 LINE 裡實際走一輪，包含加好友、邀請另一支手機、攤位核銷。

### 尚未驗證、需要對照官方文件的部分

`line_api.py` 的所有呼叫都是依官方文件寫的，還沒對著真正的頻道跑過。接上後若回應格式不同，只需要改這一個檔案。上線前要實測：

- 以 Channel ID＋secret 取得無狀態 channel access token（`/oauth2/v3/token`）。
- 加好友後是否收到專屬連結；已是好友的人輸入「任務卡」是否收到。
- 啟用 webhook 後，OA Manager 的手動聊天、自動回應、歡迎訊息是否照常。
- 在 LINE 內點專屬連結後，邀請用的 cookie 是否帶得到（帶不到時走手動輸入邀請碼）。

## 個資

- 會存：LINE userId 與顯示名稱、各站完成時間、選的距離、邀請關係、提問內容、領獎紀錄。
- 參加者開卡時要先按同意，才能開始任何一站。
- 刪除某位參加者的資料：`flask --app app delete-user`，輸入他的 5 碼代碼。
- `content.py` 的 `PRIVACY_URL` 目前是空的，請填入官網隱私權條款的網址。

## 已知限制

- PIN 錯誤次數的鎖定記在記憶體裡，多個 worker 時各算各的。
- 管理頁的代碼放在網址參數，請不要把含代碼的網址貼到公開的地方。
- 專屬連結等同那個人的登入憑證：拿到連結的人就能開他的卡。連結不會過期，訊息裡有提醒不要轉傳。
- 現有的 1,825 位好友不會收到加好友事件，要靠群發訊息請他們輸入「任務卡」（或點圖文選單送出這三個字）。
- `IDENTITY=liff`、`FRIEND_CHECK=line` 時，「已加好友」是開卡當下的查詢結果，中途退好友要等下次開卡才更新。
