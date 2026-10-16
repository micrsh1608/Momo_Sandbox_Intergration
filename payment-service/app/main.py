import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import engine
from app.models import Base
from app.routes import payments_router
from app.schemas import PaymentAPIException

logger = logging.getLogger(__name__)

EXPECTED_ACTIVE_INDEX_STATUSES = frozenset({"CREATED", "PENDING", "UNKNOWN", "SUCCESS"})

_IN_CLAUSE_PATTERN = re.compile(
    r"^(?:\[status\]|status)\s+IN\s*\(\s*('[^']*'(?:\s*,\s*'[^']*')*)\s*\)$",
    re.IGNORECASE,
)
_EQ_CLAUSE_PATTERN = re.compile(
    r"^(?:\[status\]|status)\s*=\s*'([^']*)'$",
    re.IGNORECASE,
)


def _strip_outer_parens(expr: str) -> str:
    """Loại bỏ các cặp ngoặc đơn bao ngoài toàn bộ biểu thức một cách cân bằng."""
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        wrapped_entirely = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i < len(s) - 1:
                    wrapped_entirely = False
                    break
        if wrapped_entirely and depth == 0:
            s = s[1:-1].strip()
        else:
            break
    return s


def _is_valid_active_order_filter(filter_definition: str) -> bool:
    """
    Kiểm tra toàn bộ biểu thức lọc `filter_definition` của index sau chuẩn hóa:
    - Chỉ chấp nhận biểu thức lọc duy nhất trên cột `status` với đúng 4 trạng thái
      `{'CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS'}` (ở dạng `status IN (...)` hoặc chuỗi `OR` chuẩn hóa của SQL Server).
    - Từ chối mọi điều kiện bổ sung (như `AND order_id IS NULL`, cột khác, toán tử khác).
    """
    if not filter_definition or not isinstance(filter_definition, str):
        return False

    normalized = _strip_outer_parens(filter_definition)
    if not normalized:
        return False

    # Dạng 1: status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS')
    in_match = _IN_CLAUSE_PATTERN.fullmatch(normalized)
    if in_match:
        raw_items = [item.strip() for item in in_match.group(1).split(",")]
        values = [item[1:-1] for item in raw_items if len(item) >= 2 and item.startswith("'") and item.endswith("'")]
        return len(values) == 4 and frozenset(values) == EXPECTED_ACTIVE_INDEX_STATUSES

    # Dạng 2: Chuỗi OR chuẩn hóa của SQL Server: ([status]='SUCCESS' OR [status]='UNKNOWN' OR [status]='PENDING' OR [status]='CREATED')
    or_parts = re.split(r"\s+OR\s+", normalized, flags=re.IGNORECASE)
    if len(or_parts) != 4:
        return False

    extracted_values = []
    for part in or_parts:
        clean_part = _strip_outer_parens(part)
        eq_match = _EQ_CLAUSE_PATTERN.fullmatch(clean_part)
        if not eq_match:
            return False
        extracted_values.append(eq_match.group(1))

    return len(extracted_values) == 4 and frozenset(extracted_values) == EXPECTED_ACTIVE_INDEX_STATUSES


def verify_active_order_index(conn) -> bool:
    """
    Kiểm tra toàn diện cấu trúc Filtered Unique Index `UQ_payments_active_order` trên SQL Server:
    1. Index tồn tại trên bảng `payments`.
    2. `is_unique == 1` và `is_disabled == 0`.
    3. Cột khóa chính xác là `order_id` (không thêm cột khóa phụ).
    4. `has_filter == 1` và toàn bộ biểu thức `filter_definition` khớp chuẩn `_is_valid_active_order_filter`.
    """
    row = conn.execute(
        text("""
            SELECT
                i.is_unique,
                i.is_disabled,
                i.has_filter,
                i.filter_definition,
                STRING_AGG(c.name, ',') WITHIN GROUP (ORDER BY ic.key_ordinal) AS key_columns
            FROM sys.indexes i
            JOIN sys.index_columns ic
                ON i.object_id = ic.object_id
                AND i.index_id = ic.index_id
                AND ic.is_included_column = 0
            JOIN sys.columns c
                ON ic.object_id = c.object_id
                AND ic.column_id = c.column_id
            WHERE i.name = 'UQ_payments_active_order'
              AND i.object_id = OBJECT_ID('payments')
            GROUP BY i.is_unique, i.is_disabled, i.has_filter, i.filter_definition
        """)
    ).fetchone()

    if not row:
        return False

    is_unique, is_disabled, has_filter, filter_definition, key_columns = row
    if not is_unique or is_disabled or not has_filter:
        return False

    if (key_columns or "").strip().lower() != "order_id":
        return False

    if not _is_valid_active_order_filter(filter_definition):
        return False

    return True


def ensure_db_schema_and_indexes(target_engine):
    """Tạo bảng, cột bổ sung và đảm bảo filtered unique index đúng cấu trúc trên database."""
    Base.metadata.create_all(bind=target_engine)
    with target_engine.connect() as conn:
        if target_engine.dialect.name == "mssql":
            # Ensure provider_transaction_id column exists
            conn.execute(
                text("""
                IF NOT EXISTS (
                    SELECT * FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE TABLE_NAME = 'payments' AND COLUMN_NAME = 'provider_transaction_id'
                )
                BEGIN
                    ALTER TABLE payments ADD provider_transaction_id VARCHAR(64) NULL;
                END
            """)
            )

            # Nếu index đã tồn tại nhưng bị disable hoặc sai cấu trúc -> xóa để tạo lại đúng chuẩn
            if not verify_active_order_index(conn):
                conn.execute(
                    text("""
                    IF EXISTS (
                        SELECT 1 FROM sys.indexes
                        WHERE name = 'UQ_payments_active_order' AND object_id = OBJECT_ID('payments')
                    )
                    BEGIN
                        DROP INDEX UQ_payments_active_order ON payments;
                    END
                    """)
                )
                conn.execute(
                    text("""
                    CREATE UNIQUE INDEX UQ_payments_active_order
                    ON payments(order_id)
                    WHERE status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS');
                    """)
                )

            conn.commit()

            if not verify_active_order_index(conn):
                raise RuntimeError(
                    "Index UQ_payments_active_order không đạt chuẩn cấu trúc (unique, enabled, column=order_id, filter=active statuses)"
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables, migrations and indexes in SQL Server database on application startup
    try:
        ensure_db_schema_and_indexes(engine)
        logger.info("Database tables and indexes initialized successfully")
    except Exception as exc:
        logger.critical("Failed to initialize required database tables or indexes: %s", exc)
        raise RuntimeError(f"Dịch vụ không thể khởi động do lỗi khởi tạo Database/Index: {exc}") from exc

    yield
    engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(PaymentAPIException)
async def payment_api_exception_handler(request: Request, exc: PaymentAPIException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "payment_id": exc.payment_id,
            "provider_code": exc.provider_code,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    error_msgs = []
    for err in errors:
        loc = " -> ".join([str(e) for e in err.get("loc", []) if e != "body"])
        msg = err.get("msg", "")
        if loc:
            error_msgs.append(f"{loc}: {msg}")
        else:
            error_msgs.append(msg)

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": f"Dữ liệu yêu cầu không hợp lệ: {'; '.join(error_msgs)}",
            "payment_id": None,
            "provider_code": None,
        },
    )


app.include_router(payments_router)


@app.get("/health", tags=["Health"])
def health():
    return {
        "service": settings.app_name,
        "status": "ok",
        "provider_mode": settings.payment_provider_mode,
        "timeout": settings.momo_request_timeout,
    }


@app.get("/health/db", tags=["Health"])
def database_health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()

            if engine.dialect.name == "mssql":
                if not verify_active_order_index(connection):
                    raise PaymentAPIException(
                        status_code=503,
                        detail="Index UQ_payments_active_order bị thiếu, bị vô hiệu hóa hoặc sai cấu trúc",
                    )

        return {"database": "connected", "indexes": "verified"}

    except SQLAlchemyError:
        logger.exception("Database health check failed")
        raise PaymentAPIException(
            status_code=503,
            detail="Database connection unavailable",
        ) from None