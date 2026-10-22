from __future__ import annotations

import sqlite3

import pytest

from sqltemplatelib import PARAMSTYLES, Param, compile, param, template

AGE_QUERY = ("SELECT * FROM people WHERE age >= ", param(18, "age"), " AND name = ", param("Al", "name", exact=True))


@pytest.mark.parametrize(
    ("paramstyle", "sql", "params"),
    [
        ("dollar", "SELECT * FROM people WHERE age >= $p0_age AND name = $name", {"p0_age": 18, "name": "Al"}),
        ("named", "SELECT * FROM people WHERE age >= :p0_age AND name = :name", {"p0_age": 18, "name": "Al"}),
        (
            "pyformat",
            "SELECT * FROM people WHERE age >= %(p0_age)s AND name = %(name)s",
            {"p0_age": 18, "name": "Al"},
        ),
        ("qmark", "SELECT * FROM people WHERE age >= ? AND name = ?", (18, "Al")),
        ("format", "SELECT * FROM people WHERE age >= %s AND name = %s", (18, "Al")),
        ("numeric", "SELECT * FROM people WHERE age >= :1 AND name = :2", (18, "Al")),
        ("dollar_numeric", "SELECT * FROM people WHERE age >= $1 AND name = $2", (18, "Al")),
    ],
)
def test_paramstyles(paramstyle, sql, params):
    compiled = compile(*AGE_QUERY, paramstyle=paramstyle)
    assert compiled.sql == sql
    assert compiled.params == params


def test_all_paramstyles_covered():
    assert set(PARAMSTYLES) == {"dollar", "named", "pyformat", "qmark", "format", "numeric", "dollar_numeric"}


def test_default_paramstyle_is_dollar():
    assert compile("SELECT ", 1).sql == "SELECT $p0"


def test_unknown_paramstyle_raises():
    with pytest.raises(ValueError, match="Unknown paramstyle"):
        compile("SELECT ", 1, paramstyle="bogus")  # type: ignore[arg-type]


@pytest.mark.parametrize("paramstyle", ["pyformat", "format"])
def test_percent_escaped_for_percent_styles(paramstyle):
    compiled = compile("SELECT 10 % 3, 'a%' LIKE ", "x", paramstyle=paramstyle)
    assert compiled.sql.startswith("SELECT 10 %% 3, 'a%%' LIKE ")


@pytest.mark.parametrize("paramstyle", ["dollar", "named", "qmark", "numeric", "dollar_numeric"])
def test_percent_not_escaped_for_other_styles(paramstyle):
    assert compile("SELECT 10 % 3", paramstyle=paramstyle).sql == "SELECT 10 % 3"


def test_duplicate_exact_names_raise_for_named_styles():
    parts = (param(1, "x", exact=True), param(2, "x", exact=True))
    with pytest.raises(ValueError, match="Duplicate parameter names"):
        compile(*parts, paramstyle="named")


def test_duplicate_exact_names_ok_for_positional_styles():
    parts = (param(1, "x", exact=True), param(2, "x", exact=True))
    assert compile(*parts, paramstyle="qmark").params == (1, 2)


def test_compile_method_accepts_paramstyle():
    t = template("SELECT ", 1)
    assert t.compile(paramstyle="qmark").params == (1,)


class TestParamNames:
    @pytest.mark.parametrize("name", ["1param", "param-1", "param 1", "p ; DROP TABLE users", ""])
    def test_illegal_exact_names_raise(self, name):
        with pytest.raises(ValueError, match="Illegal parameter name"):
            Param(1, name, exact=True)

    @pytest.mark.parametrize("name", ["p", "_p", "param_1", "CamelCase"])
    def test_legal_exact_names(self, name):
        assert compile(param(1, name, exact=True)).params == {name: 1}

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("user.age", "p0_user_age"),
            ("a - 1", "p0_a_1"),
            ("items[0]", "p0_items_0"),
            ("f(x)", "p0_f_x"),
            ("+", "p0"),
        ],
    )
    def test_non_exact_names_are_sanitized(self, name, expected):
        assert compile(param(1, name)).params == {expected: 1}


def test_plain_str_part_is_raw_sql():
    # Outside a t-string, a str part is SQL text, not a value. Wrap values in param().
    assert compile("SELECT ", "1 + 1").sql == "SELECT 1 + 1"


class TestSqlite3:
    """End-to-end against the stdlib sqlite3 driver."""

    @pytest.fixture
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE people (name TEXT, age INTEGER)")
        conn.executemany("INSERT INTO people VALUES (?, ?)", [("Al", 30), ("Bo", 12), ("Cy", 50)])
        yield conn
        conn.close()

    @pytest.mark.parametrize("paramstyle", ["qmark", "named"])
    def test_nested_query(self, conn, paramstyle):
        adults = template("SELECT * FROM people WHERE age >= ", param(18, "age"))
        query = template("SELECT name FROM (", adults, ") WHERE name != ", param("Cy"), " ORDER BY name")
        compiled = query.compile(paramstyle=paramstyle)
        assert conn.execute(compiled.sql, compiled.params).fetchall() == [("Al",)]

    def test_injection_is_a_param(self, conn):
        evil = "x'; DROP TABLE people; --"
        compiled = compile("SELECT count(*) FROM people WHERE name = ", param(evil), paramstyle="qmark")
        assert conn.execute(compiled.sql, compiled.params).fetchall() == [(0,)]
        assert conn.execute("SELECT count(*) FROM people").fetchall() == [(3,)]
