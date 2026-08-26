from __future__ import annotations


class _QueryRunnerMixin:
    """Shared open-session / run-one-query / materialize helper for the db store mixins.

    Mirrors writ/graph/integrity/_query.py::_QueryMixin. Reads self._driver /
    self._database (set by Neo4jConnection.__init__) via the MRO.
    """

    async def _run(self, query: str, **params) -> list:
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, **params)
            return [record async for record in result]

    async def _run_single(self, query: str, **params):
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, **params)
            return await result.single()

    async def _write_single(self, query: str, **params):
        """`_run_single` for a WRITE, through the driver's retrying API.

        `session.run` is auto-commit: a transient error (a lock conflict, a
        leader switch) surfaces immediately to the caller, and the decision-memory
        routes catch-and-log, so the record was simply lost. `execute_write`
        applies the driver's own managed retry with its own backoff, which is
        what node_store/edge_store already use for the bulk ingest. Nothing is
        swallowed here: an exhausted retry still raises, so the route's fail-open
        catch stays the last resort rather than the first line of defence.
        """
        async def _work(tx):
            result = await tx.run(query, **params)
            return await result.single()

        async with self._driver.session(database=self._database) as session:
            return await session.execute_write(_work)
