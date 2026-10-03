import asyncio
import hashlib
import hmac
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

PAYMENT_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(PAYMENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(PAYMENT_SERVICE_DIR))

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Base, Payment
from app.schemas import MoMoIPNRequest, PaymentCreateRequest
from app.services.payment_service import PaymentService

MOMO_ACCESS_KEY = "test-momo-access-key"
MOMO_SECRET_KEY = "test-momo-secret-key"
DEMO_SECRET = "test-only-demo-secret-that-is-longer-than-32-characters"


@pytest.fixture
def webhook_api(monkeypatch):
    db_path = Path(__file__).parent / f".webhook-tests-{uuid.uuid4().hex}.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        with test_sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(settings, "payment_provider_mode", "momo")
    monkeypatch.setattr(settings, "momo_partner_code", "MOMO-TEST")
    monkeypatch.setattr(settings, "momo_access_key", MOMO_ACCESS_KEY)
    monkeypatch.setattr(settings, "momo_secret_key", MOMO_SECRET_KEY)
    monkeypatch.setattr(settings, "demo_ipn_enabled", False)
    monkeypatch.setattr(settings, "demo_ipn_secret", "")
    try:
        # No context manager: it avoids running the SQL Server app startup hook.
        yield TestClient(app), test_sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
        db_path.unlink(missing_ok=True)


def create_payment(test_sessions, provider="momo", status="PENDING", environment=None):
    payment = Payment(
        payment_id=f"PAY-{uuid.uuid4().hex[:12].upper()}",
        provider_request_id=f"REQ-{uuid.uuid4().hex[:12].upper()}",
        order_id=f"ORDER-{uuid.uuid4().hex[:8]}",
        idempotency_key=f"IDEM-{uuid.uuid4().hex}",
        request_hash="0" * 64,
        amount=50000,
        currency="VND",
        description="Workshop payment",
        status=status,
        provider=provider,
        environment=environment or ("sandbox" if provider == "momo" else "mock"),
    )
    with test_sessions() as session:
        session.add(payment)
        session.commit()
    return payment


def make_ipn(
    payment,
    result_code=0,
    trans_id=123456789,
    order_info=None,
    *,
    demo=False,
    overrides=None,
):
    partner_code = "DEMO" if demo else settings.momo_partner_code
    access_key = "DEMO" if demo else settings.momo_access_key
    secret_key = settings.demo_ipn_secret if demo else settings.momo_secret_key
    payload = {
        "partnerCode": partner_code,
        "orderId": payment.payment_id,
        "requestId": payment.provider_request_id,
        "amount": payment.amount,
        "orderInfo": order_info if order_info is not None else payment.description,
        "orderType": "momo_wallet",
        "transId": trans_id,
        "resultCode": result_code,
        "message": "Successful." if result_code in {0, 9000} else "Payment failed.",
        "payType": "qr",
        "responseTime": 1721720663942,
        "extraData": "",
    }
    payload.update(overrides or {})
    raw_signature = (
        f"accessKey={access_key}"
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
        secret_key.encode("utf-8"), raw_signature.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return payload


@pytest.mark.parametrize(
    ("result_code", "expected_status"),
    [(0, "SUCCESS"), (9000, "SUCCESS"), (1001, "FAILED")],
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


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("partnerCode", "SOMEONE-ELSE", "Invalid partnerCode"),
        ("requestId", "REQ-WRONG", "Invalid requestId"),
        ("amount", 50001, "Invalid amount"),
    ],
)
def test_ipn_rejects_signed_mismatched_partner_request_or_amount(
    webhook_api, field, value, detail
):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)
    payload = make_ipn(payment, overrides={field: value})

    response = client.post("/api/v1/payments/ipn", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == detail
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "PENDING"


def test_ipn_rejects_order_info_that_does_not_match_payment(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)

    response = client.post(
        "/api/v1/payments/ipn",
        json=make_ipn(payment, order_info="Different order description"),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid orderInfo"


def test_duplicate_and_late_ipn_do_not_overwrite_final_result(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions)

    success = client.post("/api/v1/payments/ipn", json=make_ipn(payment))
    duplicate = client.post("/api/v1/payments/ipn", json=make_ipn(payment))
    late_failure = client.post(
        "/api/v1/payments/ipn",
        json=make_ipn(payment, result_code=1001, trans_id=987654321),
    )

    assert success.status_code == duplicate.status_code == late_failure.status_code == 204
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "SUCCESS"
        assert saved.provider_transaction_id == "123456789"


def test_two_callback_sessions_cannot_overwrite_each_other(webhook_api):
    _, test_sessions = webhook_api
    payment = create_payment(test_sessions)
    first_session = test_sessions()
    try:
        # Cache PENDING in one session, then let the other callback commit first.
        stale_payment = first_session.query(Payment).filter_by(
            payment_id=payment.payment_id
        ).one()
        first_ipn = make_ipn(payment, result_code=0, trans_id=101)
        second_ipn = make_ipn(payment, result_code=1001, trans_id=202)
        with test_sessions() as winning_session:
            asyncio.run(
                PaymentService(winning_session).handle_ipn(MoMoIPNRequest(**first_ipn))
            )

        # This exercises the check/update race: the conditional UPDATE must see
        # that the other callback already made the payment final.
        assert stale_payment.status == "PENDING"
        asyncio.run(
            PaymentService(first_session).handle_ipn(MoMoIPNRequest(**second_ipn))
        )
    finally:
        first_session.close()

    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "SUCCESS"
        assert saved.provider_response_code == 0
        assert saved.provider_transaction_id == "101"


def test_ipn_can_confirm_unknown_payment(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions, status="UNKNOWN")

    response = client.post("/api/v1/payments/ipn", json=make_ipn(payment))

    assert response.status_code == 204
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "SUCCESS"


def test_ipn_commit_failure_is_not_acknowledged_as_success(webhook_api, monkeypatch):
    _, test_sessions = webhook_api
    payment = create_payment(test_sessions)
    session = test_sessions()

    def fail_commit():
        raise SQLAlchemyError("simulated commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                PaymentService(session).handle_ipn(
                    MoMoIPNRequest(**make_ipn(payment))
                )
            )
        assert error.value.status_code == 503
    finally:
        session.close()

    with test_sessions() as check_session:
        saved = check_session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "PENDING"
        assert saved.provider_transaction_id is None


def test_mock_demo_callback_and_shared_lookup_response(webhook_api, monkeypatch):
    client, test_sessions = webhook_api
    monkeypatch.setattr(settings, "payment_provider_mode", "mock")
    monkeypatch.setattr(settings, "demo_ipn_enabled", True)
    monkeypatch.setattr(settings, "demo_ipn_secret", DEMO_SECRET)

    create_response = client.post(
        "/internal/payments",
        headers={
            "X-Internal-Token": settings.internal_token,
            "Idempotency-Key": f"DEMO-{uuid.uuid4().hex}",
        },
        json={
            "order_id": f"ORDER-{uuid.uuid4().hex[:8]}",
            "amount": 50000,
            "currency": "VND",
            "description": "Mock callback demo",
        },
    )
    assert create_response.status_code == 201
    payment_id = create_response.json()["payment_id"]
    with test_sessions() as session:
        payment = session.query(Payment).filter_by(payment_id=payment_id).one()
        payload = make_ipn(payment, demo=True)

    unauthenticated_lookup = client.get(f"/internal/payments/{payment_id}")
    callback = client.post("/api/v1/payments/ipn", json=payload)
    lookup = client.get(
        f"/internal/payments/{payment_id}",
        headers={"X-Internal-Token": settings.internal_token},
    )

    assert callback.status_code == 204
    assert unauthenticated_lookup.status_code == 401
    assert lookup.status_code == 200
    body = lookup.json()
    assert body["payment_id"] == payment_id
    assert body["status"] == "SUCCESS"
    assert body["pay_url"].startswith("https://mock.momo.vn/")
    assert body["amount"] == 50000
    assert body["provider"] == "mock"
    assert body["provider_response_code"] == 0
    assert body["environment"] == "mock"


def test_mock_callback_requires_demo_configuration(webhook_api):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions, provider="mock")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "payment_provider_mode", "mock")
        monkeypatch.setattr(settings, "demo_ipn_enabled", False)
        monkeypatch.setattr(settings, "demo_ipn_secret", "")
        response = client.post(
            "/api/v1/payments/ipn",
            json=make_ipn(payment, demo=True),
        )

    assert response.status_code == 403
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "PENDING"


def test_demo_callback_cannot_update_real_momo_payment(webhook_api, monkeypatch):
    client, test_sessions = webhook_api
    payment = create_payment(test_sessions, provider="momo", environment="sandbox")
    monkeypatch.setattr(settings, "payment_provider_mode", "mock")
    monkeypatch.setattr(settings, "demo_ipn_enabled", True)
    monkeypatch.setattr(settings, "demo_ipn_secret", DEMO_SECRET)

    response = client.post(
        "/api/v1/payments/ipn",
        json=make_ipn(payment, demo=True),
    )

    assert response.status_code == 400
    with test_sessions() as session:
        saved = session.query(Payment).filter_by(payment_id=payment.payment_id).one()
        assert saved.status == "PENDING"


def test_callback_during_create_is_preserved(webhook_api, monkeypatch):
    _, test_sessions = webhook_api
    monkeypatch.setattr(settings, "payment_provider_mode", "mock")
    monkeypatch.setattr(settings, "demo_ipn_enabled", True)
    monkeypatch.setattr(settings, "demo_ipn_secret", DEMO_SECRET)

    class CallbackBeforeCreateResponse:
        async def create_payment(
            self, payment_id, provider_request_id, amount, order_id, description
        ):
            with test_sessions() as callback_session:
                payment = callback_session.query(Payment).filter_by(
                    payment_id=payment_id
                ).one()
                await PaymentService(callback_session).handle_ipn(
                    MoMoIPNRequest(**make_ipn(payment, demo=True, trans_id=555))
                )
            return {
                "pay_url": f"https://mock.momo.vn/pay/{payment_id}",
                "environment": "mock",
                "provider_code": 0,
                "provider_transaction_id": "CREATE-TRANS",
            }

    request_data = PaymentCreateRequest(
        order_id=f"ORDER-{uuid.uuid4().hex[:8]}",
        amount=50000,
        currency="VND",
        description="Callback during create",
    )
    with test_sessions() as session:
        code, response = asyncio.run(
            PaymentService(session, provider=CallbackBeforeCreateResponse()).create_payment(
                request_data, f"IDEM-{uuid.uuid4().hex}"
            )
        )
    assert code == 200
    assert response.status == "SUCCESS"

    with test_sessions() as check_session:
        saved = (
            check_session.query(Payment)
            .filter_by(order_id=request_data.order_id)
            .one()
        )
        assert saved.status == "SUCCESS"
        assert saved.provider_response_code == 0
        assert saved.provider_transaction_id == "555"
        assert saved.pay_url == f"https://mock.momo.vn/pay/{saved.payment_id}"
