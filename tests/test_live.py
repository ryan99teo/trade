import asyncio
import base64
from datetime import datetime,timedelta
from decimal import Decimal
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch,MagicMock
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode,unquote

from fastapi.testclient import TestClient

from trade.app import create_app
from trade.config import Settings
from trade.exchange import UpbitExchange,ExchangeError,token
from trade.live import LiveService
from trade.market import UTC
from test_api import FakeMarket

D=Decimal


class FakeExchange:
    configured=True
    enabled=True
    binding="fake-account-binding"
    def __init__(self):
        self.placed=[];self.tested=[];self.cancelled=[];self.responses={}
        self.post_error=None;self.get_error=None
        self.krw="1000000";self.btc="1"
    def chance(self):
        return {"bid_fee":"0.0005","ask_fee":"0.0005", "bid_account":{"currency":"KRW","balance":self.krw,"locked":"0"},
                "ask_account":{"currency":"BTC","balance":self.btc,"locked":"0"},
                "market":{"id":"KRW-BTC","state":"active","bid_types":["limit"],"ask_types":["limit"],
                          "bid":{"min_total":"5000"},"ask":{"min_total":"5000"},"max_total":"1000000000"}}
    def instrument(self):return [{"market":"KRW-BTC","tick_size":"1000"}]
    def test(self,params):self.tested.append(params);return {"uuid":"test-only"}
    def place(self,params):
        self.placed.append(params)
        if self.post_error:raise self.post_error
        return {"uuid":"exchange-order"}
    def order(self,identifier):
        if self.get_error:raise self.get_error
        return self.responses[identifier]
    def cancel(self,identifier):self.cancelled.append(identifier);return {}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Settings(db_path=Path(self.temp.name)/"paper.sqlite3")
        self.client=FakeExchange();self.live=LiveService(self.base,self.client)
        self.now=datetime.now(UTC)
        self.live.engine.quote_time=self.now
        with self.live.store.db:self.live.store.set("price","100000000")

    def tearDown(self):self.live.close();self.temp.cleanup()

    def prepare(self,side="buy",identifier=None):
        self.live.engine.running=True
        candle=(datetime.now(UTC)-timedelta(hours=1)).isoformat() if identifier is None else identifier
        with self.live.store.db:self.live.engine._execute(side,D("100000000"),candle,datetime.now(UTC))
        row=self.live.active()[0]
        params=self.live.plan(side,D("100000000"),row["identifier"])
        return row["identifier"],params

    def ledger_order(self,side="buy",identifier=None):
        asyncio.run(self.live.connect())
        identifier,params=self.prepare(side,identifier)
        with self.live.store.db:self.live.store.db.execute("UPDATE live_orders SET state='submitting',params=? WHERE identifier=?",(json.dumps(params),identifier))
        return identifier,params

    def response(self,identifier,params,quantity="0.00005",price="100000000",fee="2.5",state="wait"):
        q=D(quantity)
        return {"market":"KRW-BTC","side":params["side"],"uuid":"order-"+identifier,"identifier":identifier,
                "state":state,"executed_volume":quantity,"paid_fee":fee,
                "trades":[{"uuid":"trade-"+identifier,"volume":quantity,"price":price,"funds":str(q*D(price))}] if q else []}

    def test_partial_fills_and_terminal_cancel_are_applied_exactly_once(self):
        identifier,params=self.ledger_order()
        first=self.response(identifier,params)
        self.live.reconcile(identifier,first);self.live.reconcile(identifier,first)
        self.assertEqual(self.live.store.number("btc"),D("0.00005"))
        self.assertEqual(self.live.store.number("cash"),D("94997.5"))
        last=self.response(identifier,params,quantity="0.00009",fee="4.5",state="cancel")
        self.live.reconcile(identifier,last);self.live.reconcile(identifier,last)
        self.assertEqual(self.live.store.number("btc"),D("0.00009"))
        self.assertEqual(self.live.store.number("cash"),D("90995.5"))
        self.assertEqual(self.live.store.number("cost"),D("9004.5"))
        self.assertEqual(len(self.live.store.recent("fills")),2)
        self.assertFalse(self.live.active())

    def test_partial_sell_allocates_actual_cost_and_fees(self):
        identifier,params=self.ledger_order()
        self.live.reconcile(identifier,self.response(identifier,params,quantity="0.00009",fee="4.5",state="done"))
        self.live.store.set("price","200000000");self.live.store.db.commit()
        sell=self.live.plan("sell",D("200000000"),"sell-id")
        now=datetime.now(UTC).isoformat()
        with self.live.store.db:self.live.store.db.execute("INSERT INTO live_orders(identifier,candle,side,created_at,state,mark,params) VALUES (?,?,?,?,?,?,?)",
                                                          ("sell-id",now,"sell",now,"submitting","200000000",json.dumps(sell)))
        response=self.response("sell-id",sell,quantity="0.00004",price="200000000",fee="4",state="cancel")
        self.live.reconcile("sell-id",response)
        self.assertEqual(self.live.store.number("btc"),D("0.00005"))
        self.assertEqual(self.live.store.number("cost"),D("5002.5"))
        self.assertEqual(self.live.store.number("realized"),D("3994"))
        self.assertEqual(self.live.store.number("cash"),D("98991.5"))

    def test_timeout_is_unknown_and_never_reposted_even_after_restart(self):
        asyncio.run(self.live.connect());identifier,params=self.prepare()
        self.client.post_error=ExchangeError("timeout")
        asyncio.run(self.live.sync())
        self.assertEqual(len(self.client.placed),1)
        self.assertEqual(self.live.active()[0]["state"],"unknown")
        self.assertFalse(self.live.engine.running)
        self.live.close();self.live=LiveService(self.base,self.client)
        self.client.get_error=ExchangeError("not found","order_not_found",True)
        asyncio.run(self.live.sync());asyncio.run(self.live.sync())
        self.assertEqual(len(self.client.placed),1)
        self.assertEqual(self.live.active()[0]["state"],"unknown")

    def test_post_success_then_get_401_must_remain_unknown(self):
        asyncio.run(self.live.connect());self.prepare()
        self.client.get_error=ExchangeError("auth","jwt_verification",True)
        asyncio.run(self.live.sync())
        self.assertEqual(self.live.active()[0]["state"],"unknown")
        self.assertEqual(len(self.client.placed),1)

    def test_prepared_intent_is_aborted_on_restart_and_stop(self):
        asyncio.run(self.live.connect());self.prepare()
        self.live.close();self.live=LiveService(self.base,self.client)
        asyncio.run(self.live.sync())
        self.assertFalse(self.live.active());self.assertFalse(self.client.placed)
        self.prepare(identifier=datetime.now(UTC).isoformat());self.live.stop()
        self.assertFalse(self.live.active());self.assertFalse(self.client.placed)

    def test_successful_order_requires_canonical_get_and_recovers_after_restart(self):
        asyncio.run(self.live.connect());identifier,params=self.prepare()
        self.client.responses[identifier]=self.response(identifier,params,state="done")
        asyncio.run(self.live.sync())
        self.assertFalse(self.live.active());self.assertEqual(len(self.client.placed),1)
        btc=self.live.store.number("btc")
        self.live.close();self.live=LiveService(self.base,self.client)
        asyncio.run(self.live.sync())
        self.assertEqual(self.live.store.number("btc"),btc)
        self.assertFalse(self.live.engine.running);self.assertEqual(len(self.client.placed),1)

    def test_external_holdings_are_not_adopted_and_cash_shortage_blocks(self):
        asyncio.run(self.live.connect())
        self.assertEqual(self.live.store.number("btc"),0)
        with self.assertRaises(ValueError):self.live.plan("sell",D("100000000"),"external")
        self.client.krw="1"
        with self.assertRaises(ValueError):asyncio.run(self.live.connect())

    def test_disabled_real_trading_and_dry_run_do_not_place_orders(self):
        self.client.enabled=False
        result=asyncio.run(self.live.test_order())
        self.assertIn("실제 주문",result["message"])
        self.assertEqual(len(self.client.tested),1);self.assertFalse(self.client.placed)
        self.assertFalse(self.live.store.recent("fills"))
        with self.assertRaises(ValueError):self.live.start()

    def test_binding_change_refuses_another_account(self):
        asyncio.run(self.live.connect());self.live.close();self.client.binding="another-key"
        self.live=LiveService(self.base,self.client)
        with self.assertRaises(ValueError):asyncio.run(self.live.connect())
        self.assertFalse(self.client.placed)

    def test_bad_fill_response_and_transaction_failure_cannot_corrupt_ledger(self):
        identifier,params=self.ledger_order()
        response=self.response(identifier,params)
        bad=dict(response,market="KRW-ETH")
        with self.assertRaises(ValueError):self.live.reconcile(identifier,bad)
        bad=dict(response,executed_volume="0.1")
        with self.assertRaises(ValueError):self.live.reconcile(identifier,bad)
        self.assertEqual(self.live.store.number("cash"),100000)
        with patch.object(self.live.store,"notify",side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):self.live.reconcile(identifier,response)
        self.assertEqual(self.live.store.number("cash"),100000)
        self.assertFalse(self.live.store.recent("fills"))
        self.live.reconcile(identifier,response)
        self.assertEqual(self.live.store.number("btc"),D("0.00005"))

    def test_cancel_unknown_stays_pending_until_canonical_confirmation(self):
        identifier,params=self.ledger_order()
        self.client.responses[identifier]=self.response(identifier,params,quantity="0",fee="0",state="cancel")
        asyncio.run(self.live.cancel(identifier))
        self.assertEqual(self.client.cancelled,[identifier]);self.assertFalse(self.live.active())
        with self.assertRaises(ValueError):asyncio.run(self.live.cancel("someone-elses-order"))

    def test_api_default_disabled_separate_budget_and_explicit_ack(self):
        self.live.close()
        self.client.enabled=False
        with TestClient(create_app(self.base,FakeMarket(),exchange=self.client)) as client:
            for _ in range(100):
                state=client.get("/api/live/status").json()
                if state["quote_fresh"]:break
                time.sleep(.01)
            self.assertEqual(state["mode"],"live")
            self.assertEqual(state["cash"],"100000")
            self.assertEqual(client.get("/api/status").json()["cash"],"1000000")
            self.assertEqual(client.post("/api/live/start",json={}).status_code,422)
            self.assertEqual(client.post("/api/live/start",json={"acknowledge_real_orders":False}).status_code,422)
            self.assertEqual(client.post("/api/live/test",json={}).status_code,200)
            self.assertEqual(client.post("/api/live/start",json={"acknowledge_real_orders":True}).status_code,409)
            self.assertFalse(self.client.placed)

    def test_duplicate_live_process_cannot_open_same_ledger(self):
        with self.assertRaises(BlockingIOError):LiveService(self.base,self.client)

    def test_enabled_api_needs_dry_run_and_stops_without_touching_paper(self):
        self.live.close()
        with TestClient(create_app(self.base,FakeMarket(),exchange=self.client)) as client:
            for _ in range(100):
                if client.get("/api/live/status").json()["quote_fresh"]:break
                time.sleep(.01)
            self.assertEqual(client.post("/api/live/start",json={"acknowledge_real_orders":True}).status_code,409)
            self.assertEqual(client.post("/api/live/test",json={}).status_code,200)
            response=client.post("/api/live/start",json={"acknowledge_real_orders":True})
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(response.json()["running"])
            self.assertFalse(client.post("/api/live/control/stop",json={}).json()["running"])
            self.assertFalse(client.get("/api/status").json()["running"])
            self.assertFalse(self.client.placed)


class AuthenticationTests(unittest.TestCase):
    def test_jwt_query_hash_nonce_and_hmac_signature(self):
        params={"market":"KRW-BTC","identifier":"sample-id"}
        first=token("sample-access","sample-secret",params);second=token("sample-access","sample-secret",params)
        head,body,signature=first.split(".")
        decode=lambda value:json.loads(base64.urlsafe_b64decode(value+"="*(-len(value)%4)))
        payload=decode(body)
        self.assertEqual(decode(head)["alg"],"HS512")
        self.assertEqual(payload["query_hash"],hashlib.sha512(unquote(urlencode(params)).encode()).hexdigest())
        self.assertNotEqual(payload["nonce"],decode(second.split(".")[1])["nonce"])
        expected=base64.urlsafe_b64encode(hmac.new(b"sample-secret",(head+"."+body).encode(),hashlib.sha512).digest()).rstrip(b"=").decode()
        self.assertEqual(signature,expected)

    def test_transport_disallows_withdrawals_and_disabled_order_creation(self):
        exchange=UpbitExchange("sample-access","sample-secret",False)
        with patch("trade.exchange.urlopen") as network:
            with self.assertRaises(ExchangeError):exchange.place({})
            with self.assertRaises(ExchangeError):exchange.request("POST","/v1/withdraws/coin",{})
            network.assert_not_called()

    def test_transport_errors_do_not_leak_keys_or_raw_response(self):
        exchange=UpbitExchange("sample-access","sample-secret",True)
        for error in (URLError("sample-secret"),HTTPError("sample-secret",401,"sample-secret",{},io.BytesIO(b'{"error":{"name":"jwt_verification","message":"sample-secret"}}'))):
            exchange._next_request=0
            with patch("trade.exchange.urlopen",side_effect=error):
                with self.assertRaises(ExchangeError) as raised:exchange.chance()
            self.assertNotIn("sample-secret",str(raised.exception))
