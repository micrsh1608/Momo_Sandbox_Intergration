from typing import Any, Dict
from app.providers.base import (
    BasePaymentProvider,
    ProviderBusinessError,
    ProviderMalformedResponseError,
    ProviderTimeoutError,
)


class MockProvider(BasePaymentProvider):
    def __init__(self):
        self.call_count = 0

    async def create_payment(
        self,
        payment_id: str,
        provider_request_id: str,
        amount: int,
        order_id: str,
        description: str,
    ) -> Dict[str, Any]:
        self.call_count += 1

        # Simulation hooks for testing edge cases
        if "[MOCK_TIMEOUT]" in description or amount == 999999:
            raise ProviderTimeoutError("Mô phỏng timeout khi kết nối tới MoMo Sandbox")

        if "[MOCK_REJECTED]" in description or amount == 888888:
            raise ProviderBusinessError(
                "Mô phỏng MoMo từ chối khởi tạo giao dịch",
                provider_code=99,
            )

        if "[MOCK_MALFORMED]" in description or amount == 777777:
            raise ProviderMalformedResponseError(
                "Mô phỏng MoMo trả về response sai cấu trúc"
            )

        return {
            "pay_url": f"https://mock.momo.vn/pay/{payment_id}",
            "environment": "mock",
            "provider_code": 0,
            "provider_message": "Thành công (Mock)",
            "provider_transaction_id": f"MOCK_TRANS_{payment_id}",
        }
