"""Tests for RoutingEngine (U6, U8) -- composing the primary graph engine
and a lazily-constructed GraphifyEngine behind the GraphEngine Protocol.
Uses tests/conftest.py's FakeGraphEngine as the primary engine fake;
graphify's own real extraction is covered separately in test_graphify.py,
so these tests only need to prove routing/lazy-construction, not extraction
correctness.
"""

from __future__ import annotations

import sys

import pytest

from josu.graph.engine import GraphEngineUnavailableError
from josu.graph.graphify import GraphifyEngine
from josu.graph.router import RoutingEngine
from tests.conftest import FakeGraphEngine


def _write_docx(path, text):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph(text)
    document.save(str(path))
    return path


@pytest.fixture
def primary():
    return FakeGraphEngine(
        search_results=[{"id": "helper.py::greet"}],
        execute_results={"analyze": {"ok": True}},
    )


@pytest.mark.asyncio
async def test_search_always_routes_to_primary_regardless_of_query_text(primary):
    router = RoutingEngine(primary)
    results = await router.search("budget.xlsx contents")
    assert results == [{"id": "helper.py::greet"}]


@pytest.mark.asyncio
async def test_execute_without_path_routes_to_primary(primary):
    router = RoutingEngine(primary)
    result = await router.execute("analyze", {"kind": "dead_code"})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_execute_with_non_graphify_path_routes_to_primary(primary):
    router = RoutingEngine(primary)
    result = await router.execute("analyze", {"path": "src/foo.py"})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_execute_with_graphify_eligible_path_routes_to_graphify(primary, tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "notes.docx"
    document = docx.Document()
    document.add_paragraph("routing test content")
    document.save(str(path))

    router = RoutingEngine(primary)
    result = await router.execute("read_file", {"path": str(path)})
    assert "routing test content" in result["content"]
    # Primary's canned execute_results has no "read_file" key -- if this had
    # wrongly routed to primary it would have raised ValueError instead.


@pytest.mark.asyncio
async def test_build_always_routes_to_primary(primary, tmp_path):
    router = RoutingEngine(primary)
    await router.build(tmp_path)
    assert primary.build_calls == [tmp_path]


@pytest.mark.asyncio
async def test_update_always_routes_to_primary(primary, tmp_path):
    router = RoutingEngine(primary)
    changed = [tmp_path / "a.xlsx", tmp_path / "b.py"]
    await router.update(tmp_path, changed)
    assert primary.update_calls == [(tmp_path, changed)]


@pytest.mark.asyncio
async def test_primary_unset_and_non_graphify_call_raises_unavailable():
    router = RoutingEngine(None)
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.search("anything")
    assert exc_info.value.reason == "unconfigured"


@pytest.mark.asyncio
async def test_primary_unset_but_graphify_eligible_path_still_routes_to_graphify(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "notes.docx"
    document = docx.Document()
    document.add_paragraph("still reachable without a primary engine")
    document.save(str(path))

    router = RoutingEngine(None)
    result = await router.execute("read_file", {"path": str(path)})
    assert "still reachable without a primary engine" in result["content"]


@pytest.mark.asyncio
async def test_graphify_lazily_constructed_only_on_first_eligible_call(primary, tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "notes.docx"
    document = docx.Document()
    document.add_paragraph("lazy construction test")
    document.save(str(path))

    router = RoutingEngine(primary)
    assert router._graphify is None
    await router.execute("read_file", {"path": str(path)})
    assert isinstance(router._graphify, GraphifyEngine)
    first_instance = router._graphify

    await router.execute("read_file", {"path": str(path)})
    assert router._graphify is first_instance  # not reconstructed


@pytest.mark.asyncio
async def test_graphify_not_installed_prints_instruction_and_degrades(
    primary, tmp_path, monkeypatch, capsys
):
    monkeypatch.setitem(sys.modules, "graphify", None)  # simulate ImportError

    path = tmp_path / "notes.docx"
    path.write_bytes(b"not a real docx, never read -- import fails first")

    router = RoutingEngine(primary)
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.execute("read_file", {"path": str(path)})
    assert exc_info.value.reason == "graphify-not-installed"

    captured = capsys.readouterr()
    assert "uv sync --extra graphify" in captured.out
    assert str(path) in captured.out


@pytest.mark.asyncio
async def test_graphify_not_installed_instruction_shown_only_once(
    primary, tmp_path, monkeypatch, capsys
):
    monkeypatch.setitem(sys.modules, "graphify", None)

    path_a = tmp_path / "a.docx"
    path_a.write_bytes(b"placeholder")
    path_b = tmp_path / "b.xlsx"
    path_b.write_bytes(b"placeholder")

    router = RoutingEngine(primary)
    with pytest.raises(GraphEngineUnavailableError):
        await router.execute("read_file", {"path": str(path_a)})
    with pytest.raises(GraphEngineUnavailableError):
        await router.execute("read_file", {"path": str(path_b)})

    captured = capsys.readouterr()
    assert captured.out.count("uv sync --extra graphify") == 1


# --- scope_root confinement ---------------------------------------------


@pytest.mark.asyncio
async def test_graphify_path_inside_scope_root_is_allowed(primary, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    path = _write_docx(repo_root / "notes.docx", "inside scope")

    router = RoutingEngine(primary, scope_root=repo_root)
    result = await router.execute("read_file", {"path": str(path)})
    assert "inside scope" in result["content"]


@pytest.mark.asyncio
async def test_graphify_path_outside_scope_root_is_refused(primary, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    path = _write_docx(outside_dir / "secret.docx", "outside scope")

    router = RoutingEngine(primary, scope_root=repo_root)
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.execute("read_file", {"path": str(path)})
    assert exc_info.value.reason == "out-of-scope"


@pytest.mark.asyncio
async def test_graphify_path_outside_scope_root_never_reaches_graphify(primary, tmp_path):
    """The out-of-scope rejection must happen before extraction is even
    attempted -- a path pointing at a non-office file outside scope should
    still be refused for being out-of-scope, not fail for some other
    reason, proving the scope check runs first."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    # Not a real .docx at all -- if scope confinement didn't run first,
    # this would fail during actual conversion instead.
    fake_path = outside_dir / "not_real.docx"
    fake_path.write_bytes(b"not a real docx file")

    router = RoutingEngine(primary, scope_root=repo_root)
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.execute("read_file", {"path": str(fake_path)})
    assert exc_info.value.reason == "out-of-scope"


@pytest.mark.asyncio
async def test_no_scope_root_means_no_confinement(primary, tmp_path):
    path = _write_docx(tmp_path / "anywhere.docx", "no scope configured")

    router = RoutingEngine(primary, scope_root=None)
    result = await router.execute("read_file", {"path": str(path)})
    assert "no scope configured" in result["content"]


@pytest.mark.asyncio
async def test_relative_path_inside_scope_resolves_against_scope_root_not_cwd(
    primary, tmp_path, monkeypatch
):
    """A relative `path` must resolve against `scope_root`, not the daemon
    process's own current working directory -- otherwise confinement (and
    which file actually gets read) would depend on where `josu daemon
    start` happened to be launched from rather than the declared scope.
    Sets CWD to somewhere unrelated to both the repo and the target file
    to prove `os.getcwd()` plays no part in resolution."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_docx(repo_root / "notes.docx", "relative path inside repo")

    unrelated_cwd = tmp_path / "somewhere-else"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    router = RoutingEngine(primary, scope_root=repo_root)
    result = await router.execute("read_file", {"path": "notes.docx"})
    assert "relative path inside repo" in result["content"]


@pytest.mark.asyncio
async def test_relative_path_escaping_scope_root_is_refused(primary, tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    _write_docx(outside_dir / "secret.docx", "outside scope via relative path")

    monkeypatch.chdir(tmp_path)

    router = RoutingEngine(primary, scope_root=repo_root)
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.execute("read_file", {"path": "../elsewhere/secret.docx"})
    assert exc_info.value.reason == "out-of-scope"


@pytest.mark.asyncio
async def test_relative_path_with_no_scope_root_resolves_against_cwd(
    primary, tmp_path, monkeypatch
):
    """With no `scope_root` configured (confinement opted out entirely),
    relative-path resolution falls back to ordinary CWD-relative behavior
    -- there's no repo root to anchor against."""
    _write_docx(tmp_path / "notes.docx", "no scope, relative path")
    monkeypatch.chdir(tmp_path)

    router = RoutingEngine(primary, scope_root=None)
    result = await router.execute("read_file", {"path": "notes.docx"})
    assert "no scope, relative path" in result["content"]


# --- non-string / non-dict params ----------------------------------------


@pytest.mark.asyncio
async def test_non_string_path_does_not_raise_and_routes_to_primary(primary):
    router = RoutingEngine(primary)
    result = await router.execute("analyze", {"path": ["a.docx", "b.docx"]})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_non_dict_params_does_not_raise_and_routes_to_primary(primary):
    router = RoutingEngine(primary)
    # A caller sending a non-dict params object -- should degrade to "no
    # graphify path" rather than raising AttributeError on params.get().
    result = await router.execute("analyze", "not-a-dict")
    assert result == {"ok": True}


# --- lazy re-resolution (primary_factory) ---------------------------------


@pytest.mark.asyncio
async def test_primary_factory_not_consulted_when_primary_already_set(primary):
    calls = []

    def factory():
        calls.append(1)
        return primary, None

    router = RoutingEngine(primary, primary_factory=factory)
    await router.search("anything")
    assert calls == []


@pytest.mark.asyncio
async def test_primary_factory_consulted_when_primary_unset(primary):
    calls = []

    def factory():
        calls.append(1)
        return primary, None

    router = RoutingEngine(None, primary_factory=factory)
    result = await router.search("anything")
    assert calls == [1]
    assert result == [{"id": "helper.py::greet"}]


@pytest.mark.asyncio
async def test_successful_reprobe_is_cached_for_later_calls(primary):
    calls = []

    def factory():
        calls.append(1)
        return primary, None

    router = RoutingEngine(None, primary_factory=factory)
    await router.search("first")
    await router.search("second")
    assert calls == [1]  # factory invoked once; second call reused the cached primary


@pytest.mark.asyncio
async def test_reprobe_is_rate_limited_between_failed_attempts():
    """Two calls in immediate succession (well within the real 30s
    interval) must only invoke the factory once. Manipulates the router's
    own `_last_reprobe_at` state rather than monkeypatching `time.monotonic`
    globally -- asyncio's own event loop relies on `time.monotonic()` for
    scheduling, so patching it process-wide corrupts unrelated internals."""
    calls = []

    def factory():
        calls.append(1)
        return None, "unreachable"

    router = RoutingEngine(None, primary_factory=factory)

    with pytest.raises(GraphEngineUnavailableError):
        await router.search("first")
    with pytest.raises(GraphEngineUnavailableError):
        await router.search("second")

    assert calls == [1]


@pytest.mark.asyncio
async def test_reprobe_fires_again_after_interval_elapses():
    calls = []

    def factory():
        calls.append(1)
        return None, "unreachable"

    router = RoutingEngine(None, primary_factory=factory)

    with pytest.raises(GraphEngineUnavailableError):
        await router.search("first")

    # Simulate the reprobe interval having elapsed by rewinding the
    # recorded timestamp directly, rather than sleeping in the test.
    router._last_reprobe_at -= RoutingEngine._REPROBE_INTERVAL_SECONDS

    with pytest.raises(GraphEngineUnavailableError):
        await router.search("second")

    assert calls == [1, 1]


# --- degrade-reason threading ---------------------------------------------


@pytest.mark.asyncio
async def test_primary_unavailable_reason_seeds_initial_error_detail():
    """The reason `daemon.py`'s eager startup resolution already knows
    (e.g. "unreachable") must reach the exception raised by the very
    first query -- not just after a lazy reprobe recomputes it."""
    router = RoutingEngine(None, primary_unavailable_reason="unreachable")
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.search("anything")
    assert exc_info.value.reason == "unreachable"
    assert "not reachable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_reprobe_failure_updates_the_surfaced_reason():
    """A degrade reason returned by a lazy reprobe must replace whatever
    reason was known before -- e.g. seeded as "unreachable" at startup,
    then found to be "version-incompatible" once actually reachable."""

    def factory():
        return None, "version-incompatible"

    router = RoutingEngine(
        None, primary_factory=factory, primary_unavailable_reason="unreachable"
    )
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.search("anything")
    assert exc_info.value.reason == "version-incompatible"
    assert "incompatible version" in str(exc_info.value)


@pytest.mark.asyncio
async def test_incapable_reason_surfaces_its_own_detail():
    router = RoutingEngine(None, primary_unavailable_reason="incapable")
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.search("anything")
    assert exc_info.value.reason == "incapable"
    assert "tool preset" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unrecognized_reason_falls_back_to_generic_detail():
    """A reason string not in the known table (defensive -- shouldn't
    happen with `daemon.py`'s current reasons, but the router must not
    crash on an unrecognized one) falls back to the original generic
    message rather than raising a KeyError."""
    router = RoutingEngine(None, primary_unavailable_reason="some-future-reason")
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.search("anything")
    assert exc_info.value.reason == "some-future-reason"
    assert "no graph-engine target is configured or reachable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_no_reason_supplied_defaults_to_unconfigured():
    router = RoutingEngine(None)
    with pytest.raises(GraphEngineUnavailableError) as exc_info:
        await router.search("anything")
    assert exc_info.value.reason == "unconfigured"
