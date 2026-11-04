import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import asdict, replace
from decimal import Decimal

from .config import (EDITABLE_FIELDS, FINGERPRINT_EXCLUDE, NEW_STRATEGY_FIELDS, STRATEGY_NAMES,
                     Settings, coerce_setting)


def fingerprint(settings):
    return json.dumps({k: str(v) for k, v in asdict(settings).items()
                       if k not in FINGERPRINT_EXCLUDE}, sort_keys=True)


class Store:
    def __init__(self, settings):
        self.notifications_recipient = None
        self.notification_mode = "paper"
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
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY, recipient TEXT NOT NULL, kind TEXT NOT NULL,
          created_at TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
          last_error TEXT, sent_at TEXT, message_id TEXT);
        CREATE INDEX IF NOT EXISTS notification_delivery ON notifications(recipient,status,next_attempt);
        """)
        try:
            overrides = json.loads(self.get("strategy_overrides", "{}"))
            if not isinstance(overrides, dict) or not set(overrides) <= set(EDITABLE_FIELDS):
                raise ValueError("저장된 전략 설정이 올바르지 않습니다.")
            settings = replace(settings, **{k: coerce_setting(settings, k, v) for k, v in overrides.items()})
        except (ValueError, TypeError, ArithmeticError):
            self.db.close()
            raise
        self.settings = settings
        current_fingerprint = fingerprint(settings)
        old = self.get("config")
        compatible_old = old
        if old is not None:
            old_values = json.loads(old)
            for key in NEW_STRATEGY_FIELDS:
                old_values.setdefault(key,str(getattr(Settings(),key)))
            compatible_old = json.dumps(old_values,sort_keys=True)
        if old is not None and compatible_old != current_fingerprint:
            self.db.close()
            raise ValueError("기존 모의 계좌와 설정이 다릅니다. 새 TRADE_DB_PATH를 사용하세요.")
        with self.db:
            if old is not None and old != current_fingerprint:
                self.set("config",current_fingerprint)
            if old is None:
                for key, value in {"config": current_fingerprint, "cash": settings.initial_cash,
                                   "btc": "0", "cost": "0", "realized": "0",
                                   "buy_halted": "0", "price": "0"}.items():
                    self.set(key, value)

    def save_strategy(self, settings, timestamp):
        before = {k: str(getattr(self.settings, k)) for k in EDITABLE_FIELDS}
        after = {k: str(getattr(settings, k)) for k in EDITABLE_FIELDS}
        labels = {"short_window": "단기 이동평균", "long_window": "장기 이동평균",
                  "order_krw": "주문 금액", "max_exposure": "최대 BTC 비중", "daily_loss_limit": "일일 손실 한도",
                  "strategy":"전략", "dca_interval_hours":"분할매수 주기(시간)",
                  "target_exposure":"목표 BTC 비중", "rebalance_tolerance":"허용 편차",
                  "decision_gate_enabled":"디시젼 게이트", "decision_threshold_buy":"매수 게이트 임계값",
                  "decision_threshold_sell":"매도 게이트 임계값", "decision_timeout_seconds":"게이트 제한시간(초)",
                  "openrouter_api_key":"OpenRouter API 키"}
        percent_keys = ("max_exposure", "daily_loss_limit", "target_exposure", "rebalance_tolerance",
                        "decision_threshold_buy", "decision_threshold_sell")
        def display(key, value):
            if key == "strategy":
                return STRATEGY_NAMES.get(value, value)
            if key == "decision_gate_enabled":
                return "사용" if coerce_setting(settings, key, value) else "미사용"
            if key == "openrouter_api_key":
                return "설정됨" if value.strip() else "미설정"
            if key in percent_keys:
                return str(Decimal(value) * 100) + "%"
            return value

        def changed(key):
            if key in ("strategy", "openrouter_api_key", "decision_gate_enabled"):
                return before[key] != after[key]
            if key == "decision_timeout_seconds":
                return Decimal(before[key]) != Decimal(after[key])
            return Decimal(before[key]) != Decimal(after[key])

        changes = ["{} {} → {}".format(labels[k], display(k, before[k]), display(k, after[k]))
                   for k in EDITABLE_FIELDS if changed(k)]
        # Persist key in overrides for restart; never write the secret into event text.
        with self.db:
            self.set("config", fingerprint(settings))
            self.set("strategy_overrides", json.dumps(after, sort_keys=True))
            self.event(timestamp, "settings", "전략 설정 변경: " + (" / ".join(changes) if changes else "변경 없음"))
        self.settings = settings

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
        if kind == "risk":
            self.notify(time, "risk", "[BTC 모의매매 · 위험 관리]\n"+message)

    def notify(self, timestamp, kind, message):
        if not self.notifications_recipient:
            return None
        if self.notification_mode == "live":message=message.replace("[BTC 모의매매","[BTC 실거래")
        display_time = datetime.fromisoformat(timestamp).astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST")
        cursor = self.db.execute("INSERT INTO notifications(recipient,kind,created_at,message,next_attempt) VALUES (?,?,?,?,?)",
                                 (self.notifications_recipient,kind,timestamp,message+"\n시각: "+display_time,time.time()))
        return cursor.lastrowid

    def recent(self, table, limit=100):
        if table not in ("decisions", "fills", "events", "equity"):
            raise ValueError("Unknown table")
        return [dict(r) for r in self.db.execute("SELECT * FROM {} ORDER BY rowid DESC LIMIT ?".format(table), (limit,))]
