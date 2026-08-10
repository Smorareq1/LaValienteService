from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import FileResponse

from src.api.dependencies import CurrentUser, InventoryServiceDependency, require_permission
from src.core import media
from src.core.exceptions import NotFoundError
from src.modules.inventory.models import MovementType
from src.modules.inventory.schemas import (
    MovementCreate,
    MovementRead,
    ProductCreate,
    ProductLotCreate,
    ProductLotRead,
    ProductLotUpdate,
    ProductRead,
    ProductUpdate,
)

router = APIRouter(prefix="/inventory", tags=["Inventory"])

OnDate = Annotated[date | None, Query(alias="date", description="Business date of the movement.")]


@router.get(
    "/products",
    response_model=list[ProductRead],
    dependencies=[Depends(require_permission("inventory.read"))],
)
async def list_products(
    service: InventoryServiceDependency, include_inactive: bool = False
) -> list[ProductRead]:
    """The supplies, each with the stock its lots still hold (§6.3)."""
    return await service.list_products(include_inactive=include_inactive)


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("inventory.manage"))],
)
async def create_product(data: ProductCreate, service: InventoryServiceDependency) -> ProductRead:
    return await service.create_product(data)


@router.get(
    "/products/{product_id}",
    response_model=ProductRead,
    dependencies=[Depends(require_permission("inventory.read"))],
)
async def get_product(product_id: UUID, service: InventoryServiceDependency) -> ProductRead:
    return await service.read_product(product_id)


@router.patch(
    "/products/{product_id}",
    response_model=ProductRead,
    dependencies=[Depends(require_permission("inventory.manage"))],
)
async def update_product(
    product_id: UUID, data: ProductUpdate, service: InventoryServiceDependency
) -> ProductRead:
    return await service.update_product(product_id, data)


@router.put(
    "/products/{product_id}/image",
    response_model=ProductRead,
    dependencies=[Depends(require_permission("inventory.manage"))],
)
async def set_product_image(
    product_id: UUID,
    service: InventoryServiceDependency,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP.")],
) -> ProductRead:
    """Upload the product's photo (D10).

    The file is checked by its own bytes and not by its name, stored under
    `MEDIA_DIR`, and only its path reaches the database. The returned
    `image_path` changes with every upload, which is how the app knows the photo
    it has cached is no longer the current one.
    """
    return await service.set_image(product_id, await file.read())


@router.get(
    "/products/{product_id}/image",
    response_class=FileResponse,
    dependencies=[Depends(require_permission("inventory.read"))],
)
async def get_product_image(product_id: UUID, service: InventoryServiceDependency) -> FileResponse:
    """Serve the photo.

    Not in the plan's table of §7, which only names the upload — but an image
    that can be stored and never read is a write-only file, and the app has to
    get it from somewhere. It goes through the API rather than a static mount so
    that reading it asks for `inventory.read` like everything else in the module.
    """
    product = await service.get_product(product_id)
    if not product.image_path:
        raise NotFoundError("That product has no image.")
    path = media.resolve(product.image_path)
    if not path.is_file():
        # The row points at a file that is not there: a restored database
        # without its media directory, most likely. Saying "no image" is closer
        # to the truth than a 500.
        raise NotFoundError("That product's image file is missing.")
    return FileResponse(path)


@router.get(
    "/products/{product_id}/lots",
    response_model=list[ProductLotRead],
    dependencies=[Depends(require_permission("inventory.read"))],
)
async def list_lots(
    product_id: UUID, service: InventoryServiceDependency
) -> list[ProductLotRead]:
    """The product's lots, oldest first — the order a sale would draw on (D5)."""
    return await service.list_lots(product_id)


@router.post(
    "/products/{product_id}/lots",
    response_model=ProductLotRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("inventory.manage"))],
)
async def register_lot(
    product_id: UUID,
    data: ProductLotCreate,
    service: InventoryServiceDependency,
    user: CurrentUser,
) -> ProductLotRead:
    """Register a purchase. The lot number is assigned by the system (D3).

    A `purchase_in` movement goes in with it: stock never appears without a line
    in the kardex saying where it came from.
    """
    return await service.register_lot(product_id, data, actor=user)


@router.patch(
    "/lots/{lot_id}",
    response_model=ProductLotRead,
    dependencies=[Depends(require_permission("inventory.manage"))],
)
async def update_lot(
    lot_id: UUID, data: ProductLotUpdate, service: InventoryServiceDependency
) -> ProductLotRead:
    """Correct the cost or the sale price. Quantities move only through the kardex."""
    return await service.update_lot(lot_id, data)


@router.get(
    "/movements",
    response_model=list[MovementRead],
    dependencies=[Depends(require_permission("inventory.read"))],
)
async def list_movements(
    service: InventoryServiceDependency,
    product_id: UUID | None = None,
    lot_id: UUID | None = None,
    movement_type: MovementType | None = None,
    on_date: OnDate = None,
) -> list[MovementRead]:
    """The kardex: every change of stock with the reason behind it (D4)."""
    return await service.list_movements(
        product_id=product_id, lot_id=lot_id, on_date=on_date, movement_type=movement_type
    )


@router.post(
    "/movements",
    response_model=MovementRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("inventory.adjust"))],
)
async def record_movement(
    data: MovementCreate, service: InventoryServiceDependency, user: CurrentUser
) -> MovementRead:
    """Write down internal use, or correct a count.

    Only those two: a purchase comes from registering a lot and a sale from
    selling one, so neither can be typed in here without its document.
    """
    return await service.record_movement(data, actor=user)
