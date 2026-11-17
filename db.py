"""SQLite 連線與資料表。資料量小（數千筆），單檔即可。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,          -- LINE userId（dev 模式為 dev:名稱）
    name        TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL UNIQUE,      -- 出示給攤位人員的 5 碼
    is_friend   INTEGER NOT NULL DEFAULT 0,
    is_new      INTEGER NOT NULL DEFAULT 0, -- 活動期間才加入的好友（揪朋友加碼只算新好友）
    distance    TEXT,                      -- half / full / cheer
    invited_by  TEXT REFERENCES users(id),
    arrived_at  TEXT,                      -- 攤位人員報到的時間
    plan_sent_at TEXT,                     -- 補給時間表已傳到他的 LINE
    consent_at  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by);

-- 真的假的：每人固定 3 題，記錄他的答案
CREATE TABLE IF NOT EXISTS quiz (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    q       INTEGER NOT NULL,
    answer  INTEGER NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (user_id, q)
);

CREATE TABLE IF NOT EXISTS questions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text    TEXT NOT NULL,
    at      TEXT NOT NULL,
    answered_at TEXT                      -- 已在 LINE 回覆的時間（後台勾選）
);

-- 攤位人員的操作紀錄：誰、什麼時候、對誰、做了什麼。用來取消按錯的操作，也方便事後查。
CREATE TABLE IF NOT EXISTS staff_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    staff     TEXT NOT NULL,
    action    TEXT NOT NULL,              -- checkin / onsite / friend
    user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    detail    TEXT NOT NULL DEFAULT '{}', -- JSON：這次實際改了什麼，取消時照這個還原
    undone_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_log_user ON staff_log(user_id);

-- 現場優惠抽獎：主機先洗好一副牌存起來（hand），客人再選位置（pick）。每人一次。
CREATE TABLE IF NOT EXISTS draws (
    user_id  TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    hand     TEXT NOT NULL,              -- 逗號分隔的獎項代號，順序就是牌的位置
    dealt_at TEXT NOT NULL,
    pick     INTEGER,                    -- 客人選的位置（0 起算），還沒選是 NULL
    prize    TEXT,
    at       TEXT
);

-- 現場優惠券的使用紀錄：不限次數，只記錄
CREATE TABLE IF NOT EXISTS onsite_uses (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,
    at      TEXT NOT NULL
);

-- reward: gummy（報到軟糖）/ bonus（答題加碼）/ crampeace
CREATE TABLE IF NOT EXISTS claims (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward  TEXT NOT NULL,
    day     TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (user_id, reward)
);
CREATE INDEX IF NOT EXISTS idx_claims_day ON claims(reward, day);

"""


def connect(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    # isolation_level=None：自己控制交易，領獎時用 BEGIN IMMEDIATE 鎖住份數計算
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()
