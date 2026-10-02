import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import engine
from app.models import Base
from app.routes import payments_router, webhook_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables, migrations and indexes in SQL Server database on application startup
    try:
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            if engine.dialect.name == "mssql":
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
                conn.execute(
                    text("""
                    IF NOT EXISTS (
                        SELECT *
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_NAME = 'payments'
                        AND COLUMN_NAME = 'payment_id'
                    )
                    BEGIN
                        ALTER TABLE payments ADD payment_id VARCHAR(64) NULL;
                    END
                """)
                )
                # Ensure filtered unique index for active order payments
                conn.execute(
                    text("""
                    IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'UQ_payments_active_order' AND object_id = OBJECT_ID('payments'))
                    BEGIN
                        CREATE UNIQUE INDEX UQ_payments_active_order ON payments(order_id) WHERE status IN ('CREATED', 'PENDING', 'UNKNOWN', 'SUCCESS');
                    END
                """)
                )
                conn.commit()
        logger.info("Database tables and indexes initialized successfully")
    except Exception as exc:
        logger.critical("Failed to initialize required database tables or indexes: %s", exc)
        # Stop application startup if database tables or required indexes fail to initialize (Mục 4 Review)
        raise RuntimeError(f"Dịch vụ không thể khởi động do lỗi khởi tạo Database/Index: {exc}") from exc

    yield
    engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
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
        },
    )

app.include_router(payments_router)
app.include_router(webhook_router)

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
                # Check required index UQ_payments_active_order
                index_check = connection.execute(
                    text("SELECT index_id FROM sys.indexes WHERE name = 'UQ_payments_active_order' AND object_id = OBJECT_ID('payments')")
                ).fetchone()
                if not index_check:
                    raise HTTPException(
                        status_code=503,
                        detail="Thiếu Index UQ_payments_active_order trên database",
                    )

        return {"database": "connected", "indexes": "verified"}

    except SQLAlchemyError:
        logger.exception("Database health check failed")
        raise HTTPException(
            status_code=503,
            detail="Database connection unavailable",
        ) from None