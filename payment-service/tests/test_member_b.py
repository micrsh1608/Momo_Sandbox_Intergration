import hmac
import hashlib
import sys
from pathlib import Path

PAYMENT_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(PAYMENT_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(PAYMENT_SERVICE_DIR))

from app.providers.momo import MomoProvider


def test_ipn_signature_valid_and_invalid():
    provider = MomoProvider()

    payload = {
        "partnerCode": "MOMO",
        "orderId": "PAY-TEST-001",
        "requestId": "REQ-TEST-001",
        "amount": 50000,
        "orderInfo": "Demo",
        "orderType": "momo_wallet",
        "transId": "123456789",
        "resultCode": 0,
        "message": "Success",
        "payType": "qr",
        "responseTime": "2026-10-02 12:00:00",
        "extraData": "",
    }

    raw = (
        "accessKey=demo_access_key"
        "&amount=50000"
        "&extraData="
        "&message=Success"
        "&orderId=PAY-TEST-001"
        "&orderInfo=Demo"
        "&orderType=momo_wallet"
        "&partnerCode=MOMO"
        "&payType=qr"
        "&requestId=REQ-TEST-001"
        "&responseTime=2026-10-02 12:00:00"
        "&resultCode=0"
        "&transId=123456789"
    )
    signature = hmac.new(
        b"demo_secret_key",
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    signed_payload = {**payload, "signature": signature}

    assert provider.verify_ipn_signature(
        signed_payload,
        "demo_secret_key",
        "demo_access_key",
    )

    signed_payload["signature"] = "invalid"
    assert not provider.verify_ipn_signature(
        signed_payload,
        "demo_secret_key",
        "demo_access_key",
    )
