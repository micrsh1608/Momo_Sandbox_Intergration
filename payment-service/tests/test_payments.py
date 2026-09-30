import asyncio
import concurrent.futures
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PAYMENT_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(PAYMENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(PAYMENT_SERVICE_DIR))

from app.config import settings
from app.database import engine, SessionLocal
from app.main import app
from app.models import Base, Payment
from app.providers.base import ProviderMalformedResponseError
from app.providers.mock import MockProvider
from app.providers.momo import MomoProvider
from app.schemas import PaymentCreateRequest

client = TestClient(app)

HEADERS_VALID = {
    "X-Internal-Token": settings.internal_token,
    "Content-Type": "application/json",
}


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield


def test_case_1_valid_request():
    """1. Điều kiện hợp lệ, provider trả kết quả hợp lệ -> HTTP 201, trạng thái PENDING, có pay_url."""
    idempotency_key = f"KEY-{uuid.uuid4().hex}"
    order_id = f"ORD-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "amount": 50000,
        "currency": "VND",
        "description": "Thanh toan ve workshop Python",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    response = client.post("/internal/payments", json=payload, headers=headers)
    assert response.status_code == 201
    data = response.json()
    assert data["order_id"] == order_id
    assert data["status"] == "PENDING"
    assert data["pay_url"] is not None


def test_case_2_missing_or_invalid_token():
    """2. Thiếu hoặc sai token -> 401, không gọi provider."""
    idempotency_key = f"KEY-{uuid.uuid4().hex}"
    payload = {
        "order_id": f"ORD-{uuid.uuid4().hex[:6]}",
        "amount": 50000,
        "currency": "VND",
        "description": "Test invalid token",
    }

    # Missing token
    resp1 = client.post(
        "/internal/payments",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    assert resp1.status_code == 401

    # Invalid token
    resp2 = client.post(
        "/internal/payments",
        json=payload,
        headers={"X-Internal-Token": "wrong_token", "Idempotency-Key": idempotency_key},
    )
    assert resp2.status_code == 401


def test_case_3_invalid_amount_and_length():
    """3. Số tiền 0, âm, thập phân, boolean hoặc vượt độ dài -> 400."""
    idempotency_key = f"KEY-{uuid.uuid4().hex}"

    # Amount = 0
    resp_zero = client.post(
        "/internal/payments",
        json={"order_id": "ORD-1", "amount": 0, "currency": "VND", "description": "Test"},
        headers={**HEADERS_VALID, "Idempotency-Key": idempotency_key},
    )
    assert resp_zero.status_code == 400

    # Amount negative
    resp_neg = client.post(
        "/internal/payments",
        json={"order_id": "ORD-1", "amount": -50000, "currency": "VND", "description": "Test"},
        headers={**HEADERS_VALID, "Idempotency-Key": idempotency_key},
    )
    assert resp_neg.status_code == 400

    # Amount float
    resp_float = client.post(
        "/internal/payments",
        json={"order_id": "ORD-1", "amount": 50000.5, "currency": "VND", "description": "Test"},
        headers={**HEADERS_VALID, "Idempotency-Key": idempotency_key},
    )
    assert resp_float.status_code == 400

    # Exceeding order_id length > 64 chars
    resp_len = client.post(
        "/internal/payments",
        json={"order_id": "A" * 65, "amount": 50000, "currency": "VND", "description": "Test"},
        headers={**HEADERS_VALID, "Idempotency-Key": idempotency_key},
    )
    assert resp_len.status_code == 400


def test_case_4_idempotent_duplicate_call():
    """4. Cùng key, cùng dữ liệu gọi hai lần -> Cùng payment_id, HTTP 200, không gọi lại provider."""
    idempotency_key = f"KEY-IDEM-{uuid.uuid4().hex}"
    order_id = f"ORD-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "amount": 100000,
        "currency": "VND",
        "description": "Test Idempotency",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    res1 = client.post("/internal/payments", json=payload, headers=headers)
    assert res1.status_code == 201
    data1 = res1.json()

    res2 = client.post("/internal/payments", json=payload, headers=headers)
    assert res2.status_code == 200
    data2 = res2.json()

    assert data1["payment_id"] == data2["payment_id"]
    assert data1["pay_url"] == data2["pay_url"]


def test_case_5_idempotency_key_body_mismatch():
    """5. Cùng key, khác dữ liệu -> 409 Conflict."""
    idempotency_key = f"KEY-MISMATCH-{uuid.uuid4().hex}"
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    payload1 = {
        "order_id": f"ORD-A-{uuid.uuid4().hex[:6]}",
        "amount": 50000,
        "currency": "VND",
        "description": "Description A",
    }
    res1 = client.post("/internal/payments", json=payload1, headers=headers)
    assert res1.status_code == 201

    payload2 = {
        "order_id": f"ORD-B-{uuid.uuid4().hex[:6]}",
        "amount": 90000,
        "currency": "VND",
        "description": "Description B",
    }
    res2 = client.post("/internal/payments", json=payload2, headers=headers)
    assert res2.status_code == 409


def test_case_6_concurrent_requests_same_key():
    """6. Hai request cùng key đến đồng thời -> Chỉ một lần khởi tạo chính thức thực hiện."""
    idempotency_key = f"KEY-CONCUR-{uuid.uuid4().hex}"
    order_id = f"ORD-CONCUR-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "amount": 75000,
        "currency": "VND",
        "description": "Concurrent test",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    def make_request():
        with TestClient(app) as local_client:
            return local_client.post("/internal/payments", json=payload, headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(make_request)
        f2 = executor.submit(make_request)
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    assert 201 in statuses
    assert all(code in [200, 201] for code in statuses)
    p_ids = [r1.json()["payment_id"], r2.json()["payment_id"]]
    assert p_ids[0] == p_ids[1]


def test_case_7_concurrent_different_keys_same_order():
    """7. Hai request đồng thời khác key cho CÙNG ĐƠN HÀNG -> Chỉ một request thành công, request kia nhận 409."""
    order_id = f"ORD-CONCUR-DIFFKEY-{uuid.uuid4().hex[:6]}"
    payload1 = {
        "order_id": order_id,
        "amount": 50000,
        "currency": "VND",
        "description": "Attempt 1",
    }
    payload2 = {
        "order_id": order_id,
        "amount": 50000,
        "currency": "VND",
        "description": "Attempt 2",
    }
    key1 = f"KEY-CONCUR-1-{uuid.uuid4().hex}"
    key2 = f"KEY-CONCUR-2-{uuid.uuid4().hex}"

    def make_req(p, k):
        with TestClient(app) as local_client:
            h = {**HEADERS_VALID, "Idempotency-Key": k}
            return local_client.post("/internal/payments", json=p, headers=h)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(make_req, payload1, key1)
        f2 = executor.submit(make_req, payload2, key2)
        r1 = f1.result()
        r2 = f2.result()

    codes = [r1.status_code, r2.status_code]
    assert 201 in codes
    assert 409 in codes


def test_case_8_momo_response_strict_field_and_signature_verification():
    """8. Test xác minh response MoMo: thiếu partnerCode, orderId, requestId, amount hoặc signature -> ProviderMalformedResponseError."""
    momo = MomoProvider()

    # Case A: Missing partnerCode
    with pytest.raises(ProviderMalformedResponseError):
        asyncio.run(momo.create_payment("PAY-1", "REQ-1", 50000, "ORD-1", "Test"))

    # Case B: Test verify_response_signature directly with valid and invalid signatures
    partner_code = settings.momo_partner_code
    access_key = settings.momo_access_key
    secret_key = settings.momo_secret_key

    valid_resp = {
        "partnerCode": partner_code,
        "orderId": "PAY-100",
        "requestId": "REQ-100",
        "amount": 50000,
        "responseTime": 1660000000,
        "message": "Success",
        "resultCode": 0,
        "payUrl": "https://test-payment.momo.vn/pay",
    }
    # Raw signature string format for MoMo Create Payment response
    import hmac, hashlib
    raw_sig = (
        f"accessKey={access_key}&amount=50000&extraData=&message=Success"
        f"&orderId=PAY-100&orderInfo=&orderType=&partnerCode={partner_code}"
        f"&payUrl=https://test-payment.momo.vn/pay&requestId=REQ-100&responseTime=1660000000&resultCode=0"
    )
    valid_sig = hmac.new(secret_key.encode("utf-8"), raw_sig.encode("utf-8"), hashlib.sha256).hexdigest()
    valid_resp["signature"] = valid_sig

    assert momo.verify_response_signature(valid_resp, secret_key, access_key) is True

    # Bad signature
    valid_resp["signature"] = "invalid_signature_hex"
    assert momo.verify_response_signature(valid_resp, secret_key, access_key) is False


def test_case_9_momo_rejected_business_error():
    """9. MoMo trả lỗi nghiệp vụ xác định -> 502 Bad Gateway, chứa payment_id."""
    idempotency_key = f"KEY-REJECT-{uuid.uuid4().hex}"
    payload = {
        "order_id": f"ORD-REJECT-{uuid.uuid4().hex[:6]}",
        "amount": 888888,
        "currency": "VND",
        "description": "[MOCK_REJECTED] Payment test",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    res = client.post("/internal/payments", json=payload, headers=headers)
    assert res.status_code == 502
    assert "MoMo từ chối" in str(res.json()["detail"])
    assert "PAY-" in str(res.json()["detail"])


def test_case_10_momo_timeout():
    """10. Timeout sau khi gửi -> HTTP 202, status UNKNOWN, pay_url null."""
    idempotency_key = f"KEY-TIMEOUT-{uuid.uuid4().hex}"
    payload = {
        "order_id": f"ORD-TIMEOUT-{uuid.uuid4().hex[:6]}",
        "amount": 999999,
        "currency": "VND",
        "description": "[MOCK_TIMEOUT] Payment test",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    res = client.post("/internal/payments", json=payload, headers=headers)
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "UNKNOWN"
    assert data["pay_url"] is None


def test_case_11_momo_malformed_response():
    """11. HTTP 200 nhưng response sai cấu trúc -> HTTP 502, gán status UNKNOWN để đối soát."""
    idempotency_key = f"KEY-MALFORMED-{uuid.uuid4().hex}"
    payload = {
        "order_id": f"ORD-MALFORMED-{uuid.uuid4().hex[:6]}",
        "amount": 777777,
        "currency": "VND",
        "description": "[MOCK_MALFORMED] Payment test",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    res = client.post("/internal/payments", json=payload, headers=headers)
    assert res.status_code == 502


def test_case_12_ipn_early_arrival_success_and_failed():
    """12. IPN đến sớm (SUCCESS hoặc FAILED) -> Service trả về đúng trạng thái thực tế, không ghi đè thành PENDING."""
    from app.services.payment_service import PaymentService

    # Case A: Early IPN SUCCESS
    key1 = f"KEY-IPN-SUCCESS-{uuid.uuid4().hex}"
    order1 = f"ORD-IPN-S-{uuid.uuid4().hex[:6]}"
    req1 = PaymentCreateRequest(order_id=order1, amount=50000, currency="VND", description="Test")

    with SessionLocal() as db:
        service = PaymentService(db=db)
        p1 = Payment(
            payment_id=f"PAY-S-{uuid.uuid4().hex[:6]}",
            provider_request_id=f"REQ-S-{uuid.uuid4().hex[:6]}",
            order_id=order1,
            idempotency_key=key1,
            request_hash=service._compute_request_hash(req1),
            amount=50000,
            currency="VND",
            description="IPN SUCCESS Pre-insert",
            status="SUCCESS",
        )
        db.add(p1)
        db.commit()

        status_code1, resp1 = asyncio.run(service.create_payment(request_data=req1, idempotency_key=key1))
        assert status_code1 == 200
        assert resp1.status == "SUCCESS"

    # Case B: Early IPN FAILED
    key2 = f"KEY-IPN-FAILED-{uuid.uuid4().hex}"
    order2 = f"ORD-IPN-F-{uuid.uuid4().hex[:6]}"
    req2 = PaymentCreateRequest(order_id=order2, amount=50000, currency="VND", description="Test")

    with SessionLocal() as db:
        service = PaymentService(db=db)
        p2 = Payment(
            payment_id=f"PAY-F-{uuid.uuid4().hex[:6]}",
            provider_request_id=f"REQ-F-{uuid.uuid4().hex[:6]}",
            order_id=order2,
            idempotency_key=key2,
            request_hash=service._compute_request_hash(req2),
            amount=50000,
            currency="VND",
            description="IPN FAILED Pre-insert",
            status="FAILED",
            provider_message="IPN cancelled",
        )
        db.add(p2)
        db.commit()

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(service.create_payment(request_data=req2, idempotency_key=key2))
        assert exc_info.value.status_code == 502


def test_case_13_db_commit_failure_post_provider(monkeypatch):
    """13. DB commit thất bại sau khi provider trả về thành công -> Ném 502 Bad Gateway chứ không trả 201 PENDING giả."""
    idempotency_key = f"KEY-COMMIT-FAIL-{uuid.uuid4().hex}"
    order_id = f"ORD-COMMIT-FAIL-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "amount": 50000,
        "currency": "VND",
        "description": "Commit failure test",
    }

    from sqlalchemy.exc import SQLAlchemyError
    from app.services.payment_service import PaymentService

    with SessionLocal() as db:
        service = PaymentService(db=db)
        req = PaymentCreateRequest(**payload)

        original_commit = db.commit
        commit_calls = 0

        def failing_commit():
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls >= 2:
                raise SQLAlchemyError("Disk failure during post-provider update")
            return original_commit()

        monkeypatch.setattr(db, "commit", failing_commit)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(service.create_payment(request_data=req, idempotency_key=idempotency_key))

        assert exc_info.value.status_code == 502
        assert "Lỗi lưu trữ" in str(exc_info.value.detail)


def test_case_14_persistence_across_db_reconnect():
    """14. Test Persistence: Giao dịch được lưu bền vững trên DB qua nhiều session kết nối."""
    idempotency_key = f"KEY-PERSIST-{uuid.uuid4().hex}"
    order_id = f"ORD-PERSIST-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "amount": 65000,
        "currency": "VND",
        "description": "Persistence test",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    res1 = client.post("/internal/payments", json=payload, headers=headers)
    assert res1.status_code == 201
    pid1 = res1.json()["payment_id"]

    # Verify query from a completely new Session
    with SessionLocal() as new_session:
        saved = new_session.query(Payment).filter(Payment.payment_id == pid1).first()
        assert saved is not None
        assert saved.order_id == order_id
        assert saved.status == "PENDING"
