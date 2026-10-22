from __future__ import annotations

import dataclasses
import math
import sqlite3
from decimal import Decimal

import pytest

from sqltemplatelib import (
    CompiledSql,
    DbApiRenderer,
    FlatTemplate,
    Ident,
    Lit,
    Param,
    compile,
    ident,
    is_sql_ident,
    is_sql_lit,
    lit,
    param,
    render_parts,
    template,
)


@dataclasses.dataclass
class ForeignIdent:
    """An identifier object from a library that does not import sqltemplatelib."""

    __sql_ident__ = True

    name: object


@dataclasses.dataclass
class ForeignLit:
    """A literal object from a library that does not import sqltemplatelib."""

    __sql_lit__ = True

    value: object


class TestIdent:
    @pytest.mark.parametrize("name", ["", "a\x00b"])
    def test_illegal_names_raise(self, name):
        with pytest.raises(ValueError, match="Illegal identifier"):
            Ident(name)

    def test_non_str_name_raises(self):
        with pytest.raises(TypeError, match="must be a str"):
            Ident(5)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("renderer", "sql"),
        [
            ("sqlite3", 'SELECT * FROM "user data"'),
            ("duckdb", 'SELECT * FROM "user data"'),
            ("psycopg", 'SELECT * FROM "user data"'),
            ("oracledb", 'SELECT * FROM "user data"'),
            ("pymysql", "SELECT * FROM `user data`"),
            ("qmark", 'SELECT * FROM "user data"'),
        ],
    )
    def test_quoted_per_renderer(self, renderer, sql):
        assert compile(["SELECT * FROM ", ident("user data")], renderer).sql == sql

    def test_embedded_double_quote_is_doubled(self):
        assert compile(ident('a"b')).sql == '"a""b"'

    def test_embedded_backtick_is_doubled_for_mysql(self):
        assert compile(ident("a`b"), "pymysql").sql == "`a``b`"

    def test_percent_escaped_for_percent_styles(self):
        assert compile(ident("100%"), "psycopg").sql == '"100%%"'

    def test_qualified_name_is_separate_parts(self):
        assert compile([ident("main"), ".", ident("t")]).sql == '"main"."t"'

    def test_foreign_ident_object(self):
        assert is_sql_ident(ForeignIdent("t"))
        assert compile(ForeignIdent("t")).sql == '"t"'

    def test_foreign_ident_with_non_str_name_raises(self):
        with pytest.raises(TypeError, match=r"ForeignIdent\.name must be a str"):
            compile(ForeignIdent(5))

    def test_str_subclass_cannot_change_quoted_text(self):
        class Sneaky(str):
            __slots__ = ()

            def replace(self, *args, **kwargs):
                return self

        assert compile(ident(Sneaky('a"b'))).sql == '"a""b"'


class TestLit:
    @pytest.mark.parametrize(
        ("value", "sql"),
        [
            (None, "NULL"),
            (True, "TRUE"),
            (False, "FALSE"),
            (42, "42"),
            (-42, "(-42)"),
            (1.5, "1.5"),
            (-1.5, "(-1.5)"),
            (Decimal("1.10"), "1.10"),
            (Decimal("-1.10"), "(-1.10)"),
            ("it's", "'it''s'"),
            ("", "''"),
        ],
    )
    def test_rendered(self, value, sql):
        assert compile(lit(value)).sql == sql

    def test_negative_number_cannot_start_a_comment(self):
        assert compile(["SELECT 1 -", lit(-1)]).sql == "SELECT 1 -(-1)"

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, Decimal("NaN"), Decimal("Infinity")])
    def test_non_finite_numbers_raise(self, value):
        with pytest.raises(ValueError, match="Use a Param instead"):
            compile(lit(value))

    @pytest.mark.parametrize("value", [b"abc", [1], object()])
    def test_unsupported_types_raise(self, value):
        with pytest.raises(TypeError, match=r"Cannot write .* as a SQL literal"):
            compile(lit(value))

    def test_nul_in_str_raises(self):
        with pytest.raises(ValueError, match="NUL"):
            compile(lit("a\x00b"))

    @pytest.mark.parametrize(
        ("renderer", "sql"),
        [
            ("sqlite3", r"'a\b'"),
            ("duckdb", r"'a\b'"),
            ("psycopg", r"E'a\\b'"),
            ("pymysql", r"'a\\b'"),
        ],
    )
    def test_backslash_per_renderer(self, renderer, sql):
        assert compile(lit("a\\b"), renderer).sql == sql

    def test_postgres_without_backslash_is_plain(self):
        assert compile(lit("it's"), "psycopg").sql == "'it''s'"

    @pytest.mark.parametrize("renderer", ["psycopg", "pymysql"])
    def test_backslash_quote_cannot_escape_the_literal(self, renderer):
        # With backslash escapes on, \' must not end the string early.
        assert compile(lit("\\'"), renderer).sql.endswith("\\\\'''")

    def test_percent_escaped_for_percent_styles(self):
        assert compile(lit("100%"), "psycopg").sql == "'100%%'"

    def test_int_subclass_cannot_change_text(self):
        class Sneaky(int):
            def __repr__(self):
                return "1; DROP TABLE t"

            __str__ = __repr__

        assert compile(lit(Sneaky(1))).sql == "1"

    def test_foreign_lit_object(self):
        assert is_sql_lit(ForeignLit(1))
        assert compile(ForeignLit("x")).sql == "'x'"


class TestRenderPartsHooks:
    def test_custom_quote_ident_and_render_lit(self):
        compiled = render_parts(
            [ident("t"), " ", lit(1)],
            placeholder=lambda *_: "?",
            named=False,
            quote_ident=lambda name: f"[{name}]",
            render_lit=lambda value: f"<{value}>",
        )
        assert compiled == CompiledSql("[t] <1>", ())

    def test_unknown_part_raises(self):
        with pytest.raises(TypeError, match="Cannot render int"):
            render_parts([1], placeholder=lambda *_: "?", named=False)  # type: ignore[list-item]

    def test_unknown_quoting_raises(self):
        with pytest.raises(ValueError, match="Unknown quoting"):
            DbApiRenderer("qmark", quoting="tsql")  # type: ignore[arg-type]


class TestFlatTemplate:
    def test_merges_adjacent_text_and_drops_empty_text(self):
        flat = FlatTemplate(["", "SELECT ", "1", "", param(1), "", " + ", "2", ""])
        assert list(flat) == ["SELECT 1", Param(1), " + 2"]

    def test_equality(self):
        assert FlatTemplate(["a", "b"]) == FlatTemplate(["ab"])
        assert FlatTemplate(["a"]) != ["a"]

    def test_slicing_gives_a_flat_template(self):
        flat = FlatTemplate(["a", param(1), "b"])
        assert flat[1:] == FlatTemplate([param(1), "b"])
        assert flat[0] == "a"
        assert len(flat) == 3

    def test_rejects_other_parts(self):
        with pytest.raises(TypeError, match="Unexpected part type in FlatTemplate"):
            FlatTemplate([5])  # type: ignore[list-item]

    def test_str_raises(self):
        with pytest.raises(NotImplementedError):
            str(FlatTemplate(["a"]))

    def test_expand_returns_flat_template(self):
        t = template("SELECT * FROM ", ident("t"), " WHERE x = ", lit(1), " AND y = ", param(2, "y"))
        assert t.expand() == FlatTemplate(
            ["SELECT * FROM ", Ident("t"), " WHERE x = ", Lit(1), " AND y = ", Param(2, "y")]
        )

    def test_flat_template_is_valid_input(self):
        flat = template("SELECT ", param(1, "x", exact=True), " FROM ", ident("t")).expand()
        assert compile(["(", flat, ")"], "duckdb") == CompiledSql('(SELECT $x FROM "t")', {"x": 1})


class TestSqlite3:
    """Idents and Lits round-trip through the stdlib sqlite3 driver."""

    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        yield conn
        conn.close()

    @pytest.mark.parametrize("value", [None, 1, -1, 1.5, -1.5, "it's", "a\\b", '"; DROP TABLE t; --', "ünïcode"])
    def test_lit_round_trips(self, conn, value):
        c = compile(["SELECT ", lit(value)])
        assert conn.execute(c.sql, c.params).fetchone() == (value,)

    def test_bool_lit(self, conn):
        c = compile(["SELECT ", lit(True), ", ", lit(False)])
        assert conn.execute(c.sql, c.params).fetchone() == (1, 0)

    @pytest.mark.parametrize("name", ["user data", 'we"ird', "select", 'x"; DROP TABLE t; --'])
    def test_ident_round_trips(self, conn, name):
        create = compile(["CREATE TABLE ", ident(name), " (", ident(name), " INTEGER)"])
        conn.execute(create.sql, create.params)
        insert = compile(["INSERT INTO ", ident(name), " VALUES (", param(7), ")"])
        conn.execute(insert.sql, insert.params)
        select = compile(["SELECT ", ident(name), " FROM ", ident(name)])
        assert conn.execute(select.sql, select.params).fetchall() == [(7,)]
        assert [row[0] for row in conn.execute("SELECT name FROM sqlite_master")] == [name]


# Renderer names whose paramstyle uses `%` placeholders, with the quote character each uses for identifiers.
PERCENT_RENDERERS = {
    "psycopg": '"',
    "psycopg2": '"',
    "pg8000": '"',
    "pymysql": "`",
    "MySQLdb": "`",
    "mysql.connector": "`",
    "pyformat": '"',
    "format": '"',
}


def apply_driver_percent_formatting(compiled: CompiledSql) -> str:
    """Do what `%`-placeholder drivers do with a query: format it with Python's `%` operator.

    pymysql runs exactly `query % args`, and psycopg2 and others parse the same syntax:
    `%s` or `%(name)s` is a placeholder, and `%%` is a literal `%`, anywhere in the string,
    since the driver does not parse SQL quoting.
    Each placeholder becomes a visible marker, so the result shows the SQL the server receives.
    """
    if isinstance(compiled.params, dict):
        return compiled.sql % {name: f"<{name}>" for name in compiled.params}
    return compiled.sql % tuple(f"<{position}>" for position in range(1, len(compiled.params) + 1))


# Text that a `%`-placeholder driver would misread if its `%` were not doubled.
PERCENT_TRICKY = ("%", "%%", "%s", "%(p0)s", "%(x)s", "100% sure", "a%%b%")


class TestPercentEscaping:
    """Literal `%` in quoted identifiers and literals survives `%`-placeholder drivers.

    The renderer doubles every `%` in the final SQL, including inside quotes,
    and the driver's own `%` formatting turns `%%` back into `%` before the query reaches the server.
    """

    @pytest.mark.parametrize("renderer", PERCENT_RENDERERS)
    @pytest.mark.parametrize("name", PERCENT_TRICKY)
    def test_ident_reaches_server_unchanged(self, renderer, name):
        q = PERCENT_RENDERERS[renderer]
        compiled = compile(["SELECT * FROM ", ident(name)], renderer)
        assert apply_driver_percent_formatting(compiled) == f"SELECT * FROM {q}{name}{q}"  # noqa: S608

    @pytest.mark.parametrize("renderer", PERCENT_RENDERERS)
    @pytest.mark.parametrize("value", PERCENT_TRICKY)
    def test_lit_reaches_server_unchanged(self, renderer, value):
        compiled = compile(["SELECT ", lit(value)], renderer)
        assert apply_driver_percent_formatting(compiled) == f"SELECT '{value}'"

    @pytest.mark.parametrize("renderer", PERCENT_RENDERERS)
    def test_percent_in_quotes_does_not_consume_a_param(self, renderer):
        # Unescaped, the `%s` and `%(p0)s` inside the quotes would be read as placeholders
        # and swallow the real param, or fail to format.
        parts = ["SELECT ", lit("%s %(p0)s"), " FROM ", ident("%s"), " WHERE x = ", param(1)]
        compiled = compile(parts, renderer)
        q = PERCENT_RENDERERS[renderer]
        marker = "<p0>" if isinstance(compiled.params, dict) else "<1>"
        assert apply_driver_percent_formatting(compiled) == f"SELECT '%s %(p0)s' FROM {q}%s{q} WHERE x = {marker}"  # noqa: S608

    @pytest.mark.parametrize("renderer", PERCENT_RENDERERS)
    def test_rendered_sql_has_percent_doubled(self, renderer):
        q = PERCENT_RENDERERS[renderer]
        assert compile(ident("%"), renderer).sql == f"{q}%%{q}"
        assert compile(lit("%"), renderer).sql == "'%%'"

    @pytest.mark.parametrize("renderer", PERCENT_RENDERERS)
    def test_params_are_never_none(self, renderer):
        # Drivers skip their `%` formatting when params is None, which would leave `%%` doubled.
        # So a query without params still gets an empty tuple or dict.
        assert compile(ident("%"), renderer).params in ((), {})

    @pytest.mark.parametrize("renderer", ["sqlite3", "duckdb", "asyncpg", "oracledb", "qmark", "named", "dollar"])
    def test_not_doubled_for_other_paramstyles(self, renderer):
        assert compile(ident("%"), renderer).sql == '"%"'
        assert compile(lit("100%"), renderer).sql == "'100%'"
