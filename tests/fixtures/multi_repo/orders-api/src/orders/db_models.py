from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"

    id = None
    total_cents = None
