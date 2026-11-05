import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from trade.config import Settings
from trade.engine import Engine
from trade.market import Candle, Snapshot, UTC
from trade.seconds import SecondBars, UpbitSeconds
from trade.storage import Store
from trade.strategy import consecutive_state, consecutive_signal
from trade.backtest import BacktestRequest, BacktestArchive, Bar, aggregate_bars, collect_history, replay
from trade.decision_gate import GateVerdict, build_state
from trade.live import LiveService
from test_live import FakeExchange
from test_consecutive import Gate


class SecondsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.start=datetime(2026,11,1,tzinfo=UTC)
        self.s=Settings(db_path=Path(self.tmp.name)/'test.db',strategy='consecutive',candle_minutes=1,
                        consecutive_seconds=10,decision_gate_enabled=True)
        self.store=Store(self.s);self.gate=Gate();self.e=Engine(self.s,self.store,self.gate)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def message(self, i, price=100, volume=1):
        stamp=self.start+timedelta(seconds=i)
        return dict(type='candle.1s',code='KRW-BTC',trade_price=str(price),candle_acc_trade_volume=str(volume),
                    candle_date_time_utc=stamp.replace(tzinfo=None).isoformat(),timestamp=int((stamp+timedelta(milliseconds=900)).timestamp()*1000))
    def feed(self, at=0, end=40):
        feed=SecondBars(self.start+timedelta(seconds=at))
        for i in range(end):feed.ingest(self.message(i,100+i),self.start+timedelta(seconds=i+1))
        feed.ingest(dict(type='ticker',code='KRW-BTC',trade_price='150',timestamp=int((self.start+timedelta(seconds=end)).timestamp()*1000)),self.start+timedelta(seconds=end))
        return feed
    def bars(self, prices, seconds=10):
        return [Candle(self.start+timedelta(seconds=i*seconds),D(p)) for i,p in enumerate(prices)]
    def snap(self, prices, mark=None, seconds=10, offset=0):
        bars=self.bars(prices,seconds);now=bars[-1].time+timedelta(seconds=seconds+offset)
        return Snapshot(D(mark or prices[-1]),now,bars),now
    def test_closed_boundaries_volume_and_revision(self):
        feed=self.feed(end=20)
        update=self.message(9,200,2);update['timestamp']+=50;feed.ingest(update,self.start+timedelta(seconds=20))
        snap=feed.snapshot(10,self.start+timedelta(seconds=20))
        self.assertEqual(len(snap.candles),1);self.assertEqual(snap.candles[0].close,200)
        self.assertEqual(snap.candles[0].volume,11)
        self.assertEqual(len(feed.snapshot(10,self.start+timedelta(seconds=21)).candles),2)
    def test_partial_connect_bucket_is_excluded(self):
        snap=self.feed(at=2,end=21).snapshot(10,self.start+timedelta(seconds=21))
        self.assertEqual([c.time for c in snap.candles],[self.start+timedelta(seconds=10)])
    def test_closed_bars_are_immutable(self):
        feed=self.feed(end=21);snap=feed.snapshot(10,self.start+timedelta(seconds=21))
        update=self.message(9,999);update['timestamp']+=20000;feed.ingest(update,self.start+timedelta(seconds=40))
        self.assertEqual(feed.snapshot(10,self.start+timedelta(seconds=41)).candles[0],snap.candles[0])
    def test_no_trade_bucket_is_gap(self):
        feed=self.feed(end=41)
        for t in list(feed.rows):
            if 10 <= (t-self.start).total_seconds()<20:del feed.rows[t]
        bars=feed.snapshot(10,self.start+timedelta(seconds=41)).candles
        state=consecutive_state(bars,seconds=10)
        self.assertFalse(state['ready']);self.assertEqual(state['count'],1)
    def test_out_of_order_ticker_cannot_replace_new_quote(self):
        feed=self.feed(end=21)
        feed.ingest(dict(type='ticker',code='KRW-BTC',trade_price='1',timestamp=int(self.start.timestamp()*1000)),self.start+timedelta(seconds=21))
        self.assertEqual(feed.quote[0],150)
    def test_ten_and_thirty_second_returns(self):
        for seconds in (10,30):
            s=replace(self.s,consecutive_seconds=seconds)
            state=consecutive_state(self.bars([100,101,102,103],seconds),seconds=seconds)
            self.assertEqual(state['window_seconds'],seconds*3)
            self.assertEqual(consecutive_signal(s,state,D(0),None)[0],'buy')
    def test_wrong_interval_cannot_trigger(self):
        state=consecutive_state(self.bars([100,101,102,103],60),seconds=10)
        self.assertFalse(state['ready'])
    def test_ten_second_engine_buy(self):
        snap,now=self.snap([100000000,100100000,100200000,100500000]);self.e.running=True
        self.e.process(snap,now);self.assertGreater(self.store.number('btc'),0)
        state=self.gate.calls[0][1];self.assertEqual(state['candle_seconds'],10)
        self.assertEqual(state['policy']['return_window_seconds'],30)
        self.assertIn('30s',state['price_changes_pct'])
    def test_expired_gate_response_discarded_with_fresh_quote(self):
        snap,now=self.snap([100000000,100100000,100200000,100500000]);self.e.running=True
        pending=self.e._prepare(snap,now);result=self.e._review(pending,self.s)
        self.e.quote_time=now+timedelta(seconds=11)
        with patch('trade.engine.time.monotonic',return_value=pending['started']+11):
            self.e._commit(pending,self.s,*result,now)
        self.assertEqual(self.store.number('btc'),0)
    def test_stream_stop_during_gate_review(self):
        entered,release=threading.Event(),threading.Event()
        class SlowGate:
            def evaluate(self,*args):
                entered.set();release.wait(2);return GateVerdict(True,.99)
        self.e.decision_gate=SlowGate();self.e.running=True
        with self.store.db:
            self.store.set('btc','.001');self.store.set('cost','100000');self.store.set('cash','900000')
        async def run():
            snap,now=self.snap([107000000,106000000,105000000,104000000])
            self.e.process_stream(snap,now)
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            later=now+timedelta(seconds=1)
            self.e.process_stream(Snapshot(D(89000000),later,snap.candles),later)
            self.assertEqual(self.store.number('btc'),0)
            release.set();await self.e.stream_review_task
        try:asyncio.run(run())
        finally:release.set()
        self.assertEqual(len(self.store.recent('fills')),1)
        self.assertTrue(self.store.recent('fills')[0]['identifier'].startswith('stop:'))
    def test_unit_change_clears_cache_and_invalidates_quote(self):
        snap,now=self.snap([100000000]*4);self.e.process(snap,now)
        self.e.configure({'consecutive_seconds':30},now)
        self.assertEqual(self.store.candle_count(),0);self.assertIsNone(self.e.quote_time)
        self.store.db.close();self.store=Store(self.s)
        self.assertEqual(self.store.settings.consecutive_seconds,30)
    def test_legacy_migration_keeps_one_minute(self):
        with self.store.db:
            config=json.loads(self.store.get('config'));config.pop('consecutive_seconds')
            self.store.set('config',json.dumps(config,sort_keys=True))
        self.store.db.close();self.store=Store(replace(self.s,consecutive_seconds=60))
        self.assertEqual(self.store.settings.consecutive_seconds,60)
    def test_invalid_units(self):
        for seconds in (0,1,5,15,120,10.0,True):
            with self.assertRaises(ValueError):replace(self.s,consecutive_seconds=seconds)
    def test_history_aggregation_ohlcv(self):
        raw=[Bar(self.start+timedelta(seconds=i),D(i+100),D(i+102),D(i+99),D(i+101),D(2)) for i in range(20)]
        bars=aggregate_bars(raw,10)
        self.assertEqual(len(bars),2)
        self.assertEqual((bars[0].open,bars[0].high,bars[0].low,bars[0].close,bars[0].volume),(D(100),D(111),D(99),D(110),D(20)))
    def test_seconds_history_uses_separate_cache(self):
        owner=self
        class Market:
            def __init__(self):self.calls=[]
            def _get(self,path):
                self.calls.append(path)
                return [dict(candle_date_time_utc=(owner.start+timedelta(seconds=i)).replace(tzinfo=None).isoformat(),
                             opening_price=100,high_price=101,low_price=99,trade_price=100,candle_acc_trade_volume=1) for i in reversed(range(20))]
        archive=BacktestArchive(Path(self.tmp.name)/'history.db');market=Market()
        try:
            bars,calls=collect_history(market,archive,self.start,self.start+timedelta(seconds=20),seconds=1)
            self.assertEqual(len(bars),20);self.assertEqual(calls,1)
            self.assertTrue(market.calls[0].startswith('candles/seconds?'))
            self.assertEqual(archive.bars(self.start,self.start+timedelta(seconds=20),minutes=1),[])
            _,calls=collect_history(market,archive,self.start,self.start+timedelta(seconds=20),seconds=1)
            self.assertEqual(calls,0)
        finally:archive.close()
    def test_seconds_backtest_bounds(self):
        req=BacktestRequest(start_date='2026-11-01',end_date='2026-11-01',strategy='consecutive',consecutive_seconds=10)
        now=datetime(2026,11,5,tzinfo=UTC);req.bounds(now)
        self.assertEqual(req.settings().bar_seconds,10)
        with self.assertRaises(ValueError):req.model_copy(update={'end_date':datetime(2026,11,2).date()}).bounds(now)
        with self.assertRaises(ValueError):req.model_copy(update={'start_date':datetime(2026,7,1).date(),'end_date':datetime(2026,7,1).date()}).bounds(now)
    def test_seconds_replay_next_open(self):
        req=BacktestRequest(start_date='2026-11-01',end_date='2026-11-01',strategy='consecutive',consecutive_seconds=10)
        start,_=req.bounds();prices=[D(100000000)+i*D(200000) for i in range(21)]
        bars=[Bar(start-timedelta(seconds=(21-i)*10),p,p,p,p,D(1)) for i,p in enumerate(prices)]
        bars.extend(Bar(start+timedelta(seconds=i*10),D(105000000),D(105000000),D(105000000),D(105000000),D(1)) for i in range(5))
        result=replay(req,bars)
        self.assertEqual(result['fills'][0]['time'],start.isoformat())
        self.assertEqual(result['curve'][1]['time'],(start+timedelta(seconds=10)).isoformat())
    def test_live_expires_old_ten_second_intent(self):
        live=LiveService(replace(self.s,consecutive_seconds=60),FakeExchange())
        try:
            live.engine.configure({'consecutive_seconds':10});live.engine.running=True
            now=datetime.now(UTC);live.engine.quote_time=now
            with live.store.db:
                live.store.set('price','100000000')
                live.engine._execute('buy',D(100000000),(now-timedelta(seconds=25)).isoformat(),now)
            asyncio.run(live.sync())
            self.assertEqual(live.client.placed,[])
            self.assertEqual(live.store.db.execute('select state from live_orders').fetchone()[0],'aborted')
        finally:live.close()

if __name__=='__main__':unittest.main()
