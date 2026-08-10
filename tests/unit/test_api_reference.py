"""`docs/API.md` against the application it claims to describe (PR 12).

A reference nobody checks is a reference that is wrong by the second sprint. The
route table is generated, so this only has to assert the obvious: that what is
committed is what the app currently exposes, and that the generator is still
reading permissions rather than quietly emitting a column of dashes.
"""

import pytest

from scripts.dump_api_reference import DOC, END, START, render, replace_region


@pytest.fixture(scope="module")
def document() -> str:
    return DOC.read_text(encoding="utf-8")


class TestTheRouteTable:
    def test_the_committed_table_matches_the_app(self, document: str) -> None:
        assert (
            render() in document
        ), "docs/API.md is out of date. Run `python -m scripts.dump_api_reference`."

    def test_the_generator_finds_the_permissions(self) -> None:
        """Guards the guard: an empty registry would render every route as public."""
        table = render()
        assert "| `daily_close.close` |" in table
        assert "| `attendance.record` |" in table
        # `/sync/push` carries no route permission on purpose — RBAC is evaluated
        # per operation, when it is applied (Plan 0004 §10).
        assert "| `POST` | `/api/v1/sync/push` | sesión |" in table

    def test_the_region_markers_survive_a_rewrite(self, document: str) -> None:
        assert document.count(START) == 1
        assert document.count(END) == 1
        assert replace_region(document, render()) == document
