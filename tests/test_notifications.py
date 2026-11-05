import asyncio
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from trade.app import create_app
from trade.config import Settings
from trade.engine import Engine
from trade.notifications import DeliveryError, Telegram, deliver_one, status
from trade.storage import Store
from test_api import FakeMarket
from test_trading import NOW, snapshot


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=Settings(db_path=Path(self.temp.name)/"paper.sqlite3",short_window=2,long_window=3,candle_minutes=60)
        self.store=Store(self.settings)
        self.telegram=Telegram("private-token","private-chat")
        self.store.notifications_recipient=self.telegram.recipient
        self.engine=Engine(self.settings,self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def enqueue(self):
        with self.store.db:return self.store.notify(NOW.isoformat(),"test","test")

    def rows(self):
        return self.store.db.execute("SELECT * FROM notifications ORDER BY id").fetchall()

    def test_fill_and_outbox_commit_together_without_duplicate_signal(self):
        self.engine.process(snapshot(),NOW)
        self.engine.control("start",NOW)
        self.engine.process(snapshot(),NOW)
        self.engine.process(snapshot(),NOW)
        self.assertEqual(len(self.rows()),1)
        self.assertIn("매수 체결",self.rows()[0]["message"])
        self.assertIn("KST",self.rows()[0]["message"])
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.engine._execute("buy",snapshot().price,"rollback",NOW)
                raise RuntimeError("crash")
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(len(self.store.recent("fills")),1)

    def test_repeated_error_is_suppressed_and_recovery_is_reported(self):
        self.engine.fail("network",NOW)
        self.engine.fail("network",NOW)
        self.assertEqual(len(self.rows()),1)
        self.engine.process(snapshot(),NOW)
        self.assertEqual([r["kind"] for r in self.rows()],["error","recovery"])
        self.assertFalse(self.engine.running)
        self.engine.fail("network",NOW)
        self.assertEqual(len(self.rows()),3)

    def test_risk_notification_is_queued_once(self):
        self.engine.process(snapshot(),NOW)
        self.engine.control("start",NOW)
        self.engine.process(snapshot(),NOW)
        later=NOW+timedelta(hours=1)
        shot=snapshot((100,100,100,100),later,"50000000")
        self.engine.process(shot,later)
        self.engine.process(shot,later)
        self.assertEqual(len([r for r in self.rows() if r["kind"]=="risk"]),1)

    def test_transient_failure_retries_then_sends_and_never_resends_success(self):
        self.enqueue();now=time.time()+1
        with patch.object(self.telegram,"send",side_effect=[DeliveryError("network"),123]) as send:
            asyncio.run(deliver_one(self.store,self.telegram,now))
            self.assertEqual(self.rows()[0]["status"],"pending")
            self.assertFalse(asyncio.run(deliver_one(self.store,self.telegram,now+1)))
            asyncio.run(deliver_one(self.store,self.telegram,now+31))
            self.assertEqual(self.rows()[0]["status"],"sent")
            self.assertEqual(self.rows()[0]["attempts"],2)
            self.assertFalse(asyncio.run(deliver_one(self.store,self.telegram,now+100)))
            self.assertEqual(send.call_count,2)

    def test_rate_limit_permanent_errors_and_retry_cap(self):
        self.enqueue();now=time.time()+1
        with patch.object(self.telegram,"send",side_effect=DeliveryError("rate",retry_after=120)):
            asyncio.run(deliver_one(self.store,self.telegram,now))
        self.assertGreaterEqual(self.rows()[0]["next_attempt"],now+120)
        with patch.object(self.telegram,"send",side_effect=DeliveryError("unauthorized",permanent=True)):
            asyncio.run(deliver_one(self.store,self.telegram,now+121))
        self.assertEqual(self.rows()[0]["status"],"failed")
        self.enqueue()
        with patch.object(self.telegram,"send",side_effect=DeliveryError("network")):
            for i in range(5):asyncio.run(deliver_one(self.store,self.telegram,now+i*1000))
        self.assertEqual(self.rows()[-1]["status"],"failed")
        self.assertEqual(self.rows()[-1]["attempts"],5)

    def test_queue_survives_restart_and_is_bound_to_original_recipient(self):
        self.enqueue()
        self.store.db.close();self.store=Store(self.settings)
        other=Telegram("other-token","other-chat")
        with patch.object(other,"send") as send:
            self.assertFalse(asyncio.run(deliver_one(self.store,other,time.time()+1)))
            send.assert_not_called()
        with patch.object(self.telegram,"send",return_value=123):
            asyncio.run(deliver_one(self.store,self.telegram,time.time()+1))
        self.assertEqual(self.rows()[0]["status"],"sent")

    def test_disabled_store_does_not_queue_backtest_or_historical_alerts(self):
        self.store.notifications_recipient=None
        self.engine.fail("network",NOW)
        self.assertFalse(self.rows())
        with patch.object(self.telegram,"send") as send:
            self.assertFalse(asyncio.run(deliver_one(self.store,Telegram(),time.time())))
            send.assert_not_called()

    def test_transport_posts_plain_text_and_sanitizes_token_errors(self):
        response=io.BytesIO(b'{"ok":true,"result":{"message_id":123}}')
        with patch("trade.notifications.urlopen",return_value=response) as open_url:
            self.assertEqual(self.telegram.send("test"),123)
            payload=json.loads(open_url.call_args.args[0].data)
            self.assertEqual(payload["text"],"test")
            self.assertNotIn("parse_mode",payload)
        for error in (URLError("private-token"),HTTPError("private-token",403,"private-token",{},io.BytesIO(b'{}'))):
            with patch("trade.notifications.urlopen",side_effect=error):
                with self.assertRaises(DeliveryError) as raised:self.telegram.send("test")
            self.assertNotIn("private-token",str(raised.exception))
        error=HTTPError("private-token",429,"rate",{},io.BytesIO(b'{"parameters":{"retry_after":130}}'))
        with patch("trade.notifications.urlopen",side_effect=error):
            with self.assertRaises(DeliveryError) as raised:self.telegram.send("test")
        self.assertEqual(raised.exception.retry_after,130)
        self.assertFalse(raised.exception.permanent)

    def test_api_test_enqueue_throttle_and_no_credentials_in_status(self):
        with patch.object(self.telegram,"send",return_value=321):
            with TestClient(create_app(self.settings,FakeMarket(),telegram=self.telegram)) as client:
                response=client.post("/api/notifications/test",json={})
                self.assertEqual(response.status_code,200)
                self.assertEqual(client.post("/api/notifications/test",json={}).status_code,429)
                data=client.get("/api/notifications")
                self.assertTrue(data.json()["enabled"])
                self.assertNotIn("private-token",data.text)
                self.assertNotIn("private-chat",data.text)
        with TestClient(create_app(self.settings,FakeMarket(),telegram=Telegram())) as client:
            self.assertFalse(client.get("/api/notifications").json()["enabled"])
            self.assertEqual(client.post("/api/notifications/test",json={}).status_code,409)

    def test_ui_settings_persist_without_exposing_secrets(self):
        telegram=Telegram()
        with TestClient(create_app(self.settings,FakeMarket(),telegram=telegram)) as client:
            missing=client.post("/api/notifications/settings",json={"enabled":True})
            self.assertEqual(missing.status_code,409)
            saved=client.post("/api/notifications/settings",json={
                "enabled":True,"bot_token":"ui-bot-token","chat_id":"ui-chat-id"})
            self.assertEqual(saved.status_code,200,saved.text)
            body=saved.json()
            self.assertTrue(body["enabled"])
            self.assertTrue(body["keys_set"])
            self.assertNotIn("ui-bot-token",saved.text)
            self.assertNotIn("ui-chat-id",saved.text)
            kept=client.post("/api/notifications/settings",json={"enabled":True})
            self.assertEqual(kept.status_code,200)
            self.assertTrue(telegram.enabled)
            self.assertEqual(telegram._token,"ui-bot-token")
            cleared=client.post("/api/notifications/settings",json={"enabled":False})
            self.assertEqual(cleared.status_code,200)
            self.assertFalse(cleared.json()["enabled"])
            self.assertFalse(telegram.enabled)
