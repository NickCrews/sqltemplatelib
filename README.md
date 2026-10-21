# sqltemplatelib

Composable, injection-safe SQL templates built on Python [t-strings](https://docs.python.org/3/library/string.templatelib.html) (PEP 750).

- Interpolated values become bound parameters, not SQL text.
- Templates nest, so subqueries and reusable fragments compose.
- Any object can take part by implementing `__sql_template__()`. Libraries can implement it without depending on this package.
- Compiles to any DB-API paramstyle: DuckDB, sqlite3, psycopg, asyncpg, and others.
- Pure Python, zero runtime dependencies, Python 3.11+. t-string syntax needs 3.14; on older versions, build templates with `template(...)`.

Status: pre-alpha. The API is extracted from [duckdb/duckdb-python#409](https://github.com/duckdb/duckdb-python/pull/409) and may change.

## Quickstart

```python
import sqlite3
from sqltemplatelib import template

min_age = 18
adults = template(t"SELECT * FROM people WHERE age >= {min_age}")

name = "x'; DROP TABLE people; --"
query = template(t"SELECT name FROM ({adults}) WHERE name != {name}")

query.compile()
# CompiledSql(sql='SELECT name FROM (SELECT * FROM people WHERE age >= $p0_min_age) WHERE name != $p1_name',
#             params={'p0_min_age': 18, 'p1_name': "x'; DROP TABLE people; --"})

c = query.compile(paramstyle="qmark")
sqlite3.connect(":memory:").execute(c.sql, c.params)
```

## Lifecycle

```
anything ──template()──▶ SqlTemplate ──.resolve()──▶ ResolvedSqlTemplate ──.compile()──▶ CompiledSql
```

- `SqlTemplate`: strings and interpolations, as written.
- `ResolvedSqlTemplate`: nested templates expanded; only SQL strings and `Param`s remain.
- `CompiledSql`: final `.sql` string and `.params` (dict for named styles, tuple for positional).

You can stop at any stage to inspect or transform the template. `str()` on any of them raises, so parameters cannot be dropped by accident.

## What becomes what

Inside a t-string, an interpolation `{x}` is handled as follows:

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

## Paramstyles

| `paramstyle`       | placeholder | params | used by                        |
|--------------------|-------------|--------|--------------------------------|
| `dollar` (default) | `$name`     | dict   | DuckDB                         |
| `named`            | `:name`     | dict   | sqlite3, oracledb, SQLAlchemy  |
| `pyformat`         | `%(name)s`  | dict   | psycopg, pymysql               |
| `qmark`            | `?`         | tuple  | sqlite3, DuckDB                |
| `format`           | `%s`        | tuple  | psycopg, pymysql               |
| `numeric`          | `:1`        | tuple  | PEP 249                        |
| `dollar_numeric`   | `$1`        | tuple  | asyncpg, DuckDB                |

For `pyformat` and `format`, literal `%` in the SQL is escaped as `%%`.

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
template(t"SELECT * FROM users WHERE {filters}").compile()
# CompiledSql(sql='SELECT * FROM users WHERE true AND age >= $p0_min_age', params={'p0_min_age': 18})
```

`**kwargs` is reserved for future options. Implementations must accept it and may ignore it.

The protocol is duck-typed: a library such as a dataframe or query-builder package can implement `__sql_template__` on its own types without importing `sqltemplatelib`. See [SPEC.md](SPEC.md).

## Prior art

- [psycopg 3.3](https://www.psycopg.org/psycopg3/docs/basic/tstrings.html) accepts t-strings in `execute()`, with format specs `:i` (identifier), `:l` (literal), `:q` (nested query).
- [sql-tstring](https://github.com/pgjones/sql-tstring) compiles t-strings to `(sql, values)`, with `Absent` for dropping clauses and identifier allow-lists.
- [t-sql](https://pypi.org/project/t-sql/) renders t-strings to a query and params in several styles.

This package focuses on the part those don't share: a common protocol so objects from different libraries nest inside each other's queries, and a compiler that targets any driver.

## Open questions

- Identifiers and literals. `!s` inserts raw text. psycopg uses `:i` / `:l` format specs that quote correctly. Quoting is dialect-specific, so supporting it needs a dialect hook alongside `paramstyle`.
- Default paramstyle. `dollar` comes from DuckDB. A neutral library could instead require it.
- A `join()` helper for building lists of fragments.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

t-string tests and doctests run only on Python 3.14+.
