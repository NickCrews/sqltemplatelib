# sqltemplatelib

Composable, injection-safe SQL templates built on Python [t-strings](https://docs.python.org/3/library/string.templatelib.html) (PEP 750).

This project is two things:

- **A spec** ([SPEC.md](SPEC.md)). It defines how any input (strings, t-strings, parameters, identifiers, literals, nested templates, and objects implementing `__sql_template__`) expands into a *flat template*, a flat sequence of SQL text, parameters, identifiers, and literals. Expansion does not depend on the database, so every consumer gets the same flat template from the same input, and templates and objects from different libraries nest inside each other's queries. A *renderer* then writes the flat template in one driver's syntax, under rules that keep its meaning intact. Producers and consumers can follow the spec without importing this package.
- **A reference implementation** (this package). `expand()` does the expansion, and `render()`, with renderers for common drivers, does the rendering. Use it to avoid implementing the spec yourself.

The implementation:

- Binds interpolated values as parameters, not SQL text.
- Nests templates, so subqueries and reusable fragments compose.
- Quotes identifiers and literals in each database's syntax, with `{x:i}` and `{x:l}` as in psycopg.
- Compiles for common drivers by name (duckdb, sqlite3, psycopg, asyncpg, pymysql, ...), any DB-API paramstyle, or a renderer you write.
- Pure Python, zero runtime dependencies, Python 3.11+. t-string syntax needs 3.14; on older versions, build templates with `template(...)`.

Status: pre-alpha. The API is extracted from [duckdb/duckdb-python#409](https://github.com/duckdb/duckdb-python/pull/409) and may change.

## Usage As End User

```python
import sqlite3
from sqltemplatelib import compile

min_age = 18
adults = t"SELECT * FROM people WHERE age >= {min_age}"
# if on python <3.14, can also do
# adults = ["SELECT * FROM people WHERE age >= ", sqltemplatelib.param(min_age)]

name = "x'; DROP TABLE people; -- Evil Hacker"
query = t"SELECT name FROM ({adults}) WHERE name != {name}"

c = compile(query, "sqlite3")
# CompiledSql(sql='SELECT name FROM (SELECT * FROM people WHERE age >= ?) WHERE name != ?',
#             params=(18, "x'; DROP TABLE people; -- Evil Hacker"))
sqlite3.connect(":memory:").execute(c.sql, c.params)
```

## Lifecycle

The spec's stages map onto this package's types:

```
input ──template()──▶ SqlTemplate ──expand()──▶ FlatTemplate ──render(renderer)──▶ CompiledSql
└───────────────────────────────── compile(input, renderer) ─────────────────────────────────┘
```

- `SqlTemplate`: the input, as written. Nested templates and `__sql_template__()` objects stay unexpanded.
- `FlatTemplate`: SQL text, `Param`s, `Ident`s, and `Lit`s, with nothing left to expand. Most users never see it; it is what a renderer receives.
- `CompiledSql`: final `.sql` string and `.params` (dict for named styles, tuple for positional).

`expand()` and `compile()` accept anything `template()` does, so you rarely need to call `template()` yourself. `SqlTemplate.expand()` and `SqlTemplate.compile(renderer)` are shorthands for `expand(t)` and `compile(t, renderer)`.

`compile()` expands interpolations, calling `__sql_template__()` at that point, so the SQL reflects each object's state at compile time. If expansion fails, the exception gets notes naming each interpolation and the file and line where each enclosing `SqlTemplate` was created:

```
KeyError: 'min_age'
while expanding {filters} in SqlTemplate created at app.py:8
while expanding {where} in SqlTemplate created at app.py:9
```

`str()` on any of these types raises, so parameters cannot be dropped by accident.

## What becomes what

Inside a t-string like t'SELECT * FROM table WHERE {x}', the interpolation `{x}` is handled as follows:

| `x` is...                                  | result                                                |
|--------------------------------------------|-------------------------------------------------------|
| any value, including `str`                 | a parameter, named after the expression (`$p0_x`)     |
| `{x:i}`                                    | an identifier, quoted by the renderer (`"x"`)         |
| `{x:l}`                                    | a literal, quoted by the renderer (`'x'`)             |
| `{x!s}`, `{x!r}`, `{x!a}`                  | **raw SQL text** (unsafe for untrusted input)         |
| a `Param`                                  | a parameter, keeping its name exactly                 |
| an `Ident` or `Lit`                        | itself                                                |
| a template, or has `__sql_template__()`    | expanded in place                                     |
| a non-string iterable                      | each item expanded in place                           |

Outside a t-string, `template(*parts)` treats a bare `str` part as **SQL text**. Wrap values in `param()`:

```python
from sqltemplatelib import template, param

template("SELECT * FROM users WHERE name = ", param(user_name))  # safe
template("SELECT * FROM users WHERE name = ", user_name)  # user_name is SQL text!
```

## Identifiers and literals

Use a parameter wherever the database accepts one. For names, and for places that do not take parameters, eg DDL, use `Ident` and `Lit`, or `{x:i}` and `{x:l}` in a t-string. The renderer quotes them in its database's syntax:

```python
from sqltemplatelib import compile, ident, lit

table, default = "user data", "it's"
compile(t"ALTER TABLE {table:i} ALTER COLUMN note SET DEFAULT {default:l}", "psycopg").sql
# ALTER TABLE "user data" ALTER COLUMN note SET DEFAULT 'it''s'
compile(["SELECT * FROM ", ident("order")], "pymysql").sql
# SELECT * FROM `order`
compile(["SELECT 1 -", lit(-1)]).sql
# SELECT 1 -(-1)
```

An `Ident` is one name: `Ident("a.b")` is a single name containing a dot. Write qualified names as `t"{schema:i}.{table:i}"`.

The built-in renderers write `None`, `bool`, `int`, finite `float`, finite `Decimal`, and `str` as literals, and raise `TypeError` for anything else. Each driver's renderer picks one of three quoting styles:

| quoting    | drivers                                | identifier | `str` literal                                       |
|------------|----------------------------------------|------------|-----------------------------------------------------|
| `ansi`     | sqlite3, duckdb, oracledb, paramstyles | `"name"`   | `'text'`                                            |
| `postgres` | psycopg, psycopg2, pg8000, asyncpg     | `"name"`   | `'text'`, or `E'text'` if it contains a backslash   |
| `mysql`    | pymysql, MySQLdb, mysql.connector      | `` `name` `` | `'text'`, with backslashes escaped                |

`E'...'` reads the same in Postgres whether or not `standard_conforming_strings` is on. The `mysql` style assumes MySQL's default SQL mode; with `NO_BACKSLASH_ESCAPES` set, backslashes in literals come out doubled.

## Params and naming

```python
param(42)  # $p0
param(42, "age")  # $p0_age   (name used as a suffix)
param(42, "age", exact=True)  # $age      (name used as-is)
```

Generated names are numbered by position, so the same expression used twice gets two distinct params. Expressions that are not identifiers are sanitized, eg `{user.age}` becomes `$p0_user_age`. Exact names must match `[A-Za-z_][A-Za-z0-9_]*`. Duplicate names raise `ValueError` in named paramstyles.

## Making your objects composable

Implement `__sql_template__(self, **kwargs)` and return anything `template()` accepts: a t-string, a `str` of SQL, a `Param`, `Ident`, or `Lit`, another composable object, or an iterable of those.

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
compile(t"SELECT * FROM users WHERE {filters}", "duckdb")
# CompiledSql(sql='SELECT * FROM users WHERE true AND age >= $p0_min_age', params={'p0_min_age': 18})
```

`**kwargs` is reserved for future options. Implementations must accept it and may ignore it.

The protocol is duck-typed: a library such as a dataframe or query-builder package can implement `__sql_template__` on its own types without importing `sqltemplatelib`. To return a parameter with an explicit name without importing `Param`, use any object whose type has `__sql_param__ = True` and that has `value`, `name`, and `exact` attributes. Likewise, `__sql_ident__ = True` with a `name` stands in for `Ident`, and `__sql_lit__ = True` with a `value` for `Lit`. See [SPEC.md](SPEC.md).

## Renderers

A renderer turns a `FlatTemplate` into a `CompiledSql` for a particular database and driver. Pass `compile()` or `render()` a driver name, a paramstyle, or a renderer:

```python
from sqltemplatelib import DbApiRenderer, compile, param

q = ["SELECT * FROM people WHERE age >= ", param(18, "age")]
compile(q)  # same as compile(q, "sqlite3")
compile(q, "asyncpg")  # CompiledSql(sql='SELECT * FROM people WHERE age >= $1', params=(18,))
compile(q, "format")  # ... age >= %s', params=(18,))
compile(q, DbApiRenderer("format"))  # same as above
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

For `pyformat` and `format`, literal `%` in the SQL, including in quoted identifiers and literals, is escaped as `%%`.

To use a driver's paramstyle with another database's quoting, construct the renderer yourself, eg `DbApiRenderer("qmark", quoting="mysql")`.

### Custom renderers

Any object with a `__sql_render__(self, parts: FlatTemplate, /, **future_kwargs) -> CompiledSql` method satisfies the `SqlRenderer` protocol. Most renderers pass the parts to the util function `render_parts()`, which names the params and collects their values, along with hooks for the database's syntax:

```python
from sqltemplatelib import CompiledSql, FlatTemplate, compile, ident, param, render_parts


class SqlServerRenderer:
    def __sql_render__(self, parts: FlatTemplate, /, **future_kwargs: object) -> CompiledSql:
        return render_parts(
            parts,
            placeholder=lambda name, _position: f"@{name}",
            named=True,
            quote_ident=lambda name: "[" + name.replace("]", "]]") + "]",
        )


compile(["SELECT * FROM ", ident("people"), " WHERE age >= ", param(18, "age")], SqlServerRenderer())
# CompiledSql(sql='SELECT * FROM [people] WHERE age >= @p0_age', params={'p0_age': 18})
```

`render_parts()` defaults to SQL-standard quoting for identifiers and literals. Pass `quote_ident` and `render_lit` to change them. SQL Server, for example, has no `TRUE` and `FALSE`, so a complete renderer for it would pass `render_lit` too.

`register_renderer(name, renderer)` makes a renderer available by name. The registry is shared by the whole process, so register only names you own, such as your package or driver name. Registering an existing name raises unless you pass `overwrite=True`.

## Accepting templates in a database library

A library that executes SQL is a consumer in the spec's terms. It should accept any input, expand it into a flat template, and render that in its own syntax. `compile(query, renderer)` does both:

```python
from sqltemplatelib import SupportsSqlTemplate, compile, is_compiled_sql
from my_sql_server_renderer import SqlServerRenderer


def execute(self, query, params=None):
    if params is not None:
        query, params = query, params
    elif is_compiled_sql(query) and not isinstance(query, SupportsSqlTemplate):
        query, params = query.sql, query.params
    else:
        compiled = compile(query, SqlServerRenderer())
        query, params = compiled.sql, compiled.params
    ...
```

A library that does not want the dependency on `sqltemplatelib` can implement the expansion and rendering rules in [SPEC.md](SPEC.md) itself.

## Prior art

- [psycopg 3.3](https://www.psycopg.org/psycopg3/docs/basic/tstrings.html) accepts t-strings in `execute()`, with format specs `:i` (identifier), `:l` (literal), `:q` (nested query).
- [sql-tstring](https://github.com/pgjones/sql-tstring) compiles t-strings to `(sql, values)`, with `Absent` for dropping clauses and identifier allow-lists.
- [t-sql](https://pypi.org/project/t-sql/) renders t-strings to a query and params in several styles.

This package focuses on the part those don't share: a common protocol so objects from different libraries nest inside each other's queries, and renderers that target any driver.

## Limitations

Expansion does not know the target database, so a composable object cannot produce SQL for one dialect, eg a dataframe library that generates Postgres or MySQL SQL text. Such a library has to be told the dialect another way, eg in its own constructor. Passing it through `__sql_template__` would need every producer and consumer to agree on dialect names and versions, which the spec does not define.

## Open questions

- A `join()` helper for building lists of fragments.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

t-string tests and doctests run only on Python 3.14+.
