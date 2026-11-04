import io
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

from trade.config import Settings
from trade.decision_gate import DecisionGate, GateVerdict, build_state, log_path
from trade.engine import Engine
from trade.storage import Store, fingerprint
from test_trading import NOW, snapshot

D = Decimal


class FakeResponse:
    def __init__(self, body, code=200):
        self.body = body if isinstance(body, bytes) else body.encode()
        self.code = code

    def read(self, _size=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def noul_body(value):
    return json.dumps({"answers": {"proceed": {"type": "noul", "noul": value}}})


class DecisionGateUnitTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(decision_gate_enabled=True, decision_threshold_buy=D("0.75"),
                                 decision_threshold_sell=D("0.60"), short_window=2, long_window=3,
                                 candle_minutes=60, openrouter_api_key="settings-key")

    def test_disabled_skips_without_network(self):
        transport = MagicMock(side_effect=AssertionError("should not call"))
        gate = DecisionGate(transport=transport)
        verdict = gate.evaluate(replace(self.settings, decision_gate_enabled=False), "buy", {"side": "buy"})
        self.assertTrue(verdict.skipped)
        self.assertTrue(verdict.proceed)
        transport.assert_not_called()

    def test_missing_key_is_fail_closed(self):
        gate = DecisionGate()
        verdict = gate.evaluate(replace(self.settings, openrouter_api_key=""), "buy", {"side": "buy"})
        self.assertFalse(verdict.proceed)
        self.assertEqual(verdict.detail, "missing_api_key")

    def test_uses_settings_api_key(self):
        seen = {}

        def transport(request, timeout=None):
            seen["auth"] = request.get_header("Authorization")
            return FakeResponse(noul_body(0.9))

        gate = DecisionGate(transport=transport)
        self.assertTrue(gate.evaluate(self.settings, "buy", {"side": "buy"}).proceed)
        self.assertEqual(seen["auth"], "Bearer settings-key")

    def test_threshold_boundaries(self):
        for noul, proceed in ((0.749, False), (0.75, True), (0.751, True)):
            gate = DecisionGate(transport=lambda *a, **k: FakeResponse(noul_body(noul)))
            verdict = gate.evaluate(self.settings, "buy", {"side": "buy"})
            self.assertEqual(verdict.proceed, proceed, noul)
            self.assertEqual(verdict.noul, noul)

    def test_sell_uses_sell_threshold(self):
        gate = DecisionGate(transport=lambda *a, **k: FakeResponse(noul_body(0.60)))
        self.assertTrue(gate.evaluate(self.settings, "sell", {"side": "sell"}).proceed)
        gate = DecisionGate(transport=lambda *a, **k: FakeResponse(noul_body(0.59)))
        self.assertFalse(gate.evaluate(self.settings, "sell", {"side": "sell"}).proceed)

    def test_http_and_transport_errors_abort(self):
        def boom(*args, **kwargs):
            raise HTTPError("https://example", 500, "err", hdrs=None, fp=io.BytesIO(b""))
        verdict = DecisionGate(transport=boom).evaluate(self.settings, "buy", {})
        self.assertFalse(verdict.proceed)
        self.assertEqual(verdict.detail, "http_500")

        def down(*args, **kwargs):
            raise URLError("down")
        verdict = DecisionGate(transport=down).evaluate(self.settings, "buy", {})
        self.assertEqual(verdict.detail, "transport_error")

    def test_malformed_response_aborts(self):
        gate = DecisionGate(transport=lambda *a, **k: FakeResponse('{"answers":{}}'))
        self.assertEqual(gate.evaluate(self.settings, "buy", {}).detail, "malformed_response")

    def test_build_state_includes_side_and_closes(self):
        shot = snapshot()
        state = build_state("buy", "cross", self.settings, D("100"), D("1000"), D("0.1"),
                            D("2000"), D("2000"), shot.candles, False)
        self.assertEqual(state["side"], "buy")
        self.assertEqual(len(state["recent_closes"]), len(shot.candles))
        self.assertEqual(state["daily_pnl_pct"], 0.0)

    def test_fingerprint_excludes_gate_and_secret(self):
        on = replace(self.settings, decision_gate_enabled=True, openrouter_api_key="secret",
                     decision_threshold_buy=D("0.9"))
        off = replace(self.settings, decision_gate_enabled=False, openrouter_api_key="",
                      decision_threshold_buy=D("0.1"))
        self.assertEqual(fingerprint(on), fingerprint(off))


class DecisionGateEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.temp.name)/"test.sqlite3", short_window=2, long_window=3,
                                 candle_minutes=60, decision_gate_enabled=True, openrouter_api_key="stored-key")
        self.store = Store(self.settings)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def engine_with(self, verdict):
        gate = MagicMock()
        gate.evaluate.return_value = verdict
        engine = Engine(self.settings, self.store, decision_gate=gate)
        engine.process(snapshot(), NOW)
        engine.control("start", NOW)
        return engine, gate

    def test_gate_abort_blocks_fill(self):
        engine, gate = self.engine_with(GateVerdict(proceed=False, noul=0.42, detail="ok"))
        engine.process(snapshot(), NOW)
        self.assertEqual(len(self.store.recent("fills")), 0)
        decision = self.store.recent("decisions")[0]
        self.assertEqual(decision["action"], "hold")
        self.assertIn("포기", decision["reason"])
        self.assertIn("0.42", decision["reason"])
        gate.evaluate.assert_called_once()
        self.assertTrue(engine.running)
        path = log_path(self.settings)
        self.assertTrue(path.exists())
        row = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(row["side"], "buy")
        self.assertEqual(row["noul"], 0.42)
        self.assertFalse(row["proceed"])
        self.assertEqual(row["final_action"], "hold")
        self.assertNotIn("openrouter", json.dumps(row).lower())
        self.assertNotIn("api_key", json.dumps(row).lower())

    def test_gate_pass_allows_fill(self):
        engine, gate = self.engine_with(GateVerdict(proceed=True, noul=0.81, detail="ok"))
        engine.process(snapshot(), NOW)
        self.assertEqual(len(self.store.recent("fills")), 1)
        decision = self.store.recent("decisions")[0]
        self.assertEqual(decision["action"], "buy")
        self.assertIn("통과", decision["reason"])
        gate.evaluate.assert_called_once()

    def test_gate_error_fail_closed_keeps_running(self):
        engine, _ = self.engine_with(GateVerdict(proceed=False, detail="http_503"))
        engine.process(snapshot(), NOW)
        self.assertEqual(len(self.store.recent("fills")), 0)
        self.assertTrue(engine.running)
        self.assertIn("오류로 보류", self.store.recent("decisions")[0]["reason"])

    def test_disabled_gate_matches_legacy_fill(self):
        settings = replace(self.settings, decision_gate_enabled=False)
        self.store.db.close()
        self.store = Store(settings)
        engine = Engine(settings, self.store)
        engine.process(snapshot(), NOW)
        engine.control("start", NOW)
        engine.process(snapshot(), NOW)
        self.assertEqual(len(self.store.recent("fills")), 1)
        self.assertNotIn("디시젼 게이트", self.store.recent("decisions")[0]["reason"])

    def test_settings_ui_persists_gate_without_exposing_key(self):
        engine = Engine(self.settings, self.store)
        engine.configure({
            "decision_gate_enabled": True, "decision_threshold_buy": D("0.80"),
            "decision_threshold_sell": D("0.55"), "decision_timeout_seconds": 3.0,
            "openrouter_api_key": "sk-live-secret",
        }, NOW)
        status = engine.status()
        self.assertTrue(status["settings"]["decision_gate_enabled"])
        self.assertTrue(status["settings"]["openrouter_api_key_set"])
        self.assertNotIn("openrouter_api_key", status["settings"])
        self.assertNotIn("sk-live-secret", json.dumps(status))
        self.store.db.close()
        reloaded = Store(Settings(db_path=self.settings.db_path, short_window=2, long_window=3, candle_minutes=60))
        self.assertTrue(reloaded.settings.decision_gate_enabled)
        self.assertEqual(reloaded.settings.openrouter_api_key, "sk-live-secret")
        self.assertEqual(reloaded.settings.decision_threshold_buy, D("0.80"))
        engine = Engine(reloaded.settings, reloaded)
        engine.configure({
            "decision_gate_enabled": True, "decision_threshold_buy": D("0.80"),
            "decision_threshold_sell": D("0.55"), "decision_timeout_seconds": 3.0,
            "openrouter_api_key": "",
        }, NOW)
        self.assertEqual(engine.settings.openrouter_api_key, "sk-live-secret")
        reloaded.db.close()

    def test_decision_gate_api_endpoint(self):
        import sys
        import time
        from fastapi.testclient import TestClient
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_api import FakeMarket
        from trade.app import create_app
        with TestClient(create_app(self.settings, FakeMarket())) as client:
            for _ in range(100):
                if client.get("/api/status").status_code == 200:
                    break
                time.sleep(0.01)
            client.post("/api/control/stop", json={})
            response = client.post("/api/decision-gate", json={
                "decision_gate_enabled": True,
                "decision_threshold_buy": "0.80",
                "decision_threshold_sell": "0.55",
                "decision_timeout_seconds": 3,
                "openrouter_api_key": "sk-api-secret",
            })
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body["decision_gate"]["enabled"])
            self.assertTrue(body["decision_gate"]["configured"])
            self.assertNotIn("sk-api-secret", response.text)
            blocked = client.post("/api/settings", json={
                "short_window": 2, "long_window": 3, "order_krw": "100000",
                "max_exposure": "0.3", "daily_loss_limit": "0.03",
                "decision_gate_enabled": True,
            })
            self.assertEqual(blocked.status_code, 422)


if __name__ == "__main__":
    unittest.main()
