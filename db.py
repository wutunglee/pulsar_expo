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
    action    TEXT NOT NULL,              -- checkin / sale / friend
    user_id   TEXT REFERENCES users(id) ON DELETE CASCADE,  -- 散客結帳時是 NULL
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

-- reward: gummy（報到軟糖）/ bonus（答題加碼）/ crampeace / prize（抽到的贈品）
CREATE TABLE IF NOT EXISTS claims (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward  TEXT NOT NULL,
    day     TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (user_id, reward)
);
CREATE INDEX IF NOT EXISTS idx_claims_day ON claims(reward, day);

-- 現場販售的商品。價格、上下架在後台改。
CREATE TABLE IF NOT EXISTS products (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    cat    TEXT NOT NULL DEFAULT '',
    tag    TEXT NOT NULL DEFAULT '',      -- gummy / flask / cramp / noxx：抽獎特價對應的類別
    price  INTEGER NOT NULL,              -- 官網價
    onsite INTEGER,                       -- 有券的人的現場價（設了就不再打折）
    multi  INTEGER,                       -- 沒有券的人買 2 件以上的單價
    active INTEGER NOT NULL DEFAULT 1,
    sort   INTEGER NOT NULL DEFAULT 0
);

-- 現場結帳：一筆一列，明細在 sale_items。取消的保留、標 voided_at。
CREATE TABLE IF NOT EXISTS sales (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    day        TEXT NOT NULL,
    staff      TEXT NOT NULL,
    user_id    TEXT REFERENCES users(id) ON DELETE SET NULL,   -- 散客是 NULL
    prize      TEXT,                      -- 結帳當下套用的獎
    pay        TEXT NOT NULL,             -- cash / linepay
    total      INTEGER NOT NULL,
    list_total INTEGER NOT NULL,          -- 官網價合計，用來看讓利多少
    voided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_user ON sales(user_id);
CREATE TABLE IF NOT EXISTS sale_items (
    sale_id     INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    unit_list   INTEGER NOT NULL,
    amount      INTEGER NOT NULL,         -- 這一列實收
    special_qty INTEGER NOT NULL DEFAULT 0,  -- 其中用抽獎特價的件數（計入每人上限）
    how         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);

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


def migrate(conn):
    """舊資料庫升級。staff_log.user_id 原本不能是空的，散客結帳需要它可以空。"""
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(staff_log)")}
    if cols["user_id"]["notnull"]:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
            BEGIN;
            ALTER TABLE staff_log RENAME TO staff_log_old;
            CREATE TABLE staff_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                at        TEXT NOT NULL,
                staff     TEXT NOT NULL,
                action    TEXT NOT NULL,
                user_id   TEXT REFERENCES users(id) ON DELETE CASCADE,
                detail    TEXT NOT NULL DEFAULT '{}',
                undone_at TEXT
            );
            INSERT INTO staff_log SELECT id, at, staff, action, user_id, detail, undone_at
                FROM staff_log_old;
            DROP TABLE staff_log_old;
            CREATE INDEX IF NOT EXISTS idx_staff_log_user ON staff_log(user_id);
            COMMIT;
        """)
        conn.execute("PRAGMA foreign_keys=ON")


def seed_products(conn, products):
    """商品表是空的才寫入預設清單；之後以資料庫為準。"""
    if conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        return
    for i, p in enumerate(products):
        conn.execute(
            "INSERT INTO products(id, name, cat, tag, price, onsite, multi, sort) VALUES(?,?,?,?,?,?,?,?)",
            (p["id"], p["name"], p.get("cat", ""), p.get("tag", ""), p["price"],
             p.get("onsite"), p.get("multi"), i))


def init(path, products=()):
    conn = connect(path)
    conn.executescript(SCHEMA)
    migrate(conn)
    seed_products(conn, products)
    conn.close()
