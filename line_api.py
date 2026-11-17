"""跟 LINE 平台的所有呼叫，集中在這個檔案。

兩組功能，用哪一組由 .env 的 IDENTITY 決定：
  - LINE Login／LIFF：verify_id_token、is_friend（需要 LINE Login 頻道）
  - 官方帳號 Messaging API：verify_signature、channel_token、get_profile、reply
    （只需要官方帳號那個 Messaging API 頻道的 Channel ID 與 Channel secret）

注意：端點與欄位依 LINE Developers 官方文件撰寫，尚未對著正式頻道實測。
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
FRIENDSHIP_URL = "https://api.line.me/friendship/v1/status"
TOKEN_URL = "https://api.line.me/oauth2/v3/token"
PROFILE_URL = "https://api.line.me/v2/bot/profile/"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"
TIMEOUT = 8
_token_cache = {}  # channel_id -> (token, 到期時間)


class LineError(Exception):
    pass


def _read(req):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LineError(f"LINE 回應 {e.code}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise LineError("連不上 LINE") from e


def verify_id_token(id_token, channel_id):
    """回傳 (user_id, 顯示名稱)。token 無效時丟出 LineError。"""
    body = urllib.parse.urlencode({"id_token": id_token, "client_id": channel_id}).encode()
    data = _read(urllib.request.Request(VERIFY_URL, data=body))
    if not data.get("sub"):
        raise LineError("ID token 無效")
    return data["sub"], data.get("name", "")


def is_friend(access_token):
    req = urllib.request.Request(FRIENDSHIP_URL,
                                 headers={"Authorization": f"Bearer {access_token}"})
    return bool(_read(req).get("friendFlag"))


# ---------- 官方帳號 Messaging API ----------

def verify_signature(raw_body, signature, channel_secret):
    """webhook 簽章：以 Channel secret 對原始 request body 做 HMAC-SHA256，再 Base64。"""
    if not signature or not channel_secret:
        return False
    digest = hmac.new(channel_secret.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def channel_token(channel_id, channel_secret):
    """無狀態 channel access token，效期 15 分鐘；這裡快取 10 分鐘。"""
    tok, until = _token_cache.get(channel_id, ("", 0))
    if time.time() < until:
        return tok
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": channel_id, "client_secret": channel_secret}).encode()
    data = _read(urllib.request.Request(TOKEN_URL, data=body))
    if not data.get("access_token"):
        raise LineError("拿不到 channel access token")
    _token_cache[channel_id] = (data["access_token"], time.time() + 600)
    return data["access_token"]


def get_profile(token, user_id):
    """回傳顯示名稱。拿不到時回傳空字串，不擋流程。"""
    req = urllib.request.Request(PROFILE_URL + urllib.parse.quote(user_id),
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        return _read(req).get("displayName", "")
    except LineError:
        return ""


def reply(token, reply_token, text):
    body = json.dumps({"replyToken": reply_token,
                       "messages": [{"type": "text", "text": text}]}).encode()
    _read(urllib.request.Request(REPLY_URL, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"}))
