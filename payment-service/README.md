# MoMo Payment Service - Component A (Payment Initialization)

Dịch vụ Payment Service (Phần Thành viên A) phụ trách luồng khởi tạo thanh toán cho hệ thống bán vé workshop, kết nối giữa **Order Service** (Thành viên C), **Payment Service Database** (SQL Server `HUY-PC / PaymentDB`) và **MoMo Sandbox API** / **Mock Provider**.

---

## 1. Cấu trúc Thư mục Dự án

```text
payment-service/
├── .env                                   # Cấu hình môi trường (MOMO_REQUEST_TIMEOUT=30.0)
├── .env.example                           # Cấu hình mẫu không chứa khóa thật
├── requirements.txt                       # Thư viện với phiên bản cố định (fastapi==0.142.1,...)
├── README.md                              # Tài liệu hướng dẫn chi tiết
├── payment_service_A.postman_collection.json # Collection 11 Postman request mẫu cho C
├── app/
│   ├── __init__.py
│   ├── main.py                            # FastAPI app, fatal startup on DB/Index error, health check
│   ├── config.py                          # Config validation & Timeout clamping (minimum 30.0s)
│   ├── database.py                        # Kết nối SQLAlchemy Engine và Session (pyodbc)
│   ├── models.py                          # SQLAlchemy Model bảng payments + Check Constraints
│   ├── schemas.py                         # Request / Response schemas & Strict Validation
│   ├── security.py                        # Middleware xác thực X-Internal-Token
│   ├── routes/
│   │   ├── __init__.py
│   │   └── payments.py                    # Endpoint POST /internal/payments
│   ├── services/
│   │   ├── __init__.py
│   │   └── payment_service.py            # State Machine, Commit error recovery & Idempotency
│   └── providers/
│       ├── __init__.py                    # Provider Factory
│       ├── base.py                        # Interface & Exception types
│       ├── momo.py                        # Strict validation: partnerCode, orderId, requestId, amount, payUrl & HMAC-SHA256 signature
│       └── mock.py                        # Provider mô phỏng thử nghiệm nội bộ
└── tests/
    ├── __init__.py
    └── test_payments.py                   # Bộ 14 test cases tự động (Pytest)
```

---

## 2. Hướng dẫn Đặt & Chạy Ứng dụng

### 2.1 Môi trường yêu cầu
- Python >= 3.10
- SQL Server (Instance `HUY-PC`, Database `PaymentDB`)
- Driver: `ODBC Driver 18 for SQL Server`

### 2.2 Cài đặt thư viện (Phiên bản cố định)
```bash
pip install -r requirements.txt
```

### 2.3 Khởi chạy Service (Cổng 8002)
Chạy dịch vụ bằng Uvicorn trên cổng **8002**:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```
Hoặc từ root project:
```bash
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir payment-service --port 8002 --reload
```

---

## 3. Chạy Kiểm Thử (Tests)

Bộ kiểm thử phủ 14 trường hợp bắt buộc (bao gồm test đối chiếu dữ liệu MoMo response, test commit DB thất bại sau khi gọi provider, test IPN đến sớm `SUCCESS`/`FAILED`, test race condition cấp DB, test timeout):

```bash
$env:PYTHONPATH="payment-service"; pytest payment-service/tests -v
```

---

## 4. Hợp đồng API (API Contract với Thành viên C)

### **Endpoint**: `POST /internal/payments`

#### Headers yêu cầu:
- `Content-Type`: `application/json`
- `X-Internal-Token`: `<token_noi_bo>` (mặc định: `secret_internal_token_123`)
- `Idempotency-Key`: `<dinh_danh_duy_nhat_moi_lan_goi>` (chuỗi UUID hoặc key đơn, tối đa 128 ký tự)

#### Request Body mẫu:
```json
{
  "order_id": "ORD-1001",
  "amount": 50000,
  "currency": "VND",
  "description": "Thanh toán vé workshop Python"
}
```

#### Bảng Phản Hồi (HTTP Status Codes):
| HTTP Status | Trường hợp | Response Body Mẫu |
|---|---|---|
| **201 Created** | Khởi tạo mới thành công | `{"payment_id": "PAY-xxx", "order_id": "ORD-1001", "status": "PENDING", "environment": "sandbox", "pay_url": "https://..."}` |
| **200 OK** | Gửi trùng Idempotency-Key & cùng nội dung | Trả về kết quả khởi tạo trước đó (cùng `payment_id`, `pay_url`) |
| **202 Accepted** | Replay request khi `CREATED` / Timeout / Lỗi phản hồi | `{"payment_id": "PAY-xxx", "order_id": "ORD-1001", "status": "UNKNOWN", "environment": "sandbox", "pay_url": null}` |
| **400 Bad Request** | Số tiền sai / Thiếu trường / Vượt quá độ dài | `{"detail": "Dữ liệu yêu cầu không hợp lệ: amount -> Số tiền phải là số nguyên dương"}` |
| **401 Unauthorized** | Thiếu hoặc sai `X-Internal-Token` | `{"detail": "X-Internal-Token không hợp lệ hoặc bị thiếu"}` |
| **409 Conflict** | Trùng key nhưng khác body, hoặc đơn hàng đang có giao dịch `PENDING`/`UNKNOWN`/`SUCCESS` | `{"detail": "Cùng Idempotency-Key nhưng dữ liệu yêu cầu khác với lần trước"}` |
| **502 Bad Gateway** | MoMo từ chối / Response sai cấu trúc / Commit DB thất bại | `{"detail": "MoMo từ chối khởi tạo giao dịch: ... Payment ID: PAY-xxx"}` |
| **503 Service Unavailable** | Database lỗi / chưa sẵn sàng / Thiếu Index | `{"detail": "Dịch vụ lưu trữ chưa sẵn sàng"}` |

---

## 5. Các Điểm Kỹ Thuật Đã Được Tối Ưu Triệt Đệ

1. **Xác minh Response MoMo Nghiêm Ngặt (`momo.py`)**:
   - Bắt buộc đầy đủ các trường `partnerCode`, `orderId`, `requestId`, `amount` (parse kiểu số nguyên an toàn) và `payUrl` hợp lệ.
   - Bắt buộc kiểm tra chữ ký `signature` HMAC-SHA256 của MoMo theo đúng công thức tài liệu MoMo v2 OpenAPI.

2. **Xử lý Commit DB Thất Bại Sau Khi Gọi Provider (`payment_service.py`)**:
   - Nếu `db.commit()` thất bại sau khi provider trả về thành công, hệ thống thực hiện `db.rollback()` và ném ra lỗi **`502 Bad Gateway`** kèm `payment_id` để đối soát, **không bao giờ** giả lập trả `201 PENDING` khi chưa lưu được vào DB.

3. **Chuyển Trạng Thái Theo Quy Ước & Bảo Vệ Trạng Thái Cuối (State Machine Rules)**:
   - Chỉ cho phép chuyển từ `CREATED` / `UNKNOWN` -> `PENDING`.
   - Nếu IPN của Thành viên B đã ghi nhận `SUCCESS` hoặc `FAILED` trước đó, luồng của A sẽ giữ nguyên kết quả thực tế của IPN và trả về `200 SUCCESS` / `502 FAILED` phù hợp, không ghi đè thành `PENDING`.

4. **Khóa DB Chống Tạo Trùng & Dừng Ứng Dụng Nếu Lỗi Index (`main.py`)**:
   - Tự động tạo `Filtered Unique Index` trên SQL Server: `CREATE UNIQUE INDEX UQ_payments_active_order ON payments(order_id) WHERE status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS');`.
   - Nếu khởi tạo DB hoặc Index thất bại lúc startup, ứng dụng sẽ **dừng khởi động hoàn toàn** (`RuntimeError`) để đảm bảo không chạy ứng dụng khi thiếu khóa DB.
