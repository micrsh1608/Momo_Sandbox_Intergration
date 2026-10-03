import hashlib
import hmac
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PAYMENT_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(PAYMENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(PAYMENT_SERVICE_DIR))

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Base, Payment


@pytest.fixture
def webhook_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        with test_sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        # Deliberately use TestClient without a context manager so the app's
        # SQL Server startup hook does not run in these isolated callback tests.
        yield TestClient(app), test_sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def create_payment(test_sessions, provider="momo"):
    payment = Payment(
        payment_id=f"PAY-{uuid.uuid4().hex[:12].upper()}",
        provider_request_id=f"REQ-{uuid.uuid4().hex[:12].upper()}",
        order_id=f"ORDER-{uuid.uuid4().hex[:8]}",
        idempotency_key=f"IDEM-{uuid.uuid4().hex}",
        request_hash="0" * 64,
        amount=50000,
        currency="VND",
        description="Workshop payment",
        status="PENDING",
        provider=provider,
        environment="sandbox" if provider == "momo" else "mock",
    )
    with test_sessions() as session:
        session.add(payment)
        session.commit()
    return payment


def make_ipn(payment, result_code=0, trans_id=123456789, order_info=None):
    payload = {
        "partnerCode": settings.momo_partner_code,
        "orderId": payment.payment_id,
        "requestId": payment.provider_request_id,
        "amount": payment.amount,
        "orderInfo": order_info if order_info is not None else payment.description,
        "orderType": "momo_wallet",
        "transId": trans_id,
        "resultCode": result_code,
        "message": "Successful." if result_code == 0 else "Payment failed.",
        "payType": "qr",
        "responseTime": 1721720663942,
        "extraData": "",
    }
    raw_signature = (
        f"accessKey={settings.momo_access_key}"
        f"&amount={payload['amount']}"
        f"&extraData={payload['extraData']}"
        f"&message={payload['message']}"
        f"&orderId={payload['orderId']}"
        f"&orderInfo={payload['orderInfo']}"
        f"&orderType={payload['orderType']}"
        f"&partnerCode={payload['partnerCode']}"
        f"&payType={payload['payType']}"
        f"&requestId={payload['requestId']}"
        f"&responseTime={payload['responseTime']}"
        f"&resultCode={payload['resultCode']}"
        f"&transId={payload['transId']}"
    )
    payload["signature"] = hmac.new(
        settings.momo_secret_key.encode("utf-8"),
        raw_signature.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return payload


@pytest.mark.parametrize(
    ("result_code", "expected_status"),
    [(0, "SUCCESS"), (1001, "FAILED")],
)
def test_ipn_updates_payment_and_returns_no_content(
    webhook_api, result_code, expected_status
):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)

    response = client.post(
        "/api/v1/payments/ipn",
        json=make_ipn(payment, result_code=result_code),
    )

    assert response.status_code == 204
    assert response.content == b""
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == expected_status
        assert saved.provider_transaction_id == "123456789"
        assert saved.provider_response_code == result_code


def test_ipn_rejects_invalid_signature_without_changing_payment(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)
    payload = make_ipn(payment)
    payload["signature"] = "invalid-signature"

    response = client.post("/api/v1/payments/ipn", json=payload)

    assert response.status_code == 400
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "PENDING"
        assert saved.provider_transaction_id is None


def test_ipn_rejects_order_info_that_does_not_match_payment(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)
    payload = make_ipn(payment, order_info="Different order description")

    response = client.post("/api/v1/payments/ipn", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid orderInfo"


def test_ipn_cannot_update_a_mock_payment(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions, provider="mock")

    response = client.post(
        "/api/v1/payments/ipn",
        json=make_ipn(payment),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Payment does not belong to MoMo"


def test_late_ipn_does_not_overwrite_a_final_result(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)

    success = client.post("/api/v1/payments/ipn", json=make_ipn(payment))
    later_failure = client.post(
        "/api/v1/payments/ipn",
        json=make_ipn(payment, result_code=1001, trans_id=987654321),
    )

    assert success.status_code == later_failure.status_code == 204
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "SUCCESS"
        assert saved.provider_transaction_id == "123456789"
