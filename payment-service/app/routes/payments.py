from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import PaymentCreateRequest, PaymentResponse
from app.security import verify_internal_token
from app.services.payment_service import PaymentService
from app.models import Payment

router = APIRouter(prefix="/internal", tags=["Payments"])


@router.post(
    "/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Khởi tạo thanh toán nội bộ",
)
async def create_payment(
    request_data: PaymentCreateRequest,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    token: str = Depends(verify_internal_token),
    db: Session = Depends(get_db),
):
    payment_service = PaymentService(db=db)
    http_status_code, result = await payment_service.create_payment(
        request_data=request_data,
        idempotency_key=idempotency_key,
    )
    response.status_code = http_status_code
    return result

@router.get(
    "/payments/{payment_id}",
    summary="Tra cứu trạng thái thanh toán",
)
def get_payment_status(
    payment_id: str,
    token: str = Depends(verify_internal_token),
    db: Session = Depends(get_db),
):
    payment = (
        db.query(Payment)
        .filter(Payment.payment_id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    return {
        "payment_id": payment.payment_id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "status": payment.status,
        "provider_transaction_id": payment.provider_transaction_id,
        "provider_response_code": payment.provider_response_code,
        "provider_message": payment.provider_message,
        "environment": payment.environment,
    }
