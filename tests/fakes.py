"""Fake Supabase client for testing request-handler code without a live project.

Mimics only the chainable surface our code actually calls: table().select().eq().order().limit().execute(),
table().insert().execute(), table().update().eq().execute(), rpc().execute(), and storage.from_().upload()/.download().
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

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = [r for r in self._table.rows if all(r.get(k) == v for k, v in self._filters.items())]
        if self._order:
            column, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(column), reverse=desc)
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

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def execute(self):
        updated = [r for r in self._table.rows if all(r.get(k) == v for k, v in self._filters.items())]
        for row in updated:
            row.update(self._values)
        return FakeResult(updated)


class FakeTable:
    def __init__(self, name):
        self.name = name
        self.rows: list = []

    def select(self, *_args, **_kwargs):
        return FakeQuery(self)

    def insert(self, row):
        return FakeInsert(self, row)

    def update(self, values):
        return FakeUpdate(self, values)


class FakeStorageBucket:
    def __init__(self):
        self.uploads: list = []
        self._objects: dict = {}

    def upload(self, path, contents, opts=None):
        self.uploads.append((path, contents, opts))
        self._objects[path] = contents
        return {"path": path}

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

    def table(self, name):
        return self._tables.setdefault(name, FakeTable(name))

    def rpc(self, _name, *_args, **_kwargs):
        return FakeRpc(self.org_id)
