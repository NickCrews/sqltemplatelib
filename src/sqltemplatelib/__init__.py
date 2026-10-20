"""Composable, injection-safe SQL templates built on Python's string.templatelib (PEP 750)."""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from typing_extensions import TypeIs

__all__ = [
    "PARAMSTYLES",
    "CompiledSql",
    "IntoInterpolation",
    "Param",
    "ParamStyle",
    "ResolvedSqlTemplate",
    "SqlTemplate",
    "SupportsSqlTemplate",
    "compile",
    "param",
    "template",
]

ParamStyle = Literal["dollar", "named", "pyformat", "qmark", "format", "numeric", "dollar_numeric"]
"""How parameter placeholders are written into the compiled SQL.

| style            | placeholder  | params type | used by                         |
|------------------|--------------|-------------|---------------------------------|
| `dollar`         | `$name`      | dict        | DuckDB                          |
| `named`          | `:name`      | dict        | sqlite3, oracledb, SQLAlchemy   |
| `pyformat`       | `%(name)s`   | dict        | psycopg, pymysql                |
| `qmark`          | `?`          | tuple       | sqlite3, DuckDB                 |
| `format`         | `%s`         | tuple       | psycopg, pymysql                |
| `numeric`        | `:1`         | tuple       | (PEP 249)                       |
| `dollar_numeric` | `$1`         | tuple       | asyncpg, DuckDB                 |

For `pyformat` and `format`, literal `%` characters in the SQL are escaped as `%%`.
"""

PARAMSTYLES: tuple[ParamStyle, ...] = ("dollar", "named", "pyformat", "qmark", "format", "numeric", "dollar_numeric")

_NAMED_STYLES = frozenset({"dollar", "named", "pyformat"})
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclasses.dataclass(frozen=True, slots=True)
class CompiledSql:
    """A compiled SQL statement: the final SQL string, plus the params to pass alongside it.

    `params` is a dict for named paramstyles and a tuple for positional ones.

    You will typically not create this directly, but will get it as the result
    of calling .compile() on a SqlTemplate or ResolvedSqlTemplate.

    Example:
    >>> age = 37
    >>> compile(t"SELECT * FROM users WHERE age >= {age}")
    CompiledSql(sql='SELECT * FROM users WHERE age >= $p0_age', params={'p0_age': 37})
    >>> compile(t"SELECT * FROM users WHERE age >= {age}", paramstyle="qmark")
    CompiledSql(sql='SELECT * FROM users WHERE age >= ?', params=(37,))
    """

    sql: str
    params: dict[str, object] | tuple[object, ...] = dataclasses.field(default_factory=dict)

    def __str__(self) -> NoReturn:
        """Disallow accidentally converting to a string, since that would lose the parameters."""
        msg = "CompiledSql cannot be directly converted to a string, since it may contain parameters. Please use the .sql attribute for the SQL string, and the .params attribute for the parameters."  # noqa: E501
        raise NotImplementedError(msg)


@runtime_checkable
class SupportsSqlTemplate(Protocol):
    """Something that can be converted into a SqlTemplate by implementing the __sql_template__ method.

    `**future_kwargs` is reserved so that options can be passed to implementations later
    without breaking existing ones. Implementations must accept and may ignore them.
    """

    def __sql_template__(
        self, /, **future_kwargs: object
    ) -> (
        str
        | IntoInterpolation
        | Param
        | SupportsSqlTemplate
        | object
        | Iterable[str | IntoInterpolation | Param | SupportsSqlTemplate | object]
    ):
        """Convert self into something that template() understands."""


@dataclasses.dataclass(frozen=True, slots=True)
class Param:
    """A parameter value, with an optional name and an optional exact flag.

    - `name=None`: the compiler generates a name, eg `p0`.
    - `name="age", exact=False`: the name is used as a suffix, eg `p0_age`.
    - `name="age", exact=True`: the name is used as-is, eg `age`.

    Names are ignored for positional paramstyles.
    """

    value: object
    name: str | None = None
    exact: bool = False

    def __post_init__(self) -> None:
        """Ensure passed args were valid."""
        if self.exact:
            if self.name is None:
                msg = "Param with exact=True must have a name."
                raise ValueError(msg)
            assert_param_name_legal(self.name)


def param(value: object, name: str | None = None, *, exact: bool = False) -> Param:
    """Helper function to create a Param with an optional name and an optional exact flag."""
    return Param(value=value, name=name, exact=exact)


def template(*parts: str | IntoInterpolation | Param | SupportsSqlTemplate | object) -> SqlTemplate:
    """Convert a sequence of things into a SqlTemplate.

    We go through the parts and convert it into a sequence of str and Interpolations,
    which we then hand off to SqlTemplate.
    - If the thing has a `.__sql_template__()` method, call it,
      and call template() recursively on the result.
    - If the thing is a `str`, treat it as raw SQL.
    - If it's an Interpolation, leave it as is, treating it as an interpolation.
    - If it's a Param, wrap it in an Interpolation.
    - If it's another iterable, expand each item.
    - Otherwise, treat the thing as a param, and then wrap the Param in an Interpolation.

    Examples:
    A very simple example is just passing a string, which will be treated as raw SQL:

    >>> t = template("SELECT * FROM users WHERE id = 123")
    >>> t
    SqlTemplate('SELECT * FROM users WHERE id = 123')
    >>> t.compile()
    CompiledSql(sql='SELECT * FROM users WHERE id = 123', params={})

    In python 3.14+, [tstrings](https://docs.python.org/3/library/string.templatelib.html)
    are very useful here.
    Any interpolation inside a tstring will be richly interpreted,
    either treated as a param, or expanded as a subquery:

    >>> user_id = 123
    >>> template(t"SELECT * FROM users WHERE id = {user_id}").compile()
    CompiledSql(sql='SELECT * FROM users WHERE id = $p0_user_id', params={'p0_user_id': 123})

    Templates nest:
    >>> age = 18
    >>> adults = template(t"SELECT * FROM people WHERE age >= {age}")
    >>> template(t"SELECT name FROM ({adults})").compile()
    CompiledSql(sql='SELECT name FROM (SELECT * FROM people WHERE age >= $p0_age)', params={'p0_age': 18})

    We also support iterables of strings and Interpolations/Params/etc,
    which will be joined together into a single template.
    This is useful for versions of python before 3.14 that don't have tstrings,
    since it allows you to build up a template from smaller pieces:

    >>> template("SELECT * FROM (", adults, ") WHERE age >= ", age).compile()
    CompiledSql(sql='SELECT * FROM (SELECT * FROM people WHERE age >= $p0_age) WHERE age >= $p1', params={'p0_age': 18, 'p1': 18})

    You can define evaluation logic for your custom types by defining a `.__sql_template__()` method.
    If this method is defined, the result of that call will be used to create the template.

    >>> class Record:
    ...     def __init__(self, table_name: str, id: int):
    ...         self.table_name = table_name
    ...         self.id = id
    ...
    ...     def __sql_template__(self, **kwargs):
    ...         id = self.id
    ...         # note the use of !s to indicate that the table name should be treated as raw SQL
    ...         return t"SELECT * FROM {self.table_name!s} WHERE id = {id}"
    >>> template(Record("users", 123)).compile()
    CompiledSql(sql='SELECT * FROM users WHERE id = $p0_id', params={'p0_id': 123})
    """  # noqa: E501
    expanded: list[str | IntoInterpolation] = []
    for part in parts:
        expanded.extend(_expand_part(part))
    return SqlTemplate(*expanded)


def compile(
    *parts: str | IntoInterpolation | Param | SupportsSqlTemplate | object,
    paramstyle: ParamStyle = "dollar",
) -> CompiledSql:
    """Compile a sequence of things into a final SQL string with parameter placeholders, and the params.

    This is a convenience function that combines template() and .compile() into one step.

    For more details and examples, see template().
    """
    return template(*parts).compile(paramstyle=paramstyle)


def _is_iterable_nonstring(value: object) -> TypeIs[Iterable[object]]:
    return isinstance(value, Iterable) and not isinstance(value, str | bytes)


def _expand_part(part: object) -> Iterable[str | IntoInterpolation]:
    if isinstance(part, SupportsSqlTemplate):
        raw = part.__sql_template__()
        if isinstance(raw, str):  # noqa: SIM114
            yield raw
        elif isinstance(raw, IntoInterpolation):
            yield raw
        elif isinstance(raw, Param):
            yield ParamInterpolation(raw)
        elif _is_iterable_nonstring(raw):
            for item in raw:
                yield from _expand_part(item)
        else:
            yield ParamInterpolation(param(value=raw))
    elif isinstance(part, str):  # noqa: SIM114
        yield part
    elif isinstance(part, IntoInterpolation):
        yield part
    elif isinstance(part, Param):
        yield ParamInterpolation(part)
    elif _is_iterable_nonstring(part):
        for item in part:
            yield from _expand_part(item)
    else:
        yield ParamInterpolation(param(value=part))


class ParamInterpolation:
    """A simple wrapper that implements the IntoInterpolation protocol for a given Param."""

    value: object
    expression: str | None
    conversion: Literal["s", "r", "a"] | None
    format_spec: str

    def __init__(self, param: Param) -> None:
        self.value = param
        self.expression = param.name
        self.conversion = None
        self.format_spec = ""

    def __repr__(self) -> str:
        return repr(self.value)


def _resolve(parts: Iterable[str | IntoInterpolation]) -> ResolvedSqlTemplate:
    """Resolve a stream of strings and Interpolations, recursively resolving inner interpolations."""
    resolved: list[str | Param] = []
    for part in parts:
        if isinstance(part, str):
            resolved.append(part)
        else:
            resolved.extend(_resolve_interpolation(part))
    return ResolvedSqlTemplate(resolved)


def _resolve_interpolation(interp: IntoInterpolation) -> Iterable[str | Param]:
    value = interp.value
    if isinstance(value, Param):
        # A Param passed directly to template() keeps its own settings.
        # A named Param written inside a t-string keeps its name exactly.
        if isinstance(interp, ParamInterpolation):
            return (value,)
        if value.name is None:
            return (param(value.value),)
        return (param(value.value, name=value.name, exact=True),)

    # A conversion (!s, !r, !a) means "insert as raw SQL, not as a param".
    # This is for things that cannot be params, such as identifiers or SQL keywords.
    # It is unsafe for untrusted values.
    # Follows f-string semantics: apply conversion first, then format_spec,
    # eg {x!r:.10} means format(repr(x), ".10").
    converted: str | None
    if interp.conversion == "s":
        converted = str(value)
    elif interp.conversion == "r":
        converted = repr(value)
    elif interp.conversion == "a":
        converted = ascii(value)
    else:
        converted = None

    if converted is not None:
        return (format(converted, interp.format_spec),)

    if isinstance(value, str):
        # Do NOT pass to template(), since that would treat it as raw SQL.
        return (param(value, name=interp.expression),)
    templ = template(value)
    # If the value expands to a single param, keep the interpolation's expression as its name,
    # eg `{age}` compiles to `$p0_age` rather than `$p0`.
    if len(templ.strings) == 2 and templ.strings[0] == "" and templ.strings[1] == "" and len(templ.interpolations) == 1:
        return (param(value, name=interp.expression),)
    # Nested template, eg a subquery.
    return _resolve(templ)


@runtime_checkable
class IntoInterpolation(Protocol):
    """Something that looks like a string.templatelib.Interpolation."""

    value: object
    conversion: Literal["s", "r", "a"] | None
    expression: str | None
    format_spec: str


def assert_param_name_legal(name: str) -> None:
    """Raise ValueError unless name is a legal param name.

    Eg `param_1` is legal, but `1param`, `param-1`, `param 1`, and `p ; DROP TABLE users` are not.
    """
    if not _PARAM_NAME_RE.fullmatch(name):
        msg = f"Illegal parameter name: {name!r}. Names must match {_PARAM_NAME_RE.pattern}."
        raise ValueError(msg)


def _sanitize_name_suffix(name: str) -> str:
    """Turn an arbitrary interpolation expression, eg `user.age`, into a legal suffix, eg `user_age`."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")


class SqlTemplate:
    """A sequence of strings and Interpolations."""

    def __init__(self, *parts: str | IntoInterpolation) -> None:
        for part in parts:
            if not isinstance(part, str | IntoInterpolation):
                # Guard for untyped callers; mypy considers this unreachable given the declared parameter type.
                msg = f"Unexpected part type in SqlTemplate: {type(part).__name__}. Expected str or IntoInterpolation."
                raise TypeError(msg)
        self.strings, self.interpolations = parse_parts(parts)

    def __iter__(self) -> Iterator[str | IntoInterpolation]:
        """Iterate over the strings and interpolations in order."""
        for s, i in zip(self.strings, self.interpolations, strict=False):
            yield s
            yield i
        yield self.strings[-1]

    def resolve(self) -> ResolvedSqlTemplate:
        """Recursively resolve Interpolations into Params, returning a ResolvedSqlTemplate."""
        return _resolve(self)

    def compile(self, *, paramstyle: ParamStyle = "dollar") -> CompiledSql:
        """Compile this template into a final SQL string with parameter placeholders, and the params."""
        return self.resolve().compile(paramstyle=paramstyle)

    def __str__(self) -> NoReturn:
        msg = "SqlTemplate cannot be directly converted to a string, since it may contain unresolved interpolations. Please call .resolve() or .compile() first."  # noqa: E501
        raise NotImplementedError(msg)

    def __repr__(self) -> str:
        part_strings = [repr(part) for part in self]
        return f"SqlTemplate({', '.join(part_strings)})"


class ResolvedSqlTemplate:
    """A SqlTemplate that has been resolved to only strings and Params."""

    def __init__(self, parts: Iterable[str | Param]) -> None:
        self.parts = tuple(parts)

    def compile(self, *, paramstyle: ParamStyle = "dollar") -> CompiledSql:
        """Compile this template into a final SQL string with parameter placeholders, and the params."""
        return compile_parts(self.parts, paramstyle=paramstyle)

    def __str__(self) -> NoReturn:
        msg = "ResolvedSqlTemplate cannot be directly converted to a string, since it may contain parameters. Please call .compile() first."  # noqa: E501
        raise NotImplementedError(msg)

    def __repr__(self) -> str:
        part_strings = []
        for part in self.parts:
            if isinstance(part, str):
                part_strings.append(repr(part))
            else:
                part_strings.append(f"{{{part.name}={part.value}}}")
        return f"ResolvedSqlTemplate({', '.join(part_strings)})"

    def __iter__(self) -> Iterator[str | Param]:
        start = 0
        end = len(self.parts)
        while start < end and self.parts[start] == "":
            start += 1
        while end > start and self.parts[end - 1] == "":
            end -= 1
        yield from self.parts[start:end]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CompiledSql):
            return self.compile() == other
        if isinstance(other, ResolvedSqlTemplate):
            return self.parts == other.parts
        return False


T = TypeVar("T")


def parse_parts(parts: Iterable[str | T]) -> tuple[tuple[str, ...], tuple[T, ...]]:
    """Parse an iterable of strings and others into separate tuples of strings and others.

    This merges adjacent strings and ensures that the number of strings is one more than the number of others.
    """
    strings: list[str] = []
    others: list[T] = []
    last_thing: Literal["string", "other"] | None = None
    for part in parts:
        if isinstance(part, str):
            if last_thing == "string":
                strings[-1] += part
            else:
                strings.append(part)
            last_thing = "string"
        else:
            if last_thing is None or last_thing == "other":
                # First part, or two adjacent others: insert an empty string spacer.
                strings.append("")
            others.append(part)
            last_thing = "other"
    if last_thing is None or last_thing == "other":
        strings.append("")
    assert len(strings) == len(others) + 1
    return tuple(strings), tuple(others)


def _placeholder(paramstyle: ParamStyle, name: str, position: int) -> str:
    if paramstyle == "dollar":
        return f"${name}"
    if paramstyle == "named":
        return f":{name}"
    if paramstyle == "pyformat":
        return f"%({name})s"
    if paramstyle == "qmark":
        return "?"
    if paramstyle == "format":
        return "%s"
    if paramstyle == "numeric":
        return f":{position}"
    return f"${position}"


def compile_parts(parts: Iterable[str | Param], /, *, paramstyle: ParamStyle = "dollar") -> CompiledSql:
    """Compile parts into a final SQL string with parameter placeholders, and the params."""
    if paramstyle not in PARAMSTYLES:
        msg = f"Unknown paramstyle: {paramstyle!r}. Expected one of {PARAMSTYLES}."
        raise ValueError(msg)
    escape_percent = paramstyle in ("pyformat", "format")
    sql_parts: list[str] = []
    params_items: list[tuple[str, object]] = []

    def next_name(suffix: str | None = None) -> str:
        # Exact params still count, so we get p0, my_param, p2, p3_x, ...
        base = f"p{len(params_items)}"
        if suffix:
            return f"{base}_{suffix}"
        return base

    for part in parts:
        if isinstance(part, str):
            sql_parts.append(part.replace("%", "%%") if escape_percent else part)
        else:
            if part.name is None:
                param_name = next_name()
            elif part.exact:
                param_name = part.name
            else:
                param_name = next_name(_sanitize_name_suffix(part.name))
            assert_param_name_legal(param_name)
            sql_parts.append(_placeholder(paramstyle, param_name, len(params_items) + 1))
            params_items.append((param_name, part.value))
    sql = "".join(sql_parts)
    if paramstyle not in _NAMED_STYLES:
        return CompiledSql(sql=sql, params=tuple(value for _, value in params_items))
    param_name_counts = Counter(name for name, _ in params_items)
    dupes = [name for name, count in param_name_counts.items() if count > 1]
    if dupes:
        msg = f"Duplicate parameter names found: {dupes}. Please ensure all parameter names are unique."
        raise ValueError(msg)
    return CompiledSql(sql=sql, params=dict(params_items))
