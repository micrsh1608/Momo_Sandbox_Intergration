from datetime import datetime, timezone
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint("currency = 'VND'", name="ck_payments_currency_vnd"),
        CheckConstraint(
            "status IN ('CREATED', 'PENDING', 'UNKNOWN', 'FAILED', 'SUCCESS')",
            name="ck_payments_status_valid",
        ),
        CheckConstraint(
            "provider IN ('momo', 'mock')",
            name="ck_payments_provider_valid",
        ),
        CheckConstraint(
            "environment IN ('sandbox', 'mock', 'production')",
            name="ck_payments_environment_valid",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    payment_id = Column(String(64), unique=True, nullable=False, index=True)
    provider_request_id = Column(String(64), unique=True, nullable=False)
    provider_transaction_id = Column(String(64), nullable=True)  # MoMo transId
    order_id = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), unique=True, nullable=False, index=True)
    request_hash = Column(String(64), nullable=False)
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(10), nullable=False, default="VND")
    description = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="CREATED")  # CREATED, PENDING, UNKNOWN, FAILED, SUCCESS
    provider = Column(String(20), nullable=False, default="mock")  # momo, mock
    environment = Column(String(20), nullable=False, default="mock")  # sandbox, mock
    pay_url = Column(Text, nullable=True)
    provider_response_code = Column(Integer, nullable=True)
    provider_message = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PaymentIPNEvent(Base):
    """Stores accepted MoMo IPN notifications to make webhook processing idempotent."""
    __tablename__ = "payment_ipn_events"
    __table_args__ = (
        UniqueConstraint(
            "event_key",
            name="UQ_payment_ipn_events_event_key",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(64), nullable=False, unique=True, index=True)
    payment_id = Column(String(64), nullable=False, index=True)
    request_id = Column(String(64), nullable=False)
    transaction_id = Column(String(64), nullable=False)
    result_code = Column(Integer, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
