import hashlib
import hmac
import logging
import re
from typing import Any, Dict
import httpx

from app.config import settings
from app.providers.base import (
    BasePaymentProvider,
    ProviderBusinessError,
    ProviderMalformedResponseError,
    ProviderPendingError,
    ProviderTimeoutError,
)
from app.services.payment_status import STATUS_FAILED, classify_momo_result_code

logger = logging.getLogger(__name__)

HEX_SIGNATURE_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class MomoProvider(BasePaymentProvider):
    @staticmethod
    def _parse_strict_int(value: Any, field_name: str) -> int:
        """
        Kiểm tra kiểu dữ liệu nghiêm ngặt cho trường số nguyên:
        - Từ chối boolean (True/False) vì trong Python bool là subclass của int.
        - Từ chối float hoặc chuỗi số thập phân (không dùng int() để âm thầm cắt phần thập phân).
        """
        if isinstance(value, bool):
            raise ProviderMalformedResponseError(
                f"Trường {field_name} trong response không được là kiểu boolean"
            )
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            raise ProviderMalformedResponseError(
                f"Trường {field_name} trong response không được là số thập phân ({value})"
            )
        if isinstance(value, str):
            stripped = value.strip()
            if re.fullmatch(r"-?\d+", stripped):
                return int(stripped)
            raise ProviderMalformedResponseError(
                f"Trường {field_name} trong response không phải số nguyên hợp lệ ('{value}')"
            )
        raise ProviderMalformedResponseError(
            f"Trường {field_name} trong response có kiểu dữ liệu không hợp lệ ({type(value).__name__})"
        )

    @staticmethod
    def _is_valid_hex_signature(signature: Any) -> bool:
        """Kiểm tra chữ ký đúng định dạng 64 ký tự hex ASCII trước khi gọi compare_digest."""
        return isinstance(signature, str) and bool(HEX_SIGNATURE_PATTERN.fullmatch(signature))

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
        """Tạo chữ ký HMAC-SHA256 cho request khởi tạo thanh toán (Create Payment Request)."""
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

    def verify_create_response_signature(
        self, response_data: Dict[str, Any], secret_key: str, access_key: str
    ) -> bool:
        """
        Xác minh chữ ký HMAC-SHA256 cho response khởi tạo thanh toán (Create Payment Response).
        Tách riêng với công thức IPN; KHÔNG bao gồm extraData, orderInfo, orderType.
        """
        received_signature = response_data.get("signature")
        if not self._is_valid_hex_signature(received_signature):
            return False

        required_fields = (
            "amount",
            "message",
            "orderId",
            "partnerCode",
            "payUrl",
            "requestId",
            "responseTime",
            "resultCode",
        )
        for field in required_fields:
            if field not in response_data or response_data[field] is None:
                return False

        raw_signature = (
            f"accessKey={access_key}"
            f"&amount={response_data['amount']}"
            f"&message={response_data['message']}"
            f"&orderId={response_data['orderId']}"
            f"&partnerCode={response_data['partnerCode']}"
            f"&payUrl={response_data['payUrl']}"
            f"&requestId={response_data['requestId']}"
            f"&responseTime={response_data['responseTime']}"
            f"&resultCode={response_data['resultCode']}"
        )

        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            raw_signature.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(received_signature.lower(), expected_signature.lower())

    def verify_response_signature(
        self, response_data: Dict[str, Any], secret_key: str, access_key: str
    ) -> bool:
        """Alias cho verify_create_response_signature."""
        return self.verify_create_response_signature(response_data, secret_key, access_key)

    def verify_ipn_signature(
        self, ipn_data: Dict[str, Any], secret_key: str, access_key: str
    ) -> bool:
        """
        Xác minh chữ ký HMAC-SHA256 cho callback IPN của MoMo (tách riêng với response create).
        """
        received_signature = ipn_data.get("signature")
        if not self._is_valid_hex_signature(received_signature):
            return False

        raw_signature = (
            f"accessKey={access_key}"
            f"&amount={ipn_data.get('amount', '')}"
            f"&extraData={ipn_data.get('extraData', '')}"
            f"&message={ipn_data.get('message', '')}"
            f"&orderId={ipn_data.get('orderId', '')}"
            f"&orderInfo={ipn_data.get('orderInfo', '')}"
            f"&orderType={ipn_data.get('orderType', '')}"
            f"&partnerCode={ipn_data.get('partnerCode', '')}"
            f"&payType={ipn_data.get('payType', '')}"
            f"&requestId={ipn_data.get('requestId', '')}"
            f"&responseTime={ipn_data.get('responseTime', '')}"
            f"&resultCode={ipn_data.get('resultCode', '')}"
            f"&transId={ipn_data.get('transId', '')}"
        )

        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            raw_signature.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(received_signature.lower(), expected_signature.lower())

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

        # 1. Kiểm tra kiểu dữ liệu và đối chiếu các trường định danh bắt buộc (M3)
        res_partner_code = data.get("partnerCode")
        res_order_id = data.get("orderId")
        res_request_id = data.get("requestId")
        raw_res_amount = data.get("amount")
        raw_result_code = data.get("resultCode")
        raw_message = data.get("message")

        if not isinstance(res_partner_code, str) or res_partner_code != partner_code:
            raise ProviderMalformedResponseError(
                f"Thiếu hoặc sai partnerCode trong response (nhận: {res_partner_code!r}, kỳ vọng: {partner_code!r})"
            )

        if not isinstance(res_order_id, str) or res_order_id != payment_id:
            raise ProviderMalformedResponseError(
                f"Thiếu hoặc sai orderId trong response (nhận: {res_order_id!r}, kỳ vọng: {payment_id!r})"
            )

        if not isinstance(res_request_id, str) or res_request_id != provider_request_id:
            raise ProviderMalformedResponseError(
                f"Thiếu hoặc sai requestId trong response (nhận: {res_request_id!r}, kỳ vọng: {provider_request_id!r})"
            )

        if raw_res_amount is None:
            raise ProviderMalformedResponseError("Response từ MoMo thiếu trường amount")

        res_amount = self._parse_strict_int(raw_res_amount, "amount")
        if res_amount != amount:
            raise ProviderMalformedResponseError(
                f"amount trong response ({res_amount}) không khớp với số tiền yêu cầu ({amount})"
            )

        if raw_result_code is None:
            raise ProviderMalformedResponseError(
                "Response từ MoMo thiếu trường resultCode"
            )
        result_code = self._parse_strict_int(raw_result_code, "resultCode")

        if raw_message is not None and not isinstance(raw_message, str):
            raise ProviderMalformedResponseError(
                "Trường message trong response phải là chuỗi ký tự"
            )
        message = raw_message if isinstance(raw_message, str) else "N/A"

        if "responseTime" in data and data["responseTime"] is not None:
            self._parse_strict_int(data["responseTime"], "responseTime")

        if result_code == 0:
            if not isinstance(raw_message, str):
                raise ProviderMalformedResponseError(
                    "Response thành công từ MoMo thiếu trường message hợp lệ"
                )
            if "responseTime" not in data or data["responseTime"] is None:
                raise ProviderMalformedResponseError(
                    "Response thành công từ MoMo thiếu trường responseTime"
                )

            pay_url = data.get("payUrl")
            if not pay_url or not isinstance(pay_url, str) or not pay_url.startswith(("http://", "https://")):
                raise ProviderMalformedResponseError(
                    "MoMo báo thành công nhưng thiếu payUrl hợp lệ"
                )

            # Kiểm tra định dạng chữ ký trước khi gọi compare_digest (M3 Task 4)
            res_signature = data.get("signature")
            if not self._is_valid_hex_signature(res_signature):
                raise ProviderMalformedResponseError(
                    "Response từ MoMo thiếu hoặc sai định dạng chữ ký signature (cần 64 ký tự hex)!"
                )

            if not self.verify_create_response_signature(data, secret_key, access_key):
                raise ProviderMalformedResponseError(
                    "Chữ ký HMAC-SHA256 trong response từ MoMo không hợp lệ!"
                )

            return {
                "pay_url": pay_url,
                "environment": "sandbox",
                "provider_code": 0,
                "provider_message": message,
                "provider_transaction_id": str(data.get("transId", "")) if data.get("transId") is not None else None,
            }
        else:
            # M4: Phân biệt mã từ chối cuối cùng (FAILED) với mã đang xử lý / chưa biết (UNKNOWN)
            mapped_status = classify_momo_result_code(result_code, context="create")
            if mapped_status == STATUS_FAILED:
                raise ProviderBusinessError(
                    f"MoMo từ chối giao dịch: {message} (code={result_code})",
                    provider_code=result_code,
                )
            else:
                raise ProviderPendingError(
                    f"Giao dịch MoMo đang xử lý hoặc chưa xác định kết quả: {message} (code={result_code})",
                    provider_code=result_code,
                )
