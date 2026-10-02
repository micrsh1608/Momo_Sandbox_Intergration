from typing import Optional
from pydantic import BaseModel, Field, field_validator


class PaymentCreateRequest(BaseModel):
    order_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Mã đơn hàng từ Order Service (tối đa 64 ký tự)",
    )
    amount: int = Field(..., description="Số tiền thanh toán (VND)")
    currency: str = Field(..., description="Đơn vị tiền tệ (VND)")
    description: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Mô tả thanh toán (tối đa 255 ký tự)",
    )

    @field_validator("order_id", "description")
    @classmethod
    def validate_non_empty(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"Trường {info.field_name} không được để trống hoặc chỉ chứa khoảng trắng")
        return v.strip()

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, v):
        if isinstance(v, bool):
            raise ValueError("Số tiền không được là kiểu boolean")
        if isinstance(v, float):
            raise ValueError("Số tiền không được là số thập phân")
        if not isinstance(v, int):
            raise ValueError("Số tiền phải là số nguyên")
        if v <= 0:
            raise ValueError("Số tiền phải là số nguyên dương")
        if v < 1000 or v > 50000000:
            raise ValueError("Số tiền phải nằm trong khoảng từ 1,000 đến 50,000,000 VND")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v):
        if v != "VND":
            raise ValueError("Đơn vị tiền tệ bắt buộc là VND")
        return v


class PaymentResponse(BaseModel):
    payment_id: str
    order_id: str
    status: str
    environment: str
    pay_url: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str
    payment_id: Optional[str] = None
    provider_code: Optional[int] = None


class MomoIPNRequest(BaseModel):
    """MoMo server-to-server payment notification payload."""
    partnerCode: str
    orderId: str
    requestId: str
    amount: int
    orderInfo: str = ""
    orderType: str = ""
    transId: int | str
    resultCode: int
    message: str = ""
    payType: str = ""
    responseTime: int | str
    extraData: str = ""
    signature: str

    @field_validator("amount", mode="before")
    @classmethod
    def validate_ipn_amount(cls, v):
        if isinstance(v, bool):
            raise ValueError("amount không được là boolean")
        try:
            value = int(v)
        except (TypeError, ValueError):
            raise ValueError("amount phải là số nguyên")
        if value <= 0:
            raise ValueError("amount phải lớn hơn 0")
        return value


class MomoIPNResponse(BaseModel):
    partnerCode: str
    requestId: str
    orderId: str
    resultCode: int
    message: str
    responseTime: int | str
    extraData: str = ""
    signature: str


class PaymentLookupResponse(BaseModel):
    payment_id: str
    order_id: str
    provider_request_id: str
    provider_transaction_id: str | None = None
    amount: int
    currency: str
    status: str
    provider: str
    environment: str
    pay_url: str | None = None
    provider_response_code: int | None = None
    provider_message: str | None = None


class ReconcileResponse(PaymentLookupResponse):
    reconciliation_source: str
    provider_result_code: int | None = None
    provider_message: str | None = None
