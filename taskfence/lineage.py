"""Data lineage with taint (spec §10, §13; DECISIONS §4; kit Prompt B3).

NetworkX DiGraph mirrored to SQLite (lineage_nodes / lineage_edges) so the
Streamlit dashboard — a separate process — can render flows.

**Conservative-mode tradeoff (documented per DECISIONS §4).** The gateway
cannot observe what an LLM does between tool calls, so in conservative mode
every outbound payload is treated as derived from `declared_inputs UNION
everything read during this task session`. This can OVER-BLOCK: a legitimate
message after the agent read unrelated sensitive files is tainted by them.
We accept this for the hackathon MVP and must disclose any resulting false
positive in reports/results.md rather than hide it. Declared mode (only
explicitly declared inputs taint) is available for comparison.
"""

import sqlite3
from pathlib import Path

import networkx as nx

from taskfence.registry import base_labels

DEFAULT_DB_PATH = Path("taskfence.sqlite3")


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class LineageTracker:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH,
                 mode: str = "conservative"):
        self.conn = _connect(db_path)
        self.mode = mode
        self._ensure_schema()
        self._graph = nx.DiGraph()
        self._load_graph()
        self.session_reads: set[str] = set()

    # -- schema / persistence -------------------------------------------------
    def _ensure_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS lineage_nodes ("
                " task_id TEXT NOT NULL, node TEXT NOT NULL,"
                " labels TEXT NOT NULL,"
                " PRIMARY KEY (task_id, node))")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS lineage_edges ("
                " task_id TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,"
                " transformation TEXT NOT NULL,"
                " PRIMARY KEY (task_id, src, dst))")

    def _load_graph(self) -> None:
        for task_id, node, labels in self.conn.execute(
                "SELECT task_id, node, labels FROM lineage_nodes"):
            self._graph.add_node((task_id, node), labels=set(json_loads(labels)))
        for task_id, src, dst, transformation in self.conn.execute(
                "SELECT task_id, src, dst, transformation FROM lineage_edges"):
            self._graph.add_edge((task_id, src), (task_id, dst),
                                 transformation=transformation)

    # -- mutation API -----------------------------------------------------------
    def register_asset(self, task_id: str, asset_id: str) -> None:
        """Register a source asset node with its registry labels; record the
        session read for conservative taint."""
        labels = set(base_labels(asset_id))
        self._add_node(task_id, asset_id, labels)
        self.session_reads.add(asset_id)

    def derive(self, task_id: str, input_ids: list[str], transformation: str,
               output_id: str) -> None:
        """Record a transformation: output node + edges from each input."""
        out_labels: set[str] = set()
        for input_id in input_ids:
            self._add_node(task_id, input_id,
                           self._node_labels(task_id, input_id))
            self._add_edge(task_id, input_id, output_id, transformation)
        self._add_node(task_id, output_id, out_labels)

    def record_sink(self, task_id: str, node_id: str, destination: str,
                    transformation: str = "send") -> None:
        """Record that data from `node_id` reached `destination`: wires the
        edge node -> destination so path_to() shows the full chain."""
        self._add_node(task_id, node_id, self._node_labels(task_id, node_id))
        self._add_node(task_id, destination, set())
        self._add_edge(task_id, node_id, destination, transformation)

    def _add_node(self, task_id: str, node: str, labels: set[str]) -> None:
        key = (task_id, node)
        # add_edge() auto-creates nodes without attributes, so use .get().
        merged = self._graph.nodes[key].get("labels", set()) \
            if key in self._graph else set()
        merged = merged | set(labels)
        self._graph.add_node(key, labels=merged)
        with self.conn:
            self.conn.execute(
                "INSERT INTO lineage_nodes (task_id, node, labels)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(task_id, node) DO UPDATE SET labels=excluded.labels",
                (task_id, node, json_dumps(merged)))

    def _add_edge(self, task_id: str, src: str, dst: str,
                  transformation: str) -> None:
        self._graph.add_edge((task_id, src), (task_id, dst),
                             transformation=transformation)
        with self.conn:
            self.conn.execute(
                "INSERT INTO lineage_edges (task_id, src, dst, transformation)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(task_id, src, dst) DO UPDATE SET"
                " transformation=excluded.transformation",
                (task_id, src, dst, transformation))

    # -- queries -----------------------------------------------------------------
    def _node_labels(self, task_id: str, node_id: str) -> set[str]:
        key = (task_id, node_id)
        if key in self._graph:
            return set(self._graph.nodes[key]["labels"])
        return set()

    def ancestors(self, task_id: str, node_id: str) -> set[str]:
        key = (task_id, node_id)
        if key not in self._graph:
            return set()
        return {name for _, name in nx.ancestors(self._graph, key)}

    def effective_labels(self, task_id: str, node_id: str,
                         include_session: bool | None = None) -> set[str]:
        """Node labels UNION all ancestors' labels; conservative mode unions
        everything read this session (DECISIONS §4)."""
        include = self.mode == "conservative" if include_session is None \
            else include_session
        labels: set[str] = set(self._node_labels(task_id, node_id))
        for ancestor in self.ancestors(task_id, node_id):
            labels |= self._node_labels(task_id, ancestor)
        if include:
            for read in self.session_reads:
                labels |= set(base_labels(read))
        return labels

    def path_to(self, task_id: str, node_id: str) -> list[str]:
        """Readable chain source -> ... -> node (first shortest path)."""
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

    def close(self) -> None:
        self.conn.close()


# Small JSON helpers kept module-level so _load_graph stays readable.
def json_dumps(value) -> str:
    import json
    return json.dumps(sorted(value))


def json_loads(value: str) -> set:
    import json
    return set(json.loads(value))
