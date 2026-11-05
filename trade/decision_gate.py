"""OpenRouter Decisions API gate for buy/sell proceed-or-abort checks."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.14"


@dataclass(frozen=True)
class GateVerdict:
    proceed: bool
    noul: float = None
    skipped: bool = False
    detail: str = ""


def log_path(settings):
    return Path(settings.db_path).with_name(Path(settings.db_path).stem + "-decision-gate.log")


def write_log(settings, *, side, candle, verdict, state, final_action, threshold=None):
    """Append one JSON line per gate evaluation. Never writes API keys."""
    if verdict.skipped:
        return
    if threshold is None:
        threshold = (settings.decision_threshold_sell if side == "sell"
                     else settings.decision_threshold_buy)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "candle": candle,
        "side": side,
        "strategy": settings.strategy,
        "signal_reason": (state or {}).get("signal_reason"),
        "mark_krw": (state or {}).get("mark_krw"),
        "btc_ratio": (state or {}).get("btc_ratio"),
        "daily_pnl_pct": (state or {}).get("daily_pnl_pct"),
        "indicators": {key: (state or {}).get(key) for key in ("price_changes_pct", "recent_return_volatility_pct", "volume_ratio_3_vs_20", "market_context", "consecutive", "estimated_net_return_pct")},
        "noul": verdict.noul,
        "threshold": str(threshold),
        "proceed": verdict.proceed,
        "detail": verdict.detail,
        "final_action": final_action,
        "model": MODEL,
    }
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_log(settings, limit=50):
    """Newest-first JSON records for the Web UI. Skips corrupt lines; never returns secrets."""
    if limit <= 0:
        return []
    path = log_path(settings)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    records.reverse()
    return records


def build_state(side, reason, settings, mark, cash, btc, equity, baseline, candles, buy_halted,
                cost=None, market_context=None, rule_state=None):
    mark = Decimal(mark)
    equity = Decimal(equity)
    btc = Decimal(btc)
    baseline = Decimal(baseline or 0)
    ratio = (btc * mark / equity) if equity > 0 else Decimal(0)
    daily_pnl_pct = float((equity - baseline) / baseline * 100) if baseline > 0 else 0.0
    window = max(settings.consecutive_count + 1, 20) if settings.strategy == "consecutive" else max(settings.long_window, 20)
    closes = [str(c.close) for c in candles[-window:]]
    state = {
        "side": side,
        "strategy": settings.strategy,
        "signal_reason": reason,
        "mark_krw": str(mark),
        "cash_krw": str(cash),
        "btc": str(btc),
        "equity_krw": str(equity),
        "btc_ratio": str(ratio),
        "buy_halted": bool(buy_halted),
        "daily_pnl_pct": daily_pnl_pct,
        "order_krw": str(settings.order_krw),
        "max_exposure": str(settings.max_exposure),
        "short_window": settings.short_window,
        "long_window": settings.long_window,
        "dca_interval_hours": settings.dca_interval_hours,
        "dca_offset_minutes": settings.dca_offset_minutes,
        "target_exposure": str(settings.target_exposure),
        "rebalance_tolerance": str(settings.rebalance_tolerance),
        "recent_closes": closes,
    }
    def change(period):
        bars=candles[-(period+1):]
        if len(bars)<period+1 or any((b.time-a.time).total_seconds()!=settings.bar_seconds for a,b in zip(bars,bars[1:])):
            return None
        return float((bars[-1].close/bars[0].close-1)*100)
    changes={str(p*settings.bar_seconds)+"s" if settings.bar_seconds<60 else str(p*settings.bar_seconds//60)+"m":change(p) for p in (1,3,5,15)}
    returns=[candles[i].close/candles[i-1].close-1 for i in range(max(1,len(candles)-20),len(candles))
             if (candles[i].time-candles[i-1].time).total_seconds()==settings.bar_seconds]
    volatility=None
    if len(returns)>1:
        average=sum(returns)/len(returns)
        volatility=float((sum((r-average)**2 for r in returns)/len(returns)).sqrt()*100)
    volumes=[getattr(c,"volume",None) for c in candles[-23:]]
    volume_ratio=None
    if len(volumes)>=23 and all(v is not None for v in volumes) and sum(volumes[:-3])>0:
        volume_ratio=float((sum(volumes[-3:])/3)/(sum(volumes[:-3])/20))
    state.update(candle_minutes=settings.bar_seconds/60,candle_seconds=settings.bar_seconds,price_changes_pct=changes,
                 recent_return_volatility_pct=volatility,volume_ratio_3_vs_20=volume_ratio,
                 market_context=market_context or {},consecutive=rule_state,
                 entry_cost_krw=str(cost) if cost is not None else None,
                 policy={"consecutive_count":settings.consecutive_count,"return_window_seconds":settings.consecutive_count*settings.bar_seconds,"entry_return_pct":str(settings.consecutive_entry_return*100),
                         "minimum_net_take_profit_pct":str(settings.consecutive_take_profit*100),
                         "net_stop_loss_pct":str(settings.consecutive_stop_loss*100)})
    return state


def _question(side):
    if side == "hold":
        return {
            "proceed": {
                "type": "noul",
                "instructions": ("The strategy is holding with no buy/sell signal. "
                                 "Based on the trading state, would executing a buy now be justified?"),
                "criteria": {
                    "true": "Market and risk conditions would still justify opening a buy now.",
                    "false": "There is no good reason to buy while the strategy is holding.",
                },
            }
        }
    action = "buy" if side == "buy" else "sell"
    return {
        "proceed": {
            "type": "noul",
            "instructions": "Should the bot execute this {} order now based on the trading state?".format(action),
            "criteria": {
                "true": "The strategy signal is coherent with recent price action and current risk; proceed with the trade.",
                "false": "The signal looks weak, late, noisy, or risk is unfavorable; abort the trade.",
            },
        }
    }


class DecisionGate:
    def __init__(self, api_key=None, transport=None):
        # api_key override is for tests only; production reads settings.openrouter_api_key.
        self._api_key_override = api_key
        self.transport = transport or urlopen

    def api_key_for(self, settings):
        if self._api_key_override is not None:
            return self._api_key_override.strip()
        return (settings.openrouter_api_key or "").strip()

    def evaluate(self, settings, side, state):
        if not settings.decision_gate_enabled:
            return GateVerdict(proceed=True, skipped=True, detail="disabled")
        # hold = advisory-only probe (logged, never executes). buy/sell = hard gate.
        if side not in ("buy", "sell", "hold"):
            return GateVerdict(proceed=False, detail="invalid_side")
        api_key = self.api_key_for(settings)
        if not api_key:
            return GateVerdict(proceed=False, detail="missing_api_key")
        payload = json.dumps({"model": MODEL, "state": state, "questions": _question(side)}).encode()
        request = Request(ENDPOINT, data=payload, method="POST",
                          headers={"Authorization": "Bearer " + api_key,
                                   "Content-Type": "application/json",
                                   "HTTP-Referer": "https://github.com/local/trade",
                                   "X-Title": "personal-btc-paper"})
        try:
            with self.transport(request, timeout=float(settings.decision_timeout_seconds)) as response:
                raw = response.read(1_000_000)
            data = json.loads(raw)
        except HTTPError as exc:
            return GateVerdict(proceed=False, detail="http_{}".format(exc.code))
        except (URLError, TimeoutError, OSError, ValueError, TypeError):
            return GateVerdict(proceed=False, detail="transport_error")
        try:
            answer = data["answers"]["proceed"]
            if answer.get("type") != "noul":
                return GateVerdict(proceed=False, detail="unexpected_answer_type")
            noul = float(answer["noul"])
            if not (0 <= noul <= 1):
                return GateVerdict(proceed=False, detail="invalid_noul")
        except (KeyError, TypeError, ValueError):
            return GateVerdict(proceed=False, detail="malformed_response")
        threshold = (settings.decision_threshold_sell if side == "sell"
                     else settings.decision_threshold_buy)
        return GateVerdict(proceed=noul >= float(threshold), noul=noul, detail="ok")
