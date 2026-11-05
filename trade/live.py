"""Isolated, opt-in live trading with durable order intents and incremental fills."""
import asyncio
import fcntl
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import uuid

from .config import EDITABLE_FIELDS
from .engine import Engine
from .exchange import ExchangeError
from .market import UTC
from .storage import Store

D=Decimal
ACTIVE=("prepared","submitting","unknown","wait","watch","cancel_pending")


def amount(value):
    result=D(str(value))
    if not result.is_finite() or result<0:raise ValueError("유효하지 않은 거래소 수치입니다.")
    return result


class IntentEngine(Engine):
    def _execute(self, side, mark, identifier, now, force=False):
        pending=self.store.db.execute("SELECT * FROM live_orders WHERE state IN ('prepared','submitting','unknown','wait','watch','cancel_pending')").fetchone()
        if pending:
            if force and pending["side"]=="sell" and pending["state"]=="prepared":
                self.store.db.execute("UPDATE live_orders SET purpose='stop_loss' WHERE identifier=?",(pending["identifier"],))
                return "미전송 매도 주문을 손절로 전환했습니다."
            return "미해결 실거래 주문이 있어 새 주문을 보류합니다."
        if side=="buy" and self.store.get("buy_halted")=="1":return "신규 매수가 중지되어 있습니다."
        self.store.db.execute("INSERT INTO live_orders(identifier,candle,side,created_at,state,mark,purpose) VALUES (?,?,?,?,?,?,?)",
                              ("btc-"+uuid.uuid4().hex,identifier,side,now.isoformat(),"prepared",str(mark),"stop_loss" if force else "strategy"))
        return "실거래 주문 의도를 저장했습니다. 거래소 검증 후 접수합니다."


class LiveService:
    DEFAULT_BUDGET = D("100000")
    DEFAULT_ORDER_CAP = D("10000")

    def __init__(self, paper_settings, client, budget=None, order_cap=None):
        self.client=client
        self.budget=amount(budget if budget is not None else self.DEFAULT_BUDGET)
        self.order_cap=amount(order_cap if order_cap is not None else self.DEFAULT_ORDER_CAP)
        if not 5000<=self.order_cap<=self.budget:raise ValueError("실거래 예산·주문 한도는 5,000원 이상이며 주문 한도는 예산 이하여야 합니다.")
        settings=replace(paper_settings,db_path=paper_settings.db_path.with_name(paper_settings.db_path.stem+"-live.sqlite3"),
                         initial_cash=self.budget,consecutive_seconds=60,order_krw=min(paper_settings.order_krw,self.order_cap))
        settings.db_path.parent.mkdir(parents=True,exist_ok=True)
        self._process_lock=open(str(settings.db_path)+".lock","a")
        try:
            fcntl.flock(self._process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Legacy live overrides predate the editable timeframe. Keep the live ledger's unit.
            if settings.db_path.exists():
                with closing(sqlite3.connect(str(settings.db_path))) as saved:
                    rows=dict(saved.execute("SELECT key,value FROM state WHERE key IN ('config','strategy_overrides')"))
                overrides=json.loads(rows.get("strategy_overrides", "{}"))
                config=json.loads(rows.get("config", "{}"))
                if "candle_minutes" not in overrides and "candle_minutes" in config:
                    settings=replace(settings,candle_minutes=int(config["candle_minutes"]),
                                     strategy=overrides.get("strategy",config.get("strategy",settings.strategy)))
            self.store=Store(settings)
        except Exception:
            self._process_lock.close()
            raise
        self.store.notification_mode="live"
        self.store.db.executescript("""
        CREATE TABLE IF NOT EXISTS live_orders (
          identifier TEXT PRIMARY KEY,candle TEXT UNIQUE NOT NULL,side TEXT NOT NULL,created_at TEXT NOT NULL,
          state TEXT NOT NULL,mark TEXT NOT NULL,uuid TEXT,params TEXT,
          quantity TEXT NOT NULL DEFAULT '0',funds TEXT NOT NULL DEFAULT '0',fee TEXT NOT NULL DEFAULT '0',
          last_error TEXT);
        """)
        if "purpose" not in {r["name"] for r in self.store.db.execute("PRAGMA table_info(live_orders)")}:
            self.store.db.execute("ALTER TABLE live_orders ADD COLUMN purpose TEXT NOT NULL DEFAULT 'strategy'")
            self.store.db.commit()
        # Snapshot strategy selection separately; later paper edits do not change live settings.
        if self.store.get("strategy_overrides") is None:
            with self.store.db:
                self.store.set("strategy_overrides",json.dumps({k:str(getattr(self.store.settings,k)) for k in EDITABLE_FIELDS}))
        self.engine=IntentEngine(self.store.settings,self.store)
        self.error=None
        self.connected_at=None
        self.tested_at=None
        self.chance=None
        self.tick=None
        self._lock=None
        self._restore_exchange()
        self.binding_ok=not self.store.get("exchange_binding") or self.store.get("exchange_binding")==self.client.binding

    def _restore_exchange(self):
        """Apply UI-saved credentials from the live ledger. Leave injected test clients alone when unset."""
        access = self.store.get("upbit_access_key")
        secret = self.store.get("upbit_secret_key")
        enabled = self.store.get("live_enabled")
        if access is None and secret is None and enabled is None:
            return
        if not hasattr(self.client, "configure"):
            return
        kwargs = {}
        if access is not None:
            kwargs["access"] = access
        if secret is not None:
            kwargs["secret"] = secret
        if enabled is not None:
            kwargs["enabled"] = enabled == "1"
        self.client.configure(**kwargs)

    def configure_exchange(self, *, enabled, access_key=None, secret_key=None):
        if self.engine.running or self.active():
            raise ValueError("실거래를 정지하고 미해결 주문을 확인한 뒤 연동 설정을 변경하세요.")
        if not hasattr(self.client, "configure"):
            raise ValueError("업비트 연동 클라이언트를 설정할 수 없습니다.")
        access = (access_key if access_key is not None and str(access_key).strip()
                  else self.store.get("upbit_access_key", "") or "")
        secret = (secret_key if secret_key is not None and str(secret_key).strip()
                  else self.store.get("upbit_secret_key", "") or "")
        access, secret = str(access).strip(), str(secret).strip()
        if bool(access) != bool(secret):
            raise ValueError("Access Key와 Secret Key를 함께 설정하세요.")
        if enabled and not (access and secret):
            raise ValueError("실거래를 켜려면 업비트 API 키가 필요합니다.")
        new_binding = hashlib.sha256(access.encode()).hexdigest() if access and secret else None
        stored_binding = self.store.get("exchange_binding")
        if stored_binding and new_binding and stored_binding != new_binding:
            raise ValueError("기존 실거래 기록과 API 키가 다릅니다. 기존 계좌 확인이 필요합니다.")
        now = datetime.now(UTC).isoformat()
        with self.store.db:
            self.store.set("upbit_access_key", access)
            self.store.set("upbit_secret_key", secret)
            self.store.set("live_enabled", "1" if enabled else "0")
            self.store.event(now, "live_settings",
                             "업비트 연동 설정 변경: 실거래 {} · API 키 {}".format(
                                 "활성" if enabled else "비활성", "설정됨" if access and secret else "미설정"))
        self.client.configure(access=access, secret=secret, enabled=enabled)
        self.binding_ok = not stored_binding or not self.client.binding or stored_binding == self.client.binding
        self.connected_at = None
        self.tested_at = None
        self.chance = None
        self.tick = None
        self.error = None

    def close(self):
        self.store.db.close()
        self._process_lock.close()

    @property
    def lock(self):
        if self._lock is None:self._lock=asyncio.Lock()
        return self._lock

    def fail(self, message):
        self.engine.running=False
        if self.error!=message:
            with self.store.db:
                self.store.event(datetime.now(UTC).isoformat(),"live_error",message)
                self.store.notify(datetime.now(UTC).isoformat(),"error","[BTC 실거래 · 연동 오류]\n"+message+"\n새 주문을 중지했습니다.")
        self.error=message

    def active(self):
        return self.store.db.execute("SELECT * FROM live_orders WHERE state IN ('prepared','submitting','unknown','wait','watch','cancel_pending') ORDER BY created_at").fetchall()

    def account_fresh(self):
        return self.connected_at is not None and (datetime.now(UTC)-self.connected_at).total_seconds()<20

    def on_snapshot(self, snapshot):
        if self.engine.running and (not self.account_fresh() or self.error):self.fail("실거래 계좌 동기화가 지연됐습니다.")
        self.engine.process(snapshot)

    async def on_snapshot_async(self, snapshot):
        if self.engine.running and (not self.account_fresh() or self.error):self.fail("실거래 계좌 동기화가 지연됐습니다.")
        await self.engine.process_async(snapshot)

    async def connect(self):
        if not self.client.configured:raise ValueError("업비트 API 키 설정이 필요합니다.")
        if not self.binding_ok:raise ValueError("기존 실거래 기록과 API 키가 다릅니다. 기존 계좌 확인이 필요합니다.")
        chance=await asyncio.to_thread(self.client.chance)
        instruments=await asyncio.to_thread(self.client.instrument)
        try:
            market=chance["market"]
            if market["id"]!="KRW-BTC" or market["state"]!="active":raise ValueError("KRW-BTC가 거래 가능 상태가 아닙니다.")
            if "limit" not in market["bid_types"] or "limit" not in market["ask_types"]:raise ValueError("지정가 주문 지원을 확인할 수 없습니다.")
            for key in ("bid_fee","ask_fee"):
                if not amount(chance[key])<D("0.1"):raise ValueError("수수료 정보를 확인하세요.")
            if chance["bid_account"]["currency"]!="KRW" or chance["ask_account"]["currency"]!="BTC":raise ValueError("계좌 통화가 일치하지 않습니다.")
            for account in (chance["bid_account"],chance["ask_account"]):
                amount(account["balance"]);amount(account["locked"])
            tick=amount(next(r["tick_size"] for r in instruments if r["market"]=="KRW-BTC"))
            if tick<=0:raise ValueError("호가 단위를 확인할 수 없습니다.")
            amount(market["bid"]["min_total"]);amount(market["ask"]["min_total"])
            if not self.active():
                if amount(chance["bid_account"]["balance"])<self.store.number("cash"):raise ValueError("업비트 사용 가능 원화가 봇 장부 잔액보다 부족합니다.")
                if amount(chance["ask_account"]["balance"])+D("0.000000001")<self.store.number("btc"):raise ValueError("업비트 사용 가능 BTC가 봇 보유 수량보다 부족합니다.")
        except (KeyError,TypeError,StopIteration,ArithmeticError) as exc:
            raise ValueError("계좌·수수료·호가 응답을 검증할 수 없습니다.") from exc
        self.chance,self.tick=chance,tick
        self.engine.exit_fee_rate=amount(chance["ask_fee"])
        self.connected_at=datetime.now(UTC)
        if not any(r["state"] in ("submitting","unknown") for r in self.active()):self.error=None
        with self.store.db:self.store.set("exchange_binding",self.client.binding)

    def plan(self, side, mark, identifier, force=False):
        if self.chance is None or self.tick is None:raise ValueError("계좌 연결을 먼저 확인하세요.")
        fee=amount(self.chance["bid_fee" if side=="buy" else "ask_fee"])
        settings=replace(self.engine.settings,db_path=Path(":memory:"),fee_rate=fee,
                         order_krw=min(self.engine.settings.order_krw,self.order_cap))
        shadow=Store(settings)
        try:
            with shadow.db:
                for key in ("cash","btc","cost","realized","buy_halted"):shadow.set(key,self.store.get(key,"0"))
                reason=Engine(settings,shadow)._execute(side,mark,identifier,datetime.now(UTC),force=force)
            fills=shadow.recent("fills")
            if not fills:raise ValueError(reason)
            fill=fills[0]
            bound=amount(fill["price"])
            price=(bound/self.tick).to_integral_value(rounding=ROUND_DOWN if side=="buy" else ROUND_CEILING)*self.tick
            quantity=amount(fill["quantity"])
            notional=price*quantity
            market=self.chance["market"]
            minimum=max(D(5000),amount(market["bid" if side=="buy" else "ask"]["min_total"]))
            if price<=0 or notional<minimum:raise ValueError("거래소 최소 주문 금액에 미달합니다.")
            if market.get("max_total") is not None and notional>amount(market["max_total"]):raise ValueError("거래소 최대 주문 금액을 초과합니다.")
            if side=="buy" and notional*(1+fee)>min(self.store.number("cash"),amount(self.chance["bid_account"]["balance"])):raise ValueError("매수 가능 원화가 부족합니다.")
            if side=="sell" and quantity>min(self.store.number("btc"),amount(self.chance["ask_account"]["balance"])):raise ValueError("봇 매도 가능 수량이 부족합니다.")
            return {"market":"KRW-BTC","side":"bid" if side=="buy" else "ask","ord_type":"limit", "time_in_force":"ioc",
                    "price":format(price,"f"),"volume":format(quantity,"f"),"identifier":identifier,"smp_type":"cancel_taker"}
        finally:shadow.db.close()

    async def test_order(self):
        await self.connect()
        if not self.engine.fresh(datetime.now(UTC)):raise ValueError("최신 BTC 시세를 기다려 주세요.")
        # Validation-only bid never enters the real order ledger.
        mark=self.store.number("price");market=self.chance["market"]
        buy_price=(mark*(1+self.engine.settings.slippage)/self.tick).to_integral_value(rounding=ROUND_DOWN)*self.tick
        minimum=max(D(5000),amount(market["bid"]["min_total"]))
        quantity=(minimum/buy_price).quantize(D("0.00000001"),rounding=ROUND_CEILING)
        side="bid";price=buy_price
        if quantity*price*(1+amount(self.chance["bid_fee"]))>amount(self.chance["bid_account"]["balance"]):
            side="ask"
            price=(mark*(1-self.engine.settings.slippage)/self.tick).to_integral_value(rounding=ROUND_CEILING)*self.tick
            minimum=max(D(5000),amount(market["ask"]["min_total"]))
            quantity=(minimum/price).quantize(D("0.00000001"),rounding=ROUND_CEILING)
            if quantity>min(self.store.number("btc"),amount(self.chance["ask_account"]["balance"])):raise ValueError("주문 테스트를 위한 원화 또는 봇 BTC가 부족합니다.")
        params={"market":"KRW-BTC","side":side,"ord_type":"limit","time_in_force":"ioc","smp_type":"cancel_taker",
                "price":format(price,"f"),"volume":format(quantity,"f"),"identifier":"test-"+uuid.uuid4().hex}
        response=await asyncio.to_thread(self.client.test,params)
        if not isinstance(response,dict) or not response.get("uuid"):raise ValueError("주문 테스트 응답을 확인할 수 없습니다.")
        self.tested_at=datetime.now(UTC)
        return {"tested_at":self.tested_at.isoformat(),"message":"주문 형식 검증 완료. 실제 주문은 생성하지 않았습니다."}

    def start(self):
        if not self.client.enabled:raise ValueError("실거래 탭에서 실제 주문을 활성화하세요.")
        if not self.binding_ok or self.error or not self.account_fresh():raise ValueError("계좌 연결 상태를 먼저 확인하세요.")
        if self.tested_at is None or (datetime.now(UTC)-self.tested_at).total_seconds()>300:raise ValueError("최근 5분 이내의 주문 테스트가 필요합니다.")
        if self.active():raise ValueError("미해결 주문을 먼저 확인하세요.")
        self.engine.control("start")

    def stop(self):
        self.engine.control("stop")
        with self.store.db:
            self.store.db.execute("UPDATE live_orders SET state='aborted',last_error='거래 정지로 미전송 주문 폐기' WHERE state='prepared'")

    def reconcile(self, identifier, response):
        row=self.store.db.execute("SELECT * FROM live_orders WHERE identifier=?",(identifier,)).fetchone()
        if row is None:raise ValueError("관리 대상이 아닌 주문입니다.")
        try:
            params=json.loads(row["params"])
            state=response["state"];order_uuid=response["uuid"]
            if response["market"]!="KRW-BTC" or response["side"]!=params["side"] or state not in ("wait","watch","done","cancel"):raise ValueError("주문 응답이 요청과 일치하지 않습니다.")
            if not order_uuid or (row["uuid"] and order_uuid!=row["uuid"]) or response.get("identifier",identifier)!=identifier:raise ValueError("주문 식별자가 일치하지 않습니다.")
            quantity=amount(response["executed_volume"]);fee=amount(response["paid_fee"])
            trades=response["trades"]
            if not isinstance(trades,list):raise ValueError("개별 체결 내역이 필요합니다.")
            if len({t["uuid"] for t in trades})!=len(trades):raise ValueError("중복 체결 내역입니다.")
            trade_qty=sum((amount(t["volume"]) for t in trades),D(0))
            funds=sum((amount(t["funds"]) for t in trades),D(0))
            if trade_qty!=quantity or quantity>amount(params["volume"]):raise ValueError("체결 수량을 검증할 수 없습니다.")
            if quantity and ((row["side"]=="buy" and funds>quantity*amount(params["price"])+D("0.000001")) or
                             (row["side"]=="sell" and funds<quantity*amount(params["price"])-D("0.000001"))):raise ValueError("지정가 범위를 벗어난 체결입니다.")
            dq,df,dfee=quantity-amount(row["quantity"]),funds-amount(row["funds"]),fee-amount(row["fee"])
            if min(dq,df,dfee)<0:raise ValueError("체결 누적값이 감소했습니다.")
            cash,btc,cost=self.store.number("cash"),self.store.number("btc"),self.store.number("cost")
            realized=D(0)
            if row["side"]=="buy":cash-=df+dfee;btc+=dq;cost+=df+dfee
            else:
                if dq>btc:raise ValueError("봇 보유 수량을 초과한 매도 체결입니다.")
                allocated=cost if dq==btc else (cost*dq/btc if dq else D(0))
                cash+=df-dfee;btc-=dq;cost-=allocated;realized=df-dfee-allocated
            now=datetime.now(UTC).isoformat()
            with self.store.db:
                if dq or df or dfee:
                    self.store.set("cash",cash);self.store.set("btc",btc);self.store.set("cost",cost)
                    self.store.set("realized",self.store.number("realized")+realized)
                    fill_id=identifier+":"+str(quantity)+":"+str(fee)
                    self.store.db.execute("INSERT INTO fills(time,identifier,side,price,quantity,fee,realized_pnl) VALUES (?,?,?,?,?,?,?)",
                                          (now,fill_id,row["side"],str(df/dq if dq else amount(params["price"])),str(dq),str(dfee),str(realized)))
                    self.store.notify(now,"fill","[BTC 실거래 · 체결]\n{} {} BTC\n체결 금액 {}원 · 수수료 {}원".format("매수" if row["side"]=="buy" else "매도",dq,df,dfee))
                self.store.db.execute("UPDATE live_orders SET state=?,uuid=?,quantity=?,funds=?,fee=?,last_error=NULL WHERE identifier=?",
                                      (state,order_uuid,str(quantity),str(funds),str(fee),identifier))
            if cash<0:self.fail("실제 체결 비용이 운영 예산을 초과했습니다.")
        except (KeyError,TypeError,ArithmeticError) as exc:
            raise ValueError("주문·체결 응답을 검증할 수 없습니다.") from exc

    async def sync(self):
        if not self.binding_ok:raise ValueError("기존 실거래 기록과 API 키가 다릅니다.")
        for row in self.active():
            identifier=row["identifier"]
            if row["state"]=="prepared":
                if not self.engine.running or not self.client.enabled:
                    with self.store.db:self.store.db.execute("UPDATE live_orders SET state='aborted' WHERE identifier=?",(identifier,))
                continue
            try:
                response=await asyncio.to_thread(self.client.order,identifier)
                self.reconcile(identifier,response)
            except ExchangeError as exc:
                if exc.code=="order_not_found":
                    with self.store.db:self.store.db.execute("UPDATE live_orders SET state='unknown',last_error='접수 여부 미확인: 자동 재주문하지 않습니다.' WHERE identifier=?",(identifier,))
                    self.fail("접수 여부를 확인하지 못한 주문이 있습니다.")
                else:raise
        await self.connect()
        unresolved=[r for r in self.active() if r["state"]!="prepared"]
        if unresolved and any(r["state"] in ("unknown","submitting") for r in unresolved):
            self.fail("접수 여부를 확인하지 못한 주문이 있습니다.")
        for row in self.active():
            if row["state"]!="prepared" or not self.engine.running:continue
            now=datetime.now(UTC)
            candle=datetime.fromisoformat(row["candle"].removeprefix("stop:"))
            if not self.engine.fresh(now) or (now-candle-timedelta(seconds=self.engine.settings.bar_seconds)).total_seconds()>self.engine.settings.signal_max_age:
                with self.store.db:self.store.db.execute("UPDATE live_orders SET state='aborted',last_error='신호 유효시간 경과' WHERE identifier=?",(row["identifier"],))
                continue
            try:params=self.plan(row["side"],self.store.number("price"),row["identifier"],force=row["purpose"]=="stop_loss")
            except ValueError as exc:
                with self.store.db:self.store.db.execute("UPDATE live_orders SET state='rejected',last_error=? WHERE identifier=?",(str(exc),row["identifier"]))
                continue
            with self.store.db:
                self.store.db.execute("UPDATE live_orders SET state='submitting',params=? WHERE identifier=?",(json.dumps(params),row["identifier"]))
            submitted=False
            try:
                await asyncio.to_thread(self.client.place,params)
                submitted=True
                # POST responses can omit detailed trades: always read the canonical order.
                self.reconcile(row["identifier"],await asyncio.to_thread(self.client.order,row["identifier"]))
            except ExchangeError as exc:
                state="rejected" if not submitted and exc.definite and exc.code not in ("order_not_found","duplicated_identifier") else "unknown"
                with self.store.db:self.store.db.execute("UPDATE live_orders SET state=?,last_error=? WHERE identifier=?",(state,str(exc),row["identifier"]))
                self.fail("주문 처리 확인 필요: "+str(exc))

    async def cancel(self, identifier):
        row=self.store.db.execute("SELECT * FROM live_orders WHERE identifier=?",(identifier,)).fetchone()
        if row is None or row["state"] not in ACTIVE:raise ValueError("취소 가능한 봇 주문이 아닙니다.")
        if row["state"]=="prepared":
            with self.store.db:self.store.db.execute("UPDATE live_orders SET state='aborted' WHERE identifier=?",(identifier,))
            return
        with self.store.db:self.store.db.execute("UPDATE live_orders SET state='cancel_pending' WHERE identifier=?",(identifier,))
        await asyncio.to_thread(self.client.cancel,identifier)
        self.reconcile(identifier,await asyncio.to_thread(self.client.order,identifier))

    def status(self):
        result=self.engine.status()
        result.update(mode="live",configured=self.client.configured,enabled=self.client.enabled,
                      keys_set=bool((self.store.get("upbit_access_key") or "") and (self.store.get("upbit_secret_key") or "")),
                      connected=self.account_fresh() and self.binding_ok,error=self.error,
                      connected_at=self.connected_at.isoformat() if self.connected_at else None,
                      tested_at=self.tested_at.isoformat() if self.tested_at else None,
                      budget=str(self.budget),order_cap=str(self.order_cap),
                      available_krw=str(self.chance["bid_account"]["balance"]) if self.chance else None,
                      available_btc=str(self.chance["ask_account"]["balance"]) if self.chance else None,
                      bid_fee=str(self.chance["bid_fee"]) if self.chance else None,
                      ask_fee=str(self.chance["ask_fee"]) if self.chance else None,
                      orders=[dict(r) for r in self.store.db.execute("SELECT identifier,side,created_at,state,uuid,quantity,funds,fee,last_error,purpose FROM live_orders ORDER BY created_at DESC LIMIT 100")])
        return result

    async def loop(self):
        while True:
            if self.client.configured:
                try:
                    async with self.lock:await self.sync()
                except asyncio.CancelledError:raise
                except (ExchangeError,ValueError) as exc:self.fail(str(exc))
                except Exception:self.fail("실거래 동기화 오류가 발생했습니다. 운영 기록을 확인하세요.")
            await asyncio.sleep(3)
