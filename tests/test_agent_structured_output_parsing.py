"""Structured-output extraction: a bracket in prose is never mistaken for the answer.

The last-resort scan of prose used to return the first decodable ``{`` or ``[``, so
"snapshot [1] ... {...}" gave the list [1], which can never validate as an object
answer. The scan now skips values of another root type whole. Answers that parsed
before parse exactly as before (checked against the previous implementation).
"""
import json
import random

import pytest
from pydantic import BaseModel, ConfigDict

from lumibot.components.agents.structured_output import _extract_json_value, parse_structured_output, root_type


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    weight: float


OBJECT = {"symbol": "SPY", "weight": 0.1}


def previous_extract(text):
    """The extraction before this change, kept verbatim to prove nothing that parsed has changed."""
    import re
    fence = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
    candidates = [text.strip()] + [m.strip() for m in fence.findall(text)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        return value
    raise ValueError("the final answer contained no JSON value")


def test_a_bracket_in_prose_before_the_answer_is_skipped():
    text = "I checked the snapshot [1] and the ranks [0.2, 0.4]. Final answer: " + json.dumps(OBJECT)
    assert previous_extract(text) == [1]  # The server incident: a list, so "$ (model_type)".
    parsed, error = parse_structured_output(text, Answer.model_json_schema(), Answer)
    assert error is None and parsed == Answer(**OBJECT)


def test_prose_without_an_object_is_reported_as_no_json_object():
    parsed, error = parse_structured_output("Keep the targets [1] unchanged.", Answer.model_json_schema(), Answer)
    assert parsed is None and error == "the final answer contained no JSON object"


def test_an_object_nested_inside_a_skipped_list_is_never_promoted():
    text = "Candidates: [" + json.dumps(OBJECT) + "] were considered."
    with pytest.raises(ValueError, match="no JSON object"):
        _extract_json_value(text, "object")


def test_a_fenced_block_of_another_type_before_the_answer_is_skipped():
    text = "Ranks:\n```\n[1]\n```\nAnswer:\n```json\n" + json.dumps(OBJECT) + "\n```"
    assert previous_extract(text) == [1]
    assert _extract_json_value(text, "object") == OBJECT


@pytest.mark.parametrize("text", ["[1, 2]", "3", '"text"'])
def test_a_whole_answer_is_returned_as_it_is_whatever_its_type(text):
    assert _extract_json_value(text, "object") == previous_extract(text)
    parsed, error = parse_structured_output(text, Answer.model_json_schema(), Answer)
    assert parsed is None and "did not match Answer" in error  # Validation reports it, unchanged.


def test_array_and_untyped_schemas_keep_their_behavior():
    assert root_type({"type": "array", "items": {}}) == "array" and root_type({"anyOf": []}) is None
    assert root_type(Answer.model_json_schema()) == "object" and root_type(None) is None
    text = 'Prefix {"note": 1} then [1, 2]'
    assert _extract_json_value(text, "array") == [1, 2]
    assert _extract_json_value(text) == previous_extract(text) == {"note": 1}


def fragments(rng):
    pieces = ["Final answer:", "see [1]", "[0.2, 0.4]", '["SPY"]', "[SPY", "{broken", "}", "]", "note",
              json.dumps(OBJECT), '{"other": true}', "```json\n" + json.dumps(OBJECT) + "\n```", "```\n[1]\n```",
              "[" + json.dumps(OBJECT) + "]", "{ }", "[]", "text with {braces} inside", "\n"]
    return " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 6)))


def test_every_answer_the_previous_parser_accepted_with_the_right_type_is_unchanged():
    rng = random.Random(20261110)
    changed = 0
    for _ in range(5000):
        text = fragments(rng)
        try:
            old = previous_extract(text)
        except ValueError:
            old = ValueError
        try:
            new = _extract_json_value(text, "object")
        except ValueError:
            new = ValueError
        if isinstance(old, dict):
            assert new == old, text  # Accepted before: identical now.
        elif old is ValueError:
            assert new is ValueError, text  # Nothing found before: nothing found now.
        elif new != old:
            changed += 1  # Only answers that could never validate as an object change.
            assert new is ValueError or isinstance(new, dict), text
    assert changed > 100  # The generated corpus does exercise the changed case.
