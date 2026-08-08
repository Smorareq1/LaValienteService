from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from src.api.dependencies import CurrentUser, ScanServiceDependency, require_permission
from src.modules.intake_scan.schemas import ScanRead

router = APIRouter(prefix="/scans", tags=["Intake scan"])


@router.post(
    "",
    response_model=ScanRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("scans.create"))],
)
async def create_scan(
    service: ScanServiceDependency,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="Photograph of the paper ticket.")],
) -> ScanRead:
    """Read a paper ticket and return a **draft** (Plan 0003 §8).

    Synchronous inside `SCAN_TIMEOUT_S`: the person is standing at the counter
    with the ticket in their hand, and a job to poll would be a worse experience
    than the thirty seconds this takes.

    Nothing is saved as an order here. The answer is a prefill for the capture
    screen, with a confidence on every field and coded warnings for whatever did
    not add up — and the amounts are the pricing engine's, never the paper's (D4).

    Answers 409 when the module is switched off, the daily cap is spent, or the
    provider could not be reached. That is not a server error: manual capture
    works without this module at all (D8), which is what makes it survivable.
    """
    return await service.scan(await file.read(), actor=user)


@router.get(
    "/{scan_id}",
    response_model=ScanRead,
    dependencies=[Depends(require_permission("scans.read"))],
)
async def get_scan(scan_id: UUID, service: ScanServiceDependency) -> ScanRead:
    return await service.get(scan_id)


@router.get(
    "/{scan_id}/image",
    dependencies=[Depends(require_permission("scans.read"))],
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}, "description": "The original photo."}},
)
async def get_scan_image(scan_id: UUID, service: ScanServiceDependency) -> Response:
    """The photograph, for the side-by-side review of §4.

    Served from the endpoint and never from a static path: these images carry a
    customer's name, phone and NIT in their own handwriting, so reaching one
    demands `scans.read` like everything else here (D9).
    """
    content, media_type = await service.get_image(scan_id)
    return Response(content=content, media_type=media_type)
