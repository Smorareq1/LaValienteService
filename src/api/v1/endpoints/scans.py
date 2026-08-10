from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from src.api.dependencies import (
    CashSheetServiceDependency,
    CurrentUser,
    ScanServiceDependency,
    require_permission,
)
from src.modules.intake_scan.cash_schemas import (
    CashSheetApply,
    CashSheetApplyResult,
    CashSheetRead,
)
from src.modules.intake_scan.schemas import ScanLookupRead, ScanRead

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


@router.post(
    "/lookup",
    response_model=ScanLookupRead,
    dependencies=[Depends(require_permission("scans.create"))],
)
async def lookup_ticket(
    service: ScanServiceDependency,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="Photograph of an existing ticket.")],
) -> ScanLookupRead:
    """Find the ticket somebody is holding, to hand it back (Plan 0006 §7.1.1).

    The same reading as `POST /scans`, asked a different question. There the
    paper is about to *become* a ticket; here it already is one, and the answer
    is which — the number, the balance and the state, so the counter can settle
    it without typing anything.

    Nothing is delivered here and nothing is charged: this only identifies. The
    delivery is still `POST /orders/{id}/deliver`, with its own `orders.deliver`
    (and `orders.deliver_unpaid` when a balance is left standing).

    Gated on `scans.create` and not on `orders.read`, because what is scarce here
    is not the ticket but the provider call: it comes out of the same daily
    budget as capture, and whoever may spend it is the same person.

    Declared **above** `/{scan_id}`: "lookup" is not a UUID, but the path would
    match it first and answer a validation error instead of a reading.

    Answers 409 for the same three reasons as capture — switched off, cap spent,
    provider unreachable — and the screen falls back to the search box, which is
    the path that never needed the network (D8).
    """
    return await service.lookup(await file.read(), actor=user)


@router.post(
    "/cash-close",
    response_model=CashSheetRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("scans.create"))],
)
async def scan_cash_sheet(
    service: CashSheetServiceDependency,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="Photograph of the «Registro Diario» sheet.")],
) -> CashSheetRead:
    """Read the daily sheet and propose what to file from it (Plan 0005 §1).

    A different piece of paper from the ticket, with its own prompt and its own
    schema: one page holds up to three days, each with the collections on the
    left, the money paid out on the right, and the hours worked at the foot.

    **Nothing is written here.** Every row comes back matched against the
    database — the ticket a `#Tomapedido` names and the balance it actually owes,
    the category those words mean, the employee whose hours those are — with a
    coded status saying what was found. What is charged is decided by a person on
    the next screen and sent to `/apply`.

    Declared **above** `/{scan_id}`: "cash-close" is not a UUID, but the path
    would match it first and answer a validation error instead of a reading.

    Answers 409 for the same three reasons as capture — switched off, cap spent,
    provider unreachable — and the day is still closed by hand, which is the path
    that never needed the provider at all (D8).
    """
    return await service.scan(await file.read(), actor=user)


@router.post(
    "/cash-close/{scan_id}/apply",
    response_model=CashSheetApplyResult,
    dependencies=[Depends(require_permission("scans.import_close"))],
)
async def apply_cash_sheet(
    scan_id: UUID,
    data: CashSheetApply,
    service: CashSheetServiceDependency,
    user: CurrentUser,
) -> CashSheetApplyResult:
    """File one day of a scanned sheet: collections, deliveries, expenses, hours.

    The body is what a **person confirmed**, not what was read: ids and amounts,
    no confidences. Rows the counter unticked are simply absent.

    Behind its own permission, and not `orders.collect_payment` alone, because
    the scale is the risk: those permissions are granted to take one payment in
    front of the customer it belongs to, and this takes fifteen off a photograph.
    The underlying rules still apply on every row — the payment is clamped to the
    balance, delivering with a balance left standing still demands
    `orders.deliver_unpaid`, and a closed day still refuses.

    **Row by row, never all-or-nothing.** One bad line out of fifteen must not
    undo the fourteen that were fine, so each row is its own attempt and the
    answer says what happened to each: `applied`, `skipped` (it was already
    registered — a retry landing on its own first attempt) or `failed`, with the
    reason.

    Answers 409 if the sheet was already imported. Retrying a request whose
    answer was lost is safe: the ids are derived from the scan, so the second
    attempt lands on the rows the first one wrote and reports them as skipped.
    """
    return await service.apply(scan_id, data, actor=user)


@router.get(
    "/cash-close/{scan_id}",
    response_model=CashSheetRead,
    dependencies=[Depends(require_permission("scans.read"))],
)
async def get_cash_sheet(
    scan_id: UUID, service: CashSheetServiceDependency
) -> CashSheetRead:
    """The reading again, with `already_applied` on it if it has been filed."""
    return await service.get(scan_id)


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
