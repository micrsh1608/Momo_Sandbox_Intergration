from contextlib import closing
from pathlib import Path
from sqlalchemy import text
import pyodbc
import sys

connection_string = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=HUY-PC;"
    "DATABASE=PaymentDB;"
    "Trusted_Connection=yes;"
    "Encrypt=yes;"
    "TrustServerCertificate=yes;"
)

try:
    with closing(
        pyodbc.connect(connection_string, timeout=10)
    ) as connection:
        with closing(connection.cursor()) as cursor:
            cursor.execute("""
                SELECT
                    @@SERVERNAME,
                    DB_NAME(),
                    SUSER_SNAME()
            """)

            row = cursor.fetchone()

            print("KẾT NỐI SQL SERVER THÀNH CÔNG")
            print("Server:", row[0])
            print("Database:", row[1])
            print("Tài khoản:", row[2])

except pyodbc.Error as error:
    print("KẾT NỐI THẤT BẠI")
    print(error)
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent / "payment-service"))
from app.database import engine

try:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print(f"DATABASE CONNECTED: {engine.dialect.name}")
except Exception as exc:
    print(f"DATABASE CONNECTION ERROR: {exc}")
    raise SystemExit(1)