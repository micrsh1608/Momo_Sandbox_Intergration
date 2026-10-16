"""Runnable local demo for member B; NO real MoMo payments / network needed.
Usage: cd payment-service; copy .env.example .env; python demo_member_b.py
"""
import sys
import uuid

from fastapi.testclient import TestClient
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Payment
from app.schemas_b import MomoIPN
from app.services.webhook_service import sign_ipn

if __name__ == "__main__":
    if settings.payment_provider_mode != "mock":
        sys.exit("Demo chỉ chạy với PAYMENT_PROVIDER_MODE=mock để tránh gọi thanh toán thật")
    if not settings.momo_secret_key or not settings.momo_access_key:
        sys.exit("Chưa cấu hình khóa DEMO. Hãy sao chép .env.example thành .env rồi chạy lại")

    order_id = f"ORDER-DEMO-{uuid.uuid4().hex[:8]}"
    internal_headers = {"X-Internal-Token": settings.internal_token}
    create_headers = {**internal_headers, "Idempotency-Key": str(uuid.uuid4())}
    with TestClient(app) as client:
        created = client.post("/internal/payments", headers=create_headers, json={
            "order_id": order_id, "amount": 50000,
            "currency": "VND", "description": "Demo seminar phan B"
        })
        assert created.status_code == 201, created.text
        pid = created.json()["payment_id"]
        with SessionLocal() as db:
            request_id = db.query(Payment).filter_by(payment_id=pid).one().provider_request_id

        body = {
            "partnerCode": settings.momo_partner_code,
            "orderId": pid,  # MoMo orderId = internal PAYMENT_ID, not business order_id
            "requestId": request_id,
            "amount": 50000, "orderInfo": "Demo seminar phan B",
            "orderType": "momo_wallet", "transId": 987654321,
            "resultCode": 0, "message": "Successful.",
            "payType": "qr", "responseTime": 1721720663942,
            "extraData": "", "signature": "0" * 64,
        }
        body["signature"] = sign_ipn(
            MomoIPN.model_validate(body), settings.momo_access_key, settings.momo_secret_key
        )

        print("1) CREATE payment ->", created.status_code, created.json()["status"], pid)
        bad_body = {**body, "amount": 99000}  # Tampered AFTER signing
        bad = client.post("/api/v1/payments/ipn", json=bad_body)
        print("2) TAMPERED IPN ->", bad.status_code, "(expected 401)")
        first = client.post("/api/v1/payments/ipn", json=body)
        print("3) VALID signed IPN ->", first.status_code, "(expected 204)")
        duplicate = client.post("/api/v1/payments/ipn", json=body)
        print("4) DUPLICATE IPN ->", duplicate.status_code, "(expected 204)")
        lookup = client.get(f"/internal/payments/{pid}", headers=internal_headers)
        print("5) LOOKUP ->", lookup.status_code, lookup.json()["status"])
        events = client.get(f"/internal/payments/{pid}/events", headers=internal_headers)
        print("6) WEBHOOK EVENTS ->", len(events.json()), "distinct signed events")
        report = client.get(f"/internal/payments/{pid}/reconciliation", headers=internal_headers)
        print("7) LOCAL RECONCILIATION ->", report.json())
        assert first.status_code == 204 and duplicate.status_code == 204
        assert bad.status_code == 401 and lookup.json()["status"] == "SUCCESS"
        print("DEMO PASSED (mock mode, no network calls)")
