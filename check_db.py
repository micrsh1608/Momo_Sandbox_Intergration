from contextlib import closing

import pyodbc

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