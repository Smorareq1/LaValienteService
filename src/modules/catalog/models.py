from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base
from src.core.mixins import SyncableMixin


class PricingMode(enum.StrEnum):
    """How a service resolves its price (Plan 0001 §5.2)."""

    #: Price times quantity - wash by weight, extra dry time, and every add-on.
    PER_UNIT = "per_unit"
    #: One of the service's options carries the price — tubs, drying, hand-wash level.
    TIERED = "tiered"
    #: Typed in at capture time; the courier decides what pickup/delivery costs.
    VARIABLE = "variable"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class ServiceType(SyncableMixin, Base):
    """A billable service on the ticket."""

    __tablename__ = "service_types"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    pricing_mode: Mapped[PricingMode] = mapped_column(
        Enum(PricingMode, name="pricing_mode", values_callable=_enum_values)
    )
    unit_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    options: Mapped[list[ServiceOption]] = relationship(
        back_populates="service_type",
        cascade="all, delete-orphan",
        order_by="ServiceOption.sort_order",
    )
    prices: Mapped[list[ServicePrice]] = relationship(
        back_populates="service_type", cascade="all, delete-orphan"
    )


class ServiceOption(SyncableMixin, Base):
    """One choice of a `tiered` service: tub size, drying time, hand-wash level."""

    __tablename__ = "service_options"
    __table_args__ = (UniqueConstraint("service_type_id", "code"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # No index of its own: `uq_service_options_service_type_id` already starts
    # with this column, so a second one could never be chosen and would still
    # cost a write on every catalog change.
    service_type_id: Mapped[UUID] = mapped_column(ForeignKey("service_types.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    #: Piece range that selects this option (hand-wash N2 = 1 to 4 pieces).
    min_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    service_type: Mapped[ServiceType] = relationship(back_populates="options")
    prices: Mapped[list[ServicePrice]] = relationship(back_populates="service_option")


class ServicePrice(SyncableMixin, Base):
    """A price with a validity window. Prices are never edited, only superseded (D1)."""

    __tablename__ = "service_prices"
    __table_args__ = (
        # One price in force at a time per service and option (D1). It has been
        # in the database since the first migration and only now in the model:
        # an index the schema enforces and the model does not know about is a
        # rule that disappears the day somebody rebuilds the schema from these
        # classes.
        Index(
            "uq_service_prices_open_window",
            "service_type_id",
            "service_option_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    service_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("service_types.id", ondelete="CASCADE"), index=True
    )
    service_option_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("service_options.id", ondelete="CASCADE"), nullable=True, index=True
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    #: `None` means this is the price in force today.
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    service_type: Mapped[ServiceType] = relationship(back_populates="prices")
    service_option: Mapped[ServiceOption | None] = relationship(back_populates="prices")

    def covers(self, on_date: date) -> bool:
        return self.valid_from <= on_date and (self.valid_to is None or on_date <= self.valid_to)


class GarmentType(SyncableMixin, Base):
    """A garment kind counted on the ticket (D7: administrable, not an enum)."""

    __tablename__ = "garment_types"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
