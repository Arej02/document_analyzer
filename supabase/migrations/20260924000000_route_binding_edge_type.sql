-- design.md §2, §5: LaravelExtractor emits route -> controller-method bindings
-- (a route doesn't "call" its controller in source text; the framework's
-- router invokes it by convention, the same shape `hook_binding` already
-- covers for WordPress hooks). `entity_type.route` already existed in the
-- original enum (20260918000000) but nothing populated it until now.
--
-- `ALTER TYPE ... ADD VALUE` can't run inside the same transaction as a
-- statement that uses the new value, but it's fine as the only statement in
-- its own migration (each migration file is its own transaction).
alter type edge_type add value 'route_binding';
