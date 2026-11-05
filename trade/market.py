import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

UTC = timezone.utc


@dataclass(frozen=True)
class Candle:
    time: datetime
    close: Decimal
    volume: Decimal = None


@dataclass(frozen=True)
class Snapshot:
    price: Decimal
    quote_time: datetime
    candles: List[Candle]
    market_context: dict = None


class MarketError(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = retry_after


class UpbitPublic:
    """Public GET only. No credentials or exchange order routes."""

    def _get(self, path):
        request = Request("https://api.upbit.com/v1/" + path,
                          headers={"Accept": "application/json", "User-Agent": "personal-btc-paper/0.1"})
        try:
            with urlopen(request, timeout=8) as response:
                return json.load(response, parse_float=Decimal)
        except HTTPError as exc:
            raw = exc.headers.get("Retry-After", "60" if exc.code == 418 else "30")
            try:
                delay = max(30, float(raw))
                if not math.isfinite(delay):
                    delay = 60
            except ValueError:
                delay = 60
            raise MarketError("업비트 HTTP 오류: {}".format(exc.code), delay) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise MarketError("업비트 시세를 가져오지 못했습니다.") from exc

    def fetch(self, minutes):
        # One request per group every >=10s, far below the public API limits.
        requested_at = datetime.now(UTC)
        epoch = int(requested_at.timestamp())
        boundary = datetime.fromtimestamp(epoch - epoch % (minutes * 60), UTC)
        query = urlencode({"market": "KRW-BTC", "count": 200, "to": boundary.isoformat()})
        rows = self._get("candles/minutes/{}?{}".format(minutes, query))
        tickers = self._get("ticker?markets=KRW-BTC")
        try:
            ticker = tickers[0]
            now = datetime.now(UTC)
            candles = []
            for row in rows:
                start = datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=UTC)
                close = Decimal(str(row["trade_price"]))
                if not close.is_finite() or close <= 0:
                    raise ValueError("invalid close")
                if start + timedelta(minutes=minutes) <= now:
                    volume=Decimal(str(row["candle_acc_trade_volume"])) if "candle_acc_trade_volume" in row else None
                    candles.append(Candle(start, close, volume))
            price = Decimal(str(ticker["trade_price"]))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid price")
            context={}
            if minutes == 1:
                try:
                    book=self._get("orderbook?markets=KRW-BTC")[0]
                    units=book["orderbook_units"]
                    bid=Decimal(str(units[0]["bid_price"]));ask=Decimal(str(units[0]["ask_price"]))
                    timestamp=datetime.fromtimestamp(int(book["timestamp"])/1000,UTC)
                    if not 0 <= (datetime.now(UTC)-timestamp).total_seconds() <= 10 or not 0<bid<=ask:
                        raise ValueError("stale orderbook")
                    bids=sum(Decimal(str(u["bid_size"])) for u in units[:5])
                    asks=sum(Decimal(str(u["ask_size"])) for u in units[:5])
                    context={"best_bid":str(bid),"best_ask":str(ask),"spread_pct":str((ask-bid)/bid*100),
                             "depth_imbalance_5":str((bids-asks)/(bids+asks)) if bids+asks else None,
                             "orderbook_time":timestamp.isoformat()}
                except (MarketError,KeyError,IndexError,TypeError,ValueError,ArithmeticError):
                    context={"orderbook_unavailable":True}
            return Snapshot(price, datetime.fromtimestamp(int(ticker["timestamp"]) / 1000, UTC),
                            sorted(candles, key=lambda c: c.time),context)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise MarketError("업비트 시세 응답 형식을 확인할 수 없습니다.") from exc
