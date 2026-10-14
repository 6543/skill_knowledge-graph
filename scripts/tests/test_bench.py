from bench import probe


class Graph:
    def q(self, query):
        return True if query == "ASK" else [123]


def test_ask_is_scored():
    item = {
        "question": "exists?",
        "mode": "sparql",
        "sparql": "ASK",
        "expect_first": True,
    }
    assert probe(item, Graph(), 5)["hit"]


def test_missing_expectation_is_a_failure():
    item = {"question": "how many?", "mode": "sparql", "sparql": "SELECT"}
    result = probe(item, Graph(), 5)
    assert not result["hit"] and result["error"]


def test_row_expectations_are_exact_values():
    item = {
        "question": "how many?",
        "mode": "sparql",
        "sparql": "SELECT",
        "expect_rows_contain": [12],
    }
    assert not probe(item, Graph(), 5)["hit"]


def test_wrong_expectation_for_mode_is_a_failure():
    item = {
        "question": "how many?",
        "mode": "sparql",
        "sparql": "SELECT",
        "expect_paths": ["missing.txt"],
    }
    result = probe(item, Graph(), 5)
    assert not result["hit"] and result["error"]
