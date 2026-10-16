from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas_b import MomoIPN
from app.services.webhook_service import WebhookService

router = APIRouter(tags=["B - MoMo Webhook"])


@router.post("/api/v1/payments/ipn", status_code=status.HTTP_204_NO_CONTENT,
             summary="Nhận IPN signed từ MoMo; trả 204 sau khi commit")
def momo_ipn(payload: MomoIPN, db: Session = Depends(get_db)):
    WebhookService(db).receive(payload)
    return Response(status_code=204)
