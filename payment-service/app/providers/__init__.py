from app.config import settings
from app.providers.base import (
    BasePaymentProvider,
    ProviderBusinessError,
    ProviderError,
    ProviderMalformedResponseError,
    ProviderTimeoutError,
)
from app.providers.mock import MockProvider
from app.providers.momo import MomoProvider


def get_payment_provider() -> BasePaymentProvider:
    mode = settings.payment_provider_mode.lower()
    if mode == "momo":
        return MomoProvider()
    elif mode == "mock":
        return MockProvider()
    else:
        raise ValueError(f"Chế độ provider không hợp lệ: {settings.payment_provider_mode}")


__all__ = [
    "BasePaymentProvider",
    "MomoProvider",
    "MockProvider",
    "get_payment_provider",
    "ProviderError",
    "ProviderTimeoutError",
    "ProviderBusinessError",
    "ProviderMalformedResponseError",
]
