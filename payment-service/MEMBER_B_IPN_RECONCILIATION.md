# Member B – MoMo IPN / Webhook, Security, Lookup & Reconciliation

## 1. Mục tiêu

Phần B hoàn thiện luồng sau khi Payment Service đã tạo payment:

```text
MoMo
  │
  │ POST /internal/payments/ipn
  ▼
Payment Service
  ├─ 1. Tìm payment theo orderId
  ├─ 2. Kiểm tra partnerCode / requestId / amount
  ├─ 3. Xác minh HMAC-SHA256 signature
  ├─ 4. Tạo event_key để chống xử lý IPN trùng
  ├─ 5. Cập nhật trạng thái SUCCESS / FAILED
  └─ 6. Trả HTTP 200 cho MoMo

Order Service / Admin
  │
  ├─ GET  /internal/payments/{payment_id}
  │        └─ Tra cứu giao dịch
  │
  └─ POST /internal/payments/{payment_id}/reconcile
           └─ Query MoMo/Mock Provider để đối soát khi UNKNOWN/PENDING
```

MoMo mô tả IPN là cơ chế server-to-server gửi kết quả thanh toán tới `ipnUrl`; payload thanh toán hiện tại gồm các trường như `partnerCode`, `orderId`, `requestId`, `amount`, `transId`, `resultCode`, `message`, `payType`, `responseTime`, `extraData`, `signature`. Chữ ký HMAC-SHA256 của notification dùng chuỗi dữ liệu theo thứ tự key được MoMo quy định.

## 2. Phần B đã thêm vào source code

### 2.1. `app/providers/momo.py`

Thêm:

- `verify_ipn_signature()`
  - HMAC-SHA256.
  - Kiểm tra chữ ký bằng `hmac.compare_digest()`.
  - Không log secret key/signature raw.
- `generate_ipn_ack_signature()`
  - Tạo chữ ký cho response ACK.
- `generate_query_signature()`
  - Tạo signature cho API query.
- `query_payment()`
  - Gọi MoMo Query API.
  - Endpoint: `/v2/gateway/api/query`.
  - Kiểm tra HTTP status, JSON, `partnerCode`, `orderId`, `resultCode`.

### 2.2. `app/providers/mock.py`

Có `query_payment()` để demo đối soát mà không cần tài khoản MoMo thật.

### 2.3. `app/models.py`

Thêm bảng:

`payment_ipn_events`

Bảng này lưu:

- `event_key`
- `payment_id`
- `request_id`
- `transaction_id`
- `result_code`
- `created_at`

`event_key` có unique constraint. Nếu MoMo gửi lại đúng notification, insert lần thứ hai sẽ bị bắt bởi `IntegrityError` và hệ thống trả thành công nhưng **không xử lý business lần thứ hai**.

### 2.4. `app/services/payment_service.py`

Thêm:

- `handle_ipn()`
- `get_payment()`
- `reconcile_payment()`

State machine:

```text
CREATED / PENDING / UNKNOWN
            │
            ├── resultCode = 0              ──> SUCCESS
            ├── resultCode = 7000/7002/9000 ──> PENDING
            └── other resultCode            ──> FAILED
```

Nếu payment đã ở `SUCCESS` hoặc `FAILED`, IPN mới không được phép ghi đè sang trạng thái cuối khác.

### 2.5. `app/routes/payments.py`

Thêm 3 API:

| API | Mục đích | Bảo vệ |
|---|---|---|
| `POST /internal/payments/ipn` | Nhận IPN từ MoMo | HMAC signature |
| `GET /internal/payments/{payment_id}` | Tra cứu payment | `X-Internal-Token` |
| `POST /internal/payments/{payment_id}/reconcile` | Đối soát với provider | `X-Internal-Token` |

> IPN không dùng `X-Internal-Token` vì request này do MoMo server gửi tới `ipnUrl`; chữ ký HMAC là cơ chế xác thực chính của callback.

## 3. Chạy project

Từ thư mục project:

```powershell
pip install -r payment-service/requirements.txt
```

Tạo:

```text
payment-service/.env
```

Có thể copy từ:

```text
payment-service/.env.example
```

### Demo Mock

Giữ:

```env
PAYMENT_PROVIDER_MODE=mock
IPN_DEMO_ACCESS_KEY=demo_access_key
IPN_DEMO_SECRET_KEY=demo_secret_key
```

Sau đó chạy:

```powershell
$env:PYTHONPATH="payment-service"
python -m uvicorn app.main:app --port 8002 --reload
```

Swagger:

```text
http://localhost:8002/docs
```

## 4. Demo seminar

### Bước 1 – Tạo payment

`POST /internal/payments`

Headers:

```text
X-Internal-Token: secret_internal_token_123
Idempotency-Key: DEMO-B-001
Content-Type: application/json
```

Body:

```json
{
  "order_id": "ORD-DEMO-B-001",
  "amount": 50000,
  "currency": "VND",
  "description": "Demo Member B"
}
```

Lấy `payment_id` và `provider_request_id` từ DB/API.

### Bước 2 – Tạo IPN hợp lệ

Payload ví dụ:

```json
{
  "partnerCode": "MOMO",
  "orderId": "PAY-XXXXXXXXXXXX",
  "requestId": "REQ-XXXXXXXXXXXX",
  "amount": 50000,
  "orderInfo": "Demo Member B",
  "orderType": "momo_wallet",
  "transId": "123456789",
  "resultCode": 0,
  "message": "Success",
  "payType": "qr",
  "responseTime": 1760000000000,
  "extraData": "",
  "signature": "<HMAC-SHA256>"
}
```

Chữ ký IPN được tạo trên chuỗi:

```text
accessKey=...
&amount=...
&extraData=...
&message=...
&orderId=...
&orderInfo=...
&orderType=...
&partnerCode=...
&payType=...
&requestId=...
&responseTime=...
&resultCode=...
&transId=...
```

Secret dùng để ký:

```text
demo_secret_key
```

### Bước 3 – Chứng minh chống IPN trùng

Gửi **cùng payload IPN lần thứ hai**.

Kết quả:

```json
{
  "accepted": true,
  "duplicate": true,
  "message": "IPN trùng, không xử lý lại"
}
```

Điểm cần nói khi thuyết trình:

> MoMo có thể retry webhook. Vì vậy Payment Service không coi mỗi HTTP request là một giao dịch mới. `event_key` unique giúp cùng một IPN chỉ được áp dụng business logic một lần.

### Bước 4 – Tra cứu

```http
GET /internal/payments/{payment_id}
```

Header:

```text
X-Internal-Token: secret_internal_token_123
```

API trả:

- payment_id
- order_id
- amount
- provider
- status
- provider_transaction_id
- provider_response_code
- pay_url
- provider_message

### Bước 5 – Đối soát

```http
POST /internal/payments/{payment_id}/reconcile
```

API gọi:

```text
PaymentService
      │
      ▼
Provider.query_payment()
      │
      ├── MockProvider → demo
      │
      └── MomoProvider → POST /v2/gateway/api/query
```

Sau đó cập nhật local DB theo kết quả provider.

## 5. Các case nên demo

| Case | Input | Kết quả mong đợi |
|---|---|---|
| IPN Success | `resultCode=0`, signature đúng | `SUCCESS` |
| IPN Failed | `resultCode!=0`, signature đúng | `FAILED` |
| Sai signature | signature giả | HTTP `401` |
| Sai amount | amount khác DB | HTTP `409` |
| Sai requestId | requestId khác DB | HTTP `409` |
| Không tồn tại payment | orderId không tồn tại | HTTP `404` |
| Gửi IPN 2 lần | cùng event | Lần 2 `duplicate=true` |
| Tra cứu | GET payment_id | Trả trạng thái hiện tại |
| Đối soát | POST reconcile | Query provider và cập nhật DB |

## 6. Tại sao thiết kế này phù hợp microservice?

Payment Service không để Order Service tự xử lý MoMo.

```text
Order Service
      │
      │ POST /internal/payments
      ▼
Payment Service
      │
      │ provider abstraction
      ▼
BasePaymentProvider
      ├── MomoProvider
      └── MockProvider
```

Khi đổi payment provider, phần business của `PaymentService` không cần biết chi tiết HTTP/HMAC của provider mới.

## 7. Reusable cho nhóm khác

Muốn tái sử dụng phần B:

1. Copy `PaymentIPNEvent` model.
2. Copy `handle_ipn()` vào payment service.
3. Implement `verify_ipn_signature()` cho provider.
4. Implement `query_payment()` cho provider.
5. Đổi payload schema theo provider.
6. Giữ nguyên nguyên tắc:
   - Verify signature trước khi cập nhật DB.
   - Verify order/payment ID.
   - Verify amount.
   - Idempotent webhook.
   - Không ghi đè trạng thái cuối.
   - Có API query/reconciliation khi callback thất lạc.

## 8. Nội dung trình bày 5–7 phút

**Slide 1 – Problem**

"Thanh toán không kết thúc ở lúc tạo payUrl. Payment Service cần biết khách đã thanh toán hay chưa."

**Slide 2 – IPN Workflow**

```text
Customer → MoMo
            │
            ▼
        IPN/Webhook
            │
            ▼
      Verify Signature
            │
            ▼
       Verify Payment
            │
            ▼
       Update Database
```

**Slide 3 – Security**

- HMAC-SHA256.
- Không tin `orderId`/`amount` từ callback nếu chưa đối chiếu DB.
- `X-Internal-Token` cho API nội bộ.
- Secret nằm trong `.env`, không commit Git.

**Slide 4 – Duplicate IPN**

Demo gửi cùng IPN 2 lần.

Lần đầu:

```text
duplicate=false
status=SUCCESS
```

Lần hai:

```text
duplicate=true
status=SUCCESS
```

**Slide 5 – Reconciliation**

```text
UNKNOWN/PENDING
       │
       ▼
POST /reconcile
       │
       ▼
MoMo Query API
       │
       ▼
Update local status
```

**Slide 6 – Reusable Code**

Cho lớp xem:

- `MomoProvider.verify_ipn_signature()`
- `PaymentService.handle_ipn()`
- `PaymentService.reconcile_payment()`
- `PaymentIPNEvent`

## 9. Lưu ý khi chuyển sang MoMo Sandbox thật

- Thay `MOMO_ACCESS_KEY` và `MOMO_SECRET_KEY` bằng credential Sandbox thật.
- Không commit `.env`.
- `MOMO_IPN_URL` phải là URL mà MoMo Sandbox có thể gọi tới; `localhost` trên máy sinh viên không phải public endpoint.
- Giữ timeout tối thiểu 30 giây cho MoMo Query API.
