from typing import Optional
from fastapi import Header, HTTPException, status
from app.config import settings


def verify_internal_token(
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")
) -> str:
    if not x_internal_token or x_internal_token != settings.internal_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Internal-Token không hợp lệ hoặc bị thiếu",
        )
    return x_internal_token
