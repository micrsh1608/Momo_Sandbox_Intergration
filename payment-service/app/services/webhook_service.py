import hashlib
import hmac
import logging
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Payment, PaymentWebhookEvent
from app.schemas_b import MomoIPN

logger = logging.getLogger(__name__)
FINAL = {"SUCCESS", "FAILED"}
# '9000' means authorization, NOT captured/settled. Pending codes must not fail orders.
NON_FINAL_CODES = {1000, 7000, 7002, 9000}


def sign_ipn(payload: MomoIPN, access_key: str, secret_key: str) -> str:
    """MoMo standard v2 IPN raw signature. No JSON serialization / URL encoding."""
    parts = [
        ("accessKey", access_key),
        ("amount", payload.amount),
        ("extraData", payload.extra_data),
        ("message", payload.message),
        ("orderId", payload.order_id),
        ("orderInfo", payload.order_info),
        ("orderType", payload.order_type),
        ("partnerCode", payload.partner_code),
        ("payType", payload.pay_type),
        ("requestId", payload.request_id),
        ("responseTime", payload.response_time),
        ("resultCode", payload.result_code),
        ("transId", payload.trans_id),
    ]
    raw = "&".join(f"{k}={v}" for k, v in parts)
    return hmac.new(secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def check_signature(ipn: MomoIPN):
    if not settings.momo_secret_key or not settings.momo_access_key:
        raise HTTPException(503, "MoMo IPN signing keys chưa được cấu hình")
    if ipn.partner_code != settings.momo_partner_code:
        raise HTTPException(400, "partnerCode không khớp với cấu hình merchant")
    expected = sign_ipn(ipn, settings.momo_access_key, settings.momo_secret_key)
    if not hmac.compare_digest(ipn.signature.lower(), expected):
        raise HTTPException(401, "Chữ ký IPN không hợp lệ")


def new_status_from_result(result_code: int) -> str:
    if result_code == 0:
        return "SUCCESS"
    if result_code in NON_FINAL_CODES:
        return "PENDING"
    return "FAILED"


class WebhookService:
    def __init__(self, db: Session):
        self.db = db

    def receive(self, ipn: MomoIPN) -> str:
        """Return outcome; caller replies 204 only after durable commit."""
        check_signature(ipn)
        fingerprint = hashlib.sha256(ipn.signature.lower().encode()).hexdigest()
        try:
            if self.db.query(PaymentWebhookEvent).filter_by(signature_hash=fingerprint).first():
                return "DUPLICATE"

            payment = self.db.query(Payment).filter_by(payment_id=ipn.order_id).first()
            if not payment:
                raise HTTPException(404, "Không tìm thấy payment_id từ orderId của MoMo")
            if ipn.request_id != payment.provider_request_id:
                raise HTTPException(400, "requestId không khớp")
            if ipn.amount != payment.amount:
                raise HTTPException(400, "amount không khớp với database")
            if ipn.order_info != payment.description:
                raise HTTPException(400, "orderInfo không khớp với database")
            if payment.provider_transaction_id and payment.provider_transaction_id != ipn.trans_id:
                raise HTTPException(409, "transId không khớp với giao dịch đã ghi nhận")

            desired = new_status_from_result(ipn.result_code)
            old = payment.status
            if old in FINAL:
                outcome = "DUPLICATE" if old == desired else "CONFLICT"
                actual = old
            elif desired == "PENDING":
                # Do not downgrade or replace UNKNOWN (requires reconciliation).
                if old == "CREATED":
                    self.db.query(Payment).filter_by(id=payment.id, status="CREATED").update(
                        {Payment.status: "PENDING", Payment.provider_response_code: ipn.result_code,
                         Payment.provider_message: ipn.message}, synchronize_session=False,
                    )
                outcome = "APPLIED" if old == "CREATED" else "DUPLICATE"
                actual = "PENDING" if old == "CREATED" else old
            else:
                # Atomic conditional update: callback cannot overwrite a terminal state
                count = self.db.query(Payment).filter(
                    Payment.id == payment.id,
                    Payment.status.in_(["CREATED", "PENDING", "UNKNOWN"]),
                ).update({
                    Payment.status: desired,
                    Payment.provider_response_code: ipn.result_code,
                    Payment.provider_message: ipn.message,
                    Payment.provider_transaction_id: ipn.trans_id,
                }, synchronize_session=False)
                if count == 1:
                    outcome, actual = "APPLIED", desired
                else:
                    # Another concurrent notification has won: never force overwrite.
                    self.db.expire_all()
                    actual = self.db.query(Payment).filter_by(id=payment.id).one().status
                    outcome = "DUPLICATE" if actual == desired else "CONFLICT"

            self.db.add(PaymentWebhookEvent(
                payment_id=payment.payment_id,
                signature_hash=fingerprint,
                transaction_id=ipn.trans_id,
                result_code=ipn.result_code,
                outcome=outcome,
                previous_status=old,
                current_status=actual,
            ))
            self.db.commit()
            if outcome == "CONFLICT":
                logger.warning("Conflicting signed IPN for payment_id=%s (kept %s)", ipn.order_id, actual)
            return outcome
        except HTTPException:
            self.db.rollback()
            raise
        except IntegrityError:
            self.db.rollback()
            # Duplicate signature concurrently inserted -> safe idempotent retry.
            existing = self.db.query(PaymentWebhookEvent).filter_by(signature_hash=fingerprint).first()
            if existing:
                return "DUPLICATE"
            raise HTTPException(503, "Lỗi ràng buộc dữ liệu khi lưu IPN") from None
        except SQLAlchemyError:
            self.db.rollback()
            logger.exception("IPN database transaction failed")
            raise HTTPException(503, "Không lưu được IPN; yêu cầu gửi lại") from None
