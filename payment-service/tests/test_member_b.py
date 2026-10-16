import sys
import uuid
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings
from app.main import app
from app.database import SessionLocal
from app.models import Payment
from app.schemas_b import MomoIPN
from app.services.webhook_service import sign_ipn


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(settings, "momo_partner_code", "DEMO_PARTNER")
    monkeypatch.setattr(settings, "momo_access_key", "demo-access-key")
    monkeypatch.setattr(settings, "momo_secret_key", "test-only-signing-secret")
    monkeypatch.setattr(settings, "payment_provider_mode", "mock")


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def make_payment(client):
    order = "ORDER-" + uuid.uuid4().hex[:12]
    resp = client.post("/internal/payments", headers={
        "X-Internal-Token": settings.internal_token,
        "Idempotency-Key": str(uuid.uuid4()),
    }, json={"order_id": order, "amount": 50000, "currency": "VND", "description": "Seminar member B"})
    assert resp.status_code == 201, resp.text
    pid = resp.json()["payment_id"]
    with SessionLocal() as db:
        payment = db.query(Payment).filter_by(payment_id=pid).one()
        request_id = payment.provider_request_id
    return pid, request_id, order


def make_ipn(pid, req_id, code=0, **changes):
    payload = dict(
        partnerCode=settings.momo_partner_code, orderId=pid, requestId=req_id,
        amount=50000, orderInfo="Seminar member B", orderType="momo_wallet",
        transId=123456789, resultCode=code, message="Successful.", payType="qr",
        responseTime=1721720663942, extraData="", signature="0" * 64,
    )
    payload.update(changes)
    obj = MomoIPN.model_validate(payload)
    payload["signature"] = sign_ipn(obj, settings.momo_access_key, settings.momo_secret_key)
    return payload


def status_of(client, pid):
    resp = client.get("/internal/payments/" + pid,
                      headers={"X-Internal-Token": settings.internal_token})
    assert resp.status_code == 200
    return resp.json()["status"]


def events_of(client, pid):
    r = client.get("/internal/payments/" + pid + "/events",
                   headers={"X-Internal-Token": settings.internal_token})
    assert r.status_code == 200
    return r.json()


def test_b01_signed_success_updates_db(client):
    pid, request_id, _ = make_payment(client)
    assert status_of(client, pid) == "PENDING"
    r = client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id))
    assert r.status_code == 204 and not r.content
    assert status_of(client, pid) == "SUCCESS"
    assert events_of(client, pid)[0]["outcome"] == "APPLIED"


def test_b02_signature_tampered_rejected_without_update(client):
    pid, request_id, _ = make_payment(client)
    payload = make_ipn(pid, request_id)
    payload["amount"] = 99000  # Signature is no longer valid
    assert client.post("/api/v1/payments/ipn", json=payload).status_code == 401
    assert status_of(client, pid) == "PENDING"
    assert events_of(client, pid) == []


@pytest.mark.parametrize("changed", [
    {"amount": 52000}, {"requestId": "WRONG-REQUEST"},
    {"orderInfo": "WRONG-INFO"}, {"partnerCode": "WRONG-MERCHANT"},
])
def test_b03_valid_signature_but_identity_mismatch_rejected(client, changed):
    pid, request_id, _ = make_payment(client)
    payload = make_ipn(pid, request_id, **changed)
    r = client.post("/api/v1/payments/ipn", json=payload)
    assert r.status_code == 400
    assert status_of(client, pid) == "PENDING"


def test_b04_duplicate_delivery_is_idempotent(client):
    pid, request_id, _ = make_payment(client)
    payload = make_ipn(pid, request_id)
    assert client.post("/api/v1/payments/ipn", json=payload).status_code == 204
    assert client.post("/api/v1/payments/ipn", json=payload).status_code == 204
    assert status_of(client, pid) == "SUCCESS"
    assert len(events_of(client, pid)) == 1


def test_b05_failed_and_late_success_conflict_preserves_final_state(client):
    pid, request_id, _ = make_payment(client)
    assert client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, 1001)).status_code == 204
    assert status_of(client, pid) == "FAILED"
    result = client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, 0, responseTime=1721720663943))
    assert result.status_code == 204
    assert status_of(client, pid) == "FAILED"
    assert [x["outcome"] for x in events_of(client, pid)] == ["APPLIED", "CONFLICT"]
    r = client.get(f"/internal/payments/{pid}/reconciliation",
                   headers={"X-Internal-Token": settings.internal_token})
    assert r.json()["needs_manual_review"] is True
    assert r.json()["conflict_events"] == 1


def test_b06_success_then_failed_conflict(client):
    pid, request_id, _ = make_payment(client)
    client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id))
    assert client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, 1001)).status_code == 204
    assert status_of(client, pid) == "SUCCESS"
    assert events_of(client, pid)[1]["outcome"] == "CONFLICT"


@pytest.mark.parametrize("pending_code", [1000, 7000, 7002, 9000])
def test_b07_pending_or_authorized_not_success(client, pending_code):
    pid, request_id, _ = make_payment(client)
    assert client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, pending_code)).status_code == 204
    assert status_of(client, pid) == "PENDING"


def test_b08_unknown_payment_and_invalid_schema(client):
    pid, request_id, _ = make_payment(client)
    r = client.post("/api/v1/payments/ipn", json=make_ipn("PAY-NOT-FOUND", request_id))
    assert r.status_code == 404
    r2 = client.post("/api/v1/payments/ipn", json={"foo": "bar"})
    assert r2.status_code == 400  # Existing app maps validation errors to 400
    assert status_of(client, pid) == "PENDING"


def test_b09_lookup_requires_internal_token(client):
    pid, _, order = make_payment(client)
    assert client.get(f"/internal/payments/{pid}").status_code == 401
    assert client.get(f"/internal/payments/by-order/{order}").status_code == 401
    r = client.get(f"/internal/payments/by-order/{order}",
                   headers={"X-Internal-Token": settings.internal_token})
    assert r.status_code == 200
    assert len(r.json()) == 1 and r.json()[0]["payment_id"] == pid


def test_b10_unknown_preserved_when_non_final_ipn(client):
    pid, request_id, _ = make_payment(client)
    with SessionLocal() as db:
        payment = db.query(Payment).filter_by(payment_id=pid).one()
        payment.status = "UNKNOWN"
        db.commit()
    assert client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, 1000)).status_code == 204
    assert status_of(client, pid) == "UNKNOWN"
    assert client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, 0)).status_code == 204
    assert status_of(client, pid) == "SUCCESS"


def test_b11_trans_id_mismatch_not_accepted(client):
    pid, request_id, _ = make_payment(client)
    client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id))
    r = client.post("/api/v1/payments/ipn", json=make_ipn(pid, request_id, transId=887766))
    assert r.status_code == 409
    assert status_of(client, pid) == "SUCCESS"


def test_b12_no_keys_rejected_fail_closed(client, monkeypatch):
    pid, request_id, _ = make_payment(client)
    payload = make_ipn(pid, request_id)
    monkeypatch.setattr(settings, "momo_secret_key", "")
    assert client.post("/api/v1/payments/ipn", json=payload).status_code == 503
    assert status_of(client, pid) == "PENDING"
