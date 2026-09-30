from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import PaymentCreateRequest, PaymentResponse
from app.security import verify_internal_token
from app.services.payment_service import PaymentService

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
