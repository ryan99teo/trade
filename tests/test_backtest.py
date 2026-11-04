from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from pydantic import ValidationError

from trade.app import create_app
from trade.backtest import (BacktestArchive, BacktestRequest, Bar, collect_history,
                            complete, fills_csv, read_archive, replay, run_backtest)
from trade.config import Settings
from trade.engine import SATOSHI
from trade.market import UTC, MarketError
from test_api import FakeMarket

D = Decimal


def request(**kwargs):
    defaults = dict(start_date="2026-10-01", end_date="2026-10-01", short_window=2, long_window=3)
    defaults.update(kwargs)
    return BacktestRequest(**defaults)


def bar(timestamp, opening, close=None):
    opening, close = D(str(opening)), D(str(opening if close is None else close))
    return Bar(timestamp, opening, max(opening, close), min(opening, close), close, D("1"))


def fixture(req=None):
    req = req or request()
    start, end = req.bounds()
    prices = [100000000,100000000,90000000,120000000]
    return [bar(start-timedelta(hours=4-i), p) for i,p in enumerate(prices)] + [bar(start+timedelta(hours=i), 100000000) for i in range(24)]


class HistoryMarket:
    def __init__(self, bars):
        self.bars, self.calls = bars, []

    def _get(self, path):
        self.calls.append(path)
        query = parse_qs(urlsplit(path).query)
        end = datetime.fromisoformat(query["to"][0])
        selected = sorted((b for b in self.bars if b.time < end), key=lambda b:b.time, reverse=True)[:200]
        return [{"candle_date_time_utc": b.time.replace(tzinfo=None).isoformat(),
                 "opening_price": str(b.open), "high_price": str(b.high), "low_price": str(b.low),
                 "trade_price": str(b.close), "candle_acc_trade_volume": str(b.volume)} for b in selected]


class BacktestTests(unittest.TestCase):
    def test_next_open_fill_does_not_see_current_close(self):
        req = request()
        bars = fixture(req)
        bars[4] = bar(bars[4].time, 130000000, 150000000)
        result = replay(req, bars)
        fill = result["fills"][0]
        self.assertEqual(fill["time"], bars[4].time.isoformat())
        self.assertEqual(D(fill["price"]), D("130130000"))
        mutated = list(bars)
        mutated[4] = bar(bars[4].time, 130000000, 200000000)
        self.assertEqual(replay(req, mutated)["fills"][0], fill)
        self.assertEqual(result["summary"]["candle_count"], 24)
        self.assertEqual(len(result["curve"]), 25)

    def test_fee_slippage_accounting_and_metrics_match_independent_oracle(self):
        req = request(fee_rate="0.01", slippage="0.01", max_exposure="1", daily_loss_limit="0.99")
        bars = fixture(req)
        for i in range(5,len(bars)):
            bars[i] = bar(bars[i].time, 110000000)
        signals = iter([("buy","test"), ("sell","test")]+[("hold","test")]*22)
        with patch("trade.engine.moving_average_cross", side_effect=lambda *a:next(signals)):
            result = replay(req,bars)
        qty = (D("100000")/D("101000000")).quantize(SATOSHI, rounding=ROUND_DOWN)
        cost = qty*D("101000000")*D("1.01")
        proceeds = qty*D("108900000")*D("0.99")
        profit = proceeds-cost
        summary=result["summary"]
        self.assertEqual(D(summary["final_equity"]), D("1000000")+profit)
        self.assertEqual(D(summary["realized_pnl"]),profit)
        self.assertEqual(D(summary["unrealized_pnl"]),0)
        self.assertEqual(D(summary["win_rate_pct"]),100)
        self.assertEqual(summary["closed_trades"],1)
        self.assertEqual(D(summary["fees"]),qty*(D("101000000")+D("108900000"))*D("0.01"))
        entry_equity = D("1000000")-cost+qty*D("100000000")
        pre_sell_equity = D("1000000")-cost+qty*D("110000000")
        expected_mdd = max((D("1000000")-entry_equity)/D("1000000"),
                           (pre_sell_equity-D(summary["final_equity"]))/pre_sell_equity)*100
        self.assertEqual(D(summary["max_drawdown_pct"]),expected_mdd)
        benchmark_qty = (D("1000000")/(D("101000000")*D("1.01"))).quantize(SATOSHI,rounding=ROUND_DOWN)
        benchmark_final = D("1000000")-benchmark_qty*D("101000000")*D("1.01")+benchmark_qty*D("110000000")
        self.assertEqual(D(summary["benchmark_final_equity"]),benchmark_final)

    def test_zero_trades_and_open_position_do_not_invent_win_rate_or_force_sale(self):
        req=request()
        with patch("trade.engine.moving_average_cross",return_value=("hold","test")):
            result=replay(req,fixture(req))
        self.assertEqual(result["summary"]["return_pct"],"0")
        self.assertIsNone(result["summary"]["win_rate_pct"])
        self.assertEqual(result["summary"]["fill_count"],0)
        with patch("trade.engine.moving_average_cross",side_effect=[("buy","test")]+[("hold","test")]*23):
            result=replay(req,fixture(req))
        self.assertEqual(result["summary"]["fill_count"],1)
        self.assertGreater(D(result["summary"]["btc"]),0)
        self.assertIsNone(result["summary"]["win_rate_pct"])
        self.assertEqual(D(result["summary"]["total_pnl"]),D(result["summary"]["realized_pnl"])+D(result["summary"]["unrealized_pnl"]))

    def test_loss_limit_latches_through_replay_and_sell_still_executes(self):
        req=request()
        bars=fixture(req)
        for i in range(5,len(bars)):
            bars[i]=bar(bars[i].time,50000000)
        signals=[("buy","test"),("hold","test"),("buy","test"),("sell","test"),("buy","test")]+[("hold","test")]*19
        with patch("trade.engine.moving_average_cross",side_effect=signals):
            result=replay(req,bars)
        self.assertTrue(result["summary"]["buy_halted"])
        self.assertEqual([f["side"] for f in result["fills"]],["buy","sell"])
        self.assertEqual(result["summary"]["win_rate_pct"],"0")
        self.assertTrue(result["risk_events"])

    def test_duplicate_or_out_of_range_bars_are_rejected(self):
        bars=fixture()
        for invalid in ([],[bar(bars[0].time-timedelta(hours=1),100)]+bars,bars[:8]+[bars[8]]+bars[8:]):
            with self.assertRaises(ValueError):
                replay(request(),invalid)

    def test_exchange_gaps_are_reported_and_never_filled_or_traded_across(self):
        bars=fixture()
        bars=bars[:8]+bars[10:]
        result=replay(request(),bars)
        self.assertEqual(result["summary"]["missing_candles"],2)
        self.assertEqual(result["summary"]["candle_count"],22)
        gap_end=bars[8].time
        self.assertFalse(any(datetime.fromisoformat(f["time"])==gap_end for f in result["fills"]))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"gaps.sqlite3"
            market=HistoryMarket(bars)
            first=run_backtest(path,market,request())
            second=run_backtest(path,market,request())
            self.assertEqual(first["summary"]["missing_candles"],2)
            self.assertEqual(second["data"]["network_requests"],0)

    def test_input_limits_and_dates(self):
        with self.assertRaises(ValueError):request(short_window=4).bounds()
        with self.assertRaises(ValidationError):request(initial_cash="NaN")
        with self.assertRaises(ValueError):request(order_krw="2000000").bounds()
        with self.assertRaises(ValueError):
            BacktestRequest(start_date="2026-01-01",end_date="2027-01-02").bounds()
        with self.assertRaises(ValueError):
            BacktestRequest(start_date="2027-01-01",end_date="2027-01-02").bounds()

    def test_pagination_cache_persistence_and_csv(self):
        req=BacktestRequest(start_date="2026-10-01",end_date="2026-10-10",short_window=2,long_window=3)
        start,end=req.bounds()
        bars=[bar(start-timedelta(hours=4)+timedelta(hours=i),100000000) for i in range(244)]
        market=HistoryMarket(bars)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"history.sqlite3"
            first=run_backtest(path,market,req)
            self.assertEqual(first["data"]["network_requests"],2)
            count=len(market.calls)
            second=run_backtest(path,market,req)
            self.assertEqual(second["data"]["network_requests"],0)
            self.assertEqual(len(market.calls),count)
            self.assertEqual(read_archive(path,first["id"]),first)
            self.assertEqual(len(read_archive(path)),2)
            self.assertTrue(fills_csv(first).startswith("\ufefftime,side,"))

    def test_non_advancing_page_and_missing_data_fail_without_looping(self):
        req=request();start,end=req.bounds()
        with tempfile.TemporaryDirectory() as tmp:
            archive=BacktestArchive(Path(tmp)/"cache.sqlite3")
            try:
                bad=HistoryMarket([bar(end,100)])
                bad._get=lambda path:[{"candle_date_time_utc":end.replace(tzinfo=None).isoformat(),"opening_price":"100","high_price":"100","low_price":"100","trade_price":"100","candle_acc_trade_volume":"1"}]
                with self.assertRaises(MarketError):collect_history(bad,archive,start,end,pause=lambda _:None)
                with self.assertRaises(ValueError):collect_history(HistoryMarket([]),archive,start,end,pause=lambda _:None)
            finally:archive.close()

    def test_api_result_restore_csv_and_paper_account_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(db_path=Path(tmp)/"paper.sqlite3")
            app=create_app(settings,FakeMarket(),HistoryMarket(fixture()))
            with TestClient(app) as client:
                before=client.get("/api/status").json()
                response=client.post("/api/backtests",json=request().model_dump(mode="json"))
                self.assertEqual(response.status_code,200,response.text)
                result=response.json();run_id=result["id"]
                after=client.get("/api/status").json()
                for key in ("cash","btc","running","fills"):
                    self.assertEqual(before[key],after[key])
                self.assertEqual(client.get("/api/backtests/"+run_id).json(),result)
                self.assertEqual(client.get("/api/backtests").json()["runs"][0]["id"],run_id)
                csv=client.get("/api/backtests/"+run_id+"/trades.csv")
                self.assertEqual(csv.status_code,200)
                self.assertIn("attachment",csv.headers["content-disposition"])
                self.assertEqual(client.get("/api/backtests/missing").status_code,404)
                self.assertEqual(client.post("/api/backtests",json={}).status_code,422)
            with TestClient(create_app(settings,FakeMarket())) as client:
                self.assertEqual(client.get("/api/backtests/"+run_id).json(),result)

    def test_long_download_keeps_live_api_responsive_and_rejects_second_run(self):
        entered, release = threading.Event(), threading.Event()
        market = HistoryMarket(fixture())
        original = market._get
        def delayed(path):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return original(path)
        market._get = delayed
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(db_path=Path(tmp)/"paper.sqlite3"), FakeMarket(), market)
            with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, "/api/backtests", json=request().model_dump(mode="json"))
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(client.get("/api/status").status_code, 200)
                    self.assertTrue(client.get("/api/backtests").json()["running"])
                    self.assertEqual(client.post("/api/backtests",json=request().model_dump(mode="json")).status_code,409)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=5).status_code,200)
