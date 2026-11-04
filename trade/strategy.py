from datetime import timedelta


def moving_average_cross(candles, short, long, minutes):
    """Only closed, consecutive bars; compare previous/current MA once per bar."""
    if len(candles) < long + 1:
        return "hold", "확정 캔들이 부족합니다."
    bars = candles[-(long + 1):]
    if any(b.time - a.time != timedelta(minutes=minutes) for a, b in zip(bars, bars[1:])):
        return "hold", "캔들 누락으로 판단을 보류합니다."
    prices = [b.close for b in bars]
    previous_short = sum(prices[-short-1:-1]) / short
    previous_long = sum(prices[-long-1:-1]) / long
    current_short = sum(prices[-short:]) / short
    current_long = sum(prices[-long:]) / long
    if previous_short <= previous_long and current_short > current_long:
        return "buy", "단기 이동평균이 장기 이동평균을 상향 돌파했습니다."
    if previous_short >= previous_long and current_short < current_long:
        return "sell", "단기 이동평균이 장기 이동평균을 하향 돌파했습니다."
    return "hold", "이동평균 교차가 없어 대기합니다."
