from abc import ABC, abstractmethod
from typing import Any, Dict


class ProviderError(Exception):
    """Base exception for payment provider errors."""
    pass


class ProviderTimeoutError(ProviderError):
    """Raised when the provider request times out or experiences network failure."""
    pass


class ProviderBusinessError(ProviderError):
    """Raised when provider returns an explicit final rejection error (maps to FAILED)."""
    def __init__(self, message: str, provider_code: int):
        super().__init__(message)
        self.provider_code = provider_code


class ProviderPendingError(ProviderError):
    """Raised when provider returns a processing/pending or unknown status code (maps to UNKNOWN)."""
    def __init__(self, message: str, provider_code: int):
        super().__init__(message)
        self.provider_code = provider_code


class ProviderMalformedResponseError(ProviderError):
    """Raised when provider response format, field types, or signature is invalid (maps to UNKNOWN)."""
    pass


class BasePaymentProvider(ABC):
    @abstractmethod
    async def create_payment(
        self,
        payment_id: str,
        provider_request_id: str,
        amount: int,
        order_id: str,
        description: str,
    ) -> Dict[str, Any]:
        """
        Creates a payment link with the provider.
        Returns a dict containing 'pay_url', 'environment', etc.
        """
        pass
