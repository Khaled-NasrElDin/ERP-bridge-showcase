from db import engine

with engine.begin() as connection:
    columns = [
        row[1]
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(tenant_inventory)"
        )
    ]

    if "source_version" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE tenant_inventory "
            "ADD COLUMN source_version INTEGER NOT NULL DEFAULT 0"
        )

print("Database migration complete!")
