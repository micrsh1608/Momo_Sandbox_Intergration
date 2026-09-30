import hashlib
import hmac
import logging
from typing import Any, Dict
import httpx

from app.config import settings
from app.providers.base import (
    BasePaymentProvider,
    ProviderBusinessError,
    ProviderMalformedResponseError,
    ProviderTimeoutError,
)

logger = logging.getLogger(__name__)


class MomoProvider(BasePaymentProvider):
    def generate_signature(
        self,
        partner_code: str,
        access_key: str,
        secret_key: str,
        provider_request_id: str,
        amount: int,
        payment_id: str,
        order_info: str,
        redirect_url: str,
        ipn_url: str,
        request_type: str,
        extra_data: str = "",
    ) -> str:
        raw_signature = (
            f"accessKey={access_key}"
            f"&amount={amount}"
            f"&extraData={extra_data}"
            f"&ipnUrl={ipn_url}"
            f"&orderId={payment_id}"
            f"&orderInfo={order_info}"
            f"&partnerCode={partner_code}"
            f"&redirectUrl={redirect_url}"
            f"&requestId={provider_request_id}"
            f"&requestType={request_type}"
        )
        return hmac.new(
            secret_key.encode("utf-8"),
            raw_signature.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_response_signature(
        self, response_data: Dict[str, Any], secret_key: str, access_key: str
    ) -> bool:
        received_signature = response_data.get("signature")
        if not received_signature or not isinstance(received_signature, str) or not received_signature.strip():
            return False

        # Raw signature format for MoMo Create Payment response according to MoMo v2 OpenAPI spec
        raw_signature = (
            f"accessKey={access_key}"
            f"&amount={response_data.get('amount', '')}"
            f"&extraData={response_data.get('extraData', '')}"
            f"&message={response_data.get('message', '')}"
            f"&orderId={response_data.get('orderId', '')}"
            f"&orderInfo={response_data.get('orderInfo', '')}"
            f"&orderType={response_data.get('orderType', '')}"
            f"&partnerCode={response_data.get('partnerCode', '')}"
            f"&payUrl={response_data.get('payUrl', '')}"
            f"&requestId={response_data.get('requestId', '')}"
            f"&responseTime={response_data.get('responseTime', '')}"
            f"&resultCode={response_data.get('resultCode', '')}"
        )

        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            raw_signature.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(received_signature, expected_signature)

    async def create_payment(
        self,
        payment_id: str,
        provider_request_id: str,
        amount: int,
        order_id: str,
        description: str,
    ) -> Dict[str, Any]:
        partner_code = settings.momo_partner_code
        access_key = settings.momo_access_key
        secret_key = settings.momo_secret_key
        redirect_url = settings.momo_redirect_url
        ipn_url = settings.momo_ipn_url
        request_type = "captureWallet"
        extra_data = ""

        signature = self.generate_signature(
            partner_code=partner_code,
            access_key=access_key,
            secret_key=secret_key,
            provider_request_id=provider_request_id,
            amount=amount,
            payment_id=payment_id,
            order_info=description,
            redirect_url=redirect_url,
            ipn_url=ipn_url,
            request_type=request_type,
            extra_data=extra_data,
        )

        payload = {
            "partnerCode": partner_code,
            "partnerName": "Workshop Ticketing",
            "storeId": "WorkshopStore",
            "requestId": provider_request_id,
            "amount": amount,
            "orderId": payment_id,
            "orderInfo": description,
            "redirectUrl": redirect_url,
            "ipnUrl": ipn_url,
            "lang": "vi",
            "extraData": extra_data,
            "requestType": request_type,
            "signature": signature,
        }

        try:
            async with httpx.AsyncClient(
                timeout=settings.momo_request_timeout
            ) as client:
                response = await client.post(
                    settings.momo_endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ConnectError,
        ) as exc:
            logger.warning("MoMo request timeout or network error: %s", exc)
            raise ProviderTimeoutError(
                "Không thể kết nối đến MoMo Sandbox (Timeout/Network Error)"
            ) from exc
        except Exception as exc:
            logger.error("Unexpected network error calling MoMo: %s", exc)
            raise ProviderTimeoutError("Lỗi kết nối mạng tới MoMo") from exc

        if response.status_code != 200:
            logger.error(
                "MoMo endpoint returned HTTP status %d: %s",
                response.status_code,
                response.text,
            )
            raise ProviderMalformedResponseError(
                f"MoMo endpoint trả về HTTP status {response.status_code}"
            )

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderMalformedResponseError(
                "Response từ MoMo không đúng định dạng JSON"
            ) from exc

        if not isinstance(data, dict):
            raise ProviderMalformedResponseError(
                "Response từ MoMo không phải là JSON object"
            )

        # 1. Mandatory Field Checks (Mục ① Review)
        res_partner_code = data.get("partnerCode")
        res_order_id = data.get("orderId")
        res_request_id = data.get("requestId")
        raw_res_amount = data.get("amount")

        if not res_partner_code or str(res_partner_code) != str(partner_code):
            raise ProviderMalformedResponseError(
                f"Thiếu hoặc sai partnerCode trong response (nhận: '{res_partner_code}', kỳ vọng: '{partner_code}')"
            )

        if not res_order_id or str(res_order_id) != str(payment_id):
            raise ProviderMalformedResponseError(
                f"Thiếu hoặc sai orderId trong response (nhận: '{res_order_id}', kỳ vọng: '{payment_id}')"
            )

        if not res_request_id or str(res_request_id) != str(provider_request_id):
            raise ProviderMalformedResponseError(
                f"Thiếu hoặc sai requestId trong response (nhận: '{res_request_id}', kỳ vọng: '{provider_request_id}')"
            )

        if raw_res_amount is None:
            raise ProviderMalformedResponseError("Response từ MoMo thiếu trường amount")

        try:
            res_amount = int(raw_res_amount)
        except (ValueError, TypeError) as exc:
            raise ProviderMalformedResponseError(
                f"Trường amount trong response không phải số nguyên hợp lệ ('{raw_res_amount}')"
            ) from exc

        if res_amount != int(amount):
            raise ProviderMalformedResponseError(
                f"amount trong response ({res_amount}) không khớp với số tiền yêu cầu ({amount})"
            )

        result_code = data.get("resultCode")
        message = data.get("message", "N/A")

        if result_code is None:
            raise ProviderMalformedResponseError(
                "Response từ MoMo thiếu trường resultCode"
            )

        if result_code == 0:
            pay_url = data.get("payUrl")
            if not pay_url or not isinstance(pay_url, str) or not pay_url.startswith(("http://", "https://")):
                raise ProviderMalformedResponseError(
                    "MoMo báo thành công nhưng thiếu payUrl hợp lệ"
                )

            # 2. Mandatory Signature Check for Successful Response (Mục ① Review)
            res_signature = data.get("signature")
            if not res_signature or not isinstance(res_signature, str) or not res_signature.strip():
                raise ProviderMalformedResponseError(
                    "Response từ MoMo báo thành công nhưng thiếu chữ ký signature bắt buộc!"
                )

            if not self.verify_response_signature(data, secret_key, access_key):
                raise ProviderMalformedResponseError(
                    "Chữ ký HMAC-SHA256 trong response từ MoMo không hợp lệ!"
                )

            return {
                "pay_url": pay_url,
                "environment": "sandbox",
                "provider_code": 0,
                "provider_message": message,
                "provider_transaction_id": str(data.get("transId", "")),
            }
        else:
            raise ProviderBusinessError(
                f"MoMo từ chối giao dịch: {message} (code={result_code})",
                provider_code=result_code,
            )
