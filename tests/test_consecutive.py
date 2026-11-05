import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from trade.config import Settings
from trade.decision_gate import GateVerdict
from trade.engine import Engine
from trade.market import Candle, Snapshot, UTC
from trade.storage import Store
from trade.strategy import consecutive_state, consecutive_signal
from trade.backtest import BacktestRequest, BacktestArchive, Bar, replay
from trade.live import LiveService
from test_live import FakeExchange


class Gate:
    def __init__(self, proceed=True): self.calls=[]; self.proceed=proceed
    def evaluate(self, settings, side, state):
        self.calls.append((side,state))
        return GateVerdict(self.proceed, .99 if self.proceed else .01)


class ConsecutiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Settings(db_path=Path(self.tmp.name)/'test.db',strategy='consecutive',candle_minutes=1,
                        decision_gate_enabled=True)
        self.db=Store(self.s);self.gate=Gate();self.e=Engine(self.s,self.db,self.gate)
        self.e.running=True
        self.start=datetime(2026,11,1,tzinfo=UTC)
    def tearDown(self): self.db.db.close();self.tmp.cleanup()
    def bars(self, prices):return [Candle(self.start+timedelta(minutes=i),D(str(p))) for i,p in enumerate(prices)]
    def tick(self,prices,mark=None,offset=0):
        bars=self.bars(prices); now=bars[-1].time+timedelta(minutes=1,seconds=offset)
        return Snapshot(D(str(mark if mark is not None else prices[-1])),now,bars),now
    def process(self,prices,mark=None,offset=0):
        snap,now=self.tick(prices,mark,offset);self.e.process(snap,now)
    def holding(self):
        with self.db.db:
            self.db.set('btc','.001');self.db.set('cost','100000');self.db.set('cash','900000')
    def test_equivalent_decimal_fingerprint_survives_restart(self):
        with self.db.db:
            record=json.loads(self.db.get('config'))
            record['consecutive_entry_return']='0.00500000'
            self.db.set('config',json.dumps(record,sort_keys=True))
        self.db.db.close();self.db=Store(replace(self.s,consecutive_entry_return=D('0.005')))
        self.assertEqual(self.db.settings.consecutive_entry_return,D('.005'))
    def test_legacy_live_timeframe_remains_independent(self):
        base=replace(self.s,strategy='ma_cross',candle_minutes=5)
        live=LiveService(base,FakeExchange());path=live.store.settings.db_path
        with live.store.db:
            overrides=json.loads(live.store.get('strategy_overrides'));overrides.pop('candle_minutes')
            live.store.set('strategy_overrides',json.dumps(overrides))
        live.close()
        live=LiveService(self.s,FakeExchange())
        try:self.assertEqual(live.engine.settings.candle_minutes,5)
        finally:live.close()
    def test_configurable_counts_and_window_returns(self):
        for count in (1, 3, 5, 199):
            settings=replace(self.s,consecutive_count=count)
            prices=[D(100000000)+D(1000000)*i for i in range(count+1)]
            state=consecutive_state(self.bars(prices),required=count)
            self.assertEqual(state['count'],count)
            self.assertEqual(D(state['window_return']),prices[-1]/prices[0]-1)
            self.assertEqual(consecutive_signal(settings,state,D(0),None)[0],'buy')
            short=consecutive_state(self.bars(prices[:-1]),required=count)
            self.assertEqual(consecutive_signal(settings,short,D(0),None)[0],'hold')
    def test_sell_uses_configured_count(self):
        settings=replace(self.s,consecutive_count=5)
        state=consecutive_state(self.bars([110,109,108,107,106]),required=5)
        self.assertEqual(consecutive_signal(settings,state,D(1),D('.04'))[0],'hold')
        state=consecutive_state(self.bars([110,109,108,107,106,105]),required=5)
        self.assertEqual(consecutive_signal(settings,state,D(1),D('.04'))[0],'sell')
    def test_count_change_rebuilds_review_state(self):
        bars=self.bars([100,101,102,103,104,105])
        state=consecutive_state(bars);state['reviewed']=True
        updated=consecutive_state(bars,state,required=5)
        self.assertFalse(updated['reviewed']);self.assertEqual(updated['window_minutes'],5)
        self.assertTrue(updated['ready'])
    def test_count_validation(self):
        for count in (0,200,1.5,True):
            with self.assertRaises(ValueError):replace(self.s,consecutive_count=count)
    def test_count_persists_and_old_fingerprint_upgrades(self):
        self.e.running=False;self.e.configure({'consecutive_count':5})
        self.db.db.close();self.db=Store(self.s)
        self.assertEqual(self.db.settings.consecutive_count,5)
        with self.db.db:
            for key in ('config','strategy_overrides'):
                record=json.loads(self.db.get(key));record.pop('consecutive_count',None)
                self.db.set(key,json.dumps(record,sort_keys=True))
        self.db.db.close();self.db=Store(self.s)
        self.assertEqual(self.db.settings.consecutive_count,3)
    def test_backtest_uses_count_for_warmup(self):
        req=BacktestRequest(start_date='2026-10-01',end_date='2026-10-01',strategy='consecutive',consecutive_count=199)
        self.assertEqual(req.settings().consecutive_count,199)
        self.assertEqual(req.warmup_count,200)
        with self.assertRaises(ValueError):
            BacktestRequest(start_date='2026-10-01',end_date='2026-10-01',consecutive_count=1.5)
    def test_exact_threshold_and_below(self):
        for prices,action in [([100,100.1,100.2,100.5],'buy'),([100,100.1,100.2,100.49],'hold')]:
            state=consecutive_state(self.bars(prices))
            self.assertEqual(consecutive_signal(self.s,state,D(0),None)[0],action)
    def test_equal_and_gap_reset(self):
        self.assertEqual(consecutive_state(self.bars([100,101,101,102]))['count'],1)
        bars=self.bars([100,101,102,103]);bars[-1]=Candle(bars[-1].time+timedelta(minutes=1),D(103))
        state=consecutive_state(bars);self.assertFalse(state['ready']);self.assertEqual(state['count'],0)
    def test_rejection_once_per_run_and_persists(self):
        self.gate.proceed=False
        self.process([100000000,100100000,100200000,100500000])
        self.e=Engine(self.s,self.db,self.gate);self.e.running=True
        self.process([100000000,100100000,100200000,100500000,100800000])
        self.assertEqual(len(self.gate.calls),1);self.assertEqual(self.db.number('btc'),0)
    def test_hold_and_holding_do_not_call_gate(self):
        self.process([100000000,100010000,100020000,100030000]);self.assertEqual(self.gate.calls,[])
        self.holding();self.process([100000000,100100000,100200000,100500000,101000000]);self.assertEqual(self.gate.calls,[])
    def test_approved_buy_and_net_cost_payload(self):
        self.process([100000000,100100000,100200000,100500000])
        self.assertGreater(self.db.number('btc'),0);self.assertEqual(self.gate.calls[0][0],'buy')
        self.assertIn('estimated_net_return_pct',self.gate.calls[0][1])
    def test_gross_three_percent_is_not_net_three(self):
        self.holding();self.process([105000000,104000000,103500000,103000000])
        self.assertEqual(self.gate.calls,[]);self.assertEqual(self.db.number('btc'),D('.001'))
    def test_profitable_three_down_sells_after_gate(self):
        self.holding();self.process([107000000,106000000,105000000,104000000])
        self.assertEqual(self.gate.calls[0][0],'sell');self.assertEqual(self.db.number('btc'),0)
    def test_stop_checks_same_decided_candle_without_gate(self):
        self.holding();self.gate.proceed=False
        self.process([100000000]*4)
        self.process([100000000]*4,89000000,offset=10)
        self.assertEqual(self.db.number('btc'),0);self.assertEqual(self.gate.calls,[])
        self.assertEqual(self.db.recent('fills')[0]['side'],'sell')
    def test_disabled_gate_blocks_buy_but_not_stop(self):
        self.e.running=False;self.e.configure({'decision_gate_enabled':False});self.e.running=True
        self.process([100000000,100100000,100200000,100500000]);self.assertEqual(self.db.number('btc'),0)
        self.holding();self.process([100000000]*5,89000000);self.assertEqual(self.db.number('btc'),0)
    def test_exit_fee_override(self):
        self.holding();base=self.e.net_return(D(104000000));self.e.exit_fee_rate=D('.01')
        self.assertLess(self.e.net_return(D(104000000)),base)
    def test_stopped_engine_does_not_liquidate(self):
        self.holding();self.e.running=False;self.process([100000000]*4,89000000)
        self.assertEqual(self.db.number('btc'),D('.001'))
    def test_async_stop_during_review_prevents_fill(self):
        engine=self.e
        class StopGate(Gate):
            def evaluate(self,*args):
                engine.running=False
                return GateVerdict(True,.99)
        engine.decision_gate=StopGate();snap,now=self.tick([100000000,100100000,100200000,100500000])
        asyncio.run(engine.process_async(snap,now));self.assertEqual(self.db.number('btc'),0)
    def test_timeframe_change_preserves_balance(self):
        self.e.running=False;self.holding();self.e.configure({'strategy':'ma_cross','candle_minutes':5})
        self.e.configure({'strategy':'consecutive'});self.assertEqual(self.e.settings.candle_minutes,1)
        self.assertEqual(self.db.number('btc'),D('.001'));self.assertEqual(self.db.candle_count(),0)
    def test_unfinished_candle_cannot_trigger_buy(self):
        snap,now=self.tick([100000000,100100000,100200000,100500000])
        now-=timedelta(seconds=30)
        snap=Snapshot(snap.price,now,snap.candles)
        self.e.process(snap,now);self.assertEqual(self.gate.calls,[])
    def test_live_stop_intent_plans_below_profit_threshold(self):
        live=LiveService(self.s,FakeExchange())
        try:
            asyncio.run(live.connect())
            live.engine.running=True
            with live.store.db:
                live.store.set('btc','.0001');live.store.set('cost','10000');live.store.set('cash','90000')
                live.engine._execute('sell',D(89000000),'stop:'+datetime.now(UTC).isoformat(),datetime.now(UTC),force=True)
            row=live.active()[0];self.assertEqual(row['purpose'],'stop_loss')
            with self.assertRaises(ValueError):live.plan('sell',D(89000000),row['identifier'])
            params=live.plan('sell',D(89000000),row['identifier'],force=True)
            self.assertEqual(params['side'],'ask');self.assertEqual(live.client.placed,[])
        finally:live.close()
    def test_minute_replay_uses_next_open_and_no_jev(self):
        req=BacktestRequest(start_date='2026-10-01',end_date='2026-10-01',strategy='consecutive')
        start,end=req.bounds()
        prices=[D(100000000)+i*D(200000) for i in range(21)]
        bars=[Bar(start-timedelta(minutes=21-i),p,p,p,p,D(1)) for i,p in enumerate(prices)]
        bars.extend([Bar(start+timedelta(minutes=i),D(105000000),D(105000000),D(105000000),D(105000000),D(1)) for i in range(5)])
        result=replay(req,bars)
        self.assertEqual(result['fills'][0]['time'],start.isoformat())
        self.assertEqual(result['fills'][0]['price'],str(D(105000000)*(1+req.slippage)))
        self.assertEqual(result['summary']['candle_count'],5)
    def test_one_minute_backtest_limit_and_separate_cache(self):
        req=BacktestRequest(start_date='2026-10-01',end_date='2026-10-08',strategy='consecutive')
        self.assertEqual(req.minutes,1)
        with self.assertRaises(ValueError):req.bounds(datetime(2026,11,1,tzinfo=UTC))
        archive=BacktestArchive(Path(self.tmp.name)/'history.db')
        self.assertNotEqual(archive.tables(1),archive.tables(60));archive.db.close()

if __name__=='__main__':unittest.main()
