import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from trade.app import create_app
from trade.config import Settings
from trade.market import UTC
from test_trading import snapshot


class FakeMarket:
    def fetch(self, minutes):
        return snapshot(now=datetime.now(UTC))


class ApiTests(unittest.TestCase):
    def test_controls_status_static_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp)/"db.sqlite3", candle_minutes=60)
            app = create_app(settings, FakeMarket())
            with TestClient(app) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/static/app.js").status_code, 200)
                for _ in range(100):
                    data = client.get("/api/status").json()
                    if data["quote_fresh"]:
                        break
                    time.sleep(.01)
                self.assertEqual(data["mode"], "paper")
                self.assertFalse(data["running"])
                self.assertEqual(client.post("/api/control/start", json={}).status_code, 200)
                self.assertTrue(client.get("/api/status").json()["running"])
                self.assertTrue(client.post("/api/control/pause_buys", json={}).json()["buy_halted"])
                self.assertEqual(client.post("/api/control/invalid", json={}).status_code, 409)
                self.assertFalse(client.post("/api/control/stop", json={}).json()["running"])
                self.assertEqual(client.post("/api/control/start").status_code, 415)
                self.assertEqual(client.post("/api/control/start", json={}, headers={"Origin":"https://example.com"}).status_code, 403)
                self.assertEqual(client.get("/api/status", headers={"Host":"attacker.example"}).status_code, 403)
            with TestClient(create_app(settings, FakeMarket())) as client:
                data = client.get("/api/status").json()
                self.assertFalse(data["running"])
                self.assertTrue(data["buy_halted"])

    def test_second_worker_cannot_open_same_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(db_path=Path(tmp)/"db.sqlite3", candle_minutes=60)
            with TestClient(create_app(settings, FakeMarket())):
                with self.assertRaises(RuntimeError):
                    with TestClient(create_app(settings, FakeMarket())):
                        pass
