from typing import Literal

STATUS_CREATED = "CREATED"
STATUS_PENDING = "PENDING"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

NON_TERMINAL_STATUSES = (STATUS_CREATED, STATUS_PENDING, STATUS_UNKNOWN)
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED)
ALL_STATUSES = NON_TERMINAL_STATUSES + TERMINAL_STATUSES

# 1. Nhóm mã lỗi gọi API / xác thực / cấu hình / tham số yêu cầu (Request / Auth / Config Errors)
# Theo tài liệu MoMo, các mã này báo lỗi của chính lệnh gọi API chứ không chứng minh giao dịch thanh toán
# đã thất bại:
# - Ở ngữ cảnh `create`: lệnh khởi tạo bị từ chối ngay lập tức (chưa tạo được giao dịch) -> FAILED.
# - Ở ngữ cảnh `query` / `ipn`: lỗi xác thực/yêu cầu khi đối soát (ví dụ 13 sai chữ ký/xác thực, 20 sai định dạng)
#   KHÔNG chứng minh khoản thanh toán gốc đã thất bại -> giữ UNKNOWN / giữ nguyên trạng thái hiện có và báo lỗi đối soát.
MOMO_API_REQUEST_ERROR_CODES = frozenset(
    {
        11,    # Truy cập bị từ chối
        12,    # Phiên bản API không được hỗ trợ
        13,    # Xác thực doanh nghiệp thất bại
        20,    # Yêu cầu sai định dạng
        21,    # Số tiền giao dịch không hợp lệ
        22,    # Số tiền giao dịch không nằm trong giới hạn cho phép
        42,    # OrderId không hợp lệ hoặc không tìm thấy
    }
)

# 2. Nhóm mã lỗi MoMo xác nhận giao dịch thanh toán đã thất bại cuối cùng (Final Transaction Failure)
# Áp dụng cho cả `create`, `query` và `ipn` (bao gồm 98, 99 và các mã từ chối thanh toán cuối 1001..4100).
MOMO_FINAL_FAILED_CODES = frozenset(
    {
        98,    # QR Code tạo không thành công (trạng thái cuối)
        99,    # Lỗi không xác định (trạng thái cuối theo tài liệu MoMo)
        1001,  # Tài khoản người dùng không đủ tiền
        1002,  # Giao dịch bị từ chối bởi nhà phát hành tài khoản thanh toán
        1003,  # Giao dịch đã bị hủy
        1004,  # Số tiền vượt quá hạn mức thanh toán của người dùng
        1005,  # URL hoặc QR code đã hết hạn
        1006,  # Người dùng đã từ chối xác nhận thanh toán
        1007,  # Tài khoản không tồn tại hoặc đang bị khóa
        1017,  # Giao dịch bị hủy bởi đối tác
        1026,  # Giao dịch bị hạn chế theo thể lệ chương trình khuyến mãi
        2001,  # Thông tin liên kết không hợp lệ
        2007,  # Liên kết đang bị tạm khóa
        4001,  # Giao dịch bị hạn chế do người dùng chưa hoàn tất xác thực
        4010,  # Mã OTP không hợp lệ
        4011,  # Chưa nhận được OTP hoặc OTP đã hết hạn
        4100,  # Người dùng đăng nhập thất bại
    }
)

# 3. Nhóm mã xung đột / trùng lặp đang xử lý trên MoMo (chưa có trạng thái cuối)
# Phải giữ trạng thái UNKNOWN (HTTP 202) và yêu cầu đối soát, tuyệt đối không đánh dấu FAILED
# để không mở khóa cho phép gọi provider lần hai tạo thanh toán trùng.
MOMO_CONFLICT_OR_IN_FLIGHT_CODES = frozenset(
    {
        40,    # RequestId bị trùng (giao dịch gốc có thể đang xử lý hoặc đã tạo)
        41,    # OrderId bị trùng (đơn hàng đã tồn tại giao dịch trên MoMo)
        43,    # Yêu cầu bị từ chối vì xung đột trong quá trình xử lý giao dịch tương tự
    }
)

# 4. Nhóm mã MoMo biểu thị giao dịch đang xử lý / bảo trì / chờ bước tiếp theo
MOMO_PENDING_OR_PROCESSING_CODES = frozenset(
    {
        10,    # Hệ thống đang được bảo trì
        1000,  # Giao dịch đã được khởi tạo, chờ người dùng xác nhận thanh toán
        7000,  # Giao dịch đang được xử lý
        7002,  # Giao dịch đang được xử lý bởi nhà cung cấp loại hình thanh toán
        9000,  # Giao dịch đã được xác nhận (authorized), xử lý phụ thuộc luồng 1 bước / 2 bước
    }
)

# 5. Nhóm mã lỗi thuộc nghiệp vụ Hoàn tiền (Refund) - Tách biệt khỏi luồng Thanh toán gốc
# Nếu xuất hiện trong luồng thanh toán gốc, tuyệt đối không tự động đánh dấu khoản thanh toán gốc là FAILED.
MOMO_REFUND_ERROR_CODES = frozenset(
    {
        1080,  # Giao dịch hoàn tiền thất bại
        1081,  # Hoàn tiền bị từ chối (giao dịch thanh toán gốc có thể đã được hoàn trước đó)
    }
)


def is_terminal_status(status_value: str) -> bool:
    """Kiểm tra trạng thái đã là trạng thái kết thúc (SUCCESS, FAILED) hay chưa."""
    return status_value in TERMINAL_STATUSES


def can_transition_status(current_status: str, target_status: str) -> bool:
    """
    Kiểm tra tính hợp lệ khi chuyển trạng thái:
    - Trạng thái kết thúc (SUCCESS, FAILED) không bao giờ bị ghi đè.
    - Chỉ trạng thái chưa kết thúc (CREATED, PENDING, UNKNOWN) mới được chuyển tiếp.
    """
    if is_terminal_status(current_status):
        return False
    return current_status in NON_TERMINAL_STATUSES and target_status in ALL_STATUSES


def is_momo_query_request_error(result_code: int) -> bool:
    """
    Kiểm tra xem resultCode có phải là lỗi gọi API / xác thực / tham số (11, 12, 13, 20, 21, 22, 42) hay không.
    Khi Thành viên B gọi query/reconcile mà gặp các mã này, cần báo lỗi đối soát và giữ nguyên
    trạng thái payment hiện có (ví dụ giữ nguyên PENDING hoặc UNKNOWN), tuyệt đối không kết luận FAILED.
    """
    if isinstance(result_code, bool) or not isinstance(result_code, int):
        raise ValueError(f"resultCode phải là kiểu int hợp lệ, nhận: {result_code!r}")
    return result_code in MOMO_API_REQUEST_ERROR_CODES


def classify_momo_result_code(
    result_code: int,
    context: Literal["create", "query", "ipn"] = "create",
    payment_flow: Literal["one_step", "two_step"] = "one_step",
) -> str:
    """
    Ánh xạ resultCode của MoMo sang trạng thái thanh toán nội bộ theo ngữ cảnh ('create', 'query', 'ipn')
    và loại luồng thanh toán ('one_step' cho captureWallet 1 bước, 'two_step' cho ủy quyền 2 bước):

    1. result_code == 0:
       - context == 'create' -> PENDING (mới tạo link thanh toán, chờ người dùng trả tiền)
       - context in ('query', 'ipn') -> SUCCESS (đã thanh toán thành công)

    2. result_code == 9000 (Giao dịch đã được xác nhận / authorized):
       - context == 'create' -> UNKNOWN (bước tạo link yêu cầu resultCode=0 kèm payUrl; cần đối soát)
       - context in ('query', 'ipn'):
         + payment_flow == 'one_step' -> SUCCESS (luồng một bước được MoMo đánh dấu thành công)
         + payment_flow == 'two_step' -> PENDING (mới ủy quyền thành công, cần tiếp tục capture hoặc cancel; chưa coi là đã thu tiền hoàn tất)

    3. result_code thuộc MOMO_API_REQUEST_ERROR_CODES (11, 12, 13, 20, 21, 22, 42):
       - context == 'create' -> FAILED (yêu cầu khởi tạo bị từ chối ngay từ đầu, chưa phát sinh giao dịch)
       - context in ('query', 'ipn') -> UNKNOWN (lỗi gọi API đối soát/xác thực không chứng minh giao dịch gốc thất bại)

    4. result_code thuộc MOMO_CONFLICT_OR_IN_FLIGHT_CODES (40, 41, 43):
       - Giữ UNKNOWN ở mọi ngữ cảnh để bắt buộc đối soát, chặn tạo giao dịch mới trùng đơn hàng.

    5. result_code thuộc MOMO_REFUND_ERROR_CODES (1080, 1081):
       - Lỗi hoàn tiền tách riêng khỏi trạng thái thanh toán gốc -> giữ UNKNOWN.

    6. result_code thuộc MOMO_PENDING_OR_PROCESSING_CODES (10, 1000, 7000, 7002):
       - Giữ UNKNOWN để chờ IPN hoặc đối soát.

    7. result_code thuộc MOMO_FINAL_FAILED_CODES (98, 99, 1001..4100):
       - Chuyển sang FAILED.

    8. Các mã lạ chưa xác định khác:
       - Giữ UNKNOWN thận trọng để đối soát.
    """
    if isinstance(result_code, bool) or not isinstance(result_code, int):
        raise ValueError(f"resultCode phải là kiểu int hợp lệ, nhận: {result_code!r}")

    if context not in ("create", "query", "ipn"):
        raise ValueError(f"Ngữ cảnh context không hợp lệ: {context!r}")

    if payment_flow not in ("one_step", "two_step"):
        raise ValueError(f"Luồng thanh toán payment_flow không hợp lệ: {payment_flow!r}")

    if result_code == 0:
        return STATUS_PENDING if context == "create" else STATUS_SUCCESS

    if result_code == 9000:
        if context == "create":
            return STATUS_UNKNOWN

        if payment_flow == "one_step":
            return STATUS_SUCCESS

        # Hai bước: đã authorized, chưa xác nhận capture hoàn tất.
        return STATUS_PENDING

    if result_code in MOMO_API_REQUEST_ERROR_CODES:
        return STATUS_FAILED if context == "create" else STATUS_UNKNOWN

    if result_code in MOMO_CONFLICT_OR_IN_FLIGHT_CODES:
        return STATUS_UNKNOWN

    if result_code in MOMO_REFUND_ERROR_CODES:
        return STATUS_UNKNOWN

    if result_code in MOMO_PENDING_OR_PROCESSING_CODES:
        return STATUS_UNKNOWN

    if result_code in MOMO_FINAL_FAILED_CODES:
        return STATUS_FAILED

    return STATUS_UNKNOWN
