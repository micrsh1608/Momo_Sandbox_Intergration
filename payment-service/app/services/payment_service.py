import hashlib
import json
import logging
import uuid
from typing import Optional, Tuple

from fastapi import status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Payment
from app.providers import (
    BasePaymentProvider,
    ProviderBusinessError,
    ProviderMalformedResponseError,
    ProviderPendingError,
    ProviderTimeoutError,
    get_payment_provider,
)
from app.schemas import PaymentAPIException, PaymentCreateRequest, PaymentResponse
from app.services.payment_status import (
    NON_TERMINAL_STATUSES,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
)

logger = logging.getLogger(__name__)


class PaymentService:
    def __init__(self, db: Session, provider: BasePaymentProvider = None):
        self.db = db
        self.provider = provider or get_payment_provider()

    def _compute_request_hash(self, request_data: PaymentCreateRequest) -> str:
        data_str = json.dumps(request_data.model_dump(), sort_keys=True)
        return hashlib.sha256(data_str.encode("utf-8")).hexdigest()

    def _reload_payment(self, payment_id: str) -> Optional[Payment]:
        """
        Truy vấn cưỡng bức tải lại bản ghi Payment từ DB (bỏ qua identity map cache của session)
        để lấy đúng trạng thái thực tế sau khi commit hoặc khi IPN cập nhật đồng thời (M1).
        """
        self.db.expire_all()
        return (
            self.db.query(Payment)
            .populate_existing()
            .filter(Payment.payment_id == payment_id)
            .first()
        )

    def _build_response_from_payment(
        self,
        payment: Payment,
        is_newly_pending: bool = False,
    ) -> Tuple[int, PaymentResponse]:
        """
        Hàm dùng chung để dựng HTTP status code và PaymentResponse (hoặc ném PaymentAPIException)
        dựa trên trạng thái thực tế của bản ghi Payment trong DB (M1, M2, M5).
        """
        if payment.status == STATUS_SUCCESS:
            return status.HTTP_200_OK, PaymentResponse(
                payment_id=payment.payment_id,
                order_id=payment.order_id,
                status=STATUS_SUCCESS,
                environment=payment.environment,
                pay_url=payment.pay_url,
            )

        if payment.status == STATUS_FAILED:
            raise PaymentAPIException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Giao dịch đã thất bại: {payment.provider_message or 'Provider hoặc IPN từ chối giao dịch'} "
                    f"(payment_id={payment.payment_id})"
                ),
                payment_id=payment.payment_id,
                provider_code=payment.provider_response_code,
            )

        if payment.status in (STATUS_CREATED, STATUS_UNKNOWN):
            return status.HTTP_202_ACCEPTED, PaymentResponse(
                payment_id=payment.payment_id,
                order_id=payment.order_id,
                status=payment.status,
                environment=payment.environment,
                pay_url=None,
            )

        if payment.status == STATUS_PENDING:
            http_status = status.HTTP_201_CREATED if is_newly_pending else status.HTTP_200_OK
            return http_status, PaymentResponse(
                payment_id=payment.payment_id,
                order_id=payment.order_id,
                status=STATUS_PENDING,
                environment=payment.environment,
                pay_url=payment.pay_url,
            )

        return status.HTTP_202_ACCEPTED, PaymentResponse(
            payment_id=payment.payment_id,
            order_id=payment.order_id,
            status=payment.status,
            environment=payment.environment,
            pay_url=None,
        )

    def _handle_existing_idempotent_payment(
        self,
        existing_payment: Payment,
        request_hash: str,
    ) -> Tuple[int, PaymentResponse]:
        """
        Xử lý thống nhất khi phát hiện Idempotency-Key đã tồn tại (ở bước kiểm tra đầu,
        bước kiểm tra đơn hàng đồng thời, hoặc khi bắt IntegrityError) (M2).
        """
        if existing_payment.request_hash != request_hash:
            raise PaymentAPIException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cùng Idempotency-Key nhưng dữ liệu yêu cầu khác với lần trước",
                payment_id=existing_payment.payment_id,
            )

        return self._build_response_from_payment(existing_payment, is_newly_pending=False)

    async def create_payment(
        self,
        request_data: PaymentCreateRequest,
        idempotency_key: str,
    ) -> Tuple[int, PaymentResponse]:
        if not idempotency_key or not idempotency_key.strip():
            raise PaymentAPIException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Header Idempotency-Key bắt buộc phải có và không được để trống",
            )
        if len(idempotency_key) > 128:
            raise PaymentAPIException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Header Idempotency-Key vượt quá độ dài tối đa 128 ký tự",
            )

        request_hash = self._compute_request_hash(request_data)

        # Step 1: Kiểm tra Idempotency-Key trong Database
        try:
            existing_payment = (
                self.db.query(Payment)
                .populate_existing()
                .filter(Payment.idempotency_key == idempotency_key)
                .first()
            )
        except SQLAlchemyError as exc:
            logger.exception("Database query error on idempotency check")
            raise PaymentAPIException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dịch vụ lưu trữ chưa sẵn sàng",
            ) from exc

        if existing_payment:
            return self._handle_existing_idempotent_payment(existing_payment, request_hash)

        # Step 2: Kiểm tra đơn hàng có giao dịch đang hoạt động hoặc đã hoàn tất hay chưa (M2 Task 3)
        try:
            active_payment = (
                self.db.query(Payment)
                .populate_existing()
                .filter(
                    Payment.order_id == request_data.order_id,
                    Payment.status.in_(NON_TERMINAL_STATUSES + (STATUS_SUCCESS,)),
                )
                .first()
            )
        except SQLAlchemyError as exc:
            logger.exception("Database query error on active payment check")
            raise PaymentAPIException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dịch vụ lưu trữ chưa sẵn sàng",
            ) from exc

        if active_payment:
            # Kiểm tra xem active_payment có phải chính là request cùng Idempotency-Key chạy đồng thời không
            if active_payment.idempotency_key == idempotency_key:
                return self._handle_existing_idempotent_payment(active_payment, request_hash)

            try:
                concurrent_same_key = (
                    self.db.query(Payment)
                    .populate_existing()
                    .filter(Payment.idempotency_key == idempotency_key)
                    .first()
                )
            except SQLAlchemyError as exc:
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Dịch vụ lưu trữ chưa sẵn sàng",
                ) from exc

            if concurrent_same_key:
                return self._handle_existing_idempotent_payment(concurrent_same_key, request_hash)

            if active_payment.status == STATUS_UNKNOWN:
                raise PaymentAPIException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Đơn hàng đang có giao dịch chưa xác định (UNKNOWN), cần đối soát trước khi khởi tạo lại",
                    payment_id=active_payment.payment_id,
                )
            raise PaymentAPIException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Đơn hàng đã có lần thanh toán đang hoạt động hoặc đã thành công",
                payment_id=active_payment.payment_id,
            )

        # Step 3: Lưu bản ghi trạng thái CREATED trước khi gọi Provider
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
            status=STATUS_CREATED,
            provider=provider_name,
            environment=env_name,
        )

        try:
            self.db.add(new_payment)
            self.db.commit()
            # Đóng transaction đọc/ghi trước khi chờ mạng
            self.db.expire_all()
        except IntegrityError as exc:
            self.db.rollback()
            try:
                existing = (
                    self.db.query(Payment)
                    .populate_existing()
                    .filter(Payment.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return self._handle_existing_idempotent_payment(existing, request_hash)

                active_order_payment = (
                    self.db.query(Payment)
                    .populate_existing()
                    .filter(
                        Payment.order_id == request_data.order_id,
                        Payment.status.in_(NON_TERMINAL_STATUSES + (STATUS_SUCCESS,)),
                    )
                    .first()
                )
            except SQLAlchemyError as db_exc:
                self.db.rollback()
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Dịch vụ lưu trữ chưa sẵn sàng",
                ) from db_exc

            if active_order_payment:
                if active_order_payment.status == STATUS_UNKNOWN:
                    raise PaymentAPIException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Đơn hàng đang có giao dịch chưa xác định (UNKNOWN), cần đối soát trước khi khởi tạo lại",
                        payment_id=active_order_payment.payment_id,
                    ) from exc
                raise PaymentAPIException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Đơn hàng đã có lần thanh toán đang hoạt động hoặc đã thành công",
                    payment_id=active_order_payment.payment_id,
                ) from exc

            logger.exception(
                "Unexpected IntegrityError during payment creation (neither idempotency_key nor active order matched): %s",
                exc,
            )
            raise PaymentAPIException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Lỗi lưu trữ dữ liệu khi khởi tạo giao dịch",
            ) from exc
        except SQLAlchemyError as exc:
            self.db.rollback()
            logger.exception("Failed to pre-save payment record to database")
            raise PaymentAPIException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dịch vụ lưu trữ chưa sẵn sàng",
            ) from exc

        # Step 4: Gọi Provider bên ngoài DB transaction
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

            # Step 5: Cập nhật có điều kiện và tải lại trạng thái thực tế từ DB (M1 & M5)
            try:
                current_p = (
                    self.db.query(Payment)
                    .populate_existing()
                    .filter(Payment.payment_id == payment_id)
                    .first()
                )
                if current_p and current_p.status in (STATUS_SUCCESS, STATUS_FAILED):
                    logger.info(
                        "IPN already set final status '%s' for payment_id=%s",
                        current_p.status,
                        payment_id,
                    )
                    return self._build_response_from_payment(current_p, is_newly_pending=False)

                updated_count = (
                    self.db.query(Payment)
                    .filter(
                        Payment.payment_id == payment_id,
                        Payment.status.in_([STATUS_CREATED, STATUS_UNKNOWN]),
                    )
                    .update(
                        {
                            Payment.status: STATUS_PENDING,
                            Payment.pay_url: pay_url,
                            Payment.environment: environment,
                            Payment.provider_response_code: provider_code,
                            Payment.provider_transaction_id: trans_id,
                        },
                        synchronize_session=False,
                    )
                )
                self.db.commit()
                reloaded_p = self._reload_payment(payment_id)
            except SQLAlchemyError as exc:
                self.db.rollback()
                logger.error("DB commit failed after provider success for payment_id=%s: %s", payment_id, exc)
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Lỗi lưu trữ dữ liệu sau khi nhận phản hồi từ provider (payment_id={payment_id})",
                    payment_id=payment_id,
                    provider_code=provider_code,
                ) from exc

            if not reloaded_p:
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Không tìm thấy bản ghi thanh toán sau khi cập nhật (payment_id={payment_id})",
                    payment_id=payment_id,
                    provider_code=provider_code,
                )

            if updated_count == 0:
                # IPN đã cập nhật trạng thái giữa bước SELECT và UPDATE (M1 Task 3)
                return self._build_response_from_payment(reloaded_p, is_newly_pending=False)

            return self._build_response_from_payment(reloaded_p, is_newly_pending=True)

        except ProviderPendingError as exc:
            # M4: Provider trả mã đang xử lý hoặc chưa xác định -> cập nhật UNKNOWN, giữ giao dịch để đối soát
            logger.warning("Provider pending/processing code for payment_id=%s: %s", payment_id, exc)
            try:
                updated_count = (
                    self.db.query(Payment)
                    .filter(
                        Payment.payment_id == payment_id,
                        Payment.status.in_([STATUS_CREATED, STATUS_UNKNOWN]),
                    )
                    .update(
                        {
                            Payment.status: STATUS_UNKNOWN,
                            Payment.provider_response_code: exc.provider_code,
                            Payment.provider_message: str(exc),
                        },
                        synchronize_session=False,
                    )
                )
                self.db.commit()
                reloaded_p = self._reload_payment(payment_id)
            except SQLAlchemyError as db_exc:
                self.db.rollback()
                logger.exception("Database error updating status to UNKNOWN on ProviderPendingError")
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Lỗi lưu trữ dữ liệu khi cập nhật trạng thái đang xử lý (payment_id={payment_id})",
                    payment_id=payment_id,
                    provider_code=exc.provider_code,
                ) from db_exc

            if not reloaded_p:
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Không tìm thấy bản ghi thanh toán (payment_id={payment_id})",
                    payment_id=payment_id,
                    provider_code=exc.provider_code,
                )

            return self._build_response_from_payment(reloaded_p, is_newly_pending=False)

        except ProviderTimeoutError as exc:
            # Handle Timeout: chuyển CREATED -> UNKNOWN, đọc lại DB và trả kết quả thực tế (M1 & M5)
            logger.warning("Provider timeout for payment_id=%s: %s", payment_id, exc)
            try:
                self.db.query(Payment).filter(
                    Payment.payment_id == payment_id,
                    Payment.status.in_([STATUS_CREATED, STATUS_UNKNOWN]),
                ).update(
                    {
                        Payment.status: STATUS_UNKNOWN,
                        Payment.provider_message: str(exc),
                    },
                    synchronize_session=False,
                )
                self.db.commit()
                reloaded_p = self._reload_payment(payment_id)
            except SQLAlchemyError as db_exc:
                self.db.rollback()
                logger.exception("Database error updating status to UNKNOWN on timeout")
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Lỗi lưu trữ dữ liệu khi cập nhật trạng thái timeout (payment_id={payment_id})",
                    payment_id=payment_id,
                ) from db_exc

            if not reloaded_p:
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Không tìm thấy bản ghi thanh toán (payment_id={payment_id})",
                    payment_id=payment_id,
                )

            return self._build_response_from_payment(reloaded_p, is_newly_pending=False)

        except ProviderBusinessError as exc:
            # M4 & M5: Provider từ chối cuối cùng -> chuyển CREATED/UNKNOWN -> FAILED, đọc lại DB
            logger.warning("Provider business error for payment_id=%s: %s", payment_id, exc)
            try:
                updated_count = (
                    self.db.query(Payment)
                    .filter(
                        Payment.payment_id == payment_id,
                        Payment.status.in_([STATUS_CREATED, STATUS_UNKNOWN]),
                    )
                    .update(
                        {
                            Payment.status: STATUS_FAILED,
                            Payment.provider_response_code: exc.provider_code,
                            Payment.provider_message: str(exc),
                        },
                        synchronize_session=False,
                    )
                )
                self.db.commit()
                reloaded_p = self._reload_payment(payment_id)
            except SQLAlchemyError as db_exc:
                self.db.rollback()
                logger.exception("Database error updating status to FAILED")
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Lỗi lưu trữ dữ liệu khi cập nhật trạng thái thất bại (payment_id={payment_id})",
                    payment_id=payment_id,
                    provider_code=exc.provider_code,
                ) from db_exc

            if reloaded_p and reloaded_p.status != STATUS_FAILED:
                # Ví dụ IPN đã cập nhật thành SUCCESS trước đó
                return self._build_response_from_payment(reloaded_p, is_newly_pending=False)

            raise PaymentAPIException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"MoMo từ chối khởi tạo giao dịch: {exc} (payment_id={payment_id})",
                payment_id=payment_id,
                provider_code=exc.provider_code,
            ) from exc

        except ProviderMalformedResponseError as exc:
            # Handle malformed response: chuyển CREATED -> UNKNOWN để đối soát, đọc lại DB (M1 & M5)
            logger.warning("Provider malformed response for payment_id=%s: %s", payment_id, exc)
            try:
                self.db.query(Payment).filter(
                    Payment.payment_id == payment_id,
                    Payment.status.in_([STATUS_CREATED, STATUS_UNKNOWN]),
                ).update(
                    {
                        Payment.status: STATUS_UNKNOWN,
                        Payment.provider_message: f"Response không hợp lệ: {exc}",
                    },
                    synchronize_session=False,
                )
                self.db.commit()
                reloaded_p = self._reload_payment(payment_id)
            except SQLAlchemyError as db_exc:
                self.db.rollback()
                logger.exception("Database error updating status to UNKNOWN on malformed response")
                raise PaymentAPIException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Lỗi lưu trữ dữ liệu khi xử lý phản hồi lỗi từ provider (payment_id={payment_id})",
                    payment_id=payment_id,
                ) from db_exc

            if reloaded_p and reloaded_p.status in (STATUS_SUCCESS, STATUS_FAILED):
                return self._build_response_from_payment(reloaded_p, is_newly_pending=False)

            raise PaymentAPIException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Response từ provider không đúng cấu trúc: {exc} (payment_id={payment_id})",
                payment_id=payment_id,
            ) from exc
