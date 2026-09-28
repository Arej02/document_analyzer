-- design.md §2, §5a: GraphifyExtractor maps Graphify's `inherits` / `extends` /
-- `implements` relations onto one `inherits` edge type (class -> base class or
-- interface). Agent 2 ignores it (agent2/worker/graph.py GRAPH_EDGE_TYPES), so
-- planning is unchanged; the Q&A context engine follows it.
--
-- Same rule as 20260924000000_route_binding_edge_type.sql: `ALTER TYPE ... ADD
-- VALUE` is the only statement in its own migration.
alter type edge_type add value 'inherits';
