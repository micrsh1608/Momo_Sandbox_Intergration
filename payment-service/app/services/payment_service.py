import hashlib
import json
import logging
import uuid
import hmac
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Payment
from app.providers import (
    BasePaymentProvider,
    ProviderBusinessError,
    ProviderMalformedResponseError,
    ProviderTimeoutError,
    get_payment_provider,
)
from app.schemas import MoMoIPNRequest, PaymentCreateRequest, PaymentResponse
from app.config import settings

logger = logging.getLogger(__name__)


class PaymentService:
    def __init__(self, db: Session, provider: BasePaymentProvider = None):
        self.db = db
        self.provider = provider or get_payment_provider()

    def _compute_request_hash(self, request_data: PaymentCreateRequest) -> str:
        data_str = json.dumps(request_data.model_dump(), sort_keys=True)
        return hashlib.sha256(data_str.encode("utf-8")).hexdigest()

    @staticmethod
    def _verify_ipn_signature(
        data: MoMoIPNRequest, access_key: str, secret_key: str
    ) -> bool:
        raw_signature = (
            f"accessKey={access_key}"
            f"&amount={data.amount}"
            f"&extraData={data.extraData}"
            f"&message={data.message}"
            f"&orderId={data.orderId}"
            f"&orderInfo={data.orderInfo}"
            f"&orderType={data.orderType}"
            f"&partnerCode={data.partnerCode}"
            f"&payType={data.payType}"
            f"&requestId={data.requestId}"
            f"&responseTime={data.responseTime}"
            f"&resultCode={data.resultCode}"
            f"&transId={data.transId}"
        )
        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            raw_signature.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(data.signature, expected_signature)

    async def handle_ipn(self, data: MoMoIPNRequest):
        payment = (
            self.db.query(Payment)
            .filter(Payment.payment_id == data.orderId)
            .first()
        )
        if not payment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Payment not found",
            )

        provider_mode = settings.payment_provider_mode.lower()
        if payment.provider == "momo":
            if provider_mode != "momo" or payment.environment != "sandbox":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="MoMo IPN is not enabled for this payment environment",
                )
            expected_partner_code = settings.momo_partner_code
            access_key = settings.momo_access_key
            secret_key = settings.momo_secret_key
        elif payment.provider == "mock":
            if (
                provider_mode != "mock"
                or payment.environment != "mock"
                or not settings.demo_ipn_enabled
                or len(settings.demo_ipn_secret.strip()) < 32
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Demo IPN is disabled or not configured",
                )
            expected_partner_code = "DEMO"
            access_key = "DEMO"
            secret_key = settings.demo_ipn_secret
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported payment provider",
            )

        if not self._verify_ipn_signature(data, access_key, secret_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid signature",
            )
        if data.partnerCode != expected_partner_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid partnerCode",
            )
        if payment.amount != data.amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid amount",
            )
        if payment.provider_request_id != data.requestId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid requestId",
            )
        if payment.description != data.orderInfo:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid orderInfo",
            )

        if payment.status in {"SUCCESS", "FAILED"}:
            logger.info("Duplicate IPN ignored for payment_id=%s", payment.payment_id)
            return

        # captureWallet is a one-step payment flow. MoMo documents resultCode
        # 0 as paid and 9000 as successfully authorized (which is successful
        # for one-step payments); other codes are failures.
        target_status = "SUCCESS" if data.resultCode in {0, 9000} else "FAILED"
        try:
            updated_count = (
                self.db.query(Payment)
                .filter(
                    Payment.payment_id == data.orderId,
                    Payment.status.in_(["CREATED", "PENDING", "UNKNOWN"]),
                )
                .update(
                    {
                        Payment.status: target_status,
                        Payment.provider_transaction_id: str(data.transId),
                        Payment.provider_response_code: data.resultCode,
                        Payment.provider_message: data.message,
                    },
                    synchronize_session=False,
                )
            )
            if updated_count == 0:
                self.db.rollback()
                current = (
                    self.db.query(Payment)
                    .filter(Payment.payment_id == data.orderId)
                    .first()
                )
                if current and current.status in {"SUCCESS", "FAILED"}:
                    logger.info(
                        "Concurrent duplicate IPN ignored for payment_id=%s",
                        current.payment_id,
                    )
                    return
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Payment is not in a confirmable state",
                )
            self.db.commit()
        except SQLAlchemyError as exc:
            self.db.rollback()
            logger.exception("Failed to update payment from IPN: %s", data.orderId)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to update payment",
            ) from exc
    async def create_payment(
        self,
        request_data: PaymentCreateRequest,
        idempotency_key: str,
    ) -> Tuple[int, PaymentResponse]:
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Header Idempotency-Key bắt buộc phải có và không được để trống",
            )
        if len(idempotency_key) > 128:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Header Idempotency-Key vượt quá độ dài tối đa 128 ký tự",
            )

        request_hash = self._compute_request_hash(request_data)

        # Step 1: Check Idempotency Key in Database
        try:
            existing_payment = (
                self.db.query(Payment)
                .filter(Payment.idempotency_key == idempotency_key)
                .first()
            )
        except SQLAlchemyError as exc:
            logger.exception("Database query error on idempotency check")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dịch vụ lưu trữ chưa sẵn sàng",
            ) from exc

        if existing_payment:
            if existing_payment.request_hash == request_hash:
                # Replay handling (Section 4 Review)
                if existing_payment.status == "CREATED":
                    return status.HTTP_202_ACCEPTED, PaymentResponse(
                        payment_id=existing_payment.payment_id,
                        order_id=existing_payment.order_id,
                        status="CREATED",
                        environment=existing_payment.environment,
                        pay_url=None,
                    )
                if existing_payment.status == "UNKNOWN":
                    return status.HTTP_202_ACCEPTED, PaymentResponse(
                        payment_id=existing_payment.payment_id,
                        order_id=existing_payment.order_id,
                        status="UNKNOWN",
                        environment=existing_payment.environment,
                        pay_url=None,
                    )
                if existing_payment.status == "FAILED":
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Lần khởi tạo trước đó đã thất bại: {existing_payment.provider_message}. Payment ID: {existing_payment.payment_id}",
                    )

                return status.HTTP_200_OK, PaymentResponse(
                    payment_id=existing_payment.payment_id,
                    order_id=existing_payment.order_id,
                    status=existing_payment.status,
                    environment=existing_payment.environment,
                    pay_url=existing_payment.pay_url,
                )
            else:
                # Same Idempotency Key & Different Body -> Conflict 409
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cùng Idempotency-Key nhưng dữ liệu yêu cầu khác với lần trước",
                )

        # Step 2: Check for ANY active/unresolved payment on the same order (Issue 1 Review)
        try:
            active_payment = (
                self.db.query(Payment)
                .filter(
                    Payment.order_id == request_data.order_id,
                    Payment.status.in_(["PENDING", "SUCCESS", "CREATED", "UNKNOWN"]),
                )
                .first()
            )
        except SQLAlchemyError as exc:
            logger.exception("Database query error on active payment check")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dịch vụ lưu trữ chưa sẵn sàng",
            ) from exc

        if active_payment:
            if active_payment.idempotency_key == idempotency_key:
                if active_payment.request_hash != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Cùng Idempotency-Key nhưng dữ liệu yêu cầu khác với lần trước",
                    )
                return status.HTTP_200_OK, PaymentResponse(
                    payment_id=active_payment.payment_id,
                    order_id=active_payment.order_id,
                    status=active_payment.status,
                    environment=active_payment.environment,
                    pay_url=active_payment.pay_url,
                )
            if active_payment.status == "UNKNOWN":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Đơn hàng đang có giao dịch chưa xác định (UNKNOWN), cần đối soát trước khi khởi tạo lại",
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Đơn hàng đã có lần thanh toán đang hoạt động hoặc đã thành công",
            )

        # Step 3: Pre-save payment record in database with status CREATED
        payment_id = f"PAY-{uuid.uuid4().hex[:12].upper()}"
        provider_request_id = f"REQ-{uuid.uuid4().hex[:12].upper()}"
        provider_name = "momo" if self.provider.__class__.__name__ == "MomoProvider" else "mock"
        env_name = "sandbox" if self.provider.__class__.__name__ == "MomoProvider" else "mock"

        new_payment = Payment(
            payment_id=payment_id,
            provider_request_id=provider_request_id,
            order_id=request_data.order_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            amount=request_data.amount,
            currency=request_data.currency,
            description=request_data.description,
            status="CREATED",
            provider=provider_name,
            environment=env_name,
        )

        try:
            self.db.add(new_payment)
            self.db.commit()
            self.db.expire_all()
        except IntegrityError as exc:
            self.db.rollback()
            existing = (
                self.db.query(Payment)
                .filter(Payment.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                if existing.request_hash == request_hash:
                    return status.HTTP_200_OK, PaymentResponse(
                        payment_id=existing.payment_id,
                        order_id=existing.order_id,
                        status=existing.status,
                        environment=existing.environment,
                        pay_url=existing.pay_url,
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Cùng Idempotency-Key nhưng dữ liệu yêu cầu khác với lần trước",
                    )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Đơn hàng đã có lần thanh toán đang hoạt động hoặc đã thành công",
            ) from exc
        except SQLAlchemyError as exc:
            self.db.rollback()
            # A concurrent request can win the unique-key/order race while
            # this transaction is being committed. Re-read after rollback so
            # an idempotent retry returns the winning payment instead of a
            # transient database error.
            try:
                concurrent_payment = (
                    self.db.query(Payment)
                    .filter(Payment.idempotency_key == idempotency_key)
                    .first()
                )
                if concurrent_payment:
                    if concurrent_payment.request_hash != request_hash:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Cùng Idempotency-Key nhưng dữ liệu yêu cầu khác với lần trước",
                        )
                    return status.HTTP_200_OK, PaymentResponse(
                        payment_id=concurrent_payment.payment_id,
                        order_id=concurrent_payment.order_id,
                        status=concurrent_payment.status,
                        environment=concurrent_payment.environment,
                        pay_url=concurrent_payment.pay_url,
                    )

                concurrent_order_payment = (
                    self.db.query(Payment)
                    .filter(
                        Payment.order_id == request_data.order_id,
                        Payment.status.in_(
                            ["PENDING", "SUCCESS", "CREATED", "UNKNOWN"]
                        ),
                    )
                    .first()
                )
                if concurrent_order_payment:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Đơn hàng đã có lần thanh toán đang hoạt động hoặc đã thành công",
                    )
            except HTTPException:
                raise
            except SQLAlchemyError:
                self.db.rollback()
                logger.exception("Could not recover concurrent payment insert")

            logger.exception("Failed to pre-save payment record to database")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dịch vụ lưu trữ chưa sẵn sàng",
            ) from exc

        # Step 4: Call Provider OUTSIDE DB Transaction
        try:
            result = await self.provider.create_payment(
                payment_id=payment_id,
                provider_request_id=provider_request_id,
                amount=request_data.amount,
                order_id=request_data.order_id,
                description=request_data.description,
            )

            pay_url = result["pay_url"]
            environment = result.get("environment", env_name)
            provider_code = result.get("provider_code", 0)
            trans_id = result.get("provider_transaction_id")

            # Step 5: State Machine Rules & Commit Failure Handling (Issue ② & ③ Review)
            try:
                # Re-fetch current payment row to check for early IPN (Member B)
                current_p = (
                    self.db.query(Payment)
                    .filter(Payment.payment_id == payment_id)
                    .first()
                )

                # Protect Final States (SUCCESS / FAILED) against regression (Issue ③ Review)
                if current_p and current_p.status in ["SUCCESS", "FAILED"]:
                    logger.info("IPN already set final status '%s' for payment_id=%s", current_p.status, payment_id)
                    if current_p.status == "SUCCESS":
                        if not current_p.pay_url:
                            self.db.query(Payment).filter(
                                Payment.payment_id == payment_id,
                                Payment.status == "SUCCESS",
                                Payment.pay_url.is_(None),
                            ).update(
                                {Payment.pay_url: pay_url},
                                synchronize_session=False,
                            )
                            self.db.commit()
                        return status.HTTP_200_OK, PaymentResponse(
                            payment_id=payment_id,
                            order_id=request_data.order_id,
                            status="SUCCESS",
                            environment=environment,
                            pay_url=current_p.pay_url or pay_url,
                        )
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Giao dịch đã được ghi nhận thất bại bởi IPN. Payment ID: {payment_id}",
                        )

                # A signed IPN can arrive before the create response. Preserve
                # its result code/transaction id while saving the returned URL.
                if current_p and current_p.status == "PENDING":
                    updated_count = (
                        self.db.query(Payment)
                        .filter(
                            Payment.payment_id == payment_id,
                            Payment.status == "PENDING",
                        )
                        .update(
                            {
                                Payment.pay_url: pay_url,
                                Payment.environment: environment,
                            },
                            synchronize_session=False,
                        )
                    )
                else:
                    updated_count = (
                        self.db.query(Payment)
                        .filter(
                            Payment.payment_id == payment_id,
                            Payment.status.in_(["CREATED", "UNKNOWN"]),
                        )
                        .update(
                            {
                                Payment.status: "PENDING",
                                Payment.pay_url: pay_url,
                                Payment.environment: environment,
                                Payment.provider_response_code: provider_code,
                                Payment.provider_transaction_id: trans_id,
                            },
                            synchronize_session=False,
                        )
                    )
                self.db.commit()

                if updated_count == 0:
                    p_check = self.db.query(Payment).filter(Payment.payment_id == payment_id).first()
                    if p_check and p_check.status == "SUCCESS":
                        return status.HTTP_200_OK, PaymentResponse(
                            payment_id=payment_id,
                            order_id=request_data.order_id,
                            status="SUCCESS",
                            environment=environment,
                            pay_url=p_check.pay_url or pay_url,
                        )
                    if p_check and p_check.status == "FAILED":
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Giao dịch đã được ghi nhận thất bại bởi IPN. Payment ID: {payment_id}",
                        )

                return status.HTTP_201_CREATED, PaymentResponse(
                    payment_id=payment_id,
                    order_id=request_data.order_id,
                    status="PENDING",
                    environment=environment,
                    pay_url=pay_url,
                )

            except SQLAlchemyError as exc:
                self.db.rollback()
                logger.error("DB commit failed after provider success for payment_id=%s: %s", payment_id, exc)
                # If DB commit fails after provider returns success, DO NOT claim 201 PENDING! (Issue ② Review)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Lỗi lưu trữ dữ liệu sau khi nhận phản hồi từ provider. Payment ID: {payment_id}",
                ) from exc

        except ProviderTimeoutError as exc:
            # Handle Timeout: set status UNKNOWN if CREATED, return 202 Accepted
            logger.warning("Provider timeout for payment_id=%s: %s", payment_id, exc)
            try:
                current_p = self.db.query(Payment).filter(Payment.payment_id == payment_id).first()
                if current_p and current_p.status == "SUCCESS":
                    return status.HTTP_200_OK, PaymentResponse(
                        payment_id=payment_id,
                        order_id=request_data.order_id,
                        status="SUCCESS",
                        environment=env_name,
                        pay_url=current_p.pay_url,
                    )
                if current_p and current_p.status == "FAILED":
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Giao dịch đã được ghi nhận thất bại bởi IPN. Payment ID: {payment_id}",
                    )

                self.db.query(Payment).filter(
                    Payment.payment_id == payment_id,
                    Payment.status == "CREATED",
                ).update(
                    {
                        Payment.status: "UNKNOWN",
                        Payment.provider_message: str(exc),
                    },
                    synchronize_session=False,
                )
                self.db.commit()
            except SQLAlchemyError:
                self.db.rollback()
                logger.exception("Database error updating status to UNKNOWN")

            return status.HTTP_202_ACCEPTED, PaymentResponse(
                payment_id=payment_id,
                order_id=request_data.order_id,
                status="UNKNOWN",
                environment=env_name,
                pay_url=None,
            )

        except ProviderBusinessError as exc:
            # Handle explicit MoMo refusal (resultCode != 0): set status FAILED, return 502 with payment_id
            logger.warning("Provider business error for payment_id=%s: %s", payment_id, exc)
            try:
                current_p = self.db.query(Payment).filter(Payment.payment_id == payment_id).first()
                if current_p and current_p.status == "SUCCESS":
                    return status.HTTP_200_OK, PaymentResponse(
                        payment_id=payment_id,
                        order_id=request_data.order_id,
                        status="SUCCESS",
                        environment=env_name,
                        pay_url=current_p.pay_url,
                    )

                self.db.query(Payment).filter(
                    Payment.payment_id == payment_id,
                    Payment.status == "CREATED",
                ).update(
                    {
                        Payment.status: "FAILED",
                        Payment.provider_response_code: exc.provider_code,
                        Payment.provider_message: str(exc),
                    },
                    synchronize_session=False,
                )
                self.db.commit()
            except SQLAlchemyError:
                self.db.rollback()
                logger.exception("Database error updating status to FAILED")

            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"MoMo từ chối khởi tạo giao dịch: {exc}. Payment ID: {payment_id}",
            ) from exc

        except ProviderMalformedResponseError as exc:
            # Handle unverified/malformed response: set status UNKNOWN so it can be reconciled, return 502 with payment_id
            logger.warning("Provider malformed response for payment_id=%s: %s", payment_id, exc)
            try:
                current_p = self.db.query(Payment).filter(Payment.payment_id == payment_id).first()
                if current_p and current_p.status == "SUCCESS":
                    return status.HTTP_200_OK, PaymentResponse(
                        payment_id=payment_id,
                        order_id=request_data.order_id,
                        status="SUCCESS",
                        environment=env_name,
                        pay_url=current_p.pay_url,
                    )

                self.db.query(Payment).filter(
                    Payment.payment_id == payment_id,
                    Payment.status == "CREATED",
                ).update(
                    {
                        Payment.status: "UNKNOWN",
                        Payment.provider_message: f"Response không hợp lệ: {exc}",
                    },
                    synchronize_session=False,
                )
                self.db.commit()
            except SQLAlchemyError:
                self.db.rollback()
                logger.exception("Database error updating status to UNKNOWN")

            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Response từ provider không đúng cấu trúc: {exc}. Payment ID: {payment_id}",
            ) from exc
