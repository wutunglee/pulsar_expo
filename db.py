"""SQLite 連線與資料表。資料量小（數千筆），單檔即可。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,          -- LINE userId（dev 模式為 dev:名稱）
    name        TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL UNIQUE,      -- 出示給攤位人員的 5 碼
    is_friend   INTEGER NOT NULL DEFAULT 0,
    distance    TEXT,                      -- half / full / cheer
    invited_by  TEXT REFERENCES users(id),
    consent_at  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_invited_by ON users(invited_by);

CREATE TABLE IF NOT EXISTS completions (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    station INTEGER NOT NULL,
    at      TEXT NOT NULL,
    meta    TEXT,
    PRIMARY KEY (user_id, station)
);

CREATE TABLE IF NOT EXISTS questions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text    TEXT NOT NULL,
    at      TEXT NOT NULL
);

-- reward: gummy / crampeace / fallback / coupon5
CREATE TABLE IF NOT EXISTS claims (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward  TEXT NOT NULL,
    day     TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (user_id, reward)
);
CREATE INDEX IF NOT EXISTS idx_claims_day ON claims(reward, day);

CREATE TABLE IF NOT EXISTS code_attempts (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
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
