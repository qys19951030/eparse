# -*- coding: utf-8 -*-

"""
excel parser interfaces
"""

import importlib
import re
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from datetime import datetime
from pprint import PrettyPrinter
from typing import Dict, Optional
from uuid import uuid4

import pandas as pd
from peewee import (
    AutoField,
    CharField,
    DatabaseProxy,
    DateTimeField,
    IntegerField,
    Model,
    PostgresqlDatabase,
    SqliteDatabase,
    fn,
)
from playhouse.migrate import SchemaMigrator, migrate

from .core import html_to_serialized_data

DATABASE = DatabaseProxy()

# insert chunk size keeps individual statements bounded for large tables
INSERT_CHUNK = 1000


class ExcelParse(Model):
    """
    excel parse model
    """

    id = AutoField()
    row = IntegerField()
    column = IntegerField()
    value = CharField()
    type = CharField()
    c_header = CharField(index=True)
    r_header = CharField(index=True)
    excel_RC = CharField(index=True)
    name = CharField(index=True)
    sheet = CharField(index=True)
    f_name = CharField(index=True)
    # stable identity of the table this cell belongs to (nullable for rows
    # written through the legacy plain output() path)
    table_id = CharField(index=True, null=True)
    timestamp = DateTimeField(default=datetime.utcnow)

    @classmethod
    def get_queryset(cls, *args, **kwargs):
        """
        return queryset with filters applied
        """

        query = cls.filter(**kwargs)
        return pd.DataFrame(query.dicts())

    @classmethod
    def get_column(cls, column, *args, **kwargs):
        """
        return distinct values from column with aggregations
        """

        query = (
            cls.filter(**kwargs)
            .select(
                getattr(cls, column),
                fn.COUNT(cls.id).alias("Total Rows"),
                fn.COUNT(cls.type.distinct()).alias("Data Types"),
                fn.COUNT(cls.value.distinct()).alias("Distinct Values"),
            )
            .group_by(getattr(cls, column))
        )
        return pd.DataFrame(query.dicts())

    class Meta:
        database = DATABASE
        indexes = ((("f_name", "sheet", "name"), False),)


class ExcelTable(Model):
    """
    registry of imported tables keyed by their stable table id

    one row per known table identity; a header change yields a new id and
    therefore a new row, while re-importing unchanged content updates the
    same row in place.
    """

    table_id = CharField(primary_key=True)
    f_name = CharField(index=True)
    file_digest = CharField()
    sheet = CharField(index=True)
    anchor = CharField()
    headers = CharField()
    n_rows = IntegerField()
    n_cols = IntegerField()
    n_cells = IntegerField()
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        database = DATABASE
        table_name = "exceltable"


class BaseInterface:
    """
    base interface class
    """

    endpoint = ""
    user = ""
    password = ""
    host = ""
    port = 0
    name = ""

    Database = None
    Model = None

    def __init__(self, uri: str, Model: Optional[Model] = None):
        for k, v in self.parse_uri(uri).items():
            setattr(self, k, v)
        self.Model = Model

    @abstractmethod
    def input(self):
        """
        from_X override with input handler
        """

        pass

    @abstractmethod
    def output(self, data: pd.DataFrame, obj: Dict) -> pd.DataFrame:
        """
        to_X override with output handler
        """

        pass

    @abstractmethod
    def migrate(self, migration: str):
        """
        override with migration handler
        """

        pass

    @classmethod
    def parse_uri(self, uri: str) -> Dict:
        """
        parse eparse URI string
        """

        patt = r"^(?P<endpoint>.*)://(?P<user>.*?)(:(?P<password>.*?))?(@(?P<host>.*?)(:(?P<port>.*?))?)?/(?P<name>.*)?$"  # noqa
        return re.match(patt, uri).groupdict()


class NullInterface(BaseInterface):
    """
    null interface
    """

    def input(self):
        return pd.DataFrame()

    def output(self, *args, **kwargs):
        pass

    def migrate(self, *args, **kwargs):
        pass


class StdoutInterface(BaseInterface):
    """
    stdout interface
    """

    def input(self):
        return pd.DataFrame()

    def output(self, data, *args, **kwargs):
        PrettyPrinter().pprint(data)

    def migrate(self, *args, **kwargs):
        pass


class BaseDatabaseInterface(BaseInterface):
    """
    base database interface
    """

    @abstractmethod
    def initialize(self, *args, **kwargs):
        """
        override with db-specific initialization
        """

        pass

    def ensure_schema(self):
        """
        create tables and upgrade older databases in place

        safe to run repeatedly; new columns added to an existing table are
        detected through the database catalog rather than by version alone.
        """

        self.initialize(DATABASE)
        DATABASE.connect(reuse_if_open=True)
        DATABASE.create_tables([self.Model, ExcelTable])

        if not any(
            c.name == "table_id"
            for c in DATABASE.get_columns(self.Model._meta.table_name)
        ):
            migrator = SchemaMigrator.from_database(DATABASE.obj)
            # add the bare column first; the field's index would otherwise
            # collide with the index create_tables() already attempts
            migrate(migrator.add_column("excelparse", "table_id", CharField(null=True)))
            DATABASE.execute_sql(
                "CREATE INDEX IF NOT EXISTS excelparse_table_id "
                "ON excelparse (table_id)"
            )

    def assert_writable(self):
        """
        pre-flight probe: fail before processing anything when the target
        database cannot be created or written to
        """

        self.ensure_schema()
        with DATABASE.atomic():
            DATABASE.execute_sql("SELECT 1")

    def input(self, method, **kwargs):
        m = getattr(self.Model, method, None)

        # if no explicit method is available, try get_column
        if m is None:
            m = self.Model.get_column
            patt = r"^(?:get_)?(?P<column>.*)$"
            kwargs["column"] = re.match(patt, method).group("column")

        self.ensure_schema()

        return m(**kwargs)

    def output(self, data, *args, **kwargs):
        # skip empty data
        if hasattr(data, "empty") and data.empty:
            return
        elif not hasattr(data, "empty") and not data:
            return

        # check that data is serialized
        try:
            assert isinstance(data, Iterable)
            assert isinstance(data[0], Mapping)
        except Exception:
            raise ValueError("bad data - did you serialize it first?")

        self.ensure_schema()

        # insert data into Model
        with DATABASE.atomic():
            self.Model.insert_many(data).execute()

        # DATABASE.close()

    def upsert_table(
        self,
        table_id: str,
        serialized: list,
        meta: Dict,
    ) -> Dict:
        """
        idempotent whole-table replacement inside a single transaction

        an existing table with the same id is deleted and repopulated; a new
        id is inserted.  either every change commits or none does, leaving
        the previous content untouched on failure.

        meta carries: f_name, file_digest, sheet, anchor, headers,
        n_rows, n_cols.

        returns {"action": "inserted" | "updated", "rows": <cell count>}
        """

        if not serialized:
            raise ValueError("cannot upsert an empty table")

        self.ensure_schema()

        for record in serialized:
            record["table_id"] = table_id

        n_rows = int(meta["n_rows"])
        n_cols = int(meta["n_cols"])
        n_cells = len(serialized)
        now = datetime.utcnow()

        with DATABASE.atomic():
            existing = ExcelTable.get_or_none(ExcelTable.table_id == table_id)
            action = "updated" if existing is not None else "inserted"

            # replace every cell of this table
            self.Model.delete().where(self.Model.table_id == table_id).execute()
            for start in range(0, n_cells, INSERT_CHUNK):
                self.Model.insert_many(
                    serialized[start : start + INSERT_CHUNK]
                ).execute()

            if existing is None:
                ExcelTable.create(
                    table_id=table_id,
                    f_name=meta["f_name"],
                    file_digest=meta["file_digest"],
                    sheet=meta["sheet"],
                    anchor=meta["anchor"],
                    headers=meta["headers"],
                    n_rows=n_rows,
                    n_cols=n_cols,
                    n_cells=n_cells,
                    created_at=now,
                    updated_at=now,
                )
            else:
                ExcelTable.update(
                    f_name=meta["f_name"],
                    file_digest=meta["file_digest"],
                    sheet=meta["sheet"],
                    anchor=meta["anchor"],
                    headers=meta["headers"],
                    n_rows=n_rows,
                    n_cols=n_cols,
                    n_cells=n_cells,
                    updated_at=now,
                ).where(ExcelTable.table_id == table_id).execute()

        return {"action": action, "rows": n_cells}

    def migrate(self, migration):
        try:
            m = importlib.import_module("eparse.migrations")
            migration_fcn = getattr(m, migration)
        except AttributeError:
            msg = f"migration error - there is no {migration}"
            raise AttributeError(msg)

        self.initialize(DATABASE)
        DATABASE.connect()
        migration_fcn(self.Model)


class Sqlite3Interface(BaseDatabaseInterface):
    """
    sqlite3 interface
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.name:
            self.name = f".files/{uuid4()}.db"

    def initialize(self, db):
        db.initialize(
            SqliteDatabase(
                self.name,
                pragmas={
                    # readers see a committed snapshot and never block the
                    # single writer
                    "journal_mode": "wal",
                    # wait rather than error immediately on a busy database
                    "busy_timeout": 30000,
                    "foreign_keys": 1,
                },
            )
        )


class PostgresInterface(BaseDatabaseInterface):
    """
    postgres interface
    """

    def initialize(self, db):
        db.initialize(
            PostgresqlDatabase(
                self.name,
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
            )
        )


class HtmlInterface(Sqlite3Interface):
    """
    html data interface using sqlite3
    """

    def __init__(self, *args, html: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)

        if html is not None:
            self.output(html_to_serialized_data(html))


def i_factory(uri, Model=None, **kwargs):
    """
    return interface object based on uri
    """

    if uri.startswith("null"):
        return NullInterface(uri, **kwargs)
    elif uri.startswith("stdout"):
        return StdoutInterface(uri, **kwargs)
    elif uri.startswith("sqlite3"):
        return Sqlite3Interface(uri, Model, **kwargs)
    elif uri.startswith("postgres"):
        return PostgresInterface(uri, Model, **kwargs)
    elif uri.startswith("html"):
        _uri = uri.replace("html", "sqlite3")
        return HtmlInterface(_uri, Model, **kwargs)

    raise ValueError(f"{uri} is not a recognized endpoint")
