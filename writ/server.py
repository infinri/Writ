"""Writ HTTP API -- FastAPI service.

Per PY-ASYNC-001: all endpoints are async.
Per PERF-IO-001: no sync I/O in request handlers. Pipeline uses pre-warmed indexes.
Per PY-PYDANTIC-001: request/response bodies validated through Pydantic models.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from writ.graph.db import Neo4jConnection
from writ.retrieval.pipeline import RetrievalPipeline, build_pipeline

# Per ARCH-CONST-001
DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "writdevpass"


class QueryRequest(BaseModel):
    """Request body for /query endpoint."""

    query: str
    domain: str | None = None
    scope: str | None = None
    budget_tokens: int | None = None
    exclude_rule_ids: list[str] | None = None


class ConflictsRequest(BaseModel):
    """Request body for /conflicts endpoint."""

    rule_ids: list[str]


# Module-level state set during lifespan.
_pipeline: RetrievalPipeline | None = None
_db: Neo4jConnection | None = None
_startup_time: datetime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-warm all indexes at startup per PERF-IO-001."""
    global _pipeline, _db, _startup_time
    from sentence_transformers import SentenceTransformer

    _db = Neo4jConnection(DEFAULT_NEO4J_URI, DEFAULT_NEO4J_USER, DEFAULT_NEO4J_PASSWORD)
    model = SentenceTransformer("all-MiniLM-L6-v2")
    _pipeline = await build_pipeline(_db, embedding_model=model)
    _startup_time = datetime.now()
    yield
    if _db is not None:
        await _db.close()


app = FastAPI(
    title="Writ",
    description="Hybrid RAG knowledge retrieval service for AI coding rule enforcement.",
    lifespan=lifespan,
)


@app.post("/query")
async def query_rules(request: QueryRequest) -> dict[str, Any]:
    """Ranked list of matching domain rules. Mandatory rules excluded."""
    if _pipeline is None:
        return {"error": "Pipeline not initialized. Run writ serve."}
    result = _pipeline.query(
        query_text=request.query,
        domain=request.domain,
        budget_tokens=request.budget_tokens,
        exclude_rule_ids=request.exclude_rule_ids,
    )
    return result


@app.get("/rule/{rule_id}")
async def get_rule(rule_id: str, include_graph: bool = False) -> dict[str, Any]:
    """Full rule node. Optionally includes 1-hop graph context."""
    if _db is None:
        return {"error": "Database not connected."}
    rule = await _db.get_rule(rule_id)
    if rule is None:
        return {"error": f"Rule {rule_id} not found."}
    response: dict[str, Any] = {"rule": rule}
    if include_graph:
        neighbors = await _db.traverse_neighbors(rule_id, hops=1)
        response["graph_context"] = neighbors
    return response


@app.post("/conflicts")
async def check_conflicts(request: ConflictsRequest) -> dict[str, Any]:
    """CONFLICTS_WITH edges between provided rules."""
    if _db is None:
        return {"error": "Database not connected."}
    query = """
        MATCH (a:Rule)-[:CONFLICTS_WITH]-(b:Rule)
        WHERE a.rule_id IN $ids AND b.rule_id IN $ids
        AND a.rule_id < b.rule_id
        RETURN a.rule_id AS rule_a, b.rule_id AS rule_b
    """
    async with _db._driver.session(database=_db._database) as session:
        result = await session.run(query, ids=request.rule_ids)
        conflicts = [record.data() async for record in result]
    return {"conflicts": conflicts}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Service status, rule count, index state, last ingestion timestamp."""
    if _db is None:
        return {"status": "not_ready", "error": "Database not connected."}

    rule_count = await _db.count_rules()

    # Count mandatory rules.
    query = "MATCH (r:Rule) WHERE r.mandatory = true RETURN count(r) AS count"
    async with _db._driver.session(database=_db._database) as session:
        result = await session.run(query)
        record = await result.single()
        mandatory_count = record["count"]

    return {
        "status": "healthy",
        "rule_count": rule_count,
        "mandatory_count": mandatory_count,
        "index_state": "warm" if _pipeline is not None else "cold",
        "startup_time": _startup_time.isoformat() if _startup_time else None,
    }
