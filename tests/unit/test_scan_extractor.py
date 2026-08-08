"""The provider boundary (Plan 0003 D3, D5, D6).

The real Gemini is never called here — that is the golden-set evaluation of §9,
which costs money and needs a key. What is tested is everything around it: the
schema we demand, the prompt we load, and how the envelope is unpacked when the
answer is not what we asked for.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.core.exceptions import ConflictError
from src.modules.intake_scan.extractor import (
    DRY_CODES,
    EXTRA_CODES,
    HAND_WASH_CODES,
    TUB_CODES,
    GeminiExtractor,
    ScanFailure,
    _parse,
    load_prompt,
    response_schema,
)

#: A one-pixel JPEG: enough for the magic-byte check, which is all these need.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    + "08060607060508070707090908"
    + "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434"
    + "1f27393d38323c2e333432ffc9000b080001000101011100ffcc000600101005ffda0008010100"
    + "003f00d2cf20ffd9"
)


class TestTheResponseSchema:
    """D3: structured output is what keeps this from being prose parsing."""

    def test_every_leaf_carries_its_confidence_and_what_it_read(self) -> None:
        schema = response_schema()
        serial = schema["properties"]["header"]["properties"]["booklet_serial"]

        assert set(serial["properties"]) == {"value", "confidence", "raw_text"}
        assert serial["properties"]["value"]["nullable"] is True

    def test_the_option_boxes_are_closed_lists(self) -> None:
        """Gemini's structured output has no free-form maps — and a code the
        catalog does not know is a misread, not a new service."""
        services = response_schema()["properties"]["services"]["properties"]

        assert tuple(services["wash_tub"]["properties"]) == TUB_CODES
        assert tuple(services["dry"]["properties"]) == DRY_CODES
        assert tuple(services["hand_wash"]["properties"]) == HAND_WASH_CODES
        assert tuple(services["extras"]["properties"]) == EXTRA_CODES

    def test_the_totals_are_labelled_as_cross_check_only(self) -> None:
        """D4 lives in the prompt and in the schema: a misread price must never
        be able to reach a ticket."""
        totals = response_schema()["properties"]["totals_read"]

        assert "CROSS-CHECK ONLY" in totals["description"]

    def test_it_is_valid_json(self) -> None:
        """It travels in a request body, so it has to survive serialization."""
        assert json.loads(json.dumps(response_schema()))


class TestThePrompt:
    def test_v1_is_in_the_repository(self) -> None:
        """D5: the prompt is versioned in the repo, not in a database."""
        prompt = load_prompt("v1")

        assert "Nunca inventes" in prompt
        # The closed list of §7.2. If the seeder's names drift from these, the
        # model starts proposing garments nothing can be mapped to.
        assert "Pants/Pijama" in prompt
        assert "Toalla de manos" in prompt

    def test_a_version_nobody_committed_fails_loudly(self) -> None:
        """Silently sending an empty instruction would get back plausible nonsense."""
        with pytest.raises(ScanFailure):
            load_prompt("v99")


class TestUnpackingTheAnswer:
    def test_it_digs_the_object_out_of_the_envelope(self) -> None:
        payload = {"header": {"daily_number": {"value": 41, "confidence": 0.9}}}
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}

        assert _parse(response) == payload

    def test_a_refusal_is_not_an_empty_reading(self) -> None:
        with pytest.raises(ScanFailure, match="no reading"):
            _parse({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})

    def test_a_truncated_answer_does_not_pass_as_data(self) -> None:
        truncated = '{"header": {'
        response = {"candidates": [{"content": {"parts": [{"text": truncated}]}}]}

        with pytest.raises(ScanFailure, match="not valid JSON"):
            _parse(response)

    def test_an_answer_that_is_not_an_object_is_refused(self) -> None:
        response = {"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]}

        with pytest.raises(ScanFailure, match="not an object"):
            _parse(response)


class TestTheGeminiCall:
    """`httpx.MockTransport` stands in for the provider: the request we build and
    the way failures are handled are ours, and they are what can break."""

    @staticmethod
    def _client(handler: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_the_photo_and_the_schema_travel_in_the_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]},
            )

        _configure(monkeypatch)
        async with self._client(handler) as client:
            extraction = await GeminiExtractor(client).extract(JPEG)

        body = seen["body"]
        parts = body["contents"][0]["parts"]
        assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        # Zero: the same photo should read the same way twice.
        assert body["generationConfig"]["temperature"] == 0
        # D1: the key goes in the header, from the backend, and never to the app.
        assert seen["headers"]["x-goog-api-key"] == "test-key"
        assert extraction.prompt_version == "v1"
        assert extraction.latency_ms >= 0

    async def test_a_transient_failure_is_retried_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(503, json={"error": "overloaded"})
            return httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
            )

        _configure(monkeypatch)
        async with self._client(handler) as client:
            await GeminiExtractor(client).extract(JPEG)

        assert attempts["count"] == 2

    async def test_a_rejected_request_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 403 is our key, not the weather. Repeating it just costs a second
        of the counter's time."""
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            return httpx.Response(403, json={"error": "forbidden"})

        _configure(monkeypatch)
        async with self._client(handler) as client:
            with pytest.raises(ScanFailure, match="rejected"):
                await GeminiExtractor(client).extract(JPEG)

        assert attempts["count"] == 1

    async def test_without_a_key_it_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure(monkeypatch, key=None)

        with pytest.raises(ScanFailure, match="no API key"):
            await GeminiExtractor().extract(JPEG)

    async def test_something_that_is_not_an_image_never_leaves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)

        with pytest.raises(ScanFailure, match="not a JPEG"):
            await GeminiExtractor().extract(b"this is a text file")

    def test_a_scan_failure_answers_409_and_not_500(self) -> None:
        """Nothing broke on our side: the reading did not happen, and manual
        capture works without this module at all (D8)."""
        assert issubclass(ScanFailure, ConflictError)


def _configure(monkeypatch: pytest.MonkeyPatch, key: str | None = "test-key") -> None:
    """Point the settings at a fake key without touching the real environment."""
    from pydantic import SecretStr

    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings, "gemini_api_key", SecretStr(key) if key else None, raising=False
    )
    monkeypatch.setattr(settings, "scan_prompt_version", "v1", raising=False)
    monkeypatch.setattr(settings, "scan_model", "gemini-2.5-flash", raising=False)
    monkeypatch.setattr(settings, "scan_timeout_s", 30, raising=False)
