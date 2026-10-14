"""Ranking regressions. Each case here is a failure the benchmark surfaced."""

from __future__ import annotations

from store import KG, tokenize


def chunk(source, path, index, text, heading=""):
    return {
        "label": "Chunk",
        "id": f"{source}:{path}#{index}",
        "props": {
            "path": path,
            "source": source,
            "repository": source,
            "heading": heading,
            "start_line": index * 10,
            "end_line": index * 10 + 9,
            "text": text,
        },
    }


def graph(tmp_path, monkeypatch, nodes):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    kg = KG("search", create=True)
    kg.load(nodes, [])
    return kg


def test_a_large_file_does_not_outrank_the_file_that_answers_the_question(
    tmp_path, monkeypatch
):
    """Scoring a file by the sum over its chunks makes the biggest file win every
    query, which is how changelogs bury the module that implements a feature."""
    nodes = [
        chunk("repo", "CHANGELOG.md", i, f"release note {i} about the mailer timeout")
        for i in range(60)
    ]
    nodes.append(
        chunk("repo", "runbooks/mailer.md", 1, "The mailer timeout is raised in app.ini.")
    )
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("How do I fix the mailer timeout?")
    assert results[0]["path"] == "runbooks/mailer.md"


def test_a_rare_term_outweighs_a_term_that_appears_everywhere(tmp_path, monkeypatch):
    nodes = [chunk("repo", f"common/file{i}.md", 1, "gitea configuration notes") for i in range(40)]
    nodes.append(chunk("repo", "db/pool.md", 1, "gitea sets MAX_OPEN_CONNS to 60"))
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("What MAX_OPEN_CONNS value does gitea use?")
    assert results[0]["path"] == "db/pool.md"


def test_a_match_in_the_path_locates_a_module_the_body_never_names(tmp_path, monkeypatch):
    nodes = [
        chunk("repo", "modules/markup/render.go", 1, "func New() *Renderer { return &Renderer{} }"),
        chunk("repo", "docs/intro.md", 1, "Gitea turns text into pages while it runs."),
    ]
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("Which module handles markup rendering?")
    assert results[0]["path"] == "modules/markup/render.go"


def test_a_prefix_match_in_the_path_survives_a_word_ending(tmp_path, monkeypatch):
    nodes = [
        chunk("repo", "services/repository/archiver/archiver.go", 1, "func doArchive() {}"),
        chunk("repo", "docs/other.md", 1, "unrelated prose"),
    ]
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("Where is repository archive download implemented?")
    assert results[0]["path"] == "services/repository/archiver/archiver.go"


def test_absent_terms_are_reported_so_absence_can_be_distinguished_from_bad_ranking(
    tmp_path, monkeypatch
):
    kg = graph(tmp_path, monkeypatch, [chunk("repo", "notes.md", 1, "postgres pool sizing")])

    answer = kg.search(["postgres", "Cassandra"])
    assert answer["unmatched"] == ["Cassandra"]
    assert answer["results"], "a partial match is still evidence worth returning"

    assert kg.search(["postgres", "Cassandra"], require_all=True)["results"] == []


def test_source_filter_keeps_evidence_inside_one_corpus(tmp_path, monkeypatch):
    nodes = [
        chunk("ops", "runbook.md", 1, "restart the scheduler service"),
        chunk("code", "scheduler.go", 1, "func restart() {}"),
    ]
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("how do I restart the scheduler", source="ops")
    assert {r["repository"] for r in results} == {"ops"}


def test_question_words_are_dropped_but_identifiers_survive_tokenisation():
    assert tokenize("Where is the MAX_OPEN_CONNS value set in app.ini?") == [
        "MAX_OPEN_CONNS",
        "value",
        "set",
        "app.ini",
    ]
