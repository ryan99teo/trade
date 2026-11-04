import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trade.config import Settings
from trade.engine import Engine
from trade.market import Candle, Snapshot, UTC
from trade.storage import Store
from trade.strategy import moving_average_cross

D = Decimal
NOW = datetime(2026, 11, 4, 3, 0, 30, tzinfo=UTC)


def snapshot(prices=(100, 100, 90, 120), now=NOW, mark="100000000"):
    end = now.replace(minute=0, second=0, microsecond=0)
    bars = [Candle(end - timedelta(hours=len(prices)-i), D(str(p))) for i, p in enumerate(prices)]
    return Snapshot(D(mark), now, bars)


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp.name)/"test.sqlite3", short_window=2, long_window=3, candle_minutes=60)
        self.store = Store(self.settings)
        self.engine = Engine(self.settings, self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def start(self, shot=None, now=NOW):
        self.engine.process(shot or snapshot(now=now), now)
        self.engine.control("start", now)

    def test_cross_uses_previous_and_current_closed_bars(self):
        self.assertEqual(moving_average_cross(snapshot().candles, 2, 3, 60)[0], "buy")
        self.assertEqual(moving_average_cross(snapshot((100,100,110,80)).candles, 2, 3, 60)[0], "sell")
        self.assertEqual(moving_average_cross(snapshot((100,100,100,100)).candles, 2, 3, 60)[0], "hold")

    def test_duplicate_signal_and_restart_do_not_duplicate_fill(self):
        self.start()
        self.engine.process(snapshot(), NOW)
        cash = self.store.number("cash")
        self.engine.process(snapshot(), NOW + timedelta(seconds=10))
        self.assertEqual(len(self.store.recent("fills")), 1)
        self.assertEqual(self.store.number("cash"), cash)
        self.store.db.close()
        self.store = Store(self.settings)
        self.engine = Engine(self.settings, self.store)
        self.assertFalse(self.engine.running)
        self.start()
        self.engine.process(snapshot(), NOW)
        self.assertEqual(len(self.store.recent("fills")), 1)
        self.assertEqual(self.store.number("cash"), cash)

    def test_buy_sell_accounting_includes_fees_and_slippage(self):
        self.start()
        self.engine.process(snapshot(), NOW)
        buy = self.store.recent("fills")[0]
        self.assertEqual(D(buy["price"]), D("100100000"))
        self.assertEqual(self.store.number("cash") + self.store.number("cost"), self.settings.initial_cash)
        self.assertLess(self.engine.equity(D("100000000")), self.settings.initial_cash)
        later = NOW + timedelta(hours=1)
        self.engine.process(snapshot((100,100,110,80), later, "110000000"), later)
        sell = self.store.recent("fills")[0]
        self.assertEqual(sell["side"], "sell")
        self.assertEqual(self.store.number("btc"), 0)
        self.assertEqual(self.store.number("cost"), 0)
        self.assertEqual(self.store.number("cash") - self.settings.initial_cash, self.store.number("realized"))
        self.assertGreater(self.store.number("realized"), 0)

    def test_exposure_limit_applies_after_execution_costs(self):
        self.start()
        with self.store.db:
            for i in range(10):
                self.engine._execute("buy", D("100000000"), "buy-"+str(i), NOW)
        self.assertLessEqual(self.store.number("btc")*D("100000000"),
                             self.engine.equity(D("100000000"))*self.settings.max_exposure)
        self.assertGreaterEqual(self.store.number("cash"), 0)

    def test_stale_quote_stops_and_requires_explicit_restart(self):
        self.start()
        self.engine.process(snapshot(), NOW+timedelta(seconds=61))
        self.assertFalse(self.engine.running)
        self.assertIsNotNone(self.engine.last_error)
        self.engine.process(snapshot(), NOW)
        self.assertFalse(self.engine.running)
        self.assertIsNone(self.engine.last_error)
        self.assertEqual(len(self.store.recent("fills")), 0)

    def test_stale_signal_is_not_replayed(self):
        late = NOW + timedelta(minutes=3)
        self.start(snapshot(now=late), late)
        self.engine.process(snapshot(now=late), late)
        self.assertEqual(len(self.store.recent("fills")), 0)
        self.assertIn("2분", self.store.recent("decisions")[0]["reason"])

    def test_daily_loss_latches_buys_but_permits_sell(self):
        self.start()
        self.engine.process(snapshot(), NOW)
        later = NOW + timedelta(hours=1)
        self.engine.process(snapshot((100,100,100,100), later, "50000000"), later)
        self.assertEqual(self.store.get("buy_halted"), "1")
        with self.assertRaises(ValueError):
            self.engine.control("resume_buys", later)
        later += timedelta(hours=1)
        self.engine.process(snapshot((100,100,110,80), later, "50000000"), later)
        self.assertEqual(self.store.number("btc"), 0)
        self.assertEqual(self.store.recent("fills")[0]["side"], "sell")

    def test_manual_buy_pause_does_not_stop_data_collection(self):
        self.start()
        self.engine.control("pause_buys", NOW)
        self.engine.process(snapshot(), NOW)
        self.assertTrue(self.engine.running)
        self.assertEqual(len(self.store.recent("fills")), 0)
        self.assertTrue(self.store.recent("equity"))

    def test_unfinished_candle_cannot_trigger_trade(self):
        shot = snapshot((100,100,100,100))
        shot.candles.append(Candle(NOW.replace(minute=0, second=0), D("10000")))
        self.start(shot)
        self.engine.process(shot, NOW)
        self.assertEqual(self.store.recent("decisions")[0]["action"], "hold")

    def test_missing_bars_prevent_signal(self):
        bars = snapshot().candles
        bars[1] = Candle(bars[1].time-timedelta(minutes=5), bars[1].close)
        self.assertEqual(moving_average_cross(bars, 2, 3, 60)[0], "hold")

    def test_failed_transaction_rolls_back_cash_and_fill(self):
        before = self.store.number("cash")
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.engine._execute("buy", D("100000000"), "rollback", NOW)
                raise RuntimeError("simulate process failure before commit")
        self.assertEqual(self.store.number("cash"), before)
        self.assertFalse(self.store.recent("fills"))

    def test_day_boundary_is_korean_midnight_and_pause_persists(self):
        before = datetime(2026,11,4,14,59,30,tzinfo=UTC)
        self.engine.process(snapshot(now=before), before)
        self.assertEqual(self.store.get("day"), "2026-11-04")
        self.engine.control("pause_buys", before)
        after = before + timedelta(minutes=1)
        self.engine.process(snapshot(now=after), after)
        self.assertEqual(self.store.get("day"), "2026-11-05")
        self.assertEqual(self.store.get("buy_halted"), "1")

    def test_financial_config_cannot_change_existing_account(self):
        with self.assertRaises(ValueError):
            Store(replace(self.settings, initial_cash=D("2000000")))

    def test_cannot_start_without_fresh_quote(self):
        with self.assertRaises(ValueError):
            self.engine.control("start", NOW)


if __name__ == "__main__":
    unittest.main()
