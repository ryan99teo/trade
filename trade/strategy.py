from datetime import timedelta
from decimal import Decimal


def allocation_signal(settings, candle, mark, equity, btc):
    if settings.strategy == "dca":
        # UTC epoch + offset: fire on the closed candle that contains the schedule instant.
        start = int(candle.time.timestamp())
        end = int((candle.time + timedelta(minutes=settings.candle_minutes)).timestamp())
        period = settings.dca_interval_hours * 3600
        offset = settings.dca_offset_minutes * 60
        due_at = end - ((end - offset) % period)
        due = start < due_at <= end
        return ("buy", "정액 분할매수 예약 시각입니다.") if due else ("hold", "다음 정액 분할매수 시각을 기다립니다.")
    ratio = btc * mark / equity if equity > 0 else Decimal(0)
    target, tolerance = settings.target_exposure, settings.rebalance_tolerance
    if ratio < target - tolerance:
        return "buy", "BTC 비중이 목표 허용 범위보다 낮아 매수합니다."
    if ratio > min(target + tolerance,settings.max_exposure):
        return "sell", "BTC 비중이 목표 허용 범위보다 높아 일부 매도합니다."
    return "hold", "BTC 비중이 목표 허용 범위 안에 있습니다."


def moving_average_state(candles, short, long, minutes):
    """Snapshot for UI/status: readiness, current/previous MA, and continuity."""
    needed = long + 1
    state = {
        "ready": False,
        "gap": False,
        "candle_count": len(candles),
        "required_candles": needed,
        "short_window": short,
        "long_window": long,
        "candle_minutes": minutes,
        "short_ma": None,
        "long_ma": None,
        "previous_short_ma": None,
        "previous_long_ma": None,
        "spread": None,
        "position": None,
        "latest_candle": None,
        "reason": "확정 캔들이 부족합니다.",
    }
    if len(candles) < needed:
        return state
    bars = candles[-needed:]
    state["latest_candle"] = bars[-1].time.isoformat()
    if any(b.time - a.time != timedelta(minutes=minutes) for a, b in zip(bars, bars[1:])):
        state["gap"] = True
        state["reason"] = "캔들 누락으로 판단을 보류합니다."
        return state
    prices = [b.close for b in bars]
    previous_short = sum(prices[-short - 1:-1]) / short
    previous_long = sum(prices[-long - 1:-1]) / long
    current_short = sum(prices[-short:]) / short
    current_long = sum(prices[-long:]) / long
    if current_short > current_long:
        position = "above"
    elif current_short < current_long:
        position = "below"
    else:
        position = "equal"
    state.update(
        ready=True,
        short_ma=str(current_short),
        long_ma=str(current_long),
        previous_short_ma=str(previous_short),
        previous_long_ma=str(previous_long),
        spread=str(current_short - current_long),
        position=position,
        reason="이동평균 준비 완료",
    )
    return state


def moving_average_cross(candles, short, long, minutes):
    """Only closed, consecutive bars; compare previous/current MA once per bar."""
    state = moving_average_state(candles, short, long, minutes)
    if not state["ready"]:
        return "hold", state["reason"]
    previous_short = Decimal(state["previous_short_ma"])
    previous_long = Decimal(state["previous_long_ma"])
    current_short = Decimal(state["short_ma"])
    current_long = Decimal(state["long_ma"])
    if previous_short <= previous_long and current_short > current_long:
        return "buy", "단기 이동평균이 장기 이동평균을 상향 돌파했습니다."
    if previous_short >= previous_long and current_short < current_long:
        return "sell", "단기 이동평균이 장기 이동평균을 하향 돌파했습니다."
    return "hold", "이동평균 교차가 없어 대기합니다."
