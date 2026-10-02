from datetime import datetime

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import PaymentCreateRequest, PaymentResponse, MomoIPNRequest, MomoIPNResponse, PaymentLookupResponse, ReconcileResponse
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


@router.post(
    "/payments/ipn",
    response_model=MomoIPNResponse,
    status_code=status.HTTP_200_OK,
    summary="Nhận IPN từ MoMo",
)
async def momo_ipn(
    ipn: MomoIPNRequest,
    db: Session = Depends(get_db),
):
    """Public server-to-server callback. Authentication is the MoMo HMAC signature."""
    payment_service = PaymentService(db=db)
    result = await payment_service.handle_ipn(ipn)

    from app.config import settings
    from app.providers.momo import MomoProvider

    response_time = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
    ack_message = "Success" if result["accepted"] else "Rejected"
    ack_access_key = (
        settings.momo_access_key
        if settings.payment_provider_mode.lower() == "momo"
        else (settings.momo_access_key or settings.ipn_demo_access_key)
    )
    ack_secret_key = (
        settings.momo_secret_key
        if settings.payment_provider_mode.lower() == "momo"
        else (settings.momo_secret_key or settings.ipn_demo_secret_key)
    )
    ack_signature = MomoProvider().generate_ipn_ack_signature(
        partner_code=settings.momo_partner_code,
        access_key=ack_access_key,
        secret_key=ack_secret_key,
        request_id=ipn.requestId,
        order_id=ipn.orderId,
        result_code=0,
        message=ack_message,
        response_time=response_time,
        extra_data=ipn.extraData,
    )

    return MomoIPNResponse(
        partnerCode=settings.momo_partner_code,
        requestId=ipn.requestId,
        orderId=ipn.orderId,
        resultCode=0,
        message=ack_message,
        responseTime=response_time,
        extraData=ipn.extraData,
        signature=ack_signature,
    )


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentLookupResponse,
    summary="Tra cứu giao dịch",
)
def get_payment(
    payment_id: str,
    token: str = Depends(verify_internal_token),
    db: Session = Depends(get_db),
):
    payment_service = PaymentService(db=db)
    return payment_service.get_payment(payment_id)


@router.post(
    "/payments/{payment_id}/reconcile",
    response_model=ReconcileResponse,
    summary="Đối soát giao dịch với provider",
)
async def reconcile_payment(
    payment_id: str,
    token: str = Depends(verify_internal_token),
    db: Session = Depends(get_db),
):
    payment_service = PaymentService(db=db)
    return await payment_service.reconcile_payment(payment_id)
