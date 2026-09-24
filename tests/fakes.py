"""Fake Supabase client for testing request-handler code without a live project.

Mimics only the chainable surface our code actually calls: table().select().eq().in_().order().limit().range().execute(),
table().insert().execute(), table().update().eq().in_().execute(), table().delete().eq().in_().execute(), rpc().execute(), and storage.from_().upload()/.download().
"""

import itertools
import uuid

_row_sequence = itertools.count()


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table):
        self._table = table
        self._filters: dict = {}
        self._order: tuple | None = None
        self._in: dict = {}
        self._range: tuple | None = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def in_(self, key, values):
        self._in[key] = set(values)
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, _n):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = [r for r in self._table.rows if all(r.get(k) == v for k, v in self._filters.items())]
        rows = [r for r in rows if all(r.get(k) in vs for k, vs in self._in.items())]
        if self._order:
            column, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(column), reverse=desc)
        if self._range:
            start, end = self._range
            rows = rows[start : end + 1]
        return FakeResult(rows)


class FakeInsert:
    def __init__(self, table, row_or_rows):
        self._table = table
        self._rows = row_or_rows if isinstance(row_or_rows, list) else [row_or_rows]

    def execute(self):
        inserted = []
        for row in self._rows:
            row = dict(row)
            row.setdefault("id", str(uuid.uuid4()))
            # A monotonic stand-in for created_at/updated_at so .order() has something to sort by.
            row.setdefault("created_at", next(_row_sequence))
            row.setdefault("updated_at", row["created_at"])
            self._table.rows.append(row)
            inserted.append(row)
        return FakeResult(inserted)


class FakeUpdate:
    def __init__(self, table, values):
        self._table = table
        self._values = values
        self._filters: dict = {}
        self._in: dict = {}

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def in_(self, key, values):
        self._in[key] = set(values)
        return self

    def execute(self):
        updated = [
            r
            for r in self._table.rows
            if all(r.get(k) == v for k, v in self._filters.items()) and all(r.get(k) in vs for k, vs in self._in.items())
        ]
        for row in updated:
            row.update(self._values)
        return FakeResult(updated)


class FakeDelete:
    """Deletes matching rows, then follows `FakeClient.cascades` like `on delete cascade`."""

    def __init__(self, table, client):
        self._table = table
        self._client = client
        self._filters: dict = {}
        self._in: dict = {}

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def in_(self, key, values):
        self._in[key] = set(values)
        return self

    def execute(self):
        def matches(r):
            return all(r.get(k) == v for k, v in self._filters.items()) and all(
                r.get(k) in vs for k, vs in self._in.items()
            )

        deleted = [r for r in self._table.rows if matches(r)]
        self._table.rows[:] = [r for r in self._table.rows if not matches(r)]
        deleted_ids = {r.get("id") for r in deleted}
        for child_table, fk_column in (self._client.cascades.get(self._table.name, []) if self._client else []):
            if deleted_ids:
                FakeDelete(self._client.table(child_table), self._client).in_(fk_column, deleted_ids).execute()
        return FakeResult(deleted)


class FakeTable:
    def __init__(self, name, client=None):
        self.name = name
        self.rows: list = []
        self._client = client

    def select(self, *_args, **_kwargs):
        return FakeQuery(self)

    def insert(self, row):
        return FakeInsert(self, row)

    def update(self, values):
        return FakeUpdate(self, values)

    def delete(self):
        return FakeDelete(self, self._client)


class FakeStorageBucket:
    def __init__(self):
        self.uploads: list = []
        self._objects: dict = {}

    def upload(self, path, contents, opts=None):
        self.uploads.append((path, contents, opts))
        self._objects[path] = contents
        return {"path": path}

    def remove(self, paths):
        for path in paths:
            self._objects.pop(path, None)
        return [{"name": path} for path in paths]

    def download(self, path):
        if path not in self._objects:
            raise Exception(f"object not found: {path}")
        return self._objects[path]


class FakeStorage:
    def __init__(self):
        self._buckets: dict = {}

    def from_(self, bucket):
        return self._buckets.setdefault(bucket, FakeStorageBucket())


class FakeRpc:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return FakeResult(self._data)


class FakeClient:
    def __init__(self, org_id="org-123"):
        self.org_id = org_id
        self._tables: dict = {}
        self.storage = FakeStorage()
        # parent table -> [(child table, fk column)], mirroring `on delete cascade` FKs.
        self.cascades: dict = {}

    def table(self, name):
        if name not in self._tables:
            self._tables[name] = FakeTable(name, self)
        return self._tables[name]

    def rpc(self, _name, *_args, **_kwargs):
        return FakeRpc(self.org_id)
