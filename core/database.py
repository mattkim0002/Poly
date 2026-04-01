"""SQLite persistence for trades, equity snapshots, and market cache."""

import json
import sqlite3
from datetime import datetime, timezone

import config
from utils.logger import log

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_question TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    outcome TEXT NOT NULL,
    entry_price REAL NOT NULL,
    size REAL NOT NULL,
    cost REAL NOT NULL,
    order_id TEXT,
    claude_probability REAL,
    market_probability REAL,
    edge REAL,
    kelly_fraction REAL,
    dd_multiplier REAL,
    signal_multiplier REAL,
    status TEXT DEFAULT 'open',
    exit_price REAL,
    pnl REAL,
    r_multiple REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    balance REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_equity REAL NOT NULL,
    drawdown REAL,
    peak_equity REAL
);

CREATE TABLE IF NOT EXISTS market_cache (
    token_id TEXT PRIMARY KEY,
    market_question TEXT,
    keywords TEXT,
    last_updated TIMESTAMP
);

CREATE TABLE IF NOT EXISTS loss_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_question TEXT,
    category TEXT,
    price_range TEXT,
    loss_pct REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.info("Database initialized at %s", config.DB_PATH)


def record_trade(
    market_id: str,
    market_question: str,
    token_id: str,
    side: str,
    outcome: str,
    entry_price: float,
    size: float,
    cost: float,
    order_id: str,
    claude_probability: float,
    market_probability: float,
    edge: float,
    kelly_frac: float,
    dd_mult: float,
    signal_mult: float,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO trades
           (market_id, market_question, token_id, side, outcome,
            entry_price, size, cost, order_id,
            claude_probability, market_probability, edge,
            kelly_fraction, dd_multiplier, signal_multiplier)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            market_id, market_question, token_id, side, outcome,
            entry_price, size, cost, order_id,
            claude_probability, market_probability, edge,
            kelly_frac, dd_mult, signal_mult,
        ),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def update_trade_result(trade_id: int, exit_price: float, pnl: float, r_multiple: float, status: str):
    conn = get_connection()
    conn.execute(
        """UPDATE trades
           SET exit_price = ?, pnl = ?, r_multiple = ?, status = ?,
               closed_at = ?
           WHERE id = ?""",
        (exit_price, pnl, r_multiple, status, datetime.now(timezone.utc).isoformat(), trade_id),
    )
    conn.commit()
    conn.close()


def get_open_trades() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_trades(n: int = 20) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status IN ('won', 'lost') ORDER BY closed_at DESC LIMIT ?",
        (n,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_equity_snapshot(balance: float, positions_value: float, total_equity: float, drawdown: float, peak_equity: float):
    conn = get_connection()
    conn.execute(
        """INSERT INTO equity_snapshots (balance, positions_value, total_equity, drawdown, peak_equity)
           VALUES (?, ?, ?, ?, ?)""",
        (balance, positions_value, total_equity, drawdown, peak_equity),
    )
    conn.commit()
    conn.close()


def get_peak_equity() -> float:
    conn = get_connection()
    row = conn.execute("SELECT MAX(peak_equity) as peak FROM equity_snapshots").fetchone()
    conn.close()
    if row and row["peak"] is not None:
        return row["peak"]
    return 0.0


def reset_peak_equity(new_peak: float):
    """Reset peak equity — caps ALL historical peaks to new value."""
    conn = get_connection()
    conn.execute("UPDATE equity_snapshots SET peak_equity = MIN(peak_equity, ?)", (new_peak,))
    conn.commit()
    conn.close()


def get_equity_history() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM equity_snapshots ORDER BY timestamp ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cache_market_keywords(token_id: str, question: str, keywords: list[str]):
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO market_cache (token_id, market_question, keywords, last_updated)
           VALUES (?, ?, ?, ?)""",
        (token_id, question, json.dumps(keywords), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_cached_keywords(token_id: str) -> list[str] | None:
    conn = get_connection()
    row = conn.execute("SELECT keywords FROM market_cache WHERE token_id = ?", (token_id,)).fetchone()
    conn.close()
    if row and row["keywords"]:
        return json.loads(row["keywords"])
    return None


def record_loss_pattern(question: str, category: str, price_range: str, loss_pct: float):
    """Record a stop-loss exit pattern for learning."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO loss_patterns (market_question, category, price_range, loss_pct)
           VALUES (?, ?, ?, ?)""",
        (question, category, price_range, loss_pct),
    )
    conn.commit()
    conn.close()


def get_loss_patterns(category: str, price_range: str, limit: int = 20) -> list[dict]:
    """Fetch recent loss patterns by category + price_range."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM loss_patterns
           WHERE category = ? AND price_range = ?
           ORDER BY created_at DESC LIMIT ?""",
        (category, price_range, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
