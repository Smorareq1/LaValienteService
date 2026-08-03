from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PHONE_PATTERN = r"^\+?[0-9 \-]{8,20}$"
#: "CF" (consumidor final) or a Guatemalan NIT, with or without check-digit dash.
NIT_PATTERN = r"^([Cc][Ff]|[0-9]{5,12}[-]?[0-9Kk]?)$"


class CustomerCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    nit: str | None = Field(default=None, pattern=NIT_PATTERN)
    email: str | None = Field(default=None, max_length=320)
    address: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=1000)
    #: Devices generate ids offline so an order can reference a brand-new
    #: customer before either has reached the server (Plan 0004 D3).
    id: UUID | None = None


class CustomerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    nit: str | None = Field(default=None, pattern=NIT_PATTERN)
    email: str | None = Field(default=None, max_length=320)
    address: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    phone: str | None
    nit: str | None
    email: str | None
    address: str | None
    notes: str | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class CustomerPage(BaseModel):
    items: list[CustomerRead]
    total: int
    page: int
    page_size: int
