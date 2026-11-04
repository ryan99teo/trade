import asyncio
import fcntl
import logging
from datetime import datetime
import time
from contextlib import asynccontextmanager
from pathlib import Path
from decimal import Decimal
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .engine import Engine
from .market import MarketError, UpbitPublic, UTC
from .notifications import Telegram, delivery_loop, status as notification_status
from .exchange import UpbitExchange, ExchangeError
from .live import LiveService
from .storage import Store
from .backtest import BacktestRequest, compare_backtests, fills_csv, read_archive, run_backtest

log = logging.getLogger(__name__)


class StrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_window: int = Field(ge=1, le=198, strict=True)
    long_window: int = Field(ge=2, le=199, strict=True)
    order_krw: Decimal = Field(ge=5000)
    max_exposure: Decimal = Field(gt=0, le=1)
    daily_loss_limit: Decimal = Field(gt=0, lt=1)
    strategy: Optional[Literal["ma_cross","dca","rebalance"]] = None
    dca_interval_hours: Optional[int] = Field(default=None,ge=1,le=168,strict=True)
    target_exposure: Optional[Decimal] = Field(default=None,gt=0,le=1)
    rebalance_tolerance: Optional[Decimal] = Field(default=None,gt=0,lt=1)


class DecisionGateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_gate_enabled: bool
    decision_threshold_buy: Decimal = Field(gt=0, le=1)
    decision_threshold_sell: Decimal = Field(gt=0, le=1)
    decision_timeout_seconds: float = Field(gt=0, le=30)
    openrouter_api_key: Optional[str] = Field(default=None, max_length=512)


class LiveStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_real_orders: Literal[True]


def create_app(settings=None, market=None, history_market=None, telegram=None, exchange=None):
    settings = settings or Settings.from_env()
    market = market or UpbitPublic()
    history_market = history_market or UpbitPublic()
    telegram = telegram or Telegram.from_env()
    exchange = exchange or UpbitExchange.from_env()
    archive_path = settings.db_path.with_name(settings.db_path.stem + "-backtests.sqlite3")
    backtest_lock = None

    async def collect(engine):
        while True:
            delay = settings.poll_seconds
            try:
                snapshot = await asyncio.to_thread(market.fetch, settings.candle_minutes)
                engine.process(snapshot)
                try:app.state.live.on_snapshot(snapshot)
                except Exception:app.state.live.fail("실거래 시세 처리 오류가 발생했습니다.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Market collection failed")
                engine.fail(str(exc) if isinstance(exc, MarketError) else "수집 또는 처리 오류: 서버 로그를 확인하세요.")
                app.state.live.fail("공개 시세 수신 오류로 실거래를 정지했습니다.")
                if isinstance(exc, MarketError):
                    delay = max(delay, exc.retry_after)
            await asyncio.sleep(delay)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal backtest_lock
        backtest_lock = asyncio.Lock()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(str(settings.db_path) + ".lock", "a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("같은 DB를 사용하는 거래 프로세스가 이미 실행 중입니다.") from exc
            store = Store(settings)
            live = None
            try:
                store.notifications_recipient = telegram.recipient
                app.state.engine = Engine(settings, store)
                live = LiveService(store.settings,exchange)
                live.store.notifications_recipient=telegram.recipient
                app.state.live=live
                task = asyncio.create_task(collect(app.state.engine))
                notification_task = asyncio.create_task(delivery_loop(store,telegram))
                live_task=asyncio.create_task(live.loop())
                live_notifications=asyncio.create_task(delivery_loop(live.store,telegram))
                try:
                    yield
                finally:
                    task.cancel()
                    notification_task.cancel()
                    live_task.cancel();live_notifications.cancel()
                    await asyncio.gather(task,notification_task,live_task,live_notifications,return_exceptions=True)
            finally:
                if live is not None:live.close()
                store.db.close()
        finally:
            lock.close()

    app = FastAPI(title="BTC 개인 모의매매", lifespan=lifespan)

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        # Local UI only; reject DNS rebinding, cross-origin and non-JSON control requests.
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("localhost", "127.0.0.1", "testserver"):
            return JSONResponse({"detail": "로컬 접속만 허용합니다."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "허용하지 않는 Origin입니다."}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON 요청만 허용합니다."}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        return response

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/api/status")
    async def status():
        return app.state.engine.status()

    @app.post("/api/control/{action}")
    async def control(action: str):
        try:
            app.state.engine.control(action)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return app.state.engine.status()

    @app.post("/api/settings")
    async def save_settings(request: StrategyUpdate):
        try:
            app.state.engine.configure(request.model_dump(exclude_none=True))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return app.state.engine.status()

    def _decision_gate_payload(request: DecisionGateUpdate):
        changes = request.model_dump()
        if not (changes.get("openrouter_api_key") or "").strip():
            changes.pop("openrouter_api_key", None)
        return changes

    @app.post("/api/decision-gate")
    async def save_decision_gate(request: DecisionGateUpdate):
        try:
            app.state.engine.configure(_decision_gate_payload(request))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return app.state.engine.status()

    @app.get("/api/live/status")
    async def live_status():return app.state.live.status()

    @app.post("/api/live/connect")
    async def live_connect():
        live=app.state.live
        async with live.lock:
            try:await live.sync()
            except (ExchangeError,ValueError) as exc:
                live.fail(str(exc));raise HTTPException(status_code=409,detail=str(exc)) from exc
        return live.status()

    @app.post("/api/live/test")
    async def live_test():
        live=app.state.live
        async with live.lock:
            try:return await live.test_order()
            except (ExchangeError,ValueError) as exc:
                raise HTTPException(status_code=409,detail=str(exc)) from exc

    @app.post("/api/live/start")
    async def live_start(request: LiveStart):
        live=app.state.live
        async with live.lock:
            try:live.start()
            except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc
        return live.status()

    @app.post("/api/live/control/{action}")
    async def live_control(action: str):
        live=app.state.live
        try:
            if action=="stop":live.stop()
            elif action in ("pause_buys","resume_buys"):live.engine.control(action)
            else:raise ValueError("지원하지 않는 실거래 제어입니다.")
        except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc
        return live.status()

    @app.post("/api/live/settings")
    async def live_settings(request: StrategyUpdate):
        live=app.state.live
        if live.active():raise HTTPException(status_code=409,detail="미해결 실거래 주문을 먼저 확인하세요.")
        if request.order_krw>live.order_cap:raise HTTPException(status_code=422,detail="실거래 1회 매수 상한을 초과합니다.")
        try:live.engine.configure(request.model_dump(exclude_none=True))
        except (ValueError,RuntimeError) as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc
        live.tested_at=None
        return live.status()

    @app.post("/api/live/decision-gate")
    async def live_decision_gate(request: DecisionGateUpdate):
        live=app.state.live
        if live.active():raise HTTPException(status_code=409,detail="미해결 실거래 주문을 먼저 확인하세요.")
        try:live.engine.configure(_decision_gate_payload(request))
        except (ValueError,RuntimeError) as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc
        return live.status()

    @app.post("/api/live/orders/{identifier}/cancel")
    async def live_cancel(identifier: str):
        live=app.state.live
        async with live.lock:
            try:await live.cancel(identifier)
            except (ExchangeError,ValueError) as exc:
                live.fail(str(exc));raise HTTPException(status_code=409,detail=str(exc)) from exc
        return live.status()

    @app.get("/api/notifications")
    async def notifications():
        return notification_status(app.state.engine.store,telegram)

    @app.post("/api/notifications/test")
    async def test_notification():
        if not telegram.enabled:
            raise HTTPException(status_code=409,detail="서버에 텔레그램 봇 토큰과 채팅 ID를 설정하고 재시작하세요.")
        store = app.state.engine.store
        now = time.time()
        if now-float(store.get("notification_test_last","0"))<10:
            raise HTTPException(status_code=429,detail="테스트 알림은 10초 후 다시 요청하세요.")
        with store.db:
            store.set("notification_test_last",now)
            notification_id = store.notify(datetime.now(UTC).isoformat(),"test","[BTC 모의매매 · 테스트 알림]\n텔레그램 알림 연결을 확인합니다. 실제 주문은 발생하지 않습니다.")
        return {"id":notification_id,"status":"pending"}

    @app.get("/api/backtests")
    async def backtests():
        return {"running": backtest_lock.locked(), "runs": await asyncio.to_thread(read_archive, archive_path)}

    @app.post("/api/backtests")
    async def backtest(request: BacktestRequest):
        try:
            request.bounds()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if backtest_lock.locked():
            raise HTTPException(status_code=409, detail="다른 백테스트가 실행 중입니다. 완료 후 다시 실행하세요.")
        async with backtest_lock:
            try:
                return await asyncio.to_thread(run_backtest, archive_path, history_market, request)
            except MarketError as exc:
                raise HTTPException(status_code=503, detail=str(exc) + " 잠시 후 다시 실행하세요.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/backtests/{run_id}")
    async def backtest_result(run_id: str):
        result = await asyncio.to_thread(read_archive, archive_path, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="백테스트 결과를 찾을 수 없습니다.")
        return result

    @app.post("/api/backtests/compare")
    async def compare(request: BacktestRequest):
        if backtest_lock.locked():
            raise HTTPException(status_code=409,detail="다른 백테스트가 실행 중입니다. 완료 후 다시 실행하세요.")
        async with backtest_lock:
            try:
                return await asyncio.to_thread(compare_backtests,archive_path,history_market,request)
            except MarketError as exc:
                raise HTTPException(status_code=503,detail=str(exc)+" 잠시 후 다시 실행하세요.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=422,detail=str(exc)) from exc

    @app.get("/api/backtests/{run_id}/trades.csv")
    async def backtest_csv(run_id: str):
        result = await backtest_result(run_id)
        return Response(fills_csv(result), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="backtest-{}.csv"'.format(result["id"])})

    return app
