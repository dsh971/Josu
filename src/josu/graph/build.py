"""Graphify-backed implementation of the GraphEngine interface (R10).

Scoped to AST/structural extraction only for v1 (see plan Key Technical
Decisions) -- graphify's semantic extraction path requires an LLM in the
loop (a Gemini key, or Claude Code subagent dispatch), which would make
every incremental re-index (R14) spend hosted tokens. AST extraction is
deterministic and free, which is what keeps re-indexing cheap.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
from graphify.build import build_from_json, build_merge
from graphify.detect import detect_incremental, save_manifest
from graphify.extract import collect_files, extract

_MANIFEST_NAME = "manifest.json"


class GraphifyEngine:
    """Directory-scoped, AST-only context graph backed by graphify."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._graph: nx.Graph | None = None
        self._root: Path | None = None

    @property
    def graph_path(self) -> Path:
        return self.out_dir / "graph.json"

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / _MANIFEST_NAME

    def build(self, root: Path) -> None:
        """Full AST extraction over the directory tree (R9)."""
        root = root.resolve()
        files = collect_files(root)
        extraction = extract(files, cache_root=root, root=root)
        self._graph = build_from_json(extraction, root=root)
        self._root = root
        self._persist()
        save_manifest(
            {"code": [str(f) for f in files]},
            manifest_path=str(self.manifest_path),
            kind="both",
            root=root,
        )

    def update(self, root: Path, changed_files: list[Path]) -> None:
        """Incremental re-index scoped to changed files (R14).

        `changed_files` is accepted for interface clarity but the actual
        change set is recomputed from graphify's own manifest, since that's
        the source of truth `detect_incremental` and `build_merge` share --
        passing a caller-supplied list that disagrees with the manifest
        would desync the two.
        """
        root = root.resolve()
        incremental = detect_incremental(root, manifest_path=str(self.manifest_path))
        new_total = incremental.get("new_total", 0)
        if new_total == 0:
            return

        new_files_by_type = incremental.get("new_files", {})
        code_files = [Path(f) for files in new_files_by_type.values() for f in files]
        if not code_files:
            return

        extraction = extract(code_files, cache_root=root, root=root)
        self._graph = build_merge(
            [extraction],
            graph_path=str(self.graph_path) if self.graph_path.exists() else None,
            prune_sources=list(incremental.get("deleted_files", [])) or None,
            root=root,
        )
        self._root = root
        self._persist()
        save_manifest(
            incremental["files"],
            manifest_path=str(self.manifest_path),
            kind="both",
            root=root,
        )

    def search(self, query: str, limit: int = 10) -> list[dict]:
        graph = self._require_graph()
        query_lower = query.lower()
        matches = [
            {"id": node_id, **data}
            for node_id, data in graph.nodes(data=True)
            if query_lower in str(data.get("label", "")).lower() or query_lower in node_id.lower()
        ][:limit]
        results = []
        for match in matches:
            neighbors = list(graph.neighbors(match["id"]))
            results.append({**match, "neighbors": neighbors})
        return results

    def execute(self, operation: str, params: dict) -> dict:
        graph = self._require_graph()
        if operation == "get_node":
            node_id = params["node_id"]
            if node_id not in graph:
                raise ValueError(f"unknown node: {node_id}")
            return {"id": node_id, **graph.nodes[node_id]}
        if operation == "get_neighbors":
            node_id = params["node_id"]
            if node_id not in graph:
                raise ValueError(f"unknown node: {node_id}")
            return {"id": node_id, "neighbors": list(graph.neighbors(node_id))}
        if operation == "shortest_path":
            source, target = params["source"], params["target"]
            try:
                path = nx.shortest_path(graph, source, target)
            except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
                raise ValueError(str(exc)) from exc
            return {"path": path}
        if operation == "graph_stats":
            return {
                "nodes": graph.number_of_nodes(),
                "edges": graph.number_of_edges(),
            }
        raise ValueError(f"unknown operation: {operation}")

    def _require_graph(self) -> nx.Graph:
        if self._graph is None:
            if self.graph_path.exists():
                import json

                extraction = json.loads(self.graph_path.read_text(encoding="utf-8"))
                self._graph = build_from_json(extraction, root=self._root)
            else:
                raise RuntimeError("graph has not been built yet -- call build() first")
        return self._graph

    def _persist(self) -> None:
        assert self._graph is not None
        data = {
            "nodes": [{"id": n, **d} for n, d in self._graph.nodes(data=True)],
            "edges": [
                {"source": u, "target": v, **d}
                for u, v, d in self._graph.edges(data=True)
            ],
        }
        import json

        self.graph_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
