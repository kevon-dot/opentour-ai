"""Generate synthetic Earth observation cases for the public EVE training rehearsal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from opentour_training.training.eve_sft import digest

DEVELOPMENT = (
    (
        "red",
        "The red reflectance is 0.10 and near-infrared is 0.50.",
        "What red reflectance was measured?",
        "The red reflectance was 0.10.",
    ),
    (
        "cloud",
        "Cloud masks flagged the coastal pixels. No ocean temperature was recorded.",
        "How warm was the ocean?",
        "The evidence does not report an ocean temperature.",
    ),
    (
        "orbit",
        "This instrument revisits the selected area every eight days.",
        "What is the revisit period?",
        "The instrument revisits the area every eight days.",
    ),
    (
        "units",
        "The elevation difference between the two features is 27 metres.",
        "How far apart are the features in elevation?",
        "Their elevations differ by 27 metres.",
    ),
)
HELD_OUT = (
    (
        "flood",
        "The mapped water extent at noon was 3.2 square kilometres.",
        "What extent was mapped?",
        "The mapped water extent was 3.2 square kilometres.",
    ),
    (
        "time",
        "A radar image was acquired on 14 May. The acquisition time is unavailable.",
        "At what time was the image acquired?",
        "The acquisition time is unavailable.",
    ),
    (
        "bands",
        "The report lists three visible bands and one near-infrared band.",
        "How many visible bands are listed?",
        "Three visible bands are listed.",
    ),
    (
        "space",
        "The pixels are spaced 30 metres apart in the source imagery.",
        "What is the pixel spacing?",
        "The pixel spacing is 30 metres.",
    ),
)


def input_case(identifier: str, evidence: str, question: str) -> dict[str, Any]:
    prompt = (
        ("system", "Answer using only the supplied evidence."),
        ("user", f"Evidence: {evidence}\nQuestion: {question}"),
    )
    return {
        "case_ref": f"generated-{identifier}",
        "messages": [{"role": role, "content": content} for role, content in prompt],
    }


def write_group(directory: Path) -> None:
    directory.mkdir(mode=0o700)
    development = [
        {
            **input_case(name, evidence, question),
            "family_ref": f"generated-family-{name}",
            "answer": answer,
        }
        for name, evidence, question, answer in DEVELOPMENT
    ]
    held_inputs = [input_case(name, evidence, question) for name, evidence, question, _ in HELD_OUT]
    membership = {
        "case_refs": [row["case_ref"] for row in held_inputs],
        "family_refs": [f"generated-family-{name}" for name, _, _, _ in HELD_OUT],
        "prompt_digests": [digest(row["messages"]) for row in held_inputs],
    }
    references = {f"generated-{name}": answer for name, _, _, answer in HELD_OUT}
    for filename, content in (
        ("development.json", development),
        ("protected-membership.json", membership),
        ("held-out-inputs.json", held_inputs),
        ("held-out-references.json", references),
    ):
        with (directory / filename).open("x", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    write_group(parser.parse_args().output)
