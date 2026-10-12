"""Three service-level current-period aggregates; no identity or content."""
from sqlalchemy import BigInteger, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base

class ProviderUsagePool(Base):
    __tablename__ = "provider_usage_pools"
    __table_args__ = (
        CheckConstraint("provider IN ('openai', 'brave', 'tavily')", name="provider_valid"),
        CheckConstraint("period_seconds > 0 AND period_start >= 0", name="period_valid"),
        CheckConstraint("total_limit >= 0 AND anonymous_limit >= 0 AND anonymous_limit <= total_limit", name="limits_valid"),
        CheckConstraint("total_attempts >= 0 AND anonymous_attempts >= 0 AND anonymous_attempts <= total_attempts AND total_attempts <= total_limit AND anonymous_attempts <= anonymous_limit", name="counts_valid"),
    )
    provider: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    anonymous_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_attempts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    anonymous_attempts: Mapped[int] = mapped_column(BigInteger, nullable=False)
