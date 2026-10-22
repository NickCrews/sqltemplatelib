from __future__ import annotations

import sqlite3

import pytest

from sqltemplatelib import _COMPILERS as COMPILERS
from sqltemplatelib import (
    PARAMSTYLES,
    CompiledSql,
    Compiler,
    DbApiCompiler,
    Param,
    compile,
    compile_parts,
    get_compiler,
    param,
    register_compiler,
    template,
)

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
    compiled = compile(*AGE_QUERY, compiler=DbApiCompiler(paramstyle=paramstyle))
    assert compiled.sql == sql
    assert compiled.params == params


def test_all_paramstyles_covered():
    assert set(PARAMSTYLES) == {"dollar", "named", "pyformat", "qmark", "format", "numeric", "dollar_numeric"}


def test_default_compiler_is_sqlite3():
    assert compile("SELECT ", 1) == CompiledSql("SELECT ?", (1,))
    assert template("SELECT ", 1).compile() == CompiledSql("SELECT ?", (1,))


def test_unknown_paramstyle_raises():
    with pytest.raises(ValueError, match="Unknown paramstyle"):
        DbApiCompiler(paramstyle="bogus")  # type: ignore[arg-type]


@pytest.mark.parametrize("paramstyle", PARAMSTYLES)
def test_paramstyle_names_are_registered(paramstyle):
    assert get_compiler(paramstyle) == DbApiCompiler(paramstyle)


class TestCompilePartsHooks:
    def test_placeholder_gets_name_and_position(self):
        calls = []

        def placeholder(name, position):
            calls.append((name, position))
            return "?"

        compile_parts(["a = ", param(1, "a"), " AND b = ", param(2)], placeholder=placeholder, named=False)
        assert calls == [("p0_a", 1), ("p1", 2)]

    def test_custom_placeholder(self):
        compiled = compile_parts(["SELECT ", param(1, "x")], placeholder=lambda name, _: f"@{name}", named=True)
        assert compiled == CompiledSql("SELECT @p0_x", {"p0_x": 1})

    def test_escape_percent_is_independent_of_placeholder(self):
        compiled = compile_parts(
            ["SELECT 10 % ", param(3)], placeholder=lambda *_: "?", named=False, escape_percent=True
        )
        assert compiled.sql == "SELECT 10 %% ?"


def _dbapi(paramstyle):
    return DbApiCompiler(paramstyle=paramstyle)


@pytest.mark.parametrize("paramstyle", ["pyformat", "format"])
def test_percent_escaped_for_percent_styles(paramstyle):
    compiled = compile("SELECT 10 % 3, 'a%' LIKE ", "x", compiler=_dbapi(paramstyle))
    assert compiled.sql.startswith("SELECT 10 %% 3, 'a%%' LIKE ")


@pytest.mark.parametrize("paramstyle", ["dollar", "named", "qmark", "numeric", "dollar_numeric"])
def test_percent_not_escaped_for_other_styles(paramstyle):
    assert compile("SELECT 10 % 3", compiler=_dbapi(paramstyle)).sql == "SELECT 10 % 3"


def test_duplicate_exact_names_raise_for_named_styles():
    parts = (param(1, "x", exact=True), param(2, "x", exact=True))
    with pytest.raises(ValueError, match="Duplicate parameter names"):
        compile(*parts, compiler=_dbapi("named"))


def test_duplicate_exact_names_ok_for_positional_styles():
    parts = (param(1, "x", exact=True), param(2, "x", exact=True))
    assert compile(*parts, compiler=_dbapi("qmark")).params == (1, 2)


def test_compile_method_accepts_compiler_positionally():
    t = template("SELECT ", 1)
    assert t.compile(DbApiCompiler("qmark")).params == (1,)
    assert t.compile("sqlite3").params == (1,)


class TestRegistry:
    @pytest.mark.parametrize(
        ("name", "sql", "params"),
        [
            ("duckdb", "SELECT $p0_x", {"p0_x": 1}),
            ("sqlite3", "SELECT ?", (1,)),
            ("psycopg", "SELECT %(p0_x)s", {"p0_x": 1}),
            ("psycopg2", "SELECT %(p0_x)s", {"p0_x": 1}),
            ("pg8000", "SELECT %s", (1,)),
            ("asyncpg", "SELECT $1", (1,)),
            ("pymysql", "SELECT %(p0_x)s", {"p0_x": 1}),
            ("MySQLdb", "SELECT %s", (1,)),
            ("mysql.connector", "SELECT %(p0_x)s", {"p0_x": 1}),
            ("oracledb", "SELECT :p0_x", {"p0_x": 1}),
        ],
    )
    def test_compile_by_driver_name(self, name, sql, params):
        compiled = template("SELECT ", param(1, "x")).compile(name)
        assert compiled.sql == sql
        assert compiled.params == params

    def test_compiler_instance_returned_as_is(self):
        compiler = DbApiCompiler("named")
        assert get_compiler(compiler) is compiler

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="Unknown compiler: 'oracle'"):
            get_compiler("oracle")

    @pytest.fixture
    def restore_registry(self):
        saved = dict(COMPILERS)
        yield
        COMPILERS.clear()
        COMPILERS.update(saved)

    @pytest.mark.usefixtures("restore_registry")
    def test_register_custom_compiler(self):
        class UppercaseCompiler:
            def __sql_compile__(self, template):
                parts = template.resolve()
                sql = "".join(p if isinstance(p, str) else "?" for p in parts)
                return CompiledSql(sql=sql.upper(), params=tuple(p.value for p in parts if isinstance(p, Param)))

        register_compiler("upper", UppercaseCompiler())
        assert compile("select ", 1, compiler="upper").sql == "SELECT ?"

    @pytest.mark.usefixtures("restore_registry")
    def test_register_existing_name_raises_without_overwrite(self):
        with pytest.raises(ValueError, match="already registered"):
            register_compiler("sqlite3", DbApiCompiler("dollar"))
        register_compiler("sqlite3", DbApiCompiler("dollar"), overwrite=True)
        assert compile("SELECT ", 1).sql == "SELECT $p0"

    def test_register_non_compiler_raises(self):
        with pytest.raises(TypeError, match="Expected a Compiler"):
            register_compiler("bad", "qmark")  # type: ignore[arg-type]

    def test_plain_callable_is_not_a_compiler(self):
        with pytest.raises(TypeError, match="Expected a Compiler, got function"):
            register_compiler("bad", lambda template: CompiledSql(""))  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [5, None, lambda template: CompiledSql("")])
    def test_get_compiler_rejects_non_compilers(self, bad):
        with pytest.raises(TypeError, match="Expected a compiler name or a Compiler"):
            get_compiler(bad)

    def test_compile_rejects_non_compiler(self):
        with pytest.raises(TypeError, match="Expected a compiler name or a Compiler, got int"):
            template("SELECT ", 1).compile(5)  # type: ignore[arg-type]

    def test_builtin_compilers_satisfy_protocol(self):
        assert all(isinstance(c, Compiler) for c in COMPILERS.values())


class TestParamNames:
    @pytest.mark.parametrize("name", ["1param", "param-1", "param 1", "p ; DROP TABLE users", ""])
    def test_illegal_exact_names_raise(self, name):
        with pytest.raises(ValueError, match="Illegal parameter name"):
            Param(1, name, exact=True)

    @pytest.mark.parametrize("name", ["p", "_p", "param_1", "CamelCase"])
    def test_legal_exact_names(self, name):
        assert compile(param(1, name, exact=True), compiler="duckdb").params == {name: 1}

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
        assert compile(param(1, name), compiler="duckdb").params == {expected: 1}


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
        compiled = query.compile(paramstyle)
        assert conn.execute(compiled.sql, compiled.params).fetchall() == [("Al",)]

    def test_injection_is_a_param(self, conn):
        evil = "x'; DROP TABLE people; --"
        compiled = compile("SELECT count(*) FROM people WHERE name = ", param(evil), compiler="sqlite3")
        assert conn.execute(compiled.sql, compiled.params).fetchall() == [(0,)]
        assert conn.execute("SELECT count(*) FROM people").fetchall() == [(3,)]
