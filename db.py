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
    at      TEXT NOT NULL
);

-- reward: gummy（報到軟糖）/ bonus（答題加碼）/ crampeace / onsite（現場優惠已使用）
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
