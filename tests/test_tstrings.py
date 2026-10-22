"""Tests for template.py that use t-string literals (Python 3.14+ only)."""

from __future__ import annotations

import sqlite3
import sys

import pytest

from sqltemplatelib import (
    CompiledSql,
    Param,
    compile,
    param,
    template,
)

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="t-strings require Python 3.14+",
)


class SimpleRelation:
    """A minimal SupportsSqlTemplate implementation returning a string."""

    def __init__(self, sql: str):
        self._sql = sql

    def __sql_template__(self, **kwargs):
        return self._sql


class TestTStringBasics:
    def test_simple_param(self):
        user_id = 123
        t = template(t"SELECT * FROM users WHERE id = {user_id}")
        compiled = t.compile("duckdb")
        assert "user_id" in compiled.sql
        assert "$" in compiled.sql
        assert 123 in compiled.params.values()

    def test_multiple_params(self):
        name = "Alice"
        age = 30
        t = template(t"SELECT * FROM users WHERE name = {name} AND age = {age}")
        compiled = t.compile("duckdb")
        assert len(compiled.params) == 2
        assert "Alice" in compiled.params.values()
        assert 30 in compiled.params.values()

    def test_no_params(self):
        t = template(t"SELECT 1")
        compiled = t.compile("duckdb")
        assert compiled.sql == "SELECT 1"
        assert compiled.params == {}

    def test_param_name_derived_from_expression(self):
        my_value = 99
        t = template(t"SELECT {my_value}")
        compiled = t.compile("duckdb")
        assert any("my_value" in k for k in compiled.params)

    def test_explicit_param_in_tstring(self):
        p = param(42, "answer")
        t = template(t"SELECT {p}")
        compiled = t.compile("duckdb")
        assert 42 in compiled.params.values()


class TestTStringConversions:
    def test_string_conversion_s(self):
        table = "users"
        t = template(t"SELECT * FROM {table!s}")
        compiled = t.compile("duckdb")
        assert compiled.sql == "SELECT * FROM users"
        assert compiled.params == {}

    def test_repr_conversion_r(self):
        val = "hello"
        t = template(t"SELECT {val!r}")
        compiled = t.compile("duckdb")
        assert "'hello'" in compiled.sql
        assert compiled.params == {}

    def test_ascii_conversion_a(self):
        val = "café"
        t = template(t"SELECT {val!a}")
        compiled = t.compile("duckdb")
        assert compiled.params == {}
        assert "\\xe9" in compiled.sql or "caf" in compiled.sql

    def test_string_value_becomes_param_not_raw_sql(self):
        name = "Alice"
        t = template(t"SELECT * FROM users WHERE name = {name}")
        compiled = t.compile("duckdb")
        assert "Alice" not in compiled.sql
        assert "Alice" in compiled.params.values()

    def test_format_spec_on_conversion(self):
        val = 3.14159
        t = template(t"SELECT {val!s:.5}")
        compiled = t.compile("duckdb")
        assert compiled.params == {}
        assert "3.141" in compiled.sql

    def test_format_spec_without_conversion_is_ignored(self):
        val = 3.14159
        t = template(t"SELECT {val:.2f}")
        compiled = t.compile("duckdb")
        assert 3.14159 in compiled.params.values()
        assert "3.14" not in compiled.sql


class TestTStringNesting:
    def test_nested_template_via_supports_sql_template(self):
        inner = SimpleRelation("SELECT * FROM people")
        t = template(t"SELECT name FROM ({inner})")
        compiled = t.compile("duckdb")
        assert "SELECT * FROM people" in compiled.sql
        assert compiled.params == {}

    def test_nested_tstring_chaining(self):
        age = 18
        inner = template(t"SELECT * FROM people WHERE age >= {age}")
        t = template(t"SELECT name FROM ({inner})")
        compiled = t.compile("duckdb")
        assert "SELECT * FROM people WHERE age >= $" in compiled.sql
        assert 18 in compiled.params.values()

    def test_sql_template_returns_tstring(self):
        class TStringReturner:
            def __sql_template__(self, **kwargs):
                val = 42
                return t"SELECT {val}"

        t = template(TStringReturner())
        compiled = t.compile("duckdb")
        assert 42 in compiled.params.values()


class TestTStringEdgeCases:
    def test_float_param(self):
        threshold = 3.14
        result = template(t"SELECT * FROM t WHERE score > {threshold}").compile("duckdb")
        assert 3.14 in result.params.values()

    def test_none_param(self):
        val = None
        result = template(t"SELECT * FROM t WHERE x IS {val}").compile("duckdb")
        assert None in result.params.values()

    def test_param_object_preserves_name(self):
        p = Param(value=42, name="custom_name")
        result = template(t"SELECT {p}").compile("duckdb")
        assert 42 in result.params.values()

    def test_same_variable_used_twice(self):
        x = 42
        result = template(t"SELECT * FROM t WHERE a = {x} AND b = {x}").compile("duckdb")
        assert len(result.params) == 2
        assert all(v == 42 for v in result.params.values())

    def test_mixed_conversion_and_param(self):
        table = "users"
        user_id = 5
        result = template(t"SELECT * FROM {table!s} WHERE id = {user_id}").compile("duckdb")
        assert "users" in result.sql
        assert "5" not in result.sql
        assert 5 in result.params.values()

    def test_sql_injection_prevented_by_default(self):
        evil = "1; DROP TABLE users; --"
        result = template(t"SELECT * FROM users WHERE id = {evil}").compile("duckdb")
        assert "DROP TABLE" not in result.sql
        assert evil in result.params.values()

    def test_sql_injection_possible_with_s_conversion(self):
        evil = "1; DROP TABLE users; --"
        result = template(t"SELECT * FROM users WHERE id = {evil!s}").compile("duckdb")
        assert "DROP TABLE" in result.sql


class TestReadmeExample:
    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE people (name TEXT, age INTEGER)")
        conn.executemany("INSERT INTO people VALUES (?, ?)", [("Al", 30), ("Bo", 12), ("Cy", 50)])
        yield conn
        conn.close()

    @pytest.mark.parametrize("adults_form", ["tstring", "list"])
    def test_quickstart(self, conn, adults_form):
        min_age = 18
        if adults_form == "tstring":
            adults = t"SELECT * FROM people WHERE age >= {min_age}"
        else:
            adults = ["SELECT * FROM people WHERE age >= ", param(min_age)]

        name = "x'; DROP TABLE people; -- Evil Hacker"
        query = t"SELECT name FROM ({adults}) WHERE name != {name}"

        c = compile(query, "sqlite3")
        assert c == CompiledSql(
            "SELECT name FROM (SELECT * FROM people WHERE age >= ?) WHERE name != ?",
            (18, "x'; DROP TABLE people; -- Evil Hacker"),
        )
        assert sorted(conn.execute(c.sql, c.params).fetchall()) == [("Al",), ("Cy",)]
