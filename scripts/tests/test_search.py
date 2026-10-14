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
        chunk(
            "repo", "runbooks/mailer.md", 1, "The mailer timeout is raised in app.ini."
        )
    )
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("How do I fix the mailer timeout?")
    assert results[0]["path"] == "runbooks/mailer.md"


def test_a_rare_term_outweighs_a_term_that_appears_everywhere(tmp_path, monkeypatch):
    nodes = [
        chunk("repo", f"common/file{i}.md", 1, "gitea configuration notes")
        for i in range(40)
    ]
    nodes.append(chunk("repo", "db/pool.md", 1, "gitea sets MAX_OPEN_CONNS to 60"))
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("What MAX_OPEN_CONNS value does gitea use?")
    assert results[0]["path"] == "db/pool.md"


def test_a_match_in_the_path_locates_a_module_the_body_never_names(
    tmp_path, monkeypatch
):
    nodes = [
        chunk(
            "repo",
            "modules/markup/render.go",
            1,
            "func New() *Renderer { return &Renderer{} }",
        ),
        chunk("repo", "docs/intro.md", 1, "Gitea turns text into pages while it runs."),
    ]
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("Which module handles markup rendering?")
    assert results[0]["path"] == "modules/markup/render.go"


def test_a_prefix_match_in_the_path_survives_a_word_ending(tmp_path, monkeypatch):
    nodes = [
        chunk(
            "repo", "services/repository/archiver/archiver.go", 1, "func doArchive() {}"
        ),
        chunk("repo", "docs/other.md", 1, "unrelated prose"),
    ]
    kg = graph(tmp_path, monkeypatch, nodes)

    results = kg.search_question("Where is repository archive download implemented?")
    assert results[0]["path"] == "services/repository/archiver/archiver.go"


def test_absent_terms_are_reported_so_absence_can_be_distinguished_from_bad_ranking(
    tmp_path, monkeypatch
):
    kg = graph(
        tmp_path, monkeypatch, [chunk("repo", "notes.md", 1, "postgres pool sizing")]
    )

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


def test_merging_one_chunk_leaves_the_existing_index_intact(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch, [chunk("repo", "old.md", 1, "perfectoid spaces")])

    kg.load([chunk("repo", "new.md", 1, "condensed mathematics")], [], replace=False)

    assert kg.search_keywords(["perfectoid"])[0]["path"] == "old.md"
    assert kg.search_keywords(["condensed"])[0]["path"] == "new.md"


def test_rewriting_a_chunk_removes_the_text_it_used_to_hold(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch, [chunk("repo", "notes.md", 1, "the old wording")])

    kg.load(
        [chunk("repo", "notes.md", 1, "the new wording")],
        [],
        replace=False,
        on_conflict="incoming",
    )

    assert kg.search_keywords(["old wording"]) == []
    assert kg.search_keywords(["new wording"])[0]["path"] == "notes.md"


def test_document_frequency_counts_every_chunk_not_just_the_fetched_ones(
    tmp_path, monkeypatch
):
    """Inferring frequency from the capped result set understates how common a word
    is, which inflates its weight exactly where the corpus is large enough to care."""
    nodes = [chunk("repo", f"f{i}.md", 1, "common word here") for i in range(30)]
    nodes.append(chunk("repo", "rare.md", 1, "singular word here"))
    kg = graph(tmp_path, monkeypatch, nodes)

    assert kg.document_frequency("common") == 30
    assert kg.document_frequency("singular") == 1
    assert kg.document_frequency("absent") == 0


SEARCH_KEYS = {
    "terms",
    "unmatched",
    "results",
    "candidate_limit",
    "capped_terms",
    "nodes",
}


def test_a_result_names_the_document_node_not_the_internal_grouping_key(
    tmp_path, monkeypatch
):
    node = chunk("ops", "server.md", 0, "The server timeout defaults to 30 seconds.")
    node["props"]["document_id"] = "ops:server.md@abc"
    kg = graph(tmp_path, monkeypatch, [node])
    for scope in ("file", "chunk"):
        result = kg.search(["timeout"], scope=scope)["results"][0]
        assert result["document_id"] == "ops:server.md@abc"
        assert result["document_id"] == result["evidence"][0]["document_id"]


def test_a_result_without_a_recorded_document_has_no_document_id(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch, [chunk("ops", "server.md", 0, "timeout")])
    assert kg.search(["timeout"])["results"][0]["document_id"] is None


def test_search_returns_the_same_top_level_keys_whether_or_not_anything_matches(
    tmp_path, monkeypatch
):
    kg = graph(tmp_path, monkeypatch, [chunk("ops", "server.md", 0, "timeout")])
    cases = {
        "match": kg.search(["timeout"]),
        "no terms": kg.search([]),
        "no match": kg.search(["zzznothing"]),
        "required phrase unmatched": kg.search(
            ["timeout", "zzznothing"], require_all=True
        ),
    }
    for name, result in cases.items():
        assert set(result) == SEARCH_KEYS, name
    for name in ("no terms", "no match", "required phrase unmatched"):
        assert cases[name]["capped_terms"] == [], name
        assert cases[name]["results"] == [], name
    assert kg.search(["zzznothing"], candidate_limit=7)["candidate_limit"] == 7
