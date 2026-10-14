import asyncio
import concurrent.futures
import hashlib
import hmac
import os
import subprocess
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pyodbc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

PAYMENT_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(PAYMENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(PAYMENT_SERVICE_DIR))

import app.database as db_module
import app.main as main_module
from app.config import settings
from app.database import get_db
from app.main import (
    _is_valid_active_order_filter,
    app,
    ensure_db_schema_and_indexes,
    verify_active_order_index,
)
from app.models import Payment
from app.providers.base import (
    BasePaymentProvider,
    ProviderMalformedResponseError,
    ProviderTimeoutError,
)
from app.providers.mock import MockProvider
from app.providers.momo import MomoProvider
from app.schemas import PaymentAPIException, PaymentCreateRequest
from app.services.payment_service import PaymentService
from app.services.payment_status import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
    classify_momo_result_code,
    is_momo_query_request_error,
)

# =====================================================================
# M6: CẤU HÌNH DATABASE KIỂM THỬ ĐỘC LẬP (PaymentDB_Test) KHỞI TẠO TRỄ
# Không kết nối SQL Server ở thời điểm import module để Unit Test chạy độc lập
# =====================================================================
TEST_DB_NAME = f"{settings.db_name}_Test"
_TEST_ENGINE = None
_TestSessionLocal = None

HEADERS_VALID = {
    "X-Internal-Token": settings.internal_token,
    "Content-Type": "application/json",
}

client = TestClient(app)


def _get_or_init_test_db():
    """Khởi tạo lười (lazy init) PaymentDB_Test chỉ khi chạy Integration Test cần DB."""
    global _TEST_ENGINE, _TestSessionLocal
    if _TEST_ENGINE is not None and _TestSessionLocal is not None:
        return _TEST_ENGINE, _TestSessionLocal

    trust_certificate = "yes" if settings.db_trust_server_certificate else "no"
    master_conn_str = (
        f"DRIVER={{{settings.db_driver}}};"
        f"SERVER={settings.db_server};"
        "DATABASE=master;"
        "Trusted_Connection=yes;"
        "Encrypt=yes;"
        f"TrustServerCertificate={trust_certificate};"
    )
    with pyodbc.connect(master_conn_str, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"IF DB_ID(N'{TEST_DB_NAME}') IS NULL CREATE DATABASE [{TEST_DB_NAME}];"
            )

    test_conn_str = (
        f"DRIVER={{{settings.db_driver}}};"
        f"SERVER={settings.db_server};"
        f"DATABASE={TEST_DB_NAME};"
        "Trusted_Connection=yes;"
        "Encrypt=yes;"
        f"TrustServerCertificate={trust_certificate};"
    )
    test_url = f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(test_conn_str)}"
    _TEST_ENGINE = create_engine(test_url, pool_pre_ping=True, connect_args={"timeout": 10})
    _TestSessionLocal = sessionmaker(bind=_TEST_ENGINE, autoflush=False, expire_on_commit=False)
    return _TEST_ENGINE, _TestSessionLocal


@pytest.fixture
def db_test_env(monkeypatch):
    """
    Fixture dành riêng cho Integration Tests cần SQL Server (`PaymentDB_Test`):
    1. Chỉ kết nối và khởi tạo `PaymentDB_Test` khi bài test yêu cầu fixture này.
    2. Ép `payment_provider_mode = 'mock'` mặc định.
    3. Đảm bảo bảng và filtered unique index hợp lệ rồi làm sạch dữ liệu trước mỗi test.
    """
    test_engine, test_session_local = _get_or_init_test_db()
    monkeypatch.setattr(settings, "payment_provider_mode", "mock")
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", test_session_local)
    monkeypatch.setattr(main_module, "engine", test_engine)

    def override_get_db():
        db = test_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    ensure_db_schema_and_indexes(test_engine)
    with test_engine.begin() as conn:
        conn.execute(text("DELETE FROM payments;"))

    yield test_engine, test_session_local


# =====================================================================
# PHẦN 1: UNIT TESTS THUẦN LOGIC (KHÔNG CẦN KẾT NỐI SQL SERVER)
# =====================================================================


def test_unit_no_circular_import_on_momo_provider():
    """Kiểm tra import độc lập `from app.providers.momo import MomoProvider` trên tiến trình Python mới không bị lỗi vòng phụ thuộc."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PAYMENT_SERVICE_DIR)
    proc = subprocess.run(
        [sys.executable, "-c", "from app.providers.momo import MomoProvider; print(MomoProvider.__name__)"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Circular import detected: {proc.stderr}"
    assert proc.stdout.strip() == "MomoProvider"


def test_case_8_momo_response_strict_field_and_signature_verification(monkeypatch):
    """
    8. (M3 & M6 - Unit Test) Xác minh response MoMo với mock HTTP (không cần SQL Server, không gọi mạng thật):
    - Kiểm tra vector chữ ký độc lập.
    - Kiểm tra create response signature KHÔNG đưa extraData, orderInfo, orderType vào chuỗi ký.
    - Kiểm tra chặt kiểu dữ liệu: chặn resultCode=False (bool), chặn amount=50000.5 (float), chặn chữ ký sai độ dài/non-hex.
    """
    momo = MomoProvider()

    test_partner = "MOMO_PARTNER_TEST"
    test_access = "ACCESS_KEY_TEST_123"
    test_secret = "SECRET_KEY_TEST_456"
    monkeypatch.setattr(settings, "momo_partner_code", test_partner)
    monkeypatch.setattr(settings, "momo_access_key", test_access)
    monkeypatch.setattr(settings, "momo_secret_key", test_secret)

    expected_raw = (
        "accessKey=ACCESS_KEY_TEST_123"
        "&amount=50000"
        "&message=Successful."
        "&orderId=PAY-VEC-1"
        "&partnerCode=MOMO_PARTNER_TEST"
        "&payUrl=https://test-payment.momo.vn/v2/gateway/pay?t=123"
        "&requestId=REQ-VEC-1"
        "&responseTime=1700000000000"
        "&resultCode=0"
    )
    expected_sig = hmac.new(
        test_secret.encode("utf-8"),
        expected_raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    valid_response_payload = {
        "partnerCode": test_partner,
        "orderId": "PAY-VEC-1",
        "requestId": "REQ-VEC-1",
        "amount": 50000,
        "responseTime": 1700000000000,
        "message": "Successful.",
        "resultCode": 0,
        "payUrl": "https://test-payment.momo.vn/v2/gateway/pay?t=123",
        "extraData": "some_extra_data_that_must_not_break_create_sig",
        "orderInfo": "Order info",
        "orderType": "momo_wallet",
        "signature": expected_sig,
    }

    assert momo.verify_create_response_signature(valid_response_payload, test_secret, test_access) is True

    def mock_http_response(json_data, status_code=200):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    # Case A: Hợp lệ qua mock HTTP
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        AsyncMock(return_value=mock_http_response(valid_response_payload)),
    )
    result = asyncio.run(momo.create_payment("PAY-VEC-1", "REQ-VEC-1", 50000, "ORD-1", "Test"))
    assert result["pay_url"] == "https://test-payment.momo.vn/v2/gateway/pay?t=123"
    assert result["provider_code"] == 0

    # Case B: resultCode = False (boolean không được coi là 0)
    bad_bool_payload = {**valid_response_payload, "resultCode": False}
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        AsyncMock(return_value=mock_http_response(bad_bool_payload)),
    )
    with pytest.raises(ProviderMalformedResponseError, match="boolean"):
        asyncio.run(momo.create_payment("PAY-VEC-1", "REQ-VEC-1", 50000, "ORD-1", "Test"))

    # Case C: amount = 50000.5 (float không được ép kiểu int làm cụt phần thập phân)
    bad_float_payload = {**valid_response_payload, "amount": 50000.5}
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        AsyncMock(return_value=mock_http_response(bad_float_payload)),
    )
    with pytest.raises(ProviderMalformedResponseError):
        asyncio.run(momo.create_payment("PAY-VEC-1", "REQ-VEC-1", 50000, "ORD-1", "Test"))

    # Case D: Chữ ký không phải 64 ký tự hex
    assert momo.verify_create_response_signature(
        {**valid_response_payload, "signature": "not-a-64-char-hex"},
        test_secret,
        test_access,
    ) is False
    assert momo.verify_create_response_signature(
        {**valid_response_payload, "signature": "z" * 64},
        test_secret,
        test_access,
    ) is False

    # Case E: Kiểm tra tách biệt hàm verify_ipn_signature
    ipn_payload = {
        "partnerCode": test_partner,
        "orderId": "PAY-VEC-1",
        "requestId": "REQ-VEC-1",
        "amount": 50000,
        "orderInfo": "Pay workshop",
        "orderType": "momo_wallet",
        "transId": 2999888777,
        "resultCode": 0,
        "message": "Successful.",
        "payType": "qr",
        "responseTime": 1700000000000,
        "extraData": "",
    }
    ipn_raw = (
        f"accessKey={test_access}&amount=50000&extraData=&message=Successful."
        f"&orderId=PAY-VEC-1&orderInfo=Pay workshop&orderType=momo_wallet"
        f"&partnerCode={test_partner}&payType=qr&requestId=REQ-VEC-1"
        f"&responseTime=1700000000000&resultCode=0&transId=2999888777"
    )
    ipn_sig = hmac.new(
        test_secret.encode("utf-8"),
        ipn_raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    ipn_payload["signature"] = ipn_sig
    assert momo.verify_ipn_signature(ipn_payload, test_secret, test_access) is True


@pytest.mark.parametrize(
    "code,context,flow,expected_status",
    [
        (0, "create", "one_step", STATUS_PENDING),
        (0, "query", "one_step", STATUS_SUCCESS),
        (0, "ipn", "one_step", STATUS_SUCCESS),
        # Các mã lỗi gọi API / xác thực / tham số (11/12/13/20/21/22/42):
        # - ở create -> FAILED (chưa tạo được giao dịch)
        # - ở query/ipn -> UNKNOWN (lỗi gọi API đối soát không chứng minh thanh toán gốc thất bại)
        (11, "create", "one_step", STATUS_FAILED),
        (13, "create", "one_step", STATUS_FAILED),
        (20, "create", "one_step", STATUS_FAILED),
        (42, "create", "one_step", STATUS_FAILED),
        (11, "query", "one_step", STATUS_UNKNOWN),
        (12, "query", "one_step", STATUS_UNKNOWN),
        (13, "query", "one_step", STATUS_UNKNOWN),
        (20, "query", "one_step", STATUS_UNKNOWN),
        (21, "query", "one_step", STATUS_UNKNOWN),
        (22, "query", "one_step", STATUS_UNKNOWN),
        (42, "ipn", "one_step", STATUS_UNKNOWN),
        # Các mã trùng/xung đột chưa có trạng thái cuối -> UNKNOWN
        (40, "create", "one_step", STATUS_UNKNOWN),
        (41, "create", "one_step", STATUS_UNKNOWN),
        (43, "create", "one_step", STATUS_UNKNOWN),
        (40, "query", "one_step", STATUS_UNKNOWN),
        # Các mã đang xử lý / bảo trì -> UNKNOWN
        (10, "create", "one_step", STATUS_UNKNOWN),
        (1000, "create", "one_step", STATUS_UNKNOWN),
        (7000, "create", "one_step", STATUS_UNKNOWN),
        (7002, "create", "one_step", STATUS_UNKNOWN),
        # Mã 9000 (Authorized) phân biệt theo context và luồng 1 bước vs 2 bước
        (9000, "create", "one_step", STATUS_UNKNOWN),
        (9000, "create", "two_step", STATUS_UNKNOWN),
        (9000, "query", "one_step", STATUS_SUCCESS),
        (9000, "ipn", "one_step", STATUS_SUCCESS),
        (9000, "query", "two_step", STATUS_PENDING),
        (9000, "ipn", "two_step", STATUS_PENDING),
        # Các mã hoàn tiền không được kết luận thanh toán gốc FAILED -> UNKNOWN
        (1080, "create", "one_step", STATUS_UNKNOWN),
        (1081, "query", "one_step", STATUS_UNKNOWN),
        # Các mã kết thúc thất bại (bao gồm 98, 99, 1006) -> FAILED
        (98, "create", "one_step", STATUS_FAILED),
        (99, "create", "one_step", STATUS_FAILED),
        (98, "ipn", "one_step", STATUS_FAILED),
        (99, "query", "one_step", STATUS_FAILED),
        (1006, "create", "one_step", STATUS_FAILED),
        (1006, "query", "one_step", STATUS_FAILED),
        # Mã lạ chưa xác định -> UNKNOWN
        (5555, "create", "one_step", STATUS_UNKNOWN),
    ],
)
def test_unit_classify_momo_result_codes(code, context, flow, expected_status):
    """(M4 - Unit Test) Kiểm tra bảng ánh xạ mã resultCode theo loại nghiệp vụ, ngữ cảnh và luồng 1 bước/2 bước."""
    assert classify_momo_result_code(code, context=context, payment_flow=flow) == expected_status
    if code in (11, 12, 13, 20, 21, 22, 42):
        assert is_momo_query_request_error(code) is True
    else:
        assert is_momo_query_request_error(code) is False


@pytest.mark.parametrize(
    "filter_expr,expected_valid",
    [
        ("([status]='SUCCESS' OR [status]='UNKNOWN' OR [status]='PENDING' OR [status]='CREATED')", True),
        ("(([status]='CREATED' OR [status]='PENDING' OR [status]='UNKNOWN' OR [status]='SUCCESS'))", True),
        ("status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS')", True),
        ("[status] IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS')", True),
        # Từ chối biểu thức lọc có điều kiện phụ như AND order_id IS NULL
        ("status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS') AND order_id IS NULL", False),
        ("([status]='SUCCESS' OR [status]='UNKNOWN' OR [status]='PENDING' OR [status]='CREATED') AND [order_id] IS NULL", False),
        ("([status]='SUCCESS' OR [status]='UNKNOWN' OR [status]='PENDING' OR [status]='CREATED' OR [order_id] IS NULL)", False),
        # Từ chối biểu thức thiếu trạng thái hoặc thừa FAILED
        ("([status]='CREATED' OR [status]='PENDING' OR [status]='UNKNOWN')", False),
        ("status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS', 'FAILED')", False),
        ("([status]<>'FAILED')", False),
        ("", False),
    ],
)
def test_unit_active_order_index_filter_validation(filter_expr, expected_valid):
    """(Issue 4 - Unit Test) Kiểm tra hàm _is_valid_active_order_filter từ chối mọi điều kiện lọc phụ hoặc sai tập trạng thái."""
    assert _is_valid_active_order_filter(filter_expr) is expected_valid


# =====================================================================
# PHẦN 2: INTEGRATION TESTS VỚI DATABASE ĐỘC LẬP (PaymentDB_Test)
# =====================================================================


def test_case_1_valid_request(db_test_env):
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


def test_case_2_missing_or_invalid_token(db_test_env):
    """2. Thiếu hoặc sai token -> 401, không gọi provider."""
    idempotency_key = f"KEY-{uuid.uuid4().hex}"
    payload = {
        "order_id": f"ORD-{uuid.uuid4().hex[:6]}",
        "amount": 50000,
        "currency": "VND",
        "description": "Test invalid token",
    }

    resp1 = client.post(
        "/internal/payments",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    assert resp1.status_code == 401

    resp2 = client.post(
        "/internal/payments",
        json=payload,
        headers={"X-Internal-Token": "wrong_token", "Idempotency-Key": idempotency_key},
    )
    assert resp2.status_code == 401


def test_case_3_invalid_amount_and_length(db_test_env):
    """3. Số tiền 0, âm, thập phân, boolean hoặc vượt độ dài -> 400."""
    idempotency_key = f"KEY-{uuid.uuid4().hex}"

    for bad_amount in [0, -50000, 50000.5, True, False]:
        resp = client.post(
            "/internal/payments",
            json={"order_id": "ORD-1", "amount": bad_amount, "currency": "VND", "description": "Test"},
            headers={**HEADERS_VALID, "Idempotency-Key": idempotency_key},
        )
        assert resp.status_code == 400

    resp_len = client.post(
        "/internal/payments",
        json={"order_id": "A" * 65, "amount": 50000, "currency": "VND", "description": "Test"},
        headers={**HEADERS_VALID, "Idempotency-Key": idempotency_key},
    )
    assert resp_len.status_code == 400


def test_case_4_idempotent_duplicate_call(db_test_env):
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


def test_case_5_idempotency_key_body_mismatch(db_test_env):
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


def test_case_6_concurrent_requests_same_key(db_test_env, monkeypatch):
    """
    6. (M2 & M6) Hai request cùng key đến đồng thời:
    - Request 1 tạo bản ghi CREATED và đang gọi provider.
    - Request 2 đụng IntegrityError / tìm thấy bản ghi CREATED -> nhận 202 Accepted (hoặc 200 nếu R1 đã xong),
      trả về cùng payment_id và provider chỉ được gọi đúng 1 lần.
    """
    idempotency_key = f"KEY-CONCUR-{uuid.uuid4().hex}"
    order_id = f"ORD-CONCUR-{uuid.uuid4().hex[:6]}"
    payload = {
        "order_id": order_id,
        "amount": 75000,
        "currency": "VND",
        "description": "Concurrent same key test",
    }
    headers = {**HEADERS_VALID, "Idempotency-Key": idempotency_key}

    provider_entered = threading.Event()
    allow_provider_finish = threading.Event()
    provider_call_count = 0
    count_lock = threading.Lock()

    class SlowSingleCallProvider(BasePaymentProvider):
        async def create_payment(
            self,
            payment_id: str,
            provider_request_id: str,
            amount: int,
            order_id: str,
            description: str,
        ):
            nonlocal provider_call_count
            with count_lock:
                provider_call_count += 1
            provider_entered.set()
            allow_provider_finish.wait(timeout=5.0)
            return {
                "pay_url": f"https://test-payment.momo.vn/pay/{payment_id}",
                "environment": "mock",
                "provider_code": 0,
                "provider_message": "Successful.",
                "provider_transaction_id": f"MOMO-{payment_id}",
            }

    monkeypatch.setattr(
        "app.services.payment_service.get_payment_provider",
        lambda: SlowSingleCallProvider(),
    )

    def make_request_1():
        with TestClient(app) as local_client:
            return local_client.post("/internal/payments", json=payload, headers=headers)

    def make_request_2():
        provider_entered.wait(timeout=5.0)
        with TestClient(app) as local_client:
            resp = local_client.post("/internal/payments", json=payload, headers=headers)
        allow_provider_finish.set()
        return resp

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(make_request_1)
        f2 = executor.submit(make_request_2)
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    assert 201 in statuses
    assert 202 in statuses or 200 in statuses
    assert all(code in [200, 201, 202] for code in statuses)
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    assert provider_call_count == 1


def test_case_7_concurrent_different_keys_same_order(db_test_env):
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


def test_case_9_momo_rejected_business_error(db_test_env):
    """9. (M4 & M5) MoMo trả mã lỗi từ chối xác định -> 502 Bad Gateway, chứa payment_id và provider_code ở top-level JSON."""
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
    body = res.json()
    assert "MoMo từ chối" in body["detail"]
    assert body["payment_id"].startswith("PAY-")
    assert body["provider_code"] == 1006


def test_case_10_momo_timeout(db_test_env):
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


def test_case_11_momo_malformed_response(db_test_env):
    """11. HTTP 200 nhưng response sai cấu trúc -> HTTP 502 kèm top-level payment_id, gán status UNKNOWN để đối soát."""
    _, test_session_local = db_test_env
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
    body = res.json()
    assert body["payment_id"].startswith("PAY-")

    with test_session_local() as db:
        saved = db.query(Payment).filter(Payment.payment_id == body["payment_id"]).first()
        assert saved is not None
        assert saved.status == "UNKNOWN"


def test_case_12_ipn_concurrent_update_before_and_between_select_and_update(db_test_env, monkeypatch):
    """
    12. (M1) Kiểm tra IPN cập nhật đồng thời ở cả 2 khoảng thời gian:
    - Khoảng 1: Trong lúc đang gọi provider (trước bước SELECT kiểm tra trạng thái sau provider).
    - Khoảng 2: Trên nhánh provider trả về THÀNH CÔNG (`MockProvider`), IPN xen vào đúng khe hở
      SAU bước SELECT (`current_p.status == 'CREATED'`) và TRƯỚC câu lệnh `UPDATE ... SET status='PENDING' WHERE status IN ('CREATED', 'UNKNOWN')`,
      khiến `updated_count == 0` và buộc hệ thống phải dựa vào `_reload_payment` để trả đúng kết quả.
    """
    _, test_session_local = db_test_env

    # --- Khoảng 1: IPN cập nhật trong lúc gọi provider (timeout nhưng IPN đã ghi SUCCESS) ---
    class ConcurrentIPNSuccessOnTimeoutProvider(BasePaymentProvider):
        async def create_payment(self, payment_id: str, provider_request_id: str, amount: int, order_id: str, description: str):
            with test_session_local() as ipn_db:
                ipn_db.query(Payment).filter(Payment.payment_id == payment_id).update(
                    {
                        "status": "SUCCESS",
                        "provider_transaction_id": "MOMO-IPN-S2",
                        "provider_response_code": 0,
                    },
                    synchronize_session=False,
                )
                ipn_db.commit()
            raise ProviderTimeoutError("Timeout after MoMo processed and sent IPN")

    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda: ConcurrentIPNSuccessOnTimeoutProvider())
    res_timeout_ipn = client.post(
        "/internal/payments",
        json={"order_id": f"ORD-M1-T-{uuid.uuid4().hex[:6]}", "amount": 50000, "currency": "VND", "description": "M1 Timeout IPN"},
        headers={**HEADERS_VALID, "Idempotency-Key": f"KEY-M1-T-{uuid.uuid4().hex}"},
    )
    assert res_timeout_ipn.status_code == 200
    assert res_timeout_ipn.json()["status"] == "SUCCESS"

    # Khôi phục provider về MockProvider (nhánh provider trả về thành công) trước vòng lặp Khoảng 2
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda: MockProvider())

    # --- Khoảng 2 (Regression): Trên nhánh provider thành công, IPN xen vào SAU SELECT và TRƯỚC UPDATE sang PENDING ---
    for target_ipn_status, expected_http in [("SUCCESS", 200), ("FAILED", 502)]:
        with test_session_local() as db:
            service = PaymentService(db=db, provider=MockProvider())
            req = PaymentCreateRequest(
                order_id=f"ORD-M1-GAP-{target_ipn_status}-{uuid.uuid4().hex[:6]}",
                amount=50000,
                currency="VND",
                description="Gap between SELECT and UPDATE on success branch",
            )
            key = f"KEY-M1-GAP-{target_ipn_status}-{uuid.uuid4().hex}"

            original_query = db.query
            pending_update_hook_triggered = False

            def intercepting_query(*entities, **kwargs):
                q = original_query(*entities, **kwargs)
                orig_filter = q.filter

                def wrapped_filter(*criterion):
                    fq = orig_filter(*criterion)
                    crit_str = " ".join(str(c) for c in criterion)
                    if "payments.payment_id" in crit_str and "payments.status IN" in crit_str:
                        orig_update = fq.update

                        def wrapped_update(values, *u_args, **u_kwargs):
                            nonlocal pending_update_hook_triggered
                            target_status_values = {str(v) for v in values.values()}
                            # Xác nhận đang chặn đúng câu lệnh UPDATE chuyển sang PENDING ở nhánh provider thành công
                            if STATUS_PENDING in target_status_values and not pending_update_hook_triggered:
                                pending_update_hook_triggered = True
                                with test_session_local() as ipn_db:
                                    ipn_db.query(Payment).filter(Payment.order_id == req.order_id).update(
                                        {
                                            "status": target_ipn_status,
                                            "provider_response_code": 0 if target_ipn_status == "SUCCESS" else 1006,
                                            "provider_message": f"IPN {target_ipn_status} after SELECT before UPDATE",
                                        },
                                        synchronize_session=False,
                                    )
                                    ipn_db.commit()
                            return orig_update(values, *u_args, **u_kwargs)

                        fq.update = wrapped_update
                    return fq

                q.filter = wrapped_filter
                return q

            monkeypatch.setattr(db, "query", intercepting_query)

            if expected_http == 200:
                code, resp_obj = asyncio.run(service.create_payment(request_data=req, idempotency_key=key))
                assert pending_update_hook_triggered is True
                assert code == 200
                assert resp_obj.status == "SUCCESS"
            else:
                with pytest.raises(PaymentAPIException) as exc_info:
                    asyncio.run(service.create_payment(request_data=req, idempotency_key=key))
                assert pending_update_hook_triggered is True
                assert exc_info.value.status_code == 502
                assert exc_info.value.provider_code == 1006


def test_case_13_db_commit_failure_post_provider_all_branches(db_test_env, monkeypatch):
    """
    13. (M5) DB commit thất bại sau khi gọi provider ở CẢ 5 NHÁNH (thành công, đang xử lý, timeout, lỗi nghiệp vụ, sai cấu trúc):
    - Trả về 503 Service Unavailable (lỗi lưu trữ có kiểm soát), KHÔNG trả về response thành công giả,
      và có trường payment_id ở top-level JSON.
    """
    _, test_session_local = db_test_env
    branches = [
        ("SUCCESS_BRANCH", "Normal payment"),
        ("PENDING_BRANCH", "[MOCK_PROCESSING] Pending payment"),
        ("TIMEOUT_BRANCH", "[MOCK_TIMEOUT] Timeout payment"),
        ("REJECTED_BRANCH", "[MOCK_REJECTED] Rejected payment"),
        ("MALFORMED_BRANCH", "[MOCK_MALFORMED] Malformed payment"),
    ]

    for label, desc in branches:
        with test_session_local() as db:
            service = PaymentService(db=db, provider=MockProvider())
            req = PaymentCreateRequest(
                order_id=f"ORD-M5-{label}-{uuid.uuid4().hex[:4]}",
                amount=50000,
                currency="VND",
                description=desc,
            )
            original_commit = db.commit
            commit_calls = 0

            def failing_second_commit():
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls >= 2:
                    raise SQLAlchemyError(f"Simulated DB failure on {label}")
                return original_commit()

            monkeypatch.setattr(db, "commit", failing_second_commit)

            with pytest.raises(PaymentAPIException) as exc_info:
                asyncio.run(service.create_payment(request_data=req, idempotency_key=f"KEY-M5-{label}-{uuid.uuid4().hex}"))

            assert exc_info.value.status_code == 503
            assert exc_info.value.payment_id is not None
            assert exc_info.value.payment_id.startswith("PAY-")
            assert "Lỗi lưu trữ" in exc_info.value.detail


def test_case_14_persistence_across_db_reconnect(db_test_env):
    """14. Test Persistence: Giao dịch được lưu bền vững trên DB qua nhiều session kết nối."""
    _, test_session_local = db_test_env
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

    with test_session_local() as new_session:
        saved = new_session.query(Payment).filter(Payment.payment_id == pid1).first()
        assert saved is not None
        assert saved.order_id == order_id
        assert saved.status == "PENDING"


@pytest.mark.parametrize("momo_code", [40, 41, 43, 10, 1000, 7000, 7002, 9000, 1080, 1081, 5555])
def test_case_15_m4_each_non_final_code_keeps_unknown_and_blocks_second_provider_call(
    db_test_env, monkeypatch, momo_code
):
    """
    15. (M4) Kiểm tra từng mã MoMo chưa kết thúc (đặc biệt 40/41/43, 10, 1000, 7000, 7002, 9000, 1080/1081, mã lạ):
    - Lần gọi 1: Nhận 202 Accepted, lưu trạng thái UNKNOWN.
    - Lần gọi 2 cùng order_id nhưng Idempotency-Key mới: Phải bị chặn 409 Conflict,
      tuyệt đối KHÔNG gọi provider lần thứ hai (`provider_http_calls == 1`).
    """
    monkeypatch.setattr(settings, "payment_provider_mode", "momo")
    monkeypatch.setattr(settings, "momo_partner_code", "MOMO_TEST")
    monkeypatch.setattr(settings, "momo_access_key", "ACCESS_TEST")
    monkeypatch.setattr(settings, "momo_secret_key", "SECRET_TEST")

    provider_http_calls = 0

    async def fake_momo_post(self_client, url, json=None, headers=None, **kwargs):
        nonlocal provider_http_calls
        provider_http_calls += 1
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "partnerCode": "MOMO_TEST",
            "orderId": json["orderId"],
            "requestId": json["requestId"],
            "amount": json["amount"],
            "responseTime": 1700000000000,
            "message": f"MoMo non-final code {momo_code}",
            "resultCode": momo_code,
        }
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_momo_post)

    order_id = f"ORD-M4-{momo_code}-{uuid.uuid4().hex[:6]}"
    key1 = f"KEY-M4-1-{momo_code}-{uuid.uuid4().hex}"
    key2 = f"KEY-M4-2-{momo_code}-{uuid.uuid4().hex}"
    payload = {
        "order_id": order_id,
        "amount": 50000,
        "currency": "VND",
        "description": f"Test MoMo code {momo_code}",
    }

    # Request thứ nhất nhận mã `momo_code` -> 202 UNKNOWN
    res1 = client.post("/internal/payments", json=payload, headers={**HEADERS_VALID, "Idempotency-Key": key1})
    assert res1.status_code == 202
    assert res1.json()["status"] == "UNKNOWN"
    assert res1.json()["pay_url"] is None
    assert provider_http_calls == 1

    # Request thứ hai cùng order nhưng key mới -> Phải bị chặn 409, provider KHÔNG được gọi lần hai
    res2 = client.post("/internal/payments", json=payload, headers={**HEADERS_VALID, "Idempotency-Key": key2})
    assert res2.status_code == 409
    assert res2.json()["payment_id"] == res1.json()["payment_id"]
    assert provider_http_calls == 1


def test_case_16_m6_sql_server_filtered_unique_index_and_health_verification(db_test_env):
    """
    16. (M6 & M5) Kiểm tra trực tiếp Filtered Unique Index `UQ_payments_active_order` và `/health/db`:
    - Chặn 2 bản ghi cùng order_id khi status thuộc ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS').
    - Cho phép tạo bản ghi mới cho cùng order_id nếu bản ghi trước đó đã 'FAILED'.
    - Kiểm tra `verify_active_order_index` và `/health/db` phát hiện đúng cả 2 trường hợp:
      (a) Index bị DISABLE.
      (b) Index có điều kiện lọc sai (chẳng hạn có thêm `AND description IS NULL` hoặc thiếu trạng thái),
          và `ensure_db_schema_and_indexes` tự động xóa index sai để tạo lại đúng chuẩn.
    """
    test_engine, test_session_local = db_test_env
    order_id = f"ORD-IDX-{uuid.uuid4().hex[:6]}"

    with test_session_local() as db:
        p_failed = Payment(
            payment_id=f"PAY-IDX-1-{uuid.uuid4().hex[:6]}",
            provider_request_id=f"REQ-IDX-1-{uuid.uuid4().hex[:6]}",
            order_id=order_id,
            idempotency_key=f"KEY-IDX-1-{uuid.uuid4().hex}",
            request_hash="hash1",
            amount=50000,
            currency="VND",
            status="FAILED",
        )
        db.add(p_failed)
        db.commit()

        p_active = Payment(
            payment_id=f"PAY-IDX-2-{uuid.uuid4().hex[:6]}",
            provider_request_id=f"REQ-IDX-2-{uuid.uuid4().hex[:6]}",
            order_id=order_id,
            idempotency_key=f"KEY-IDX-2-{uuid.uuid4().hex}",
            request_hash="hash2",
            amount=50000,
            currency="VND",
            status="UNKNOWN",
        )
        db.add(p_active)
        db.commit()

        p_duplicate_active = Payment(
            payment_id=f"PAY-IDX-3-{uuid.uuid4().hex[:6]}",
            provider_request_id=f"REQ-IDX-3-{uuid.uuid4().hex[:6]}",
            order_id=order_id,
            idempotency_key=f"KEY-IDX-3-{uuid.uuid4().hex}",
            request_hash="hash3",
            amount=50000,
            currency="VND",
            status="CREATED",
        )
        db.add(p_duplicate_active)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    # (a) Giả lập vô hiệu hóa (DISABLE) index -> /health/db phải phát hiện và trả 503
    with test_engine.begin() as conn:
        conn.execute(text("ALTER INDEX UQ_payments_active_order ON payments DISABLE;"))
        assert verify_active_order_index(conn) is False

    health_disabled = client.get("/health/db")
    assert health_disabled.status_code == 503

    # (b) Giả lập index có điều kiện lọc sai (thêm `AND description IS NULL` hoặc thiếu trạng thái)
    with test_engine.begin() as conn:
        conn.execute(text("DROP INDEX UQ_payments_active_order ON payments;"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX UQ_payments_active_order ON payments(order_id) "
                "WHERE status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS') AND description IS NULL;"
            )
        )
        assert verify_active_order_index(conn) is False

    health_wrong_filter = client.get("/health/db")
    assert health_wrong_filter.status_code == 503

    # Khôi phục qua ensure_db_schema_and_indexes -> /health/db trả 200 OK
    ensure_db_schema_and_indexes(test_engine)
    health_ok = client.get("/health/db")
    assert health_ok.status_code == 200
    assert health_ok.json() == {"database": "connected", "indexes": "verified"}


def test_case_17_unexpected_integrity_error_returns_503_not_409(db_test_env, monkeypatch):
    """
    17. (Issue 3) Khi xảy ra IntegrityError lúc INSERT nhưng truy vấn lại không có cả
    idempotency_key lẫn active order (ví dụ lỗi constraint khác), hệ thống phải trả 503
    chứ không được báo nhầm 409 trùng đơn.
    """
    _, test_session_local = db_test_env

    with test_session_local() as db:
        service = PaymentService(db=db, provider=MockProvider())
        req = PaymentCreateRequest(
            order_id=f"ORD-UNEXP-IE-{uuid.uuid4().hex[:6]}",
            amount=50000,
            currency="VND",
            description="Unexpected IntegrityError test",
        )

        def fail_with_unexpected_integrity_error():
            raise IntegrityError("INSERT INTO payments ...", params=None, orig=Exception("Unexpected constraint violation"))

        monkeypatch.setattr(db, "commit", fail_with_unexpected_integrity_error)

        with pytest.raises(PaymentAPIException) as exc_info:
            asyncio.run(service.create_payment(request_data=req, idempotency_key=f"KEY-UNEXP-{uuid.uuid4().hex}"))

        assert exc_info.value.status_code == 503
        assert exc_info.value.payment_id is None
        assert "Lỗi lưu trữ" in exc_info.value.detail
