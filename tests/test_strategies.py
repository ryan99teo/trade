from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from trade.app import create_app
from trade.backtest import compare_backtests, read_archive, replay
from trade.config import NEW_STRATEGY_FIELDS, Settings
from trade.engine import Engine
from trade.market import Candle, Snapshot, UTC
from trade.storage import Store
from trade.strategy import allocation_signal
from test_api import FakeMarket
from test_backtest import HistoryMarket, fixture, request

D=Decimal


class AdditionalStrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Settings(db_path=Path(self.temp.name)/"paper.sqlite3",candle_minutes=60)
        self.store=Store(self.base)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def engine(self,**changes):
        engine=Engine(self.base,self.store)
        engine.configure(changes)
        return engine

    def test_dca_is_due_at_fixed_utc_boundaries_and_never_sells(self):
        settings=replace(self.base,strategy="dca",dca_interval_hours=24)
        midnight=datetime(2026,10,1,tzinfo=UTC)
        candle=Candle(midnight-timedelta(hours=1),D("100000000"))
        self.assertEqual(allocation_signal(settings,candle,D(1),D(1),D(0))[0],"buy")
        candle=Candle(midnight,D("100000000"))
        self.assertEqual(allocation_signal(settings,candle,D(1),D(1),D(100))[0],"hold")
        offset=replace(settings,dca_offset_minutes=35)
        # 00:00-01:00 UTC candle contains 00:35; previous hour candle does not.
        self.assertEqual(allocation_signal(offset,Candle(midnight-timedelta(hours=1),D(1)),D(1),D(1),D(0))[0],"hold")
        self.assertEqual(allocation_signal(offset,Candle(midnight,D(1)),D(1),D(1),D(0))[0],"buy")
        with self.assertRaises(ValueError):replace(settings,dca_offset_minutes=24*60)
        req=request(strategy="dca",dca_interval_hours=6,max_exposure="1",fee_rate="0",slippage="0")
        result=replay(req,fixture(req))
        self.assertEqual(len(result["fills"]),4)
        self.assertTrue(all(f["side"]=="buy" for f in result["fills"]))
        self.assertEqual(D(result["summary"]["cash"]),600000)
        self.assertEqual(D(result["summary"]["btc"]),D("0.004"))

    def test_dca_deduplication_stale_signals_and_buy_halt(self):
        engine=self.engine(strategy="dca",dca_interval_hours=24)
        now=datetime(2026,10,1,tzinfo=UTC)
        shot=Snapshot(D("100000000"),now,[Candle(now-timedelta(hours=1),D("100000000"))])
        engine.process(shot,now);engine.control("start",now)
        engine.process(shot,now);engine.process(shot,now)
        self.assertEqual(len(self.store.recent("fills")),1)
        later=now+timedelta(days=1,minutes=3)
        shot=Snapshot(shot.price,later,[Candle(now+timedelta(hours=23),shot.price)])
        engine.process(shot,later)
        self.assertEqual(len(self.store.recent("fills")),1)
        engine.control("pause_buys",later)
        later=now+timedelta(days=2)
        engine.process(Snapshot(shot.price,later,[Candle(later-timedelta(hours=1),shot.price)]),later)
        self.assertEqual(len(self.store.recent("fills")),1)

    def test_rebalance_tolerance_and_target_validation(self):
        settings=replace(self.base,strategy="rebalance")
        candle=Candle(datetime(2026,10,1,tzinfo=UTC),D(1))
        for ratio,expected in (("0.1","buy"),("0.18","hold"),("0.2","hold"),("0.22","hold"),("0.24","sell")):
            self.assertEqual(allocation_signal(settings,candle,D(1),D(1),D(ratio))[0],expected)
        with self.assertRaises(ValueError):replace(settings,target_exposure=D("0.4"))
        with self.assertRaises(ValueError):replace(settings,rebalance_tolerance=D("0.3"))
        with self.assertRaises(ValueError):replace(settings,dca_interval_hours=7)

    def test_rebalance_partial_sell_allocates_cost_and_hits_target_after_fees(self):
        engine=self.engine(strategy="rebalance",order_krw=D("500000"))
        with self.store.db:
            self.store.set("cash",800000);self.store.set("btc","0.002");self.store.set("cost",200000)
            engine._execute("sell",D("200000000"),"partial",datetime.now(UTC))
        fill=self.store.recent("fills")[0]
        quantity=D(fill["quantity"])
        allocated=D(200000)*quantity/D("0.002")
        self.assertGreater(quantity,0);self.assertLess(quantity,D("0.002"))
        self.assertEqual(self.store.number("cost"),D(200000)-allocated)
        self.assertEqual(D(fill["realized_pnl"]),quantity*D(fill["price"])-D(fill["fee"])-allocated)
        ratio=self.store.number("btc")*D("200000000")/engine.equity(D("200000000"))
        self.assertLess(abs(ratio-D("0.20")),D("0.000002"))
        total=engine.equity(D("200000000"))-self.base.initial_cash
        self.assertAlmostEqual(total,self.store.number("realized")+self.store.number("btc")*D("200000000")-self.store.number("cost"),places=12)

    def test_rebalance_buy_stops_at_target_and_sell_respects_order_limit(self):
        engine=self.engine(strategy="rebalance",order_krw=D("500000"))
        with self.store.db:engine._execute("buy",D("100000000"),"target",datetime.now(UTC))
        ratio=self.store.number("btc")*D("100000000")/engine.equity(D("100000000"))
        self.assertLessEqual(ratio,D("0.20"))
        self.assertLess(D("0.20")-ratio,D("0.000002"))
        engine.configure({"order_krw":D("10000")})
        with self.store.db:engine._execute("sell",D("1000000000"),"limited",datetime.now(UTC))
        fill=self.store.recent("fills")[0]
        self.assertEqual(fill["side"],"sell")
        self.assertLessEqual(D(fill["quantity"])*D(fill["price"]),D(10000))

    def test_legacy_config_migrates_and_selected_strategy_survives_restart(self):
        old=json.loads(self.store.get("config"))
        for key in NEW_STRATEGY_FIELDS:old.pop(key)
        with self.store.db:self.store.set("config",json.dumps(old,sort_keys=True))
        self.store.db.close();self.store=Store(self.base)
        self.assertEqual(self.store.settings.strategy,"ma_cross")
        self.engine(strategy="dca",dca_interval_hours=12)
        self.store.db.close();self.store=Store(self.base)
        self.assertEqual(self.store.settings.strategy,"dca")
        self.assertEqual(self.store.settings.dca_interval_hours,12)

    def test_compare_uses_same_candles_once_and_persists_all_three_runs(self):
        req=request();market=HistoryMarket(fixture(req));path=Path(self.temp.name)/"compare.sqlite3"
        result=compare_backtests(path,market,req)
        self.assertEqual(len(market.calls),1)
        self.assertEqual({r["request"]["strategy"] for r in result["runs"]},{"ma_cross","dca","rebalance"})
        self.assertEqual(len(read_archive(path)),3)
        self.assertEqual(len({r["summary"]["candle_count"] for r in result["runs"]}),1)
        self.assertEqual(len({r["summary"]["benchmark_return_pct"] for r in result["runs"]}),1)
        self.assertEqual(self.store.number("cash"),self.base.initial_cash)

    def test_api_strategy_selection_and_compare(self):
        req=request();market=HistoryMarket(fixture(req))
        with TestClient(create_app(self.base,FakeMarket(),market)) as client:
            values={"short_window":5,"long_window":20,"order_krw":"100000","max_exposure":"0.3","daily_loss_limit":"0.03","strategy":"rebalance","target_exposure":"0.2","rebalance_tolerance":"0.02","dca_interval_hours":24}
            response=client.post("/api/settings",json=values)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["settings"]["strategy"],"rebalance")
            response=client.post("/api/backtests/compare",json=req.model_dump(mode="json"))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(len(response.json()["runs"]),3)
            self.assertFalse(client.get("/api/status").json()["running"])
