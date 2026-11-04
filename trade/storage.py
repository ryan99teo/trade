import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal


class Store:
    def __init__(self, settings):
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(settings.db_path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candles (time TEXT PRIMARY KEY, close TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY, time TEXT NOT NULL, candle TEXT UNIQUE NOT NULL,
          action TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills (
          id INTEGER PRIMARY KEY, time TEXT NOT NULL, identifier TEXT UNIQUE NOT NULL,
          side TEXT NOT NULL, price TEXT NOT NULL, quantity TEXT NOT NULL,
          fee TEXT NOT NULL, realized_pnl TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS equity (
          time TEXT PRIMARY KEY, value TEXT NOT NULL, price TEXT NOT NULL);
        """)
        fingerprint = json.dumps({k: str(v) for k, v in asdict(settings).items()
                                  if k not in ("db_path", "poll_seconds", "max_quote_age_seconds")}, sort_keys=True)
        old = self.get("config")
        if old is not None and old != fingerprint:
            self.db.close()
            raise ValueError("기존 모의 계좌와 설정이 다릅니다. 새 TRADE_DB_PATH를 사용하세요.")
        with self.db:
            if old is None:
                for key, value in {"config": fingerprint, "cash": settings.initial_cash,
                                   "btc": "0", "cost": "0", "realized": "0",
                                   "buy_halted": "0", "price": "0"}.items():
                    self.set(key, value)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def number(self, key):
        return Decimal(self.get(key, "0"))

    def set(self, key, value):
        self.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def event(self, time, kind, message):
        self.db.execute("INSERT INTO events(time,kind,message) VALUES (?,?,?)", (time, kind, message))

    def recent(self, table, limit=100):
        if table not in ("decisions", "fills", "events", "equity"):
            raise ValueError("Unknown table")
        return [dict(r) for r in self.db.execute("SELECT * FROM {} ORDER BY rowid DESC LIMIT ?".format(table), (limit,))]
