import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


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
    candle_minutes: int = 60
    poll_seconds: int = 30
    max_quote_age_seconds: int = 60

    def __post_init__(self):
        for name in ("initial_cash", "order_krw", "max_exposure", "daily_loss_limit", "fee_rate", "slippage"):
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

    @classmethod
    def from_env(cls):
        fields = {}
        defaults = cls()
        for name in cls.__dataclass_fields__:
            value = os.getenv("TRADE_" + name.upper())
            if value is not None:
                fields[name] = type(getattr(defaults, name))(value)
        # Path defaults may be a platform-specific subclass.
        return cls(**fields)
