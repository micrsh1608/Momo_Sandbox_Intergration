import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MomoIPN(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    partner_code: str = Field(alias="partnerCode", min_length=1, max_length=100)
    order_id: str = Field(alias="orderId", min_length=1, max_length=200)
    request_id: str = Field(alias="requestId", min_length=1, max_length=200)
    amount: int
    order_info: str = Field(alias="orderInfo", max_length=255)
    order_type: str = Field(alias="orderType", max_length=100)
    trans_id: str = Field(alias="transId", max_length=64)
    result_code: int = Field(alias="resultCode")
    message: str = Field(max_length=255)
    pay_type: str = Field(alias="payType", max_length=64)
    response_time: int = Field(alias="responseTime")
    extra_data: str = Field(alias="extraData", max_length=2000)
    signature: str = Field(min_length=64, max_length=64)

    @field_validator("amount", "result_code", "response_time", mode="before")
    @classmethod
    def numbers_only(cls, v):
        if isinstance(v, bool) or isinstance(v, float) or not (
            isinstance(v, int) or (isinstance(v, str) and re.fullmatch(r"[0-9]+", v))
        ):
            raise ValueError("Phải là số nguyên không âm")
        return int(v)

    @field_validator("trans_id", mode="before")
    @classmethod
    def trans_id_string(cls, v):
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("transId không hợp lệ")
        result = str(v)
        if not result or not re.fullmatch(r"[0-9A-Za-z_-]+", result):
            raise ValueError("transId không hợp lệ")
        return result

    @field_validator("signature")
    @classmethod
    def validate_signature_format(cls, value):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("signature phải là 64 ký tự hex")
        return value
