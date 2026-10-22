# sqltemplatelib

Composable, injection-safe SQL templates built on Python [t-strings](https://docs.python.org/3/library/string.templatelib.html) (PEP 750).

- Interpolated values become bound parameters, not SQL text.
- Templates nest, so subqueries and reusable fragments compose.
- Any object can take part by implementing `__sql_template__()`. Libraries can implement it without depending on this package.
- Compiles for common drivers by name (duckdb, sqlite3, psycopg, asyncpg, pymysql, ...), any DB-API paramstyle, or a compiler you write.
- Pure Python, zero runtime dependencies, Python 3.11+. t-string syntax needs 3.14; on older versions, build templates with `template(...)`.

Status: pre-alpha. The API is extracted from [duckdb/duckdb-python#409](https://github.com/duckdb/duckdb-python/pull/409) and may change.

## Quickstart

```python
import sqlite3
from sqltemplatelib import template

min_age = 18
adults = template(t"SELECT * FROM people WHERE age >= {min_age}")

name = "x'; DROP TABLE people; -- Evil Hacker"
query = template(t"SELECT name FROM ({adults}) WHERE name != {name}")

c = query.compile("sqlite3")
# CompiledSql(sql='SELECT name FROM (SELECT * FROM people WHERE age >= ?) WHERE name != ?',
#             params=(18, "x'; DROP TABLE people; -- Evil Hacker"))
sqlite3.connect(":memory:").execute(c.sql, c.params)
```

## Lifecycle

```
anything ──template()──▶ SqlTemplate ──.compile(compiler)──▶ CompiledSql
```

- `SqlTemplate`: strings and interpolations, as written. Nested templates and `__sql_template__()` objects stay unexpanded.
- `CompiledSql`: final `.sql` string and `.params` (dict for named styles, tuple for positional).

`compile()` expands interpolations, calling `__sql_template__()` at that point, so the SQL reflects each object's state at compile time. If expansion fails, the exception gets notes naming each interpolation and the file and line where each enclosing `SqlTemplate` was created:

```
KeyError: 'min_age'
while expanding {filters} in SqlTemplate created at app.py:8
while expanding {where} in SqlTemplate created at app.py:9
```

`str()` on either type raises, so parameters cannot be dropped by accident.

## What becomes what

Inside a t-string like t'SELECT * FROM table WHERE {x}', the interpolation `{x}` is handled as follows:

| `x` is...                                  | result                                                |
|--------------------------------------------|-------------------------------------------------------|
| any value, including `str`                 | a parameter, named after the expression (`$p0_x`)     |
| `{x!s}`, `{x!r}`, `{x!a}`                  | **raw SQL text** (unsafe for untrusted input)         |
| a `Param`                                  | a parameter, keeping its name exactly                 |
| a template, or has `__sql_template__()`    | expanded in place                                     |
| a non-string iterable                      | each item expanded in place                           |

Outside a t-string, `template(*parts)` treats a bare `str` part as **SQL text**. Wrap values in `param()`:

```python
from sqltemplatelib import template, param

template("SELECT * FROM users WHERE name = ", param(user_name))  # safe
template("SELECT * FROM users WHERE name = ", user_name)  # user_name is SQL text!
```

## Params and naming

```python
param(42)  # $p0
param(42, "age")  # $p0_age   (name used as a suffix)
param(42, "age", exact=True)  # $age      (name used as-is)
```

Generated names are numbered by position, so the same expression used twice gets two distinct params. Expressions that are not identifiers are sanitized, eg `{user.age}` becomes `$p0_user_age`. Exact names must match `[A-Za-z_][A-Za-z0-9_]*`. Duplicate names raise `ValueError` in named paramstyles.

## Making your objects composable

Implement `__sql_template__(self, **kwargs)` and return anything `template()` accepts: a t-string, a `str` of SQL, a `Param`, another composable object, or an iterable of those.

```python
import dataclasses
from sqltemplatelib import template


@dataclasses.dataclass
class UserFilters:
    min_age: int | None = None
    name_like: str | None = None

    def __sql_template__(self, **kwargs):
        parts = ["true"]
        if self.min_age is not None:
            min_age = self.min_age
            parts.append(t" AND age >= {min_age}")
        if self.name_like is not None:
            name_like = self.name_like
            parts.append(t" AND name ILIKE {name_like}")
        return parts


filters = UserFilters(min_age=18)
template(t"SELECT * FROM users WHERE {filters}").compile("duckdb")
# CompiledSql(sql='SELECT * FROM users WHERE true AND age >= $p0_min_age', params={'p0_min_age': 18})
```

`**kwargs` is reserved for future options. Implementations must accept it and may ignore it.

The protocol is duck-typed: a library such as a dataframe or query-builder package can implement `__sql_template__` on its own types without importing `sqltemplatelib`. To return a parameter with an explicit name without importing `Param`, use any object whose type has `__sql_param__ = True` and that has `value`, `name`, and `exact` attributes. See [SPEC.md](SPEC.md).

## Compilers

A compiler turns a `SqlTemplate` into a `CompiledSql` for a particular driver. Pass `.compile()` a driver name, a paramstyle, or a compiler:

```python
from sqltemplatelib import DbApiCompiler, param, template

t = template("SELECT * FROM people WHERE age >= ", param(18, "age"))
t.compile()  # same as t.compile("sqlite3")
t.compile("asyncpg")  # CompiledSql(sql='SELECT * FROM people WHERE age >= $1', params=(18,))
t.compile("format")  # ... age >= %s', params=(18,))
t.compile(DbApiCompiler("format"))  # same as above
```

Drivers are named by their import name, since that is what appears in your code and stdlib `sqlite3` has no PyPI name. Each uses the driver's own `paramstyle`, except `duckdb`, which declares `qmark` but also accepts `$name`.

| name                | paramstyle       | placeholder | params |
|---------------------|------------------|-------------|--------|
| `sqlite3` (default) | `qmark`          | `?`         | tuple  |
| `duckdb`            | `dollar`         | `$name`     | dict   |
| `psycopg`           | `pyformat`       | `%(name)s`  | dict   |
| `psycopg2`          | `pyformat`       | `%(name)s`  | dict   |
| `pg8000`            | `format`         | `%s`        | tuple  |
| `asyncpg`           | `dollar_numeric` | `$1`        | tuple  |
| `pymysql`           | `pyformat`       | `%(name)s`  | dict   |
| `MySQLdb`           | `format`         | `%s`        | tuple  |
| `mysql.connector`   | `pyformat`       | `%(name)s`  | dict   |
| `oracledb`          | `named`          | `:name`     | dict   |

Most drivers accept more than one paramstyle. To use another, pass its name:

| `paramstyle`       | placeholder | params |
|--------------------|-------------|--------|
| `dollar`           | `$name`     | dict   |
| `named`            | `:name`     | dict   |
| `pyformat`         | `%(name)s`  | dict   |
| `qmark`            | `?`         | tuple  |
| `format`           | `%s`        | tuple  |
| `numeric`          | `:1`        | tuple  |
| `dollar_numeric`   | `$1`        | tuple  |

For `pyformat` and `format`, literal `%` in the SQL is escaped as `%%`.

### Custom compilers

Any object with a `__sql_compile__(template: SqlTemplate) -> CompiledSql` method satisfies the `Compiler` protocol. `template.resolve()` expands every interpolation into a flat list of SQL text and `Param`s. Most compilers pass that to the util function `compile_parts()`, which names the params and collects their values, and pass it hooks for the rendering:

```python
from sqltemplatelib import CompiledSql, SqlTemplate, compile_parts, param, template


class SqlServerCompiler:
    def __sql_compile__(self, template: SqlTemplate) -> CompiledSql:
        return compile_parts(template.resolve(), placeholder=lambda name, _position: f"@{name}", named=True)


template("SELECT * FROM people WHERE age >= ", param(18, "age")).compile(SqlServerCompiler())
# CompiledSql(sql='SELECT * FROM people WHERE age >= @p0_age', params={'p0_age': 18})
```

`register_compiler(name, compiler)` makes a compiler available by name. The registry is shared by the whole process, so register only names you own, such as your package or driver name. Registering an existing name raises unless you pass `overwrite=True`.

## Accepting templates in a database library

A library that executes SQL should accept any template or composable object, expand it, and render the params in its own syntax.

```python
from sqltemplatelib import SupportsSqlTemplate, is_compiled_sql, template
from my_sql_server_compiler import SqlServerCompiler


def execute(self, query, params=None):
    if params is not None:
        query, params = query, params
    elif is_compiled_sql(query) and not isinstance(query, SupportsSqlTemplate):
        query, params = query.sql, query.params
    else:
        compiled = template(query).compile(SqlServerCompiler())
        query, params = compiled.sql, compiled.params
    ...
```

A library that does not want the dependency on `sqltemplatelib` can implement the expansion rules in [SPEC.md](SPEC.md) itself.

## Prior art

- [psycopg 3.3](https://www.psycopg.org/psycopg3/docs/basic/tstrings.html) accepts t-strings in `execute()`, with format specs `:i` (identifier), `:l` (literal), `:q` (nested query).
- [sql-tstring](https://github.com/pgjones/sql-tstring) compiles t-strings to `(sql, values)`, with `Absent` for dropping clauses and identifier allow-lists.
- [t-sql](https://pypi.org/project/t-sql/) renders t-strings to a query and params in several styles.

This package focuses on the part those don't share: a common protocol so objects from different libraries nest inside each other's queries, and a compiler that targets any driver.

## Open questions

- Identifiers and literals. `!s` inserts raw text. psycopg uses `:i` / `:l` format specs that quote correctly. Quoting is dialect-specific, so it would need per-database compilers that call `compile_parts()` with their own hooks.
- A `join()` helper for building lists of fragments.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

t-string tests and doctests run only on Python 3.14+.
