"""跟 LINE 平台的兩個呼叫，集中在這個檔案。

注意：這裡的端點與欄位是依 LINE Login v2.1 的做法寫的，尚未對著正式頻道實測。
接上 LIFF 時請對照 LINE Developers 官方文件確認：
  - 驗證 ID token
  - 取得與官方帳號的好友狀態（需在 LINE Login 頻道連結官方帳號）
"""
import json
import urllib.error
import urllib.parse
import urllib.request

VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
FRIENDSHIP_URL = "https://api.line.me/friendship/v1/status"
TIMEOUT = 8


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
