from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.mixins import SyncableMixin


class DiscountType(enum.StrEnum):
    """How a promotion works out what it takes off (Plan 0001 §5.4)."""

    #: `value`% of the applicable charges — 50% of the delivery fee.
    PERCENTAGE = "percentage"
    #: A flat amount, regardless of what the ticket adds up to — Q5 off.
    FIXED_AMOUNT = "fixed_amount"
    #: The applicable charges are sold for `value` together; the discount is the
    #: difference. The July duvet promo: tub Q30 + drying Q30 for Q35.
    SPECIAL_PRICE = "special_price"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class Promotion(SyncableMixin, Base):
    """A discount the counter may pick, with a window it is valid in.

    Nothing here applies itself: in this phase the collaborator chooses the
    promotion and the engine checks it is in force and works out the amount
    (§5.4). Automatic application is a later decision, and it is a very different
    one — it changes what a customer is charged without anyone deciding it.
    """

    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint("value > 0", name="ck_promotions_value_positive"),
        # A promotion of 150% would hand money back to the customer. The rule
        # lives in the schema because the arithmetic that would go wrong is the
        # kind nobody re-checks once it is stored.
        CheckConstraint(
            "discount_type <> 'percentage' OR value <= 100",
            name="ck_promotions_percentage_within_100",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_promotions_window_ordered"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_type: Mapped[DiscountType] = mapped_column(
        Enum(DiscountType, name="discount_type", values_callable=_enum_values)
    )
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: Service codes the promotion bites on; `None` means the whole ticket.
    #: Codes and not ids: a promotion is written against what the business sells
    #: ("50% del domicilio"), and the code is the stable name of that, while the
    #: id of a service re-seeded on a fresh database is not.
    applies_to_service_codes: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(50)), nullable=True
    )
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    #: `None` means it has no end date, not that it ended.
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def covers(self, on_date: date) -> bool:
        """Whether the promotion is live on `on_date`.

        Deactivating is not the same as ending: `is_active` is the switch an
        administrator flips today, `valid_to` is the date the promotion was
        always going to stop. Both have to hold.
        """
        if not self.is_active or self.deleted_at is not None:
            return False
        return self.valid_from <= on_date and (self.valid_to is None or on_date <= self.valid_to)

    def applies_to(self, service_code: str) -> bool:
        if self.applies_to_service_codes is None:
            return True
        return service_code in self.applies_to_service_codes

    def discount_on(self, applicable_base: Decimal) -> Decimal:
        """What this promotion takes off, given the charges it bites on (§5.4).

        `applicable_base` is the sum of the lines whose service is in
        `applies_to_service_codes` — the whole subtotal when the promotion names
        no services. Never returns a negative: a special price above what the
        ticket already costs is worth nothing, not money owed to the shop.

        Exact, unrounded: the engine rounds every amount on a ticket the same
        way, and doing it twice is how a cent goes missing.
        """
        if self.discount_type is DiscountType.PERCENTAGE:
            raw = applicable_base * self.value / Decimal(100)
        elif self.discount_type is DiscountType.FIXED_AMOUNT:
            raw = self.value
        else:
            raw = applicable_base - self.value

        return max(raw, Decimal(0))
