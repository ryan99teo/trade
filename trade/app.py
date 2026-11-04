import asyncio
import fcntl
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .engine import Engine
from .market import MarketError, UpbitPublic
from .storage import Store
from .backtest import BacktestRequest, fills_csv, read_archive, run_backtest

log = logging.getLogger(__name__)


class StrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_window: int = Field(ge=1, le=198, strict=True)
    long_window: int = Field(ge=2, le=199, strict=True)
    order_krw: Decimal = Field(ge=5000)
    max_exposure: Decimal = Field(gt=0, le=1)
    daily_loss_limit: Decimal = Field(gt=0, lt=1)


def create_app(settings=None, market=None, history_market=None):
    settings = settings or Settings.from_env()
    market = market or UpbitPublic()
    history_market = history_market or UpbitPublic()
    archive_path = settings.db_path.with_name(settings.db_path.stem + "-backtests.sqlite3")
    backtest_lock = asyncio.Lock()

    async def collect(engine):
        while True:
            delay = settings.poll_seconds
            try:
                snapshot = await asyncio.to_thread(market.fetch, settings.candle_minutes)
                engine.process(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Market collection failed")
                engine.fail(str(exc) if isinstance(exc, MarketError) else "수집 또는 처리 오류: 서버 로그를 확인하세요.")
                if isinstance(exc, MarketError):
                    delay = max(delay, exc.retry_after)
            await asyncio.sleep(delay)

    @asynccontextmanager
    async def lifespan(app):
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(str(settings.db_path) + ".lock", "a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("같은 DB를 사용하는 거래 프로세스가 이미 실행 중입니다.") from exc
            store = Store(settings)
            try:
                app.state.engine = Engine(settings, store)
                task = asyncio.create_task(collect(app.state.engine))
                try:
                    yield
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
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
            app.state.engine.configure(request.model_dump())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return app.state.engine.status()

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

    @app.get("/api/backtests/{run_id}/trades.csv")
    async def backtest_csv(run_id: str):
        result = await backtest_result(run_id)
        return Response(fills_csv(result), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="backtest-{}.csv"'.format(result["id"])})

    return app
