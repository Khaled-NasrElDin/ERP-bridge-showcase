"""Initialize local SQLite tables before starting the training prototype.

Original application source is unchanged. Run this from the repository root:
    python dev_bootstrap.py

The database is intentionally local and gitignored.
"""
from db import initialize_database
from observability import IntegrationEvent
from writeback import TransferAction
from db import engine


def main():
    initialize_database()
    TransferAction.__table__.create(bind=engine, checkfirst=True)
    IntegrationEvent.__table__.create(bind=engine, checkfirst=True)
    print("Local ERP Bridge database tables initialized.")


if __name__ == "__main__":
    main()
