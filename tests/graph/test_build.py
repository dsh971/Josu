"""Tests for the graphify-backed GraphEngine (U1)."""

from __future__ import annotations

import pytest

from josu.graph.build import GraphifyEngine


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    (repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def engine(tmp_path):
    return GraphifyEngine(out_dir=tmp_path / "graphify-out")


def test_build_produces_nodes_and_edges(engine, fixture_repo):
    engine.build(fixture_repo)
    stats = engine.execute("graph_stats", {})
    assert stats["nodes"] > 0
    assert stats["edges"] > 0


def test_build_persists_graph_to_disk(engine, fixture_repo):
    engine.build(fixture_repo)
    assert engine.graph_path.exists()


def test_search_finds_matching_node_with_neighbors(engine, fixture_repo):
    engine.build(fixture_repo)
    results = engine.search("greet")
    assert len(results) > 0
    assert any("neighbors" in r for r in results)


def test_search_scoped_to_single_repo_directory(engine, fixture_repo, tmp_path):
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    (other_repo / "unrelated.py").write_text("def unrelated():\n    pass\n", encoding="utf-8")

    engine.build(fixture_repo)
    results = engine.search("unrelated")
    assert results == []


def test_search_spans_parent_directory_with_multiple_repos(tmp_path):
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    (repo_a / "a.py").write_text("def from_a():\n    pass\n", encoding="utf-8")
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    (repo_b / "b.py").write_text("def from_b():\n    pass\n", encoding="utf-8")

    engine = GraphifyEngine(out_dir=tmp_path / "graphify-out")
    engine.build(tmp_path)

    assert engine.search("from_a") != []
    assert engine.search("from_b") != []


def test_get_node_raises_on_unknown_id(engine, fixture_repo):
    engine.build(fixture_repo)
    with pytest.raises(ValueError):
        engine.execute("get_node", {"node_id": "does_not_exist"})


def test_execute_unknown_operation_raises(engine, fixture_repo):
    engine.build(fixture_repo)
    with pytest.raises(ValueError):
        engine.execute("not_a_real_operation", {})


def test_shortest_path_between_connected_nodes(engine, fixture_repo):
    engine.build(fixture_repo)
    stats_before = engine.execute("graph_stats", {})
    # main.py imports helper.py -- there should be a path between their file nodes.
    result = engine.execute("shortest_path", {"source": "main", "target": "helper"})
    assert "path" in result
    assert stats_before["nodes"] > 0


def test_update_with_no_changes_is_a_noop(engine, fixture_repo):
    engine.build(fixture_repo)
    stats_before = engine.execute("graph_stats", {})
    engine.update(fixture_repo, changed_files=[])
    stats_after = engine.execute("graph_stats", {})
    assert stats_before == stats_after


def test_update_reindexes_only_changed_file(engine, fixture_repo):
    engine.build(fixture_repo)

    (fixture_repo / "new_module.py").write_text(
        "def brand_new_function():\n    pass\n", encoding="utf-8"
    )
    engine.update(fixture_repo, changed_files=[fixture_repo / "new_module.py"])

    results = engine.search("brand_new_function")
    assert len(results) > 0
    # Original nodes are still present -- update merged rather than replaced.
    assert engine.search("greet") != []


def test_reload_from_disk_after_process_restart(tmp_path, fixture_repo):
    out_dir = tmp_path / "graphify-out"
    first_engine = GraphifyEngine(out_dir=out_dir)
    first_engine.build(fixture_repo)

    second_engine = GraphifyEngine(out_dir=out_dir)
    second_engine._root = fixture_repo  # simulates a fresh process re-attaching to a persisted graph
    stats = second_engine.execute("graph_stats", {})
    assert stats["nodes"] > 0
