"""Score the extractor against the golden set (Plan 0003 §9).

    python -m scripts.eval_scan [--set tests/fixtures/scan_golden]

Run it **by hand** when the prompt or the model changes, and compare against the
previous run. It is deliberately not in CI: it needs a real API key and every run
costs money.

The rule the plan sets, and the reason this exists: *no prompt version ships that
lowers overall accuracy.* Without a number, "it seems better" is how a prompt
quietly gets worse at reading fours.

The golden set is a directory of pairs — `ticket-01.jpg` next to
`ticket-01.json`, the latter annotated by hand with the fields as a person reads
them. Twenty varied tickets is the floor: different handwriting, crooked shots,
shadow, boxes crossed out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.modules.intake_scan.extractor import GeminiExtractor, ScanFailure

DEFAULT_SET = Path("tests/fixtures/scan_golden")

#: The groups accuracy is reported by, and the path into the payload of each.
GROUPS = {
    "header": ("header",),
    "customer": ("customer",),
    "garments": ("garments",),
    "services": ("services",),
}


@dataclass
class Score:
    hits: int = 0
    total: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.hits / self.total if self.total else 1.0


def leaves(node: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a reading to `path -> value`, dropping confidence and raw text.

    Only `value` is scored. `confidence` is the model's opinion of itself and
    grading it here would reward a model that is confidently wrong exactly as
    much as one that is right.
    """
    flat: dict[str, Any] = {}
    if isinstance(node, dict):
        if "value" in node and "confidence" in node:
            flat[prefix] = node["value"]
            return flat
        for key, value in node.items():
            flat.update(leaves(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            flat.update(leaves(item, f"{prefix}[{index}]"))
    else:
        flat[prefix] = node
    return flat


def compare(expected: Any, actual: Any, group: str) -> Score:
    score = Score()
    want = leaves(expected.get(group, {}))
    got = leaves(actual.get(group, {}))

    for path, value in want.items():
        score.total += 1
        if _same(value, got.get(path)):
            score.hits += 1
        else:
            score.misses.append(f"{path}: expected {value!r}, read {got.get(path)!r}")
    return score


def _same(expected: Any, actual: Any) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(expected) - float(actual)) < 0.01
    return str(expected).strip().lower() == str(actual or "").strip().lower()


async def evaluate(golden: Path) -> dict[str, Score]:
    images = sorted(
        path
        for path in golden.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if not images:
        raise SystemExit(
            f"No tickets in {golden}. The golden set of §9 starts at 20 varied photos."
        )

    extractor = GeminiExtractor()
    totals = {group: Score() for group in GROUPS}

    for image in images:
        annotation = image.with_suffix(".json")
        if not annotation.is_file():
            print(f"  skipped {image.name}: no {annotation.name} beside it")
            continue

        expected = json.loads(annotation.read_text(encoding="utf-8"))
        try:
            extraction = await extractor.extract(image.read_bytes())
        except ScanFailure as failure:
            print(f"  FAILED {image.name}: {failure}")
            continue

        print(f"  {image.name} ({extraction.latency_ms} ms)")
        for group in GROUPS:
            score = compare(expected, extraction.payload, group)
            totals[group].hits += score.hits
            totals[group].total += score.total
            totals[group].misses.extend(f"{image.name} · {miss}" for miss in score.misses)

    return totals


def report(totals: dict[str, Score]) -> None:
    print("\nAccuracy by group")
    print("-" * 46)
    overall_hits = overall_total = 0
    for group, score in totals.items():
        overall_hits += score.hits
        overall_total += score.total
        print(f"  {group:<12} {score.accuracy:>7.1%}  ({score.hits}/{score.total})")
    overall = overall_hits / overall_total if overall_total else 1.0
    print("-" * 46)
    print(f"  {'OVERALL':<12} {overall:>7.1%}  ({overall_hits}/{overall_total})")

    misses = [miss for score in totals.values() for miss in score.misses]
    if misses:
        print(f"\nWhat it got wrong ({len(misses)})")
        for miss in misses[:40]:
            print(f"  · {miss}")
        if len(misses) > 40:
            print(f"  … and {len(misses) - 40} more")

    print(
        "\nWrite this number into prompts/CHANGELOG.md. A version that lowers it "
        "does not ship (§9)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, dest="golden")
    arguments = parser.parse_args()
    if not arguments.golden.is_dir():
        raise SystemExit(f"{arguments.golden} does not exist.")
    report(asyncio.run(evaluate(arguments.golden)))


if __name__ == "__main__":
    main()
