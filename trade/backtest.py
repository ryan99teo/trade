"""Historical replay with next-bar-open fills and isolated accounting."""
import csv
import calendar
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta
from decimal import Decimal, ROUND_DOWN
import io
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
import uuid
from zoneinfo import ZoneInfo
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings, STRATEGY_NAMES
from .engine import Engine, SATOSHI
from .market import Candle, MarketError, Snapshot, UTC
from .storage import Store

KST = ZoneInfo("Asia/Seoul")
D = Decimal


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date
    initial_cash: Decimal = Field(default=D("1000000"), ge=5000, le=100000000000)
    order_krw: Decimal = Field(default=D("100000"), ge=5000)
    short_window: int = Field(default=5, ge=1, le=198)
    long_window: int = Field(default=20, ge=2, le=199)
    fee_rate: Decimal = Field(default=D("0.0005"), ge=0, lt=D("0.1"))
    slippage: Decimal = Field(default=D("0.001"), ge=0, lt=D("0.1"))
    max_exposure: Decimal = Field(default=D("0.30"), gt=0, le=1)
    daily_loss_limit: Decimal = Field(default=D("0.03"), gt=0, lt=1)
    strategy: Literal["ma_cross","dca","rebalance","consecutive"] = "ma_cross"
    dca_interval_hours: int = Field(default=24,ge=1,le=168)
    dca_offset_minutes: int = Field(default=0, ge=0, le=10079)
    target_exposure: Decimal = Field(default=D("0.20"),gt=0,le=1)
    rebalance_tolerance: Decimal = Field(default=D("0.02"),gt=0,lt=1)

    consecutive_seconds: Literal[10,30,60] = 60
    consecutive_count: int = Field(default=3,ge=1,le=199,strict=True)
    consecutive_entry_return: Decimal = Field(default=D("0.005"),gt=0,lt=1)
    consecutive_take_profit: Decimal = Field(default=D("0.03"),gt=0,lt=1)
    consecutive_stop_loss: Decimal = Field(default=D("0.10"),gt=0,lt=1)

    @property
    def minutes(self):return 1 if self.strategy == "consecutive" else 60

    @property
    def seconds(self):return self.consecutive_seconds if self.strategy == "consecutive" else 3600

    @property
    def warmup_count(self):return max(21,self.consecutive_count+1) if self.minutes==1 else self.long_window+1

    def bounds(self, now=None):
        today = (now or datetime.now(UTC)).astimezone(KST).date()
        if not self.start_date <= self.end_date < today:
            raise ValueError("시작일 ≤ 종료일이어야 하며 종료일은 한국시간 어제까지 가능합니다.")
        if (self.end_date - self.start_date).days >= 365:
            raise ValueError("한 번에 최대 365일을 검증할 수 있습니다.")
        if self.minutes==1 and (self.end_date-self.start_date).days>=7:
            raise ValueError("1분봉 연속 전략은 한 번에 최대 7일까지 검증할 수 있습니다.")
        if self.seconds < 60:
            if self.start_date != self.end_date:
                raise ValueError("초봉 백테스트는 한 번에 하루만 검증할 수 있습니다.")
            current = now or datetime.now(UTC)
            month = current.year * 12 + current.month - 4
            year, index = divmod(month, 12)
            cutoff = current.replace(year=year, month=index+1,
                                     day=min(current.day, calendar.monthrange(year,index+1)[1]))
            start = datetime.combine(self.start_date, day_time(), KST).astimezone(UTC)
            if start - timedelta(seconds=self.seconds*self.warmup_count) < cutoff:
                raise ValueError("초봉은 준비 구간을 포함해 최근 3개월 데이터만 조회할 수 있습니다.")
        # Shared validation prevents divergence from the paper account rules.
        self.settings()
        start = datetime.combine(self.start_date, day_time(), KST).astimezone(UTC)
        end = datetime.combine(self.end_date + timedelta(days=1), day_time(), KST).astimezone(UTC)
        return start, end

    def settings(self):
        return Settings(db_path=Path(":memory:"), initial_cash=self.initial_cash, order_krw=self.order_krw,
                        short_window=self.short_window, long_window=self.long_window,
                        fee_rate=self.fee_rate, slippage=self.slippage, max_exposure=self.max_exposure,
                        daily_loss_limit=self.daily_loss_limit, candle_minutes=self.minutes,strategy=self.strategy,
                        dca_interval_hours=self.dca_interval_hours,dca_offset_minutes=self.dca_offset_minutes,
                        target_exposure=self.target_exposure,rebalance_tolerance=self.rebalance_tolerance,
                        consecutive_seconds=self.consecutive_seconds,consecutive_count=self.consecutive_count,consecutive_entry_return=self.consecutive_entry_return,consecutive_take_profit=self.consecutive_take_profit,
                        consecutive_stop_loss=self.consecutive_stop_loss)


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def decode_bar(row, minutes=60, seconds=None):
    seconds = seconds if seconds is not None else minutes * 60
    try:
        timestamp = datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=UTC)
        values = [D(str(row[key])) for key in ("opening_price", "high_price", "low_price", "trade_price", "candle_acc_trade_volume")]
        opening, high, low, close, volume = values
        if not all(v.is_finite() for v in values) or min(values[:4]) <= 0 or volume < 0:
            raise ValueError("invalid price/volume")
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise ValueError("invalid OHLC")
        if int(timestamp.timestamp()) % seconds or timestamp.microsecond:
            raise ValueError("unaligned hourly bar")
        return Bar(timestamp, *values)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise MarketError("과거 캔들 데이터 형식이 올바르지 않습니다.") from exc


class BacktestArchive:
    """Each instance owns its connection in the thread where it is created."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS history (
          time TEXT PRIMARY KEY, open TEXT NOT NULL, high TEXT NOT NULL,
          low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, request TEXT NOT NULL,
          summary TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS coverage (start TEXT NOT NULL, end TEXT NOT NULL,
          PRIMARY KEY(start,end));
        CREATE TABLE IF NOT EXISTS history_1m (
          time TEXT PRIMARY KEY, open TEXT NOT NULL, high TEXT NOT NULL,
          low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history_1s (
          time TEXT PRIMARY KEY, open TEXT NOT NULL, high TEXT NOT NULL,
          low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS coverage_1s (start TEXT NOT NULL,end TEXT NOT NULL,PRIMARY KEY(start,end));
        CREATE TABLE IF NOT EXISTS coverage_1m (start TEXT NOT NULL,end TEXT NOT NULL,PRIMARY KEY(start,end));
        """)

    def close(self):
        self.db.close()

    @staticmethod
    def tables(minutes, seconds=None):
        if seconds==1:return "history_1s","coverage_1s"
        if seconds is not None:raise ValueError("원시 초봉 캐시는 1초 단위만 지원합니다.")
        if minutes==1:return "history_1m","coverage_1m"
        if minutes==60:return "history","coverage"
        raise ValueError("지원하지 않는 백테스트 캔들 단위입니다.")

    def bars(self, start, end, minutes=60, seconds=None):
        table,_=self.tables(minutes, seconds)
        rows = self.db.execute("SELECT * FROM {} WHERE time>=? AND time<? ORDER BY time".format(table),
                               (start.isoformat(), end.isoformat()))
        return [Bar(datetime.fromisoformat(r["time"]), *(D(r[k]) for k in ("open", "high", "low", "close", "volume"))) for r in rows]

    def save_bars(self, bars, minutes=60, seconds=None):
        table,_=self.tables(minutes, seconds)
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO {} VALUES (?,?,?,?,?,?)".format(table),
                                [(b.time.isoformat(), *(str(v) for v in (b.open, b.high, b.low, b.close, b.volume))) for b in bars])

    def covered(self, start, end, minutes=60, seconds=None):
        _,table=self.tables(minutes, seconds)
        return self.db.execute("SELECT 1 FROM {} WHERE start<=? AND end>=? LIMIT 1".format(table),
                               (start.isoformat(), end.isoformat())).fetchone() is not None

    def mark_covered(self, start, end, minutes=60, seconds=None):
        _,table=self.tables(minutes, seconds)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO {} VALUES (?,?)".format(table), (start.isoformat(), end.isoformat()))

    def save_result(self, request, result):
        result["id"] = uuid.uuid4().hex
        result["created_at"] = datetime.now(UTC).isoformat()
        result["request"] = request.model_dump(mode="json")
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                            (result["id"], result["created_at"], json.dumps(result["request"]),
                             json.dumps(result["summary"]), json.dumps(result)))
        return result

    def runs(self):
        return [{"id": r["id"], "created_at": r["created_at"], "request": json.loads(r["request"]),
                 "summary": json.loads(r["summary"])} for r in
                self.db.execute("SELECT id,created_at,request,summary FROM runs ORDER BY rowid DESC LIMIT 30")]

    def result(self, run_id):
        row = self.db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None


def complete(bars, start, end, minutes=60, seconds=None):
    seconds = seconds if seconds is not None else minutes * 60
    expected = int((end - start).total_seconds() // seconds)
    return (len(bars) == expected and expected > 0 and bars[0].time == start
            and bars[-1].time == end - timedelta(seconds=seconds)
            and all(b.time - a.time == timedelta(seconds=seconds) for a, b in zip(bars, bars[1:])))


def collect_history(market, archive, start, end, pause=time.sleep, minutes=60, seconds=None):
    step = seconds if seconds is not None else minutes * 60
    cached = archive.bars(start, end, minutes, seconds=seconds)
    if complete(cached, start, end, minutes, seconds=seconds) or archive.covered(start, end, minutes, seconds=seconds):
        return cached, 0
    cursor, requests, exhausted = end, 0, False
    # Bounded hourly range (<=365 days + warmup); never loop on an unchanged cursor.
    page_limit = (int((end-start).total_seconds() // step) + 199) // 200 + 2
    while cursor > start and requests < page_limit:
        query = urlencode({"market": "KRW-BTC", "count": 200, "to": cursor.isoformat()})
        rows = market._get(("candles/seconds?" if seconds == 1 else "candles/minutes/{}?".format(minutes)) + query)
        requests += 1
        if not isinstance(rows, list):
            raise MarketError("과거 캔들 목록 형식이 올바르지 않습니다.")
        if not rows:
            exhausted = True
            break
        bars = [decode_bar(row, minutes, seconds=seconds) for row in rows]
        if any(b.time >= cursor for b in bars):
            raise MarketError("과거 캔들의 페이지 경계가 올바르지 않습니다.")
        archive.save_bars([b for b in bars if start <= b.time < end], minutes, seconds=seconds)
        cursor = min(b.time for b in bars)
        if cursor > start:
            pause(.3)  # At most ~3.3 history requests/s; paper collection remains separate.
    bars = archive.bars(start, end, minutes, seconds=seconds)
    if not bars or (cursor > start and not exhausted):
        raise ValueError("요청 기간의 캔들 수집을 완료하지 못했습니다. 기간을 변경하거나 다시 수집하세요.")
    # Cache completed traversals, including genuine exchange gaps.
    archive.mark_covered(start, end, minutes, seconds=seconds)
    return bars, requests


def aggregate_bars(bars, seconds):
    """REST range is fully traversed: absent seconds mean no trades, not made-up prices."""
    groups = {}
    for bar in bars:
        start = datetime.fromtimestamp(int(bar.time.timestamp()) // seconds * seconds, UTC)
        groups.setdefault(start, []).append(bar)
    return [Bar(start, group[0].open, max(b.high for b in group), min(b.low for b in group),
                group[-1].close, sum((b.volume for b in group), D(0))) for start, group in sorted(groups.items())]


def replay(request, bars):
    start, end = request.bounds()
    minutes=request.minutes
    seconds=request.seconds
    warmup = start - timedelta(seconds=seconds*request.warmup_count)
    if not bars or any(b.time < warmup or b.time >= end or int(b.time.timestamp()) % seconds or b.time.microsecond for b in bars):
        raise ValueError("백테스트 캔들 범위 또는 시간 단위가 올바르지 않습니다.")
    if any(b.time <= a.time for a,b in zip(bars,bars[1:])):
        raise ValueError("백테스트 캔들은 중복 없이 시간순이어야 합니다.")
    trade_indices = [i for i,b in enumerate(bars) if b.time >= start]
    if not trade_indices:
        raise ValueError("선택한 기간에 거래 가능한 캔들이 없습니다.")
    settings = request.settings()
    store = Store(settings)
    try:
        engine = Engine(settings, store)
        engine.running = True
        engine.require_gate = False  # Historical rule baseline: never call current AI on past data.
        initial = request.initial_cash
        first = bars[trade_indices[0]]
        benchmark_price = first.open * (1 + request.slippage)
        benchmark_qty = (initial / (benchmark_price * (1 + request.fee_rate))).quantize(SATOSHI, rounding=ROUND_DOWN)
        benchmark_cash = initial - benchmark_qty * benchmark_price * (1 + request.fee_rate)
        peak, benchmark_peak = initial, initial
        mdd, benchmark_mdd = D(0), D(0)
        curve = [{"time": start.isoformat(), "equity": str(initial), "benchmark": str(initial), "drawdown_pct": "0"}]
        for index in trade_indices:
            bar = bars[index]
            previous = [Candle(b.time, b.close, b.volume) for b in bars[max(0,index-request.warmup_count):index]]
            before_fill = engine.equity(bar.open)
            # Current close/high/low are deliberately withheld until after execution.
            engine.process(Snapshot(bar.open, bar.time, previous), bar.time)
            # Include open and post-fill observations as well as closes in drawdown.
            open_equity = engine.equity(bar.open)
            open_benchmark = benchmark_cash + benchmark_qty * bar.open
            for value, reference in ((before_fill, open_benchmark), (open_equity, open_benchmark),
                                     (engine.equity(bar.close), benchmark_cash + benchmark_qty * bar.close)):
                peak, benchmark_peak = max(peak, value), max(benchmark_peak, reference)
                mdd = max(mdd, (peak - value) / peak)
                benchmark_mdd = max(benchmark_mdd, (benchmark_peak - reference) / benchmark_peak)
            value = engine.equity(bar.close)
            curve.append({"time": (bar.time + timedelta(seconds=seconds)).isoformat(), "equity": str(value),
                          "benchmark": str(benchmark_cash + benchmark_qty * bar.close),
                          "drawdown_pct": str((peak - value) / peak * 100)})
        fills = [dict(r) for r in store.db.execute("SELECT * FROM fills ORDER BY id")]
        sells = [f for f in fills if f["side"] == "sell"]
        wins = sum(D(f["realized_pnl"]) > 0 for f in sells)
        final = engine.equity(bars[-1].close)
        benchmark_final = benchmark_cash + benchmark_qty * bars[-1].close
        summary = {
            "initial_equity": str(initial), "final_equity": str(final),
            "total_pnl": str(final - initial), "return_pct": str((final / initial - 1) * 100),
            "max_drawdown_pct": str(mdd * 100), "benchmark_final_equity": str(benchmark_final),
            "benchmark_return_pct": str((benchmark_final / initial - 1) * 100),
            "benchmark_max_drawdown_pct": str(benchmark_mdd * 100),
            "excess_return_pp": str((final - benchmark_final) / initial * 100),
            "fill_count": len(fills), "closed_trades": len(sells), "winning_trades": wins,
            "win_rate_pct": str(D(wins) / len(sells) * 100) if sells else None,
            "fees": str(sum((D(f["fee"]) for f in fills), D(0))),
            "realized_pnl": str(store.number("realized")),
            "unrealized_pnl": str(store.number("btc") * bars[-1].close - store.number("cost")),
            "cash": str(store.number("cash")), "btc": str(store.number("btc")),
            "buy_halted": store.get("buy_halted") == "1", "candle_count": len(trade_indices),
            "missing_candles": int((end-start).total_seconds()//seconds)-len(trade_indices),
            "missing_warmup_candles": request.warmup_count-trade_indices[0],
            "observed_start": first.time.isoformat(), "observed_end": (bars[-1].time+timedelta(seconds=seconds)).isoformat(),
        }
        return {"summary": summary, "curve": curve, "fills": fills,
                "risk_events": [dict(r) for r in store.db.execute("SELECT * FROM events ORDER BY id")],
                "notes": ["확정 {}봉 신호 → 다음 봉 시가에 수수료·슬리피지를 적용해 전량 체결합니다.".format(str(seconds)+"초" if seconds<60 else str(seconds//60)+"분"),
                          "이 결과는 룰 단독 백테스트입니다. 과거 데이터에 현재 Jev를 호출하지 않으며 Jev 승인 전략의 성과를 뜻하지 않습니다.",
                          "손절은 관측된 봉 시가에서만 검사합니다. 봉 내부의 손절 가격 도달이나 실시간 체결을 재현하지 않습니다.",
                          "준비 구간은 성과에서 제외합니다. 종료 시 BTC는 강제 매도하지 않고 종가로 평가합니다.",
                          "단순 보유는 동일 초기 자금 전액을 첫 시가에 매수하고 동일 비용을 적용합니다. 전략의 최대 BTC 비중과는 다릅니다.",
                          "승률은 수수료·배분 원가를 반영한 매도 체결 기준입니다. 일부 매도도 1회로 계산하며 매도가 없으면 미정입니다.",
                          "최대 낙폭은 시가·체결 직후·종가 관측 기준이며 봉 내부의 가격 경로는 재현하지 않습니다.",
                          "누락 캔들은 보간하지 않습니다. 누락 전후에는 연속 캔들이 충분할 때까지 신호를 보류하며 미관측 구간의 손실은 측정할 수 없습니다.",
                          "일일 손실은 시가에서 검사하며, 한도 도달 후 신규 매수 중지는 테스트 종료까지 유지됩니다."]}
    finally:
        store.db.close()


def run_backtest(path, market, request):
    start, end = request.bounds()
    archive = BacktestArchive(path)
    try:
        warmup = start - timedelta(seconds=request.seconds*request.warmup_count)
        if request.seconds < 60:
            raw, calls = collect_history(market, archive, warmup, end, seconds=1)
            bars = aggregate_bars(raw, request.seconds)
        else:
            bars, calls = collect_history(market, archive, warmup, end, minutes=request.minutes)
        result = replay(request, bars)
        result["data"] = {"source": "Upbit KRW-BTC", "candle_seconds": request.seconds, "candle_minutes": request.seconds/60, "network_requests": calls, "evaluation": "rule_only",
                          "warmup_candles": sum(b.time < start for b in bars),
                          "start": start.isoformat(), "end_exclusive": end.isoformat()}
        return archive.save_result(request, result)
    finally:
        archive.close()


def read_archive(path, run_id=None):
    archive = BacktestArchive(path)
    try:
        return archive.result(run_id) if run_id is not None else archive.runs()
    finally:
        archive.close()


def compare_backtests(path, market, request):
    if request.strategy == "consecutive":raise ValueError("봉 연속 전략은 단일 룰 백테스트로 실행하세요. 기존 세 전략 비교는 1시간봉입니다.")
    trials = [request.model_copy(update={"strategy":name}) for name in ("ma_cross","dca","rebalance")]
    for trial in trials:
        trial.bounds()
    start,end = request.bounds()
    archive = BacktestArchive(path)
    try:
        bars,calls = collect_history(market,archive,start-timedelta(hours=request.long_window+1),end)
        results=[]
        for trial in trials:
            result=replay(trial,bars)
            result["data"]={"source":"Upbit KRW-BTC","candle_minutes":60,"network_requests":calls if not results else 0,
                            "warmup_candles":sum(b.time<start for b in bars),"start":start.isoformat(),"end_exclusive":end.isoformat()}
            results.append(archive.save_result(trial,result))
        return {"runs":results}
    finally:
        archive.close()


def fills_csv(result):
    output = io.StringIO(newline="")
    fields = ["time", "side", "price", "quantity", "fee", "realized_pnl", "identifier"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result["fills"])
    return "\ufeff" + output.getvalue()
