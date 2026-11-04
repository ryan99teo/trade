from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from trade.app import create_app
from trade.config import Settings
from trade.engine import Engine
from trade.market import UTC, UpbitPublic
from trade.storage import Store
from test_api import FakeMarket
from test_trading import NOW, snapshot

D = Decimal


def values(**kwargs):
    result = {"short_window": 3, "long_window": 10, "order_krw": "50000",
              "max_exposure": "0.20", "daily_loss_limit": "0.02"}
    result.update(kwargs)
    return result


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Settings(db_path=Path(self.temp.name)/"paper.sqlite3",short_window=2,long_window=3)
        self.store = Store(self.base)
        self.engine = Engine(self.base,self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def change(self, **kwargs):
        data = values(**kwargs)
        for key in ("order_krw","max_exposure","daily_loss_limit"):
            data[key] = D(data[key])
        self.engine.configure(data,NOW)

    def test_saved_settings_survive_restart_without_changing_account(self):
        self.engine.process(snapshot(),NOW)
        self.engine.control("start",NOW)
        self.engine.process(snapshot(),NOW)
        self.engine.control("stop",NOW)
        self.engine.control("pause_buys",NOW)
        before = {key:self.store.get(key) for key in ("cash","btc","cost","realized","day_baseline","buy_halted")}
        fills=self.store.recent("fills")
        self.change()
        self.store.db.close()
        self.store=Store(self.base)
        self.engine=Engine(self.base,self.store)
        self.assertEqual(self.engine.settings.short_window,3)
        self.assertEqual(self.engine.settings.long_window,10)
        self.assertEqual(self.engine.settings.order_krw,D("50000"))
        self.assertFalse(self.engine.running)
        self.assertEqual({key:self.store.get(key) for key in before},before)
        self.assertEqual(self.store.recent("fills"),fills)
        with self.assertRaises(ValueError):Store(replace(self.base,initial_cash=D("2000000")))

    def test_running_or_invalid_changes_do_not_mutate_config(self):
        self.engine.process(snapshot(),NOW)
        self.engine.control("start",NOW)
        with self.assertRaises(RuntimeError):self.change()
        self.engine.control("stop",NOW)
        before=self.store.get("config")
        with self.assertRaises(ValueError):self.change(short_window=20,long_window=10)
        with self.assertRaises(ValueError):self.change(order_krw="2000000")
        self.assertEqual(self.store.get("config"),before)
        self.assertEqual(self.engine.settings,self.base)

    def test_transaction_failure_keeps_memory_and_database_unchanged(self):
        before=self.store.get("config")
        with patch.object(self.store,"event",side_effect=RuntimeError("simulated disk failure")):
            with self.assertRaises(RuntimeError):self.change()
        self.assertEqual(self.store.get("config"),before)
        self.assertIsNone(self.store.get("strategy_overrides"))
        self.assertEqual(self.engine.settings,self.base)
        self.assertEqual(self.store.settings,self.base)

    def test_settings_change_does_not_replay_already_processed_candle(self):
        self.engine.process(snapshot(),NOW)
        self.engine.control("start",NOW)
        self.engine.process(snapshot(),NOW)
        self.engine.control("stop",NOW)
        self.change(short_window=1,long_window=3)
        count=len(self.store.recent("fills"))
        self.engine.control("start",NOW)
        self.engine.process(snapshot(),NOW)
        self.assertEqual(len(self.store.recent("fills")),count)

    def test_tighter_exposure_never_forces_sale_and_blocks_new_buy(self):
        self.engine.process(snapshot(),NOW)
        self.engine.control("start",NOW)
        self.engine.process(snapshot(),NOW)
        self.engine.control("stop",NOW)
        btc=self.store.number("btc")
        self.change(max_exposure="0.01")
        with self.store.db:self.engine._execute("buy",D("100000000"),"new",NOW)
        self.assertEqual(self.store.number("btc"),btc)
        self.assertEqual(len(self.store.recent("fills")),1)

    def test_api_validation_lock_and_restart(self):
        with TestClient(create_app(self.base,FakeMarket())) as client:
            for _ in range(100):
                if client.get("/api/status").json()["quote_fresh"]:break
                time.sleep(.01)
            self.assertEqual(client.post("/api/control/start",json={}).status_code,200)
            self.assertEqual(client.post("/api/settings",json=values()).status_code,409)
            client.post("/api/control/stop",json={})
            for bad in (values(short_window=10,long_window=3),values(order_krw="NaN"),values(initial_cash="2000000"),values(short_window=1.5),values(max_exposure="1.1")):
                self.assertEqual(client.post("/api/settings",json=bad).status_code,422)
            response=client.post("/api/settings",json=values())
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["settings"]["order_krw"],"50000")
            self.assertFalse(response.json()["running"])
        with TestClient(create_app(self.base,FakeMarket())) as client:
            self.assertEqual(client.get("/api/status").json()["settings"]["long_window"],10)

    def test_market_requests_200_completed_bars_for_maximum_ma_window(self):
        client=UpbitPublic()
        epoch=int(datetime.now(UTC).timestamp()*1000)
        with patch.object(client,"_get",side_effect=[[],[{"trade_price":"100000000","timestamp":epoch}]] ) as fetch:
            client.fetch(60)
        query=parse_qs(urlsplit(fetch.call_args_list[0].args[0]).query)
        boundary=datetime.fromisoformat(query["to"][0])
        self.assertEqual(query["count"],["200"])
        self.assertEqual(boundary.minute,0)
        self.assertEqual(boundary.second,0)
        self.assertLessEqual(boundary,datetime.now(UTC))
