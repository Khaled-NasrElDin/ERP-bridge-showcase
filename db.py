
from sqlalchemy import create_engine, String, Integer, Column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Connect to our local SQLite database
DATABASE_URL = "sqlite:///./inventory.db"

engine = create_engine(DATABASE_URL)


# Base class for database models
class Base(DeclarativeBase):
    pass

# Inventory table definition
class InventoryRecord(Base):
    __tablename__ = "inventory_records"

    sku: Mapped[str] = mapped_column(String, primary_key=True)
    warehouse: Mapped[str] = mapped_column(String, primary_key=True)
    product_name: Mapped[str] = mapped_column(String)
    stock_on_hand: Mapped[int] = mapped_column(Integer)

class TenantInventoryRecord(Base):
    __tablename__ = "tenant_inventory"

    tenant_id = Column(String, primary_key=True)
    sku = Column(String, primary_key=True)
    warehouse = Column(String, primary_key=True)

    product_name = Column(String, nullable=False)
    stock_on_hand = Column(Integer, nullable=False)

    source_version = Column(
    Integer,
    nullable=False,
    server_default="0"
)

class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"

    # Which customer owns this synchronization?
    tenant_id = Column(String, primary_key=True)

    # Which data feed are we synchronizing?
    sync_name = Column(String, primary_key=True)

    # Last page successfully committed
    last_completed_page = Column(
        Integer,
        nullable=False,
        default=0
    )

    # Current synchronization status
    status = Column(
        String,
        nullable=False,
        default="not_started"
    )

class SyncRun(Base):
    __tablename__ = "sync_runs"

    # Unique identifier for each synchronization attempt
    run_id = Column(String, primary_key=True)

    # Customer being synchronized
    tenant_id = Column(String, nullable=False)

    # Type of integration
    sync_name = Column(String, nullable=False)

    # running, completed, or failed
    status = Column(String, nullable=False)

    # Last successfully committed page for this run
    last_completed_page = Column(
        Integer,
        nullable=False,
        default=0
    )

# Create the tables if they don't exist
def initialize_database():
    Base.metadata.create_all(bind=engine)
