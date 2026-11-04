import asyncio
import fcntl
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .engine import Engine
from .market import MarketError, UpbitPublic
from .storage import Store

log = logging.getLogger(__name__)


def create_app(settings=None, market=None):
    settings = settings or Settings.from_env()
    market = market or UpbitPublic()

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

    return app
