# MoMo Payment Service - Component A (Payment Initialization)

Dịch vụ Payment Service (Phần Thành viên A) phụ trách luồng khởi tạo thanh toán cho hệ thống bán vé workshop, kết nối giữa **Order Service** (Thành viên C), **Payment Service Database** (SQL Server `HUY-PC / PaymentDB`) và **MoMo Sandbox API** / **Mock Provider**, đồng thời phối hợp trạng thái với luồng **IPN Callback & Reconcile** (Thành viên B).

---

## 1. Cấu trúc Thư mục Dự án

```text
payment-service/
├── .env                                      # Cấu hình môi trường (MOMO_REQUEST_TIMEOUT=30.0)
├── .env.example                              # Cấu hình mẫu không chứa khóa thật
├── requirements.txt                          # Thư viện với phiên bản cố định (fastapi==0.142.1,...)
├── README.md                                 # Tài liệu hướng dẫn chi tiết & API Contract
├── payment_service_A.postman_collection.json # Collection 12 Postman request mẫu cho Thành viên C
├── app/
│   ├── __init__.py
│   ├── main.py                               # FastAPI app, PaymentAPIException handler, fatal startup on DB/Index error
│   ├── config.py                             # Config validation & Timeout clamping (minimum 30.0s)
│   ├── database.py                           # Kết nối SQLAlchemy Engine và Session (pyodbc)
│   ├── models.py                             # SQLAlchemy Model bảng payments + Check Constraints
│   ├── schemas.py                            # Request/Response schemas, ErrorResponse, MomoIPNRequest
│   ├── security.py                           # Middleware xác thực X-Internal-Token
│   ├── routes/
│   │   ├── __init__.py
│   │   └── payments.py                       # Endpoint POST /internal/payments
│   ├── services/
│   │   ├── __init__.py
│   │   ├── payment_status.py                 # Quy ước trạng thái dùng chung & phân loại mã resultCode MoMo
│   │   └── payment_service.py                # State Machine, Idempotency thống nhất, Reload DB chống ghi đè IPN
│   └── providers/
│       ├── __init__.py                       # Provider Factory
│       ├── base.py                           # Interface & Exception types (Timeout, Business, Pending, Malformed)
│       ├── momo.py                           # Strict field/type validation & tách riêng chữ ký Create Response vs IPN
│       └── mock.py                           # Provider mô phỏng thử nghiệm nội bộ
└── tests/
    ├── __init__.py
    └── test_payments.py                      # Bộ 16 test cases tự động chạy trên DB riêng (PaymentDB_Test)
```

---

## 2. Quy Ước Dùng Chung Cả Nhóm (A, B, C)

| Hạng mục | Quy ước thống nhất |
|---|---|
| **API tạo thanh toán (A -> C)** | `POST /internal/payments` |
| **API truy vấn thanh toán (B -> C)** | `GET /internal/payments/{payment_id}` |
| **API đối soát chủ động (B)** | `POST /internal/payments/{payment_id}/reconcile` |
| **API nhận callback MoMo (B)** | `POST /api/v1/payments/ipn` (trả về `204 No Content`) |
| **Schema dữ liệu callback** | `MomoIPNRequest` (`app/schemas.py`) |
| **Hàm xử lý nghiệp vụ callback** | `PaymentService.handle_ipn()` (`app/services/payment_service.py`) |
| **Trạng thái chưa kết thúc** | `CREATED`, `PENDING`, `UNKNOWN` |
| **Trạng thái kết thúc (Terminal)** | `SUCCESS`, `FAILED` (không bao giờ bị ghi đè bởi kết quả đến muộn) |

---

## 3. Hướng dẫn Cài đặt & Chạy Ứng dụng

### 3.1 Môi trường yêu cầu
- Python >= 3.10
- SQL Server (Instance `HUY-PC`, Database `PaymentDB`)
- Driver: `ODBC Driver 18 for SQL Server`

### 3.2 Cài đặt thư viện (Phiên bản cố định)
```powershell
.\.venv\Scripts\python.exe -m pip install -r payment-service/requirements.txt
```

### 3.3 Khởi chạy Service (Cổng 8002)
```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir payment-service --port 8002 --reload
```

---

## 4. Chạy Kiểm Thử Tự Động (Tests)

Bộ kiểm thử gồm **17 hàm test với 76 kịch bản kiểm thử (Unit Tests & Integration Tests)**:
- **Unit Tests** (chữ ký MoMo, kiểm tra kiểu dữ liệu, bảng phân loại `resultCode`) chạy hoàn toàn độc lập, **không cần kết nối SQL Server**.
- **Integration Tests** chỉ khởi tạo lười (lazy-init) thông qua fixture `db_test_env` trên database kiểm thử riêng **`PaymentDB_Test`** (tự động khởi tạo trên SQL Server `HUY-PC`, tuyệt đối không ghi dữ liệu vào `PaymentDB` dùng hàng ngày):

```powershell
$env:PYTHONPATH="payment-service"; .\.venv\Scripts\python.exe -m pytest payment-service/tests/test_payments.py -v
```

---

## 5. Hợp đồng API (API Contract với Thành viên C)

### **Endpoint**: `POST /internal/payments`

#### Headers yêu cầu:
- `Content-Type`: `application/json`
- `X-Internal-Token`: `<token_noi_bo>` (mặc định: `secret_internal_token_123`)
- `Idempotency-Key`: `<dinh_danh_duy_nhat_moi_lan_goi>` (tối đa 128 ký tự)

#### Request Body mẫu:
```json
{
  "order_id": "ORD-1001",
  "amount": 50000,
  "currency": "VND",
  "description": "Thanh toán vé workshop Python"
}
```

#### Cấu trúc JSON khi thành công (`200`, `201`, `202`):
```json
{
  "payment_id": "PAY-A1B2C3D4E5F6",
  "order_id": "ORD-1001",
  "status": "PENDING",
  "environment": "sandbox",
  "pay_url": "https://test-payment.momo.vn/v2/gateway/pay?t=..."
}
```

#### Cấu trúc JSON khi lỗi (`4xx`, `5xx`) - Tách riêng các trường ở cấp cao nhất:
```json
{
  "detail": "MoMo từ chối khởi tạo giao dịch: ... (payment_id=PAY-A1B2C3D4E5F6)",
  "payment_id": "PAY-A1B2C3D4E5F6",
  "provider_code": 1006
}
```

#### Bảng Mã Phản Hồi HTTP (HTTP Status Codes):
| HTTP Status | Trường hợp | Ý nghĩa & Hành động cho Order Service (C) |
|---|---|---|
| **201 Created** | Khởi tạo thanh toán mới thành công | Trạng thái `PENDING`, chuyển hướng người dùng tới `pay_url` |
| **200 OK** | Gửi lại cùng `Idempotency-Key` & cùng body khi giao dịch đã `PENDING` hoặc `SUCCESS` | Nhận lại cùng `payment_id`, `status` và `pay_url` mà không gọi lại MoMo |
| **202 Accepted** | Timeout mạng / MoMo trả mã trùng hoặc xung đột đang xử lý (`40`, `41`, `43`), mã đang xử lý (`10`, `1000`, `7000`, `7002`, `9000`), mã hoàn tiền (`1080`, `1081`), mã lạ / Replay khi giao dịch đang `CREATED` hoặc `UNKNOWN` | Giao dịch ở trạng thái `CREATED` hoặc `UNKNOWN` (`pay_url: null`), chờ IPN hoặc đối soát (Reconcile), **không** tạo giao dịch mới trùng đơn hàng |
| **400 Bad Request** | Số tiền `<= 0`, số thập phân, boolean, sai tiền tệ, vượt độ dài | Kiểm tra lại dữ liệu đầu vào |
| **401 Unauthorized** | Thiếu hoặc sai `X-Internal-Token` | Kiểm tra cấu hình token nội bộ |
| **409 Conflict** | Cùng `Idempotency-Key` nhưng khác body, hoặc `order_id` đang có giao dịch active (`CREATED`, `PENDING`, `UNKNOWN`, `SUCCESS`) | Không tạo giao dịch trùng |
| **502 Bad Gateway** | MoMo trả mã từ chối cuối cùng (`FAILED`, gồm cả `98`, `99`, `1001`–`1007`, ...) hoặc trả response sai cấu trúc/sai chữ ký (`UNKNOWN`) | Đọc trường `payment_id` và `provider_code` ở JSON để xử lý hoặc đối soát |
| **503 Service Unavailable** | Database lỗi kết nối, lỗi `IntegrityError` không thuộc key/active order, lỗi `commit()` trước/sau khi gọi provider, hoặc Index `UQ_payments_active_order` bị disable/sai cấu trúc | Có trường `payment_id` (nếu đã sinh) để đối soát; tuyệt đối không trả kết quả thành công giả |

