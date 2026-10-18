"""Data lineage with durable, monotonic taint (Phase 2; AD-25, G-02).

Nodes and edges are stored in the shared database, and session taint is the
append-only ``task_reads`` table. Taint is union-only: no code path removes a
read, so a restart or a second gateway process can only *preserve* or add
restriction — never drop it.

**Conservative-mode tradeoff (documented per DECISIONS §4).** The gateway
cannot observe what an LLM does between tool calls, so in conservative mode
every outbound payload is treated as derived from declared inputs UNION
everything read during this task session. This can over-block; that is
disclosed in reports rather than hidden. Declared mode is available for
comparison.
"""

from __future__ import annotations

import json

import networkx as nx

from taskfence.persistence import Database, coerce_database, now_iso
from taskfence.registry import base_labels


class LineageTracker:
    def __init__(self, db: Database | str | None = None,
                 mode: str = "conservative", db_path=None):
        self.db = coerce_database(db if db is not None else db_path)
        self.mode = mode
        self._graph = nx.DiGraph()
        self._load_graph()

    def close(self) -> None:
        """Legacy lifecycle hook: connections are per operation now."""
        return None

    # -- persistence --------------------------------------------------------
    def _load_graph(self) -> None:
        for row in self.db.query(
                "SELECT task_id, node, labels FROM lineage_nodes"):
            self._graph.add_node((row["task_id"], row["node"]),
                                 labels=set(json.loads(row["labels"])))
        for row in self.db.query(
                "SELECT task_id, src, dst, transformation FROM lineage_edges"):
            self._graph.add_edge((row["task_id"], row["src"]),
                                 (row["task_id"], row["dst"]),
                                 transformation=row["transformation"])

    # -- mutation API -------------------------------------------------------
    def register_asset(self, task_id: str, asset_id: str) -> None:
        """Register a source node AND its durable taint in one transaction."""
        labels = set(base_labels(asset_id))
        merged = self._node_labels(task_id, asset_id) | labels
        stamp = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                self.db._sql(
                    "INSERT INTO lineage_nodes (task_id, node, labels)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(task_id, node) DO UPDATE SET"
                    " labels=excluded.labels"),
                (task_id, asset_id, json.dumps(sorted(merged))))
            conn.execute(
                self.db._sql(
                    "INSERT INTO task_reads (task_id, asset_id, first_seen_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(task_id, asset_id) DO NOTHING"),
                (task_id, asset_id, stamp))
        self._graph.add_node((task_id, asset_id), labels=merged)

    def derive(self, task_id: str, input_ids: list[str], transformation: str,
               output_id: str) -> None:
        """Record a transformation: output node + edges from each input.

        The output keeps no own labels (Phase 1 semantics): restriction is
        inherited through the edges, so label propagation stays exactly as
        the existing scenario tests pin it.
        """
        for input_id in input_ids:
            self._add_node(task_id, input_id,
                           self._node_labels(task_id, input_id))
            self._add_edge(task_id, input_id, output_id, transformation)
        self._add_node(task_id, output_id, set())

    def record_sink(self, task_id: str, node_id: str, destination: str,
                    transformation: str = "send") -> None:
        self._add_node(task_id, node_id, self._node_labels(task_id, node_id))
        self._add_node(task_id, destination, set())
        self._add_edge(task_id, node_id, destination, transformation)

    def _add_node(self, task_id: str, node: str, labels: set[str]) -> None:
        merged = self._node_labels(task_id, node) | set(labels)
        self.db.execute(
            "INSERT INTO lineage_nodes (task_id, node, labels)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(task_id, node) DO UPDATE SET labels=excluded.labels",
            (task_id, node, json.dumps(sorted(merged))))
        self._graph.add_node((task_id, node), labels=merged)

    def _add_edge(self, task_id: str, src: str, dst: str,
                  transformation: str) -> None:
        for endpoint in (src, dst):
            key = (task_id, endpoint)
            if key not in self._graph:
                self._graph.add_node(key, labels=set())
        self.db.execute(
            "INSERT INTO lineage_edges (task_id, src, dst, transformation)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(task_id, src, dst) DO UPDATE SET"
            " transformation=excluded.transformation",
            (task_id, src, dst, transformation))
        self._graph.add_edge((task_id, src), (task_id, dst),
                             transformation=transformation)

    # -- queries ------------------------------------------------------------
    def session_asset_ids(self, task_id: str) -> set[str]:
        """Durable taint for this task, visible to every process."""
        rows = self.db.query(
            "SELECT asset_id FROM task_reads WHERE task_id = ?", (task_id,))
        return {row["asset_id"] for row in rows}

    def _node_labels(self, task_id: str, node_id: str) -> set[str]:
        key = (task_id, node_id)
        if key in self._graph:
            return set(self._graph.nodes[key].get("labels", set()))
        return set()

    def ancestors(self, task_id: str, node_id: str) -> set[str]:
        key = (task_id, node_id)
        if key not in self._graph:
            return set()
        return {name for _, name in nx.ancestors(self._graph, key)}

    def effective_labels(self, task_id: str, node_id: str,
                         include_session: bool | None = None) -> set[str]:
        include = self.mode == "conservative" if include_session is None \
            else include_session
        labels: set[str] = set(self._node_labels(task_id, node_id))
        for ancestor in self.ancestors(task_id, node_id):
            labels |= self._node_labels(task_id, ancestor)
        if include:
            for asset_id in self.session_asset_ids(task_id):
                labels |= set(base_labels(asset_id))
        return labels

    def path_to(self, task_id: str, node_id: str) -> list[str]:
        key = (task_id, node_id)
        if key not in self._graph:
            return [node_id]
        sources = [n for n in self._graph.nodes
                   if n[0] == task_id and self._graph.in_degree(n) == 0]
        if not sources:
            return [node_id]
        for source in sources:
            try:
                path = nx.shortest_path(self._graph, source, key)
                return [name for _, name in path]
            except nx.NetworkXNoPath:
                continue
        return [node_id]
