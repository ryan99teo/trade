"""Public one-second stream aggregated into immutable, closed 10/30-second bars."""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
import uuid

from .market import Candle, MarketError, Snapshot, UTC


def book_context(book, now):
    stamp = datetime.fromtimestamp(int(book['timestamp']) / 1000, UTC)
    units = book['orderbook_units'][:5]
    bid, ask = Decimal(str(units[0]['bid_price'])), Decimal(str(units[0]['ask_price']))
    bids = sum(Decimal(str(u['bid_size'])) for u in units)
    asks = sum(Decimal(str(u['ask_size'])) for u in units)
    if not all(v.is_finite() for v in (bid, ask, bids, asks)) or not 0 < bid <= ask or min(bids, asks) < 0:
        raise ValueError('invalid orderbook')
    if not 0 <= (now - stamp).total_seconds() <= 10:
        raise ValueError('stale orderbook')
    return {'best_bid': str(bid), 'best_ask': str(ask), 'spread_pct': str((ask-bid)/bid*100),
            'depth_imbalance_5': str((bids-asks)/(bids+asks)) if bids+asks else None,
            'orderbook_time': stamp.isoformat(), 'source': 'websocket'}


class SecondBars:
    def __init__(self, connected_at):
        self.connected_at = connected_at
        self.rows = {}
        self.closed = {10: {}, 30: {}}
        self.watermark = {10: None, 30: None}
        self.quote = None
        self.book = None

    def ingest(self, message, now):
        if message.get('code') != 'KRW-BTC':
            return
        kind = message.get('type')
        if kind == 'orderbook':
            self.book = message
            return
        if kind not in ('ticker', 'candle.1s'):
            return
        price = Decimal(str(message['trade_price']))
        stamp = datetime.fromtimestamp(int(message['timestamp'])/1000, UTC)
        if not price.is_finite() or price <= 0 or stamp > now + timedelta(seconds=2):
            raise MarketError('초봉 스트림 가격·시각이 유효하지 않습니다.')
        if kind == 'ticker':
            if self.quote is None or stamp >= self.quote[1]:
                self.quote = (price, stamp)
            return
        start = datetime.fromisoformat(message['candle_date_time_utc']).replace(tzinfo=UTC)
        volume = Decimal(str(message['candle_acc_trade_volume']))
        if start.microsecond or not volume.is_finite() or volume < 0 or start > stamp:
            raise MarketError('초봉 스트림 캔들이 유효하지 않습니다.')
        old = self.rows.get(start)
        if old is None or stamp >= old[2]:
            self.rows[start] = (price, volume, stamp)
        cutoff = now - timedelta(hours=2)
        if len(self.rows) > 7200:
            self.rows = {t: v for t, v in self.rows.items() if t >= cutoff}

    def snapshot(self, seconds, now):
        if seconds not in self.closed:
            raise ValueError('10/30 second bars only')
        if self.quote is None:
            return None
        groups = defaultdict(list)
        watermark = self.watermark[seconds]
        for start, row in self.rows.items():
            bucket = datetime.fromtimestamp(int(start.timestamp())//seconds*seconds, UTC)
            # Skip the partial bucket at connect and allow one second for final updates.
            if bucket < self.connected_at or bucket + timedelta(seconds=seconds+1) > now:
                continue
            if watermark is None or bucket > watermark:
                groups[bucket].append((start, row))
        closed = self.closed[seconds]
        for bucket, rows in sorted(groups.items()):
            rows.sort(key=lambda item: item[0])
            closed[bucket] = Candle(bucket, rows[-1][1][0], sum((r[1][1] for r in rows), Decimal(0)))
        if groups:
            self.watermark[seconds] = max(groups)
        if len(closed) > 200:
            self.closed[seconds] = closed = dict(sorted(closed.items())[-200:])
        context = {'orderbook_unavailable': True, 'source': 'websocket'}
        if self.book:
            try:
                context = book_context(self.book, now)
            except (ValueError, KeyError, IndexError, TypeError, ArithmeticError):
                pass
        return Snapshot(self.quote[0], self.quote[1], [closed[t] for t in sorted(closed)], context)


class UpbitSeconds:
    endpoint = 'wss://api.upbit.com/websocket/v1'

    async def run(self, active_units, publish, fail):
        from websockets.asyncio.client import connect
        retry = 1
        while True:
            if not active_units():
                await asyncio.sleep(1)
                continue
            receiver = None
            try:
                async with connect(self.endpoint, open_timeout=8, ping_interval=20, ping_timeout=10,
                                   close_timeout=3, max_queue=1024) as socket:
                    feed = SecondBars(datetime.now(UTC))
                    await socket.send(json.dumps([{'ticket': 'btc-seconds-'+uuid.uuid4().hex},
                                                  *({'type': kind, 'codes': ['KRW-BTC']} for kind in
                                                    ('candle.1s', 'ticker', 'orderbook'))]))

                    async def receive():
                        async for raw in socket:
                            message = json.loads(raw, parse_float=Decimal)
                            if 'error' in message:
                                raise MarketError('업비트 초봉 구독 오류입니다.')
                            feed.ingest(message, datetime.now(UTC))
                        raise MarketError('초봉 스트림 연결이 종료됐습니다.')

                    receiver = asyncio.create_task(receive())
                    retry = 1
                    warned = False
                    while active_units():
                        if receiver.done():
                            await receiver
                        now = datetime.now(UTC)
                        units = active_units()
                        if feed.quote is None and (now-feed.connected_at).total_seconds() > 10 and not warned:
                            fail('초봉 스트림의 최신 시세를 받지 못해 거래를 정지했습니다.')
                            warned = True
                        for seconds in units:
                            snapshot = feed.snapshot(seconds, now)
                            if snapshot is not None:
                                if 0 <= (now-snapshot.quote_time).total_seconds() <= seconds:
                                    warned = False
                                    publish(seconds, snapshot)
                                elif not warned:
                                    fail('초봉 스트림 시세가 지연되어 거래를 정지했습니다.')
                                    warned = True
                        await asyncio.sleep(.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                fail('초봉 스트림 연결 오류로 거래를 정지했습니다. 재연결 후 직접 재개하세요.')
                await asyncio.sleep(retry)
                retry = min(30, retry * 2)
            finally:
                if receiver is not None:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
