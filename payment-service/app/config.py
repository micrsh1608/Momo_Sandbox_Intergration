import logging
from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

SERVICE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "MoMo Payment Service"

    db_server: str = "HUY-PC"
    db_name: str = "PaymentDB"
    db_driver: str = "ODBC Driver 18 for SQL Server"
    db_trust_server_certificate: bool = True

    internal_token: str = "secret_internal_token_123"
    payment_provider_mode: str = "mock"  # "mock" or "momo"

    momo_partner_code: str = "MOMO"
    momo_access_key: str = ""
    momo_secret_key: str = ""
    # Only used for local MockProvider IPN demonstrations when real MoMo keys are not configured.
    ipn_demo_access_key: str = "demo_access_key"
    ipn_demo_secret_key: str = "demo_secret_key"
    momo_endpoint: str = "https://test-payment.momo.vn/v2/gateway/api/create"
    momo_redirect_url: str = "https://example.com/payment/callback"
    momo_ipn_url: str = "https://example.com/api/v1/payments/ipn"
    momo_request_timeout: float = 30.0

    model_config = SettingsConfigDict(
        env_file=SERVICE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def clamp_and_validate_config(self):
        # Enforce minimum 30.0s timeout requirement for MoMo API (Issue ⑤)
        if self.momo_request_timeout < 30.0:
            logger.warning(
                "MOMO_REQUEST_TIMEOUT (%.1fs) thấp hơn quy định 30.0s. Tự động điều chỉnh lên 30.0s.",
                self.momo_request_timeout,
            )
            self.momo_request_timeout = 30.0

        if self.payment_provider_mode.lower() == "momo":
            if not self.momo_access_key or not self.momo_secret_key:
                raise ValueError(
                    "Khi bật PAYMENT_PROVIDER_MODE=momo, MOMO_ACCESS_KEY và MOMO_SECRET_KEY không được để trống!"
                )
            if not self.momo_redirect_url.startswith("http") or not self.momo_ipn_url.startswith("http"):
                raise ValueError(
                    "MOMO_REDIRECT_URL và MOMO_IPN_URL phải là URL hợp lệ (bắt đầu bằng http:// hoặc https://)"
                )
        return self


settings = Settings()