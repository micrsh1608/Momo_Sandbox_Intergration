from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import MoMoIPNRequest
from app.services.payment_service import PaymentService

router = APIRouter(
    prefix="/api/v1/payments",
    tags=["MoMo Webhook"],
)


@router.post("/ipn", status_code=status.HTTP_204_NO_CONTENT)
async def momo_ipn(
    data: MoMoIPNRequest,
    db: Session = Depends(get_db),
):
    service = PaymentService(db=db)
    await service.handle_ipn(data)
    return Response(status_code=status.HTTP_204_NO_CONTENT)