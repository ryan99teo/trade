import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

STRATEGY_NAMES = {"ma_cross": "이동평균 교차", "dca": "정액 분할매수", "rebalance": "목표 비중 리밸런싱",
                  "consecutive": "봉 연속 전략"}
NEW_STRATEGY_FIELDS = ("strategy", "dca_interval_hours", "dca_offset_minutes",
                       "target_exposure", "rebalance_tolerance", "consecutive_entry_return",
                       "consecutive_take_profit", "consecutive_stop_loss", "consecutive_count", "consecutive_seconds")
DECISION_FIELDS = ("decision_gate_enabled", "decision_threshold_buy", "decision_threshold_sell",
                   "decision_timeout_seconds", "openrouter_api_key")
EDITABLE_FIELDS = (("short_window", "long_window", "order_krw", "max_exposure", "daily_loss_limit", "candle_minutes")
                   + NEW_STRATEGY_FIELDS + DECISION_FIELDS)
# Must not invalidate an existing account fingerprint (secrets / gate knobs / poll timing).
FINGERPRINT_EXCLUDE = ("db_path", "poll_seconds", "max_quote_age_seconds") + DECISION_FIELDS
# Decision gate is configured in the app UI/DB only — not from process environment.
ENV_SKIP = set(DECISION_FIELDS)


def coerce_setting(settings, key, value):
    target = type(getattr(settings, key))
    if target is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if target is str:
        return "" if value is None else str(value)
    return target(value)


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/trade.sqlite3")
    initial_cash: Decimal = Decimal("1000000")
    order_krw: Decimal = Decimal("100000")
    max_exposure: Decimal = Decimal("0.30")
    daily_loss_limit: Decimal = Decimal("0.03")
    fee_rate: Decimal = Decimal("0.0005")
    slippage: Decimal = Decimal("0.001")
    short_window: int = 5
    long_window: int = 20
    candle_minutes: int = 5
    poll_seconds: int = 30
    max_quote_age_seconds: int = 60
    strategy: str = "ma_cross"
    dca_interval_hours: int = 24
    dca_offset_minutes: int = 0
    target_exposure: Decimal = Decimal("0.20")
    rebalance_tolerance: Decimal = Decimal("0.02")
    decision_gate_enabled: bool = False
    decision_threshold_buy: Decimal = Decimal("0.75")
    decision_threshold_sell: Decimal = Decimal("0.60")
    decision_timeout_seconds: float = 2.0
    openrouter_api_key: str = ""
    consecutive_seconds: int = 60
    consecutive_count: int = 3
    consecutive_entry_return: Decimal = Decimal("0.005")
    consecutive_take_profit: Decimal = Decimal("0.03")
    consecutive_stop_loss: Decimal = Decimal("0.10")

    def __post_init__(self):
        for name in ("initial_cash", "order_krw", "max_exposure", "daily_loss_limit", "fee_rate", "slippage",
                     "target_exposure", "rebalance_tolerance", "decision_threshold_buy", "decision_threshold_sell",
                     "consecutive_entry_return", "consecutive_take_profit", "consecutive_stop_loss"):
            if not getattr(self, name).is_finite():
                raise ValueError("금액과 비율에는 유한한 숫자만 사용할 수 있습니다.")
        if not (0 < self.initial_cash and 5000 <= self.order_krw <= self.initial_cash):
            raise ValueError("운영 예산은 양수, 주문 금액은 5,000원 이상이며 예산 이하여야 합니다.")
        if not (0 < self.max_exposure <= 1 and 0 < self.daily_loss_limit < 1):
            raise ValueError("보유 비중/손실 한도를 확인하세요.")
        if not (0 <= self.fee_rate < 0.1 and 0 <= self.slippage < 0.1):
            raise ValueError("수수료/슬리피지를 확인하세요.")
        if not (1 <= self.short_window < self.long_window <= 199):
            raise ValueError("이동평균 기간은 1 <= 단기 < 장기 <= 199여야 합니다.")
        if self.candle_minutes not in (1, 3, 5, 10, 15, 30, 60, 240):
            raise ValueError("지원하지 않는 캔들 단위입니다.")
        if self.poll_seconds < 10 or self.max_quote_age_seconds < 1:
            raise ValueError("수집 주기는 10초 이상이어야 합니다.")
        if self.strategy not in STRATEGY_NAMES:
            raise ValueError("지원하지 않는 전략입니다.")
        if self.strategy == "consecutive" and self.candle_minutes != 1:
            raise ValueError("연속 전략은 1분봉으로 설정하세요.")
        if type(self.consecutive_seconds) is not int or self.consecutive_seconds not in (10,30,60):
            raise ValueError("연속 전략 봉 단위는 10초·30초·1분 중 선택하세요.")
        if type(self.consecutive_count) is not int or not 1 <= self.consecutive_count <= 199:
            raise ValueError("연속 횟수는 1~199 사이의 정수여야 합니다.")
        if not (0 < self.consecutive_entry_return < 1 and 0 < self.consecutive_take_profit < 1
                and 0 < self.consecutive_stop_loss < 1):
            raise ValueError("연속 전략의 매수·익절·손절 기준은 0% 초과 100% 미만이어야 합니다.")
        if self.dca_interval_hours not in (1,4,6,12,24,48,168):
            raise ValueError("분할매수 주기는 1·4·6·12·24·48·168시간 중 선택하세요.")
        if not 0 <= self.dca_offset_minutes < self.dca_interval_hours * 60:
            raise ValueError("분할매수 시작 분은 0 이상이며 주기(분)보다 작아야 합니다.")
        if not 0 < self.target_exposure <= 1 or not 0 < self.rebalance_tolerance < 1:
            raise ValueError("목표 비중과 리밸런싱 허용 편차를 확인하세요.")
        if self.strategy == "dca" and self.dca_interval_hours * 60 % self.candle_minutes:
            raise ValueError("분할매수 주기는 캔들 단위의 배수여야 합니다.")
        if self.strategy == "rebalance" and not self.rebalance_tolerance < self.target_exposure <= self.max_exposure:
            raise ValueError("리밸런싱은 허용 편차 < 목표 비중 ≤ 최대 BTC 비중이어야 합니다.")
        if not (0 < self.decision_threshold_buy <= 1 and 0 < self.decision_threshold_sell <= 1):
            raise ValueError("디시젼 게이트 임계값은 0 초과 1 이하여야 합니다.")
        if not (0 < self.decision_timeout_seconds <= 30):
            raise ValueError("디시젼 게이트 제한 시간은 0 초과 30초 이하여야 합니다.")
        if "\n" in self.openrouter_api_key or "\r" in self.openrouter_api_key:
            raise ValueError("OpenRouter API 키 형식을 확인하세요.")

    @property
    def bar_seconds(self):
        return self.consecutive_seconds if self.strategy == "consecutive" else self.candle_minutes * 60

    @property
    def signal_max_age(self):
        return self.bar_seconds if self.strategy == "consecutive" else 120

    @property
    def quote_max_age(self):
        return min(self.max_quote_age_seconds, self.bar_seconds) if self.bar_seconds < 60 else self.max_quote_age_seconds

    @classmethod
    def from_env(cls):
        fields = {}
        defaults = cls()
        for name in cls.__dataclass_fields__:
            if name in ENV_SKIP:
                continue
            value = os.getenv("TRADE_" + name.upper())
            if value is None:
                continue
            fields[name] = type(getattr(defaults, name))(value)
        # Path defaults may be a platform-specific subclass.
        return cls(**fields)
