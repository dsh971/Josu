"""Tests for GraphifyEngine (U7) -- against real .docx/.xlsx fixture files
generated on the fly via python-docx/openpyxl (graphifyy's own [office]
extra), mirroring this repo's "prefer a real fixture over mocking" testing
convention. No mocking of graphify's conversion functions.

Google Workspace fixtures (.gdoc/.gslides) are real JSON shortcut files;
since the external `gws` CLI is not installed in this test environment,
those tests exercise the "gws is required" degrade-cleanly path rather than
a real export.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from josu.graph.engine import GraphEngineUnavailableError
from josu.graph.graphify import GraphifyEngine, GraphifyUnavailableError, RECOGNIZED_EXTENSIONS


@pytest.fixture
def engine():
    return GraphifyEngine()


@pytest.fixture
def real_docx(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "notes.docx"
    document = docx.Document()
    document.add_paragraph("This is a real docx fixture for GraphifyEngine tests.")
    document.save(str(path))
    return path


@pytest.fixture
def real_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "budget.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "Quarter"
    sheet["B1"] = "Revenue"
    sheet["A2"] = "Q1"
    sheet["B2"] = 1000
    workbook.save(str(path))
    return path


@pytest.mark.asyncio
async def test_build_is_a_no_op(engine, tmp_path):
    assert await engine.build(tmp_path) is None


@pytest.mark.asyncio
async def test_update_is_a_no_op(engine, tmp_path):
    assert await engine.update(tmp_path, []) is None


@pytest.mark.asyncio
async def test_search_always_returns_empty(engine):
    assert await engine.search("anything") == []
    assert await engine.search("") == []


@pytest.mark.asyncio
async def test_execute_extracts_real_docx_content(engine, real_docx):
    result = await engine.execute("read_file", {"path": str(real_docx)})
    assert result["path"] == str(real_docx)
    assert "real docx fixture" in result["content"]


@pytest.mark.asyncio
async def test_execute_extracts_real_xlsx_content(engine, real_xlsx):
    result = await engine.execute("read_file", {"path": str(real_xlsx)})
    assert "Revenue" in result["content"] or "1000" in result["content"]


@pytest.mark.asyncio
async def test_execute_without_path_raises(engine):
    with pytest.raises(GraphifyUnavailableError) as exc_info:
        await engine.execute("read_file", {})
    assert exc_info.value.reason == "no-path"


@pytest.mark.asyncio
async def test_execute_with_unsupported_extension_raises(engine, tmp_path):
    path = tmp_path / "script.py"
    path.write_text("print('not a graphify format')")
    with pytest.raises(GraphifyUnavailableError) as exc_info:
        await engine.execute("read_file", {"path": str(path)})
    assert exc_info.value.reason == "unsupported-format"


@pytest.mark.asyncio
async def test_execute_on_gdoc_shortcut_without_gws_installed_degrades_cleanly(
    engine, tmp_path, monkeypatch
):
    """The `gws` CLI is not installed in this test environment -- confirms
    that condition surfaces as a clear GraphifyUnavailableError, not an
    unhandled RuntimeError or crash."""
    monkeypatch.setenv("PATH", "")  # ensure `gws` truly cannot be found
    shortcut = tmp_path / "plan.gdoc"
    shortcut.write_text(
        json.dumps({"doc_id": "abc123", "url": "https://docs.google.com/document/d/abc123"})
    )
    with pytest.raises(GraphifyUnavailableError) as exc_info:
        await engine.execute("read_file", {"path": str(shortcut)})
    assert exc_info.value.reason == "conversion-error"
    assert "gws" in str(exc_info.value)


def test_graphify_unavailable_error_is_a_graph_engine_unavailable_error():
    """Existing `except GraphEngineUnavailableError`/`except RuntimeError`
    call sites (graph/server.py, delegate/local_model.py) must keep working
    unmodified for this engine too."""
    assert issubclass(GraphifyUnavailableError, GraphEngineUnavailableError)
    assert issubclass(GraphifyUnavailableError, RuntimeError)


def test_recognized_extensions_matches_r8():
    assert RECOGNIZED_EXTENSIONS == {".xlsx", ".docx", ".gdoc", ".gsheet", ".gslides"}


@pytest.mark.asyncio
async def test_execute_rejects_content_over_the_size_cap(engine, real_docx, monkeypatch):
    """Mirrors GortexEngine's own response-size cap -- an oversized
    converted file must not be returned whole. The cap check reads the
    converted file's size on disk before ever reading its content into
    memory (not after), so this also proves the check runs pre-read."""
    monkeypatch.setattr("josu.graph.graphify.MAX_EXTRACTED_BYTES", 1)
    with pytest.raises(GraphifyUnavailableError) as exc_info:
        await engine.execute("read_file", {"path": str(real_docx)})
    assert exc_info.value.reason == "content-too-large"


@pytest.mark.asyncio
async def test_execute_on_gws_timeout_raises_graphify_unavailable(engine, tmp_path, monkeypatch):
    """A `gws` CLI call blocked on interactive re-authentication raises
    `subprocess.TimeoutExpired`, not `RuntimeError` -- must degrade to
    `GraphifyUnavailableError`, not escape as a raw unhandled exception."""
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gws", timeout=30.0)

    monkeypatch.setattr(
        "graphify.google_workspace.convert_google_workspace_file", _raise_timeout
    )
    shortcut = tmp_path / "plan.gdoc"
    shortcut.write_text(
        json.dumps({"doc_id": "abc123", "url": "https://docs.google.com/document/d/abc123"})
    )
    with pytest.raises(GraphifyUnavailableError) as exc_info:
        await engine.execute("read_file", {"path": str(shortcut)})
    assert exc_info.value.reason == "conversion-timeout"
