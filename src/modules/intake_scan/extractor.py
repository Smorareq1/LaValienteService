"""Reading a paper document with a vision model (Plan 0003 D3, D5, D6).

The provider sits behind :class:`ScanExtractor` so that changing model — or
vendor — is this one file and nothing else (D6). Everything downstream works on
:class:`RawScan` or :class:`RawCashSheet`, which are our shapes, not Gemini's.

The call goes over plain HTTP with `httpx` rather than a vendor SDK. Two reasons:
the SDK would be a dependency the retirement of this module has to unwind (D2),
and what we need of the API is one POST with a JSON schema attached — the part of
it that is stable.

Two documents come through here, and :class:`ScanDocument` is the whole of the
difference between them: which prompt is loaded and which schema is attached.
The ticket is one customer's order; the «Registro Diario» sheet is a whole day's
money. They share the transport, the retry, the timeout and the daily budget —
and nothing else, because a prompt that tried to describe both would describe
neither well.
"""

from __future__ import annotations

import base64
import enum
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from src.core.config import get_settings
from src.core.exceptions import ConflictError
from src.core.media import detect_image_format

PROMPTS_DIR = Path(__file__).parent / "prompts"

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

#: The option codes the paper ticket actually has. Closed lists, because Gemini's
#: structured output has no free-form maps — and because a code the catalog does
#: not know is a misread, not a new service.
TUB_CODES = ("G", "E", "P")
DRY_CODES = ("T40", "T50", "T60")
HAND_WASH_CODES = ("N2", "N3", "N4")
EXTRA_CODES = ("rins", "spin", "t10_lapses", "urgent")

#: The marks a highlighter leaves on an income row of the daily sheet, and what
#: each one means (Plan 0005 §1). Closed list for the same reason as the codes
#: above: a colour nobody defined is a misreading, not a new kind of money.
CASH_MARKS = ("none", "transfer", "invoice", "supply")

MIME_BY_FORMAT = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


class ScanDocument(enum.StrEnum):
    """Which piece of paper is in the photograph.

    Not a `ScanPurpose`: that column records *why* a photo was taken and is what
    the quality metric of §9 groups by. This is which prompt and which schema the
    call needs, and it never reaches the database.
    """

    TICKET = "ticket"
    CASH_SHEET = "cash_sheet"


class ScanFailure(ConflictError):
    """The provider could not be reached, or answered with something unusable.

    A `ConflictError` so an ordinary request answers 409 rather than 500: nothing
    broke on our side, the reading did not happen. D8 makes this survivable —
    manual capture works without this module at all.
    """


@dataclass(frozen=True)
class Extraction:
    """What one call produced, before anything believes it."""

    payload: dict[str, Any]
    model: str
    prompt_version: str
    latency_ms: int


class ScanExtractor(Protocol):
    """Turn a photograph into the raw shape of §6."""

    async def extract(
        self, image: bytes, *, document: ScanDocument = ScanDocument.TICKET
    ) -> Extraction: ...


def load_prompt(version: str) -> str:
    """The prompt of `version`, from the repo (D5).

    Missing is loud on purpose: a deployment pointing `SCAN_PROMPT_VERSION` at a
    file nobody committed would otherwise send an empty instruction and get back
    plausible nonsense.
    """
    path = PROMPTS_DIR / f"{version}.md"
    if not path.is_file():
        raise ScanFailure(f"There is no prompt version '{version}' in the repository.")
    return path.read_text(encoding="utf-8")


def _leaf(kind: str, description: str) -> dict[str, Any]:
    """One `{value, confidence, raw_text}` of D3.

    Every leaf of the contract is this shape. `raw_text` is what makes a
    disagreement diagnosable: it tells a bad transcription apart from bad
    handwriting.
    """
    return {
        "type": "object",
        "properties": {
            "value": {"type": kind, "nullable": True, "description": description},
            "confidence": {"type": "number", "description": "0 to 1. Low means unsure."},
            "raw_text": {
                "type": "string",
                "nullable": True,
                "description": "The strokes as read, literally.",
            },
        },
        "required": ["value", "confidence"],
    }


def _counts(codes: tuple[str, ...], description: str) -> dict[str, Any]:
    """A box of options, one integer each. Absent means the box was not marked."""
    return {
        "type": "object",
        "description": description,
        "properties": {code: _leaf("integer", f"How many of '{code}'.") for code in codes},
    }


def response_schema() -> dict[str, Any]:
    """The JSON Schema Gemini must answer with (D3, §6).

    Structured output is what keeps this from being prose parsing. The schema is
    built here rather than derived from the pydantic models because the two
    answer to different masters: pydantic is what we accept, this is what the
    provider supports — no `additionalProperties`, no `$ref`, no unions.
    """
    return {
        "type": "object",
        "properties": {
            "header": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "object",
                        "description": "An X in the box means annulled: send null.",
                        "properties": {
                            "day": _leaf("integer", "Day of the month."),
                            "month": _leaf("integer", "Month, 1 to 12."),
                            "year": _leaf("integer", "Four digits."),
                        },
                    },
                    "daily_number": _leaf("integer", "The handwritten 'No.'."),
                    "booklet_serial": _leaf(
                        "string", "The PRINTED '#Tomapedido' serial of the sheet."
                    ),
                    "weight_lbs": _leaf("number", "Pounds of laundry."),
                    "nit": _leaf("string", "Printed as 'Factura'. May read 'CF'."),
                },
            },
            "customer": {
                "type": "object",
                "properties": {
                    "full_name": _leaf("string", "Customer's full name."),
                    "phone": _leaf("string", "8 digits in Guatemala."),
                    "address": _leaf("string", "Street address."),
                    "email": _leaf("string", "Email address."),
                },
            },
            "garments": {
                "type": "array",
                "description": "Only rows with a quantity written in.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": _leaf("string", "One of the 21 listed garment types."),
                        "quantity": _leaf("integer", "How many pieces."),
                    },
                },
            },
            "observations": _leaf("string", "The free line at the bottom, verbatim."),
            "services": {
                "type": "object",
                "properties": {
                    "wash_by_weight_lbs": _leaf("number", "Pounds being charged."),
                    "wash_tub": _counts(TUB_CODES, "Tubs: G grande, E estándar, P pequeña."),
                    "dry": _counts(DRY_CODES, "Drying minutes: T40, T50, T60."),
                    "hand_wash": _counts(HAND_WASH_CODES, "Hand wash level: N2, N3, N4."),
                    "extras": _counts(
                        EXTRA_CODES, "rins (R), spin (S), t10_lapses (T10), urgent (SU)."
                    ),
                    "pickup_amount": _leaf("number", "Pickup fee in quetzales."),
                    "delivery_amount": _leaf("number", "Delivery fee in quetzales."),
                },
            },
            "discounts_marked": {
                "type": "array",
                "items": _leaf("string", "A promotion marked on the ticket."),
            },
            "totals_read": {
                "type": "object",
                "description": "CROSS-CHECK ONLY. These never become charges.",
                "properties": {
                    "subtotal": _leaf("number", "Subtotal as written."),
                    "discount": _leaf("number", "Discount as written."),
                    "total": _leaf("number", "Total as written."),
                },
            },
        },
        "required": ["header", "customer", "garments", "services"],
    }


def cash_response_schema() -> dict[str, Any]:
    """The JSON Schema for the «Registro Diario» sheet (Plan 0005 §1).

    Two things about its shape are worth saying out loud.

    It is a **list of blocks**, because the printed sheet stacks three days on one
    page and a photograph of it catches whichever of them happen to be filled in.
    Flattening that into one day would silently merge two days' money.

    Amounts *are* read here, unlike on the ticket, where D4 forbids it. There is
    no catalog to recompute a day's cash from: the sheet is the record. What
    replaces the pricing engine as the check is the database itself — every
    income row is matched against the ticket it names and compared with the
    balance actually owed (`cash_normalization`), and nothing is charged until a
    person has confirmed the row.
    """
    return {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "description": (
                    "One per REGISTRO DIARIO block with handwriting on it. "
                    "Blank blocks are omitted entirely."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "object",
                            "description": "Written by 'DIA/ FECHA' as 04-08-26.",
                            "properties": {
                                "day": _leaf("integer", "Day of the month."),
                                "month": _leaf("integer", "Month, 1 to 12."),
                                "year": _leaf("integer", "Two or four digits."),
                            },
                        },
                        "incomes": {
                            "type": "array",
                            "description": "Left half. Only rows with something written.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "booklet_serial": _leaf(
                                        "string", "The '#Tomapedido' of the ticket collected."
                                    ),
                                    "customer_text": _leaf(
                                        "string", "The customer's name as written."
                                    ),
                                    "amount": _leaf("number", "'Ingreso Q', in quetzales."),
                                    "mark": _leaf(
                                        "string",
                                        "Highlighter colour: one of "
                                        f"{', '.join(CASH_MARKS)}. "
                                        "pink=transfer, yellow=invoice, blue=supply.",
                                    ),
                                },
                            },
                        },
                        "expenses": {
                            "type": "array",
                            "description": "Right half. Only rows with something written.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "object_text": _leaf(
                                        "string",
                                        "Column '#Factura', which is really the object "
                                        "paid for ('2 sac', '#7', 'Claudia').",
                                    ),
                                    "description": _leaf(
                                        "string",
                                        "Column 'Proveedor', which is really the "
                                        "description ('gas', 'moto', 'detergente').",
                                    ),
                                    "amount": _leaf("number", "'Gasto Q', in quetzales."),
                                    "observations": _leaf("string", "The notes column."),
                                },
                            },
                        },
                        "attendance": {
                            "type": "array",
                            "description": "The ENTRADA / ALMUERZO / SALIDA line of the block.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "employee_text": _leaf(
                                        "string", "The name or signature on that line."
                                    ),
                                    "clock_in": _leaf("string", "'Entrada', as 7:00."),
                                    "lunch": _leaf("string", "'Almuerzo'. Often just a dash."),
                                    "clock_out": _leaf("string", "'Salida', as 12:40."),
                                },
                            },
                        },
                        "totals": {
                            "type": "object",
                            "description": "The sums written at the foot. CROSS-CHECK ONLY.",
                            "properties": {
                                "income_total": _leaf("number", "Sum of the income column."),
                                "expenses_total": _leaf("number", "Sum of the expense column."),
                                "accumulated": _leaf("number", "'Total acumulado': net."),
                            },
                        },
                    },
                },
            }
        },
        "required": ["blocks"],
    }


#: Which prompt and which schema each document is read with. Adding a third piece
#: of paper is adding a row here and a file in `prompts/`.
_DOCUMENTS = {
    ScanDocument.TICKET: ("scan_prompt_version", response_schema),
    ScanDocument.CASH_SHEET: ("scan_cash_prompt_version", cash_response_schema),
}


class GeminiExtractor:
    """The provider we use today (D1: the key never leaves the backend)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def extract(
        self, image: bytes, *, document: ScanDocument = ScanDocument.TICKET
    ) -> Extraction:
        settings = get_settings()
        if settings.gemini_api_key is None:
            raise ScanFailure("The scan service has no API key configured.")

        detected = detect_image_format(image)
        if detected is None:
            raise ScanFailure("That file is not a JPEG, PNG or WebP image.")

        setting_name, schema = _DOCUMENTS[document]
        prompt_version: str = getattr(settings, setting_name)
        prompt = load_prompt(prompt_version)
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": MIME_BY_FORMAT[detected],
                                "data": base64.b64encode(image).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema(),
                # Zero: transcription is not a task that benefits from variety,
                # and the same photo should read the same way twice.
                "temperature": 0,
            },
        }

        started = time.monotonic()
        payload = await self._post(settings, body)
        latency_ms = int((time.monotonic() - started) * 1000)

        return Extraction(
            payload=payload,
            model=settings.scan_model,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
        )

    async def _post(self, settings: Any, body: dict[str, Any]) -> dict[str, Any]:
        """One call, with one retry for a transient failure (§8, resiliencia).

        Only retried on a timeout, a connection error or a 5xx — the cases where
        trying again is not the same request being refused twice. A 400 or a 403
        is our bug or our key, and repeating it just costs another second of the
        counter's time.
        """
        url = GEMINI_ENDPOINT.format(model=settings.scan_model)
        headers = {"x-goog-api-key": settings.gemini_api_key.get_secret_value()}
        # Half the budget per attempt, so a retry still fits inside the timeout
        # the app is waiting on.
        per_attempt = max(settings.scan_timeout_s / 2, 5.0)

        last: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._request(url, headers, body, per_attempt)
                if response.status_code >= 500:
                    last = ScanFailure(f"The scan provider answered {response.status_code}.")
                    continue
                if response.status_code != 200:
                    raise ScanFailure(
                        f"The scan provider rejected the request ({response.status_code})."
                    )
                return _parse(response.json())
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last = error
                if attempt == 1:
                    break
        raise ScanFailure(f"The scan provider could not be reached: {last}")

    async def _request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, headers=headers, json=body, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, headers=headers, json=body)


def _parse(response: dict[str, Any]) -> dict[str, Any]:
    """Dig the JSON object out of the provider's envelope.

    Structured output still arrives as text inside a candidate part, so this is
    where a refusal or a truncation stops being silent: an empty candidate list
    means the model declined, and half a JSON object means the answer was cut.
    """
    candidates = response.get("candidates") or []
    if not candidates:
        feedback = response.get("promptFeedback", {})
        raise ScanFailure(f"The model returned no reading ({feedback or 'no candidates'}).")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise ScanFailure("The model returned an empty reading.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ScanFailure(f"The model's answer was not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ScanFailure("The model's answer was not an object.")
    return parsed
