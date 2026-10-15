"""`same_json` replaces `canonical(a) == canonical(b)` on hot paths; it must agree."""

from __future__ import annotations

import pytest

from store import canonical, same_json

VALUES = [
    None,
    True,
    False,
    0,
    1,
    -1,
    0.0,
    -0.0,
    1.0,
    1.5,
    "",
    "1",
    "true",
    [],
    [1],
    [1.0],
    [True],
    [1, 2],
    [2, 1],
    {},
    {"a": 1},
    {"a": 1.0},
    {"a": True},
    {"a": 1, "b": 2},
    {"b": 2, "a": 1},
    {"a": [1, {"b": None}]},
    {"a": [1, {"b": False}]},
    {"a": [1, {"c": None}]},
]


@pytest.mark.parametrize("a", VALUES, ids=repr)
@pytest.mark.parametrize("b", VALUES, ids=repr)
def test_same_json_agrees_with_canonical_comparison(a, b):
    assert same_json(a, b) == (canonical(a) == canonical(b))
