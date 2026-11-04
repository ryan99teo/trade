from datetime import datetime, timedelta
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from .market import UTC
from .config import EDITABLE_FIELDS
from .strategy import moving_average_cross

SATOSHI = Decimal("0.00000001")


class Engine:
    def __init__(self, settings, store):
        self.settings = store.settings
        self.store = store
        self.running = False
        self.last_error = None
        self.last_received = None
        self.quote_time = None

    def configure(self, changes, now=None):
        if self.running:
            raise RuntimeError("거래를 먼저 정지한 뒤 설정을 저장하세요.")
        if not changes or not set(changes) <= set(EDITABLE_FIELDS):
            raise ValueError("변경 가능한 전략 설정을 확인하세요.")
        settings = replace(self.settings, **changes)
        self.store.save_strategy(settings, (now or datetime.now(UTC)).isoformat())
        self.settings = settings

    def equity(self, price):
        return self.store.number("cash") + self.store.number("btc") * price

    def fresh(self, now):
        return self.quote_time is not None and 0 <= (now - self.quote_time).total_seconds() <= self.settings.max_quote_age_seconds

    def control(self, action, now=None):
        now = now or datetime.now(UTC)
        if action == "start":
            if not self.fresh(now) or self.last_error:
                raise ValueError("최신 시세를 정상 수신한 후 시작할 수 있습니다.")
            self.running = True
        elif action == "stop":
            self.running = False
        elif action == "pause_buys":
            with self.store.db:
                self.store.set("buy_halted", "1")
        elif action == "resume_buys":
            if not self.fresh(now) or self.last_error:
                raise ValueError("최신 시세를 확인한 후 재개하세요.")
            baseline = self.store.number("day_baseline")
            if baseline and self.equity(self.store.number("price")) <= baseline * (1 - self.settings.daily_loss_limit):
                raise ValueError("일일 손실 한도에 도달했습니다.")
            with self.store.db:
                self.store.set("buy_halted", "0")
        else:
            raise ValueError("지원하지 않는 명령입니다.")
        with self.store.db:
            self.store.event(now.isoformat(), "control", action)

    def fail(self, message, now=None):
        self.running = False
        self.last_error = message
        with self.store.db:
            self.store.event((now or datetime.now(UTC)).isoformat(), "error", message)

    def process(self, snapshot, now=None):
        now = now or datetime.now(UTC)
        s, db = self.settings, self.store
        age = (now - snapshot.quote_time).total_seconds()
        if not snapshot.price.is_finite() or snapshot.price <= 0 or not 0 <= age <= s.max_quote_age_seconds:
            self.fail("시세가 오래되었거나 유효하지 않아 거래를 중지했습니다.", now)
            return
        self.quote_time = snapshot.quote_time
        self.last_received = now
        self.last_error = None
        # Filter again so injected/replayed snapshots cannot introduce unfinished bars.
        candles = sorted((c for c in snapshot.candles
                          if c.time + timedelta(minutes=s.candle_minutes) <= now), key=lambda c: c.time)
        with db.db:
            db.set("price", snapshot.price)
            for candle in candles:
                db.db.execute("INSERT INTO candles VALUES (?,?) ON CONFLICT(time) DO UPDATE SET close=excluded.close",
                              (candle.time.isoformat(), str(candle.close)))
            equity = self.equity(snapshot.price)
            day = now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            if db.get("day") != day:
                # If data missed midnight, baseline is the first valid observation of this day.
                db.set("day", day)
                db.set("day_baseline", equity)
            baseline = db.number("day_baseline")
            if equity <= baseline * (1 - s.daily_loss_limit) and db.get("buy_halted") != "1":
                db.set("buy_halted", "1")
                db.event(now.isoformat(), "risk", "일일 손실 한도 도달: 신규 매수 중지")
            db.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)",
                          (now.isoformat(), str(equity), str(snapshot.price)))
            if not self.running or not candles:
                return
            candle = candles[-1]
            candle_id = candle.time.isoformat()
            if db.db.execute("SELECT 1 FROM decisions WHERE candle=?", (candle_id,)).fetchone():
                return
            action, reason = moving_average_cross(candles, s.short_window, s.long_window, s.candle_minutes)
            signal_age = (now - candle.time - timedelta(minutes=s.candle_minutes)).total_seconds()
            if signal_age > 120:
                action, reason = "hold", "캔들 확정 후 2분이 지나 과거 신호를 건너뜁니다."
            if action != "hold":
                reason += " " + self._execute(action, snapshot.price, candle_id, now)
            db.db.execute("INSERT INTO decisions(time,candle,action,reason) VALUES (?,?,?,?)",
                          (now.isoformat(), candle_id, action, reason))

    def _execute(self, side, mark, identifier, now):
        # Called inside the decision transaction: fill + balance + deduplication commit together.
        s, db = self.settings, self.store
        cash, btc, cost = db.number("cash"), db.number("btc"), db.number("cost")
        price = mark * (1 + s.slippage if side == "buy" else 1 - s.slippage)
        if side == "buy":
            if db.get("buy_halted") == "1":
                return "위험 관리: 신규 매수가 중지되어 있습니다."
            # Exposure cap includes projected fees/slippage loss in the equity denominator.
            cap = max(Decimal(0), (s.max_exposure * self.equity(mark) - btc * mark) /
                      (mark + s.max_exposure * (price * (1 + s.fee_rate) - mark)))
            quantity = min(s.order_krw / price, cash / (price * (1 + s.fee_rate)), cap).quantize(SATOSHI, rounding=ROUND_DOWN)
            notional = quantity * price
            if notional < 5000:
                return "위험 관리: 잔고·보유 한도 또는 최소 모의 주문 금액 미충족."
            fee = notional * s.fee_rate
            db.set("cash", cash - notional - fee)
            db.set("btc", btc + quantity)
            db.set("cost", cost + notional + fee)
            realized = Decimal(0)
        else:
            quantity = btc
            notional = quantity * price
            if notional < 5000:
                return "매도 가능한 BTC가 없거나 최소 모의 주문 금액 미충족."
            fee = notional * s.fee_rate
            realized = notional - fee - cost
            db.set("cash", cash + notional - fee)
            db.set("btc", "0")
            db.set("cost", "0")
            db.set("realized", db.number("realized") + realized)
        db.db.execute("INSERT INTO fills(time,identifier,side,price,quantity,fee,realized_pnl) VALUES (?,?,?,?,?,?,?)",
                      (now.isoformat(), identifier, side, str(price), str(quantity), str(fee), str(realized)))
        return "모의 체결 완료."

    def status(self):
        db, s = self.store, self.settings
        price = db.number("price")
        equity = self.equity(price)
        baseline = db.number("day_baseline")
        return {"mode": "paper", "market": "KRW-BTC", "running": self.running,
                "buy_halted": db.get("buy_halted") == "1", "last_error": self.last_error,
                "quote_fresh": self.fresh(datetime.now(UTC)),
                "last_received": self.last_received.isoformat() if self.last_received else None,
                "quote_time": self.quote_time.isoformat() if self.quote_time else None,
                "cash": str(db.number("cash")), "btc": str(db.number("btc")), "price": str(price),
                "equity": str(equity), "total_pnl": str(equity - s.initial_cash),
                "realized_pnl": str(db.number("realized")),
                "unrealized_pnl": str(db.number("btc") * price - db.number("cost")),
                "daily_pnl": str(equity - baseline) if baseline else "0",
                "settings": {"initial_cash": str(s.initial_cash), "order_krw": str(s.order_krw),
                             "max_exposure": str(s.max_exposure), "daily_loss_limit": str(s.daily_loss_limit),
                             "fee_rate": str(s.fee_rate), "slippage": str(s.slippage),
                             "short_window": s.short_window, "long_window": s.long_window,
                             "candle_minutes": s.candle_minutes, "poll_seconds": s.poll_seconds},
                "fills": db.recent("fills"), "decisions": db.recent("decisions"),
                "events": db.recent("events", 30), "history": list(reversed(db.recent("equity", 240)))}
