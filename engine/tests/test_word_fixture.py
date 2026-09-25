"""The Engine and the shell must agree on what a word is: `_WORD` in
`timings.py` and `WORD` in `src/shell/readalong.ts` are held to one fixture."""

import json
from pathlib import Path

from readily_engine.timings import word_spans

FIXTURE = Path(__file__).parent / "fixtures" / "words.json"


def test_word_spans_match_the_shared_fixture():
    fixture = json.loads(FIXTURE.read_text())
    assert word_spans(fixture["text"]) == [tuple(span) for span in fixture["words"]]
