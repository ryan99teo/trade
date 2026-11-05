from datetime import timedelta
from decimal import Decimal
from datetime import datetime


def consecutive_state(candles, previous=None, required=3, seconds=60):
    """Durable streak; unchanged bars do not reset the once-per-run review flag."""
    state=dict(previous or {})
    if state.get("required_count", state.get("window_minutes", 3)) != required or state.get("bar_seconds", 60) != seconds:
        state = {}
    state["required_count"] = required
    state["window_minutes"] = required * seconds / 60
    state["bar_seconds"] = seconds
    state["window_seconds"] = required * seconds
    if not candles:return state
    last=datetime.fromisoformat(state["last_time"]) if state.get("last_time") else None
    if last and candles[0].time > last+timedelta(seconds=seconds):
        state={"required_count": required,"window_minutes": required*seconds/60,"bar_seconds": seconds,"window_seconds": required*seconds};last=None
    for candle in candles:
        if last and candle.time <= last:continue
        before=Decimal(state["last_close"]) if state.get("last_close") else None
        contiguous=last is not None and candle.time-last==timedelta(seconds=seconds)
        direction="up" if before is not None and candle.close>before else "down" if before is not None and candle.close<before else "flat"
        continuing=contiguous and direction!="flat" and state.get("direction")==direction
        state.update(direction=direction if contiguous else "flat",count=(state.get("count",0)+1 if continuing else 1 if contiguous and direction!="flat" else 0),
                     reviewed=state.get("reviewed",False) if continuing else False,
                     last_time=candle.time.isoformat(),last_close=str(candle.close))
        last=candle.time
    recent=candles[-(required+1):]
    ready=len(recent)==required+1 and all(b.time-a.time==timedelta(seconds=seconds) for a,b in zip(recent,recent[1:]))
    state["ready"]=ready
    state["window_return"]=str(recent[-1].close/recent[0].close-1) if ready else None
    return state


def consecutive_signal(settings, state, btc, net_return):
    if not state.get("ready") or state.get("count",0)<settings.consecutive_count:return "hold","연속된 {}초봉 가격 변화 {}회가 필요합니다.".format(settings.bar_seconds, settings.consecutive_count)
    if state.get("reviewed"):return "hold","현재 연속 구간은 이미 Jev 검토를 요청했습니다."
    if btc<=0 and state["direction"]=="up" and Decimal(state["window_return"])>=settings.consecutive_entry_return:
        return "buy","{}초봉 {}회 이상 연속 상승 · 최근 {}초 상승률 기준 충족.".format(settings.bar_seconds,settings.consecutive_count,settings.bar_seconds*settings.consecutive_count)
    if btc>0 and state["direction"]=="down" and net_return is not None and net_return>=settings.consecutive_take_profit:
        return "sell","{}초봉 {}회 이상 연속 하락 · 비용 차감 후 최소 순익절률 충족.".format(settings.bar_seconds,settings.consecutive_count)
    return "hold","방향·상승폭 또는 비용 반영 익절 조건이 충족되지 않았습니다."


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
