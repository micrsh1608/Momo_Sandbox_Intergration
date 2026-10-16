from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Payment, PaymentWebhookEvent
from app.security import verify_internal_token

router = APIRouter(prefix="/internal", tags=["B - Lookup & Reconciliation"],
                   dependencies=[Depends(verify_internal_token)])


def get_payment(db, payment_id):
    item = db.query(Payment).filter_by(payment_id=payment_id).first()
    if not item:
        raise HTTPException(404, "Không tìm thấy giao dịch")
    return item


def payment_data(item):
    return {
        "payment_id": item.payment_id, "order_id": item.order_id,
        "amount": item.amount, "currency": item.currency, "status": item.status,
        "provider": item.provider, "environment": item.environment,
        "provider_transaction_id": item.provider_transaction_id,
        "provider_response_code": item.provider_response_code,
        "updated_at": item.updated_at,
    }


@router.get("/payments/by-order/{order_id}")
def find_by_order(order_id: str, db: Session = Depends(get_db)):
    rows = db.query(Payment).filter_by(order_id=order_id).order_by(Payment.id.desc()).all()
    return [payment_data(p) for p in rows]


@router.get("/payments/{payment_id}")
def find_by_payment(payment_id: str, db: Session = Depends(get_db)):
    return payment_data(get_payment(db, payment_id))


@router.get("/payments/{payment_id}/events")
def get_events(payment_id: str, db: Session = Depends(get_db)):
    get_payment(db, payment_id)
    events = db.query(PaymentWebhookEvent).filter_by(payment_id=payment_id).order_by(
        PaymentWebhookEvent.id.asc()).all()
    return [{"id": e.id, "result_code": e.result_code,
             "transaction_id": e.transaction_id, "outcome": e.outcome,
             "previous_status": e.previous_status, "current_status": e.current_status,
             "received_at": e.received_at} for e in events]


@router.get("/payments/{payment_id}/reconciliation")
def reconciliation(payment_id: str, db: Session = Depends(get_db)):
    payment = get_payment(db, payment_id)
    rows = db.query(PaymentWebhookEvent).filter_by(payment_id=payment_id).order_by(
        PaymentWebhookEvent.id.asc()).all()
    conflicts = [e for e in rows if e.outcome == "CONFLICT"]
    last_applied = next((e for e in reversed(rows) if e.outcome == "APPLIED"), None)
    needs_review = bool(conflicts) or payment.status == "UNKNOWN"
    if payment.status == "PENDING" and rows and not last_applied:
        needs_review = True
    return {
        "payment_id": payment.payment_id,
        "order_id": payment.order_id,
        "stored_status": payment.status,
        "verified_ipn_events": len(rows),
        "conflict_events": len(conflicts),
        "last_applied_result_code": last_applied.result_code if last_applied else None,
        "needs_manual_review": needs_review,
        "recommendation": (
            "Check MoMo transaction status via official Query API, investigate mismatch"
            if needs_review else
            "No local IPN conflict detected; remote provider confirmation not performed"
        ),
        "scope": "local_ipn_audit_only",
    }
