"""Composable, injection-safe SQL templates built on Python's string.templatelib (PEP 750)."""

from __future__ import annotations

import dataclasses
import math
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Literal, Never, Protocol, TypeVar, get_args, overload, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    from typing_extensions import TypeIs

__all__ = [
    "PARAMSTYLES",
    "CompiledSql",
    "DbApiRenderer",
    "FlatTemplate",
    "Ident",
    "IntoInterpolation",
    "Lit",
    "Param",
    "ParamStyle",
    "Quoting",
    "SqlRenderer",
    "SqlTemplate",
    "SupportsSqlTemplate",
    "compile",
    "expand",
    "get_renderer",
    "ident",
    "is_compiled_sql",
    "is_sql_ident",
    "is_sql_lit",
    "is_sql_param",
    "lit",
    "param",
    "register_renderer",
    "render",
    "render_parts",
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

PARAMSTYLES: tuple[ParamStyle, ...] = get_args(ParamStyle)

_NAMED_STYLES = frozenset({"dollar", "named", "pyformat"})
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclasses.dataclass(frozen=True, slots=True)
class CompiledSql:
    """A compiled SQL statement: the final SQL string, plus the params to pass alongside it.

    `params` is a dict for named paramstyles and a tuple for positional ones.

    You will typically not create this directly, but will get it as the result of compile().

    Example:
    >>> age = 37
    >>> compile(t"SELECT * FROM users WHERE age >= {age}")
    CompiledSql(sql='SELECT * FROM users WHERE age >= ?', params=(37,))
    >>> compile(t"SELECT * FROM users WHERE age >= {age}", renderer="duckdb")
    CompiledSql(sql='SELECT * FROM users WHERE age >= $p0_age', params={'p0_age': 37})

    `__sql_compiled__` marks this as compiled SQL as defined in SPEC.md, so it cannot be embedded in a template.
    """

    __sql_compiled__: ClassVar[bool] = True

    sql: str
    params: dict[str, object] | tuple[object, ...] = dataclasses.field(default_factory=dict)

    def __str__(self) -> Never:
        """Disallow accidentally converting to a string, since that would lose the parameters."""
        msg = "CompiledSql cannot be directly converted to a string, since it may contain parameters. Please use the .sql attribute for the SQL string, and the .params attribute for the parameters."  # noqa: E501
        raise NotImplementedError(msg)


def is_compiled_sql(obj: object) -> bool:
    """Return whether obj is compiled SQL as defined in SPEC.md: SQL text whose placeholders are already rendered.

    obj is compiled SQL if its type has a truthy `__sql_compiled__` attribute.
    Having `sql` and `params` attributes is not enough, so mocks and objects with a catch-all `__getattr__`
    are still bound as ordinary values.

    Compiled SQL cannot be embedded in a template, since its placeholders are in one driver's syntax
    and its param names may collide with the enclosing template's.

    >>> is_compiled_sql(CompiledSql("SELECT 1"))
    True
    >>> is_compiled_sql(template("SELECT 1"))
    False
    """
    return bool(getattr(type(obj), "__sql_compiled__", False))


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

    - `name=None`: the renderer generates a name, eg `p0`.
    - `name="age", exact=False`: the name is used as a suffix, eg `p0_age`.
    - `name="age", exact=True`: the name is used as-is, eg `age`.

    Names are ignored for positional paramstyles.

    `__sql_param__` marks this as a parameter object as defined in SPEC.md.
    Any object with that marker and `value`, `name`, and `exact` attributes is treated the same way.
    """

    __sql_param__: ClassVar[bool] = True

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


def is_sql_param(obj: object) -> bool:
    """Return whether obj is a parameter object as defined in SPEC.md: its type has a truthy `__sql_param__`.

    The marker is read from the type, not the instance, like Python's own dunder lookups.
    So a class that is itself a parameter type, eg `Param`, is not a parameter object when used as a value,
    and an object whose `__getattr__` answers any name is not one by accident.

    >>> is_sql_param(param(1))
    True
    >>> is_sql_param(Param)
    False
    """
    return bool(getattr(type(obj), "__sql_param__", False))


def _as_param(obj: object) -> Param | None:
    """Return obj as a Param if it is a parameter object as defined in SPEC.md, else None."""
    if isinstance(obj, Param):
        return obj
    if not is_sql_param(obj):
        return None
    if not hasattr(obj, "value"):
        msg = f"{type(obj).__name__} sets __sql_param__ but has no `value` attribute, which a parameter object needs."
        raise TypeError(msg)
    return Param(obj.value, getattr(obj, "name", None), exact=getattr(obj, "exact", False))


@dataclasses.dataclass(frozen=True, slots=True)
class Ident:
    """An identifier, eg a table or column name, quoted by the renderer in its dialect's syntax.

    In a t-string, `{name:i}` makes an Ident from a str.
    The name is one identifier, so `Ident("a.b")` is a single name containing a dot, not a qualified name.
    Write qualified names as separate parts, eg `t"{schema:i}.{table:i}"`.

    `__sql_ident__` marks this as an identifier object as defined in SPEC.md.
    Any object with that marker and a `name` attribute is treated the same way.
    """

    __sql_ident__: ClassVar[bool] = True

    name: str

    def __post_init__(self) -> None:
        """Ensure the name is a non-empty str without NUL characters, which no database accepts in identifiers."""
        if not isinstance(self.name, str):
            msg = f"Ident name must be a str, got {type(self.name).__name__}."
            raise TypeError(msg)
        if not self.name or "\x00" in self.name:
            msg = f"Illegal identifier: {self.name!r}. Identifiers must be non-empty and not contain NUL."
            raise ValueError(msg)


def ident(name: str) -> Ident:
    """Helper function to create an Ident."""
    return Ident(name)


def is_sql_ident(obj: object) -> bool:
    """Return whether obj is an identifier object as defined in SPEC.md: its type has a truthy `__sql_ident__`.

    >>> is_sql_ident(ident("users"))
    True
    >>> is_sql_ident(Ident)
    False
    """
    return bool(getattr(type(obj), "__sql_ident__", False))


def _as_ident(obj: object) -> Ident | None:
    """Return obj as an Ident if it is an identifier object as defined in SPEC.md, else None."""
    if isinstance(obj, Ident):
        return obj
    if not is_sql_ident(obj):
        return None
    if not hasattr(obj, "name"):
        msg = f"{type(obj).__name__} sets __sql_ident__ but has no `name` attribute, which an identifier object needs."
        raise TypeError(msg)
    name: object = obj.name
    if not isinstance(name, str):
        msg = f"{type(obj).__name__}.name must be a str, got {type(name).__name__}."
        raise TypeError(msg)
    return Ident(name)


@dataclasses.dataclass(frozen=True, slots=True)
class Lit:
    """A value written into the SQL as a literal, eg `'O''Brien'` or `42`, rather than bound as a parameter.

    In a t-string, `{value:l}` makes a Lit.
    Use a Param wherever the database accepts one. A Lit is for places it does not, eg DDL such as
    `CREATE VIEW` or a column `DEFAULT`.

    The renderer writes the literal in its dialect's syntax. The built-in renderers support
    None, bool, int, finite float, finite Decimal, and str, and raise TypeError for anything else.

    `__sql_lit__` marks this as a literal object as defined in SPEC.md.
    Any object with that marker and a `value` attribute is treated the same way.
    """

    __sql_lit__: ClassVar[bool] = True

    value: object


def lit(value: object) -> Lit:
    """Helper function to create a Lit."""
    return Lit(value)


def is_sql_lit(obj: object) -> bool:
    """Return whether obj is a literal object as defined in SPEC.md: its type has a truthy `__sql_lit__`.

    >>> is_sql_lit(lit(1))
    True
    >>> is_sql_lit(Lit)
    False
    """
    return bool(getattr(type(obj), "__sql_lit__", False))


def _as_lit(obj: object) -> Lit | None:
    """Return obj as a Lit if it is a literal object as defined in SPEC.md, else None."""
    if isinstance(obj, Lit):
        return obj
    if not is_sql_lit(obj):
        return None
    if not hasattr(obj, "value"):
        msg = f"{type(obj).__name__} sets __sql_lit__ but has no `value` attribute, which a literal object needs."
        raise TypeError(msg)
    return Lit(obj.value)


def _as_ident_or_lit(obj: object) -> Ident | Lit | None:
    """Return obj as an Ident or Lit if it is an identifier or literal object as defined in SPEC.md, else None."""
    as_ident = _as_ident(obj)
    if as_ident is not None:
        return as_ident
    return _as_lit(obj)


class FlatTemplate(Sequence["str | Param | Ident | Lit"]):
    """A fully expanded template: SQL text, Params, Idents, and Lits, with nothing left to expand.

    This is what expand() returns and what a SqlRenderer renders.
    Adjacent SQL text is merged and empty SQL text is dropped,
    so equivalent templates expand to equal FlatTemplates.

    >>> FlatTemplate(["SELECT ", "* FROM ", ident("users"), ""])
    FlatTemplate('SELECT * FROM ', Ident(name='users'))
    """

    __slots__ = ("_parts",)

    def __init__(self, parts: Iterable[str | Param | Ident | Lit] = (), /) -> None:
        merged: list[str | Param | Ident | Lit] = []
        for part in parts:
            if not isinstance(part, str | Param | Ident | Lit):
                # Guard for untyped callers; mypy considers this unreachable given the declared parameter type.
                msg = (
                    f"Unexpected part type in FlatTemplate: {type(part).__name__}. Expected str, Param, Ident, or Lit."
                )
                raise TypeError(msg)
            if isinstance(part, str):
                if not part:
                    continue
                if merged and isinstance(merged[-1], str):
                    merged[-1] += part
                    continue
            merged.append(part)
        self._parts = tuple(merged)

    @overload
    def __getitem__(self, index: int) -> str | Param | Ident | Lit: ...
    @overload
    def __getitem__(self, index: slice) -> FlatTemplate: ...
    def __getitem__(self, index: int | slice) -> str | Param | Ident | Lit | FlatTemplate:
        if isinstance(index, slice):
            return FlatTemplate(self._parts[index])
        return self._parts[index]

    def __len__(self) -> int:
        return len(self._parts)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FlatTemplate):
            return NotImplemented
        return self._parts == other._parts

    def __str__(self) -> Never:
        msg = "FlatTemplate cannot be directly converted to a string, since it may contain parameters. Please render it first."  # noqa: E501
        raise NotImplementedError(msg)

    def __repr__(self) -> str:
        return f"FlatTemplate({', '.join(repr(part) for part in self._parts)})"


def template(*parts: str | IntoInterpolation | Param | SupportsSqlTemplate | object) -> SqlTemplate:
    """Convert a sequence of things into a SqlTemplate.

    We go through the parts and convert it into a sequence of str and Interpolations,
    which we then hand off to SqlTemplate.
    - If the thing is a SqlTemplate or has a `.__sql_template__()` method,
      wrap it in an Interpolation, to be expanded at compile time.
    - If the thing is a `str`, treat it as raw SQL.
    - If it's an Interpolation, leave it as is, treating it as an interpolation.
    - If it's a Param, Ident, or Lit, wrap it in an Interpolation.
    - If it's compiled SQL (see is_compiled_sql()), raise TypeError.
    - If it's another iterable, expand each item.
    - Otherwise, treat the thing as a param, and then wrap the Param in an Interpolation.

    Examples:
    A very simple example is just passing a string, which will be treated as raw SQL:

    >>> t = template("SELECT * FROM users WHERE id = 123")
    >>> t
    SqlTemplate('SELECT * FROM users WHERE id = 123')
    >>> t.compile()
    CompiledSql(sql='SELECT * FROM users WHERE id = 123', params=())

    In python 3.14+, [tstrings](https://docs.python.org/3/library/string.templatelib.html)
    are very useful here.
    Any interpolation inside a tstring will be richly interpreted,
    either treated as a param, or expanded as a subquery:

    >>> user_id = 123
    >>> template(t"SELECT * FROM users WHERE id = {user_id}").compile()
    CompiledSql(sql='SELECT * FROM users WHERE id = ?', params=(123,))

    Templates nest:
    >>> age = 18
    >>> adults = template(t"SELECT * FROM people WHERE age >= {age}")
    >>> template(t"SELECT name FROM ({adults})").compile()
    CompiledSql(sql='SELECT name FROM (SELECT * FROM people WHERE age >= ?)', params=(18,))

    We also support iterables of strings and Interpolations/Params/etc,
    which will be joined together into a single template.
    This is useful for versions of python before 3.14 that don't have tstrings,
    since it allows you to build up a template from smaller pieces:

    >>> template("SELECT * FROM (", adults, ") WHERE age >= ", age).compile()
    CompiledSql(sql='SELECT * FROM (SELECT * FROM people WHERE age >= ?) WHERE age >= ?', params=(18, 18))

    You can define evaluation logic for your custom types by defining a `.__sql_template__()` method.
    If this method is defined, the result of that call will be used to create the template.
    The method is called when the template is compiled, not when it is built,
    so the compiled SQL reflects the object's state at compile time.

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
    CompiledSql(sql='SELECT * FROM users WHERE id = ?', params=(123,))
    """
    expanded: list[str | IntoInterpolation] = []
    for part in parts:
        expanded.extend(_expand_part(part))
    return SqlTemplate(*expanded)


def _as_template(obj: object) -> SqlTemplate:
    """Return obj if it is a SqlTemplate, so its creation site is kept, else template(obj)."""
    if isinstance(obj, SqlTemplate):
        return obj
    return template(obj)


def expand(obj: str | IntoInterpolation | Param | SupportsSqlTemplate | object, /) -> FlatTemplate:
    """Expand all interpolations, recursively, into a FlatTemplate.

    This is the expansion described in SPEC.md. It does not depend on the database or driver;
    render() then renders the result in a renderer's syntax.
    `obj` is anything template() accepts as a single part, so an iterable joins several parts.
    Calls `__sql_template__()` on any objects that implement it.

    >>> expand(["SELECT * FROM people WHERE age >= ", param(18, "age")])
    FlatTemplate('SELECT * FROM people WHERE age >= ', Param(value=18, name='age', exact=False))
    """
    t = _as_template(obj)
    return FlatTemplate(_expand(t, t._site))  # noqa: SLF001


def render(parts: Iterable[str | Param | Ident | Lit], /, renderer: str | SqlRenderer = "sqlite3") -> CompiledSql:
    """Render expanded parts, as from expand(), into a final SQL string with parameter placeholders, and the params.

    `renderer` is a SqlRenderer, or the name of one in the registry: a driver name or a paramstyle.
    See get_renderer().

    >>> flat = expand(["SELECT * FROM people WHERE age >= ", param(18, "age")])
    >>> render(flat, "asyncpg")
    CompiledSql(sql='SELECT * FROM people WHERE age >= $1', params=(18,))
    """
    if not isinstance(parts, FlatTemplate):
        parts = FlatTemplate(parts)
    return get_renderer(renderer).__sql_render__(parts)


def compile(
    obj: str | IntoInterpolation | Param | SupportsSqlTemplate | object,
    /,
    renderer: str | SqlRenderer = "sqlite3",
) -> CompiledSql:
    """Compile something into a final SQL string with parameter placeholders, and the params.

    Compiling is expand() followed by render() with `renderer`.
    `obj` is anything template() accepts as a single part, so an iterable joins several parts.
    `renderer` is a SqlRenderer, or the name of one in the registry: a driver name or a paramstyle.
    See get_renderer(), and template() for more examples.

    >>> age = 18
    >>> compile(t"SELECT * FROM people WHERE age >= {age}")
    CompiledSql(sql='SELECT * FROM people WHERE age >= ?', params=(18,))
    >>> compile(t"SELECT * FROM people WHERE age >= {age}", "duckdb")
    CompiledSql(sql='SELECT * FROM people WHERE age >= $p0_age', params={'p0_age': 18})
    >>> compile(t"SELECT * FROM people WHERE age >= {age}", "format")
    CompiledSql(sql='SELECT * FROM people WHERE age >= %s', params=(18,))
    >>> parts = ["SELECT * FROM people WHERE age >= ", age]
    >>> compile(parts, "duckdb")
    CompiledSql(sql='SELECT * FROM people WHERE age >= $p0', params={'p0': 18})
    """
    renderer = get_renderer(renderer)
    t = _as_template(obj)
    site = t._site  # noqa: SLF001
    try:
        return render(expand(t), renderer)
    except Exception as e:
        # Errors from expanding interpolations already carry a note naming this site.
        if site and not any(note.endswith(_site_suffix(site)) for note in getattr(e, "__notes__", ())):
            e.add_note(f"while compiling {_site_suffix(site)}")
        raise


def _is_iterable_nonstring(value: object) -> TypeIs[Iterable[object]]:
    return isinstance(value, Iterable) and not isinstance(value, str | bytes)


def _expand_part(part: object) -> Iterable[str | IntoInterpolation]:
    if isinstance(part, SqlTemplate | SupportsSqlTemplate):
        yield DeferredInterpolation(part)
    elif isinstance(part, str):  # noqa: SIM114
        yield part
    elif isinstance(part, IntoInterpolation):
        yield part
    elif (p := _as_param(part)) is not None:
        yield ParamInterpolation(p)
    elif (ident_or_lit := _as_ident_or_lit(part)) is not None:
        yield ItemInterpolation(ident_or_lit)
    elif is_compiled_sql(part):
        msg = (
            f"Cannot embed compiled SQL ({type(part).__name__}) in a template, since its placeholders are already"
            " rendered. Pass the template it was compiled from, or wrap it in param() to bind it as a value."
        )
        raise TypeError(msg)
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


class ItemInterpolation:
    """Implements the IntoInterpolation protocol for an Ident or Lit, which expands to itself."""

    value: object
    expression: str | None
    conversion: Literal["s", "r", "a"] | None
    format_spec: str

    def __init__(self, item: Ident | Lit) -> None:
        self.value = item
        self.expression = None
        self.conversion = None
        self.format_spec = ""

    def __repr__(self) -> str:
        return repr(self.value)


class DeferredInterpolation:
    """Implements the IntoInterpolation protocol for a SqlTemplate or SupportsSqlTemplate, expanded at compile time."""

    value: object
    expression: str | None
    conversion: Literal["s", "r", "a"] | None
    format_spec: str

    def __init__(self, value: SqlTemplate | SupportsSqlTemplate) -> None:
        self.value = value
        self.expression = None
        self.conversion = None
        self.format_spec = ""

    def __repr__(self) -> str:
        return repr(self.value)


def _expand(
    parts: Iterable[str | IntoInterpolation], site: tuple[str, int] | None = None
) -> list[str | Param | Ident | Lit]:
    """Expand a stream of strings and Interpolations into SQL text, Params, Idents, and Lits, recursively.

    `site` is where the enclosing SqlTemplate was created, if known.
    Errors get a note naming the interpolation that failed and that site,
    since expansion happens at compile time, far from where the template was written.
    """
    expanded: list[str | Param | Ident | Lit] = []
    for part in parts:
        if isinstance(part, str):
            expanded.append(part)
            continue
        try:
            expanded.extend(_expand_interpolation(part))
        except Exception as e:
            what = f"{{{part.expression}}}" if part.expression else f"{type(part.value).__name__} object"
            where = f" in {_site_suffix(site)}" if site else ""
            e.add_note(f"while expanding {what}{where}")
            raise
    return expanded


def _expand_interpolation(interp: IntoInterpolation) -> Iterable[str | Param | Ident | Lit]:
    value = interp.value
    if (as_param := _as_param(value)) is not None:
        # A Param passed directly to template() keeps its own settings.
        # A named Param written inside a t-string keeps its name exactly.
        if isinstance(interp, ParamInterpolation):
            return (as_param,)
        if as_param.name is None:
            return (param(as_param.value),)
        return (param(as_param.value, name=as_param.name, exact=True),)
    if (item := _as_ident_or_lit(value)) is not None:
        return (item,)

    # A conversion (!s, !r, !a) means "insert as raw SQL, not as a param".
    # This is for things that cannot be params or Idents, such as SQL keywords.
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

    # The same format specs as psycopg: `:i` for an identifier, `:l` for a literal.
    # Other format specs are reserved, and currently ignored.
    if interp.format_spec == "i":
        if not isinstance(value, str):
            msg = f"{{{interp.expression}:i}} needs a str to use as an identifier, got {type(value).__name__}."
            raise TypeError(msg)
        return (Ident(value),)
    if interp.format_spec == "l":
        return (Lit(value),)

    if isinstance(value, str):
        # Do NOT pass to _expand_part(), since that would treat it as raw SQL.
        return (param(value, name=interp.expression),)
    parts: tuple[str | IntoInterpolation, ...]
    site: tuple[str, int] | None
    if isinstance(value, SqlTemplate):
        parts, site = tuple(value), value._site  # noqa: SLF001
    elif isinstance(value, SupportsSqlTemplate):
        parts, site = tuple(_expand_part(value.__sql_template__())), None
    else:
        parts, site = tuple(_expand_part(value)), None
    # If the value expands to a single unnamed param, name it after the interpolation's expression,
    # eg `{age}` compiles to `$p0_age` rather than `$p0`.
    non_empty = [part for part in parts if part != ""]
    if (
        len(non_empty) == 1
        and isinstance(only := non_empty[0], ParamInterpolation)
        and isinstance(p := only.value, Param)
        and p.name is None
    ):
        return (param(p.value, name=interp.expression),)
    # Nested template, eg a subquery.
    return _expand(parts, site)


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
    """A sequence of strings and Interpolations.

    Interpolations are expanded into SQL text, Params, Idents, and Lits when the template is compiled.
    The template records the file and line where it was created,
    so that errors raised while compiling can point back to it.
    """

    def __init__(self, *parts: str | IntoInterpolation) -> None:
        for part in parts:
            if not isinstance(part, str | IntoInterpolation):
                # Guard for untyped callers; mypy considers this unreachable given the declared parameter type.
                msg = f"Unexpected part type in SqlTemplate: {type(part).__name__}. Expected str or IntoInterpolation."
                raise TypeError(msg)
        self.strings, self.interpolations = parse_parts(parts)
        self._site = _caller_site()

    def __iter__(self) -> Iterator[str | IntoInterpolation]:
        """Iterate over the strings and interpolations in order."""
        for s, i in zip(self.strings, self.interpolations, strict=False):
            yield s
            yield i
        yield self.strings[-1]

    def __sql_template__(self, /, **future_kwargs: object) -> tuple[str | IntoInterpolation, ...]:
        """Return the strings and interpolations, so a SqlTemplate is composable like any other object."""
        return tuple(self)

    def expand(self) -> FlatTemplate:
        """Same as `expand(self)`."""
        return expand(self)

    def compile(self, renderer: str | SqlRenderer = "sqlite3") -> CompiledSql:
        """Same as `compile(self, renderer)`."""
        return compile(self, renderer)

    def __str__(self) -> Never:
        msg = "SqlTemplate cannot be directly converted to a string, since it may contain parameters. Please call .compile() first."  # noqa: E501
        raise NotImplementedError(msg)

    def __repr__(self) -> str:
        part_strings = [repr(part) for part in self]
        return f"SqlTemplate({', '.join(part_strings)})"


def _caller_site() -> tuple[str, int] | None:
    """Return the (filename, lineno) of the nearest caller outside this module."""
    frame: FrameType | None = sys._getframe(1)  # noqa: SLF001
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:
        return None
    return frame.f_code.co_filename, frame.f_lineno


def _site_suffix(site: tuple[str, int]) -> str:
    """Return the end of an error note naming where a template was created.

    Every note about a template ends with this, so compile() can tell whether one is already there.
    """
    filename, lineno = site
    return f"SqlTemplate created at {filename}:{lineno}"


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


@runtime_checkable
class SqlRenderer(Protocol):
    """Renders a FlatTemplate into a CompiledSql for a particular database and driver.

    The renderer decides everything that depends on the database and driver:
    which placeholder syntax params use, whether params are a dict or a tuple,
    and how Idents and Lits are quoted. Most renderers call render_parts() with their own hooks.

    A renderer opts in by defining `__sql_render__`, so a plain function or other callable is not one by accident.
    `**future_kwargs` is reserved so that options can be passed to renderers later
    without breaking existing ones. Implementations must accept and may ignore them.
    """

    def __sql_render__(self, parts: FlatTemplate, /, **future_kwargs: object) -> CompiledSql:
        """Render parts into a final SQL string with parameter placeholders, and the params."""


def render_parts(  # noqa: PLR0913
    parts: Iterable[str | Param | Ident | Lit],
    /,
    *,
    placeholder: Callable[[str, int], str],
    named: bool,
    escape_percent: bool = False,
    quote_ident: Callable[[str], str] | None = None,
    render_lit: Callable[[object], str] | None = None,
) -> CompiledSql:
    """Render expanded parts, as from expand(), into a CompiledSql.

    This does the work shared by all renderers: naming params, checking names, and collecting values.
    The caller decides the syntax:

    - `placeholder(name, position)` returns the SQL text for a param, given its name and 1-based position.
    - `named=True` returns params as a dict keyed by name, and raises ValueError on duplicate names.
      `named=False` returns them as a tuple in order.
    - `escape_percent=True` escapes literal `%` in the SQL text as `%%`, for drivers that use `%` placeholders.
    - `quote_ident(name)` returns the quoted SQL text for an Ident.
      The default uses SQL-standard double quotes, doubling any `"` in the name.
    - `render_lit(value)` returns the SQL text for a Lit's value, and raises if it cannot be written as a literal.
      The default writes None, bool, int, finite float, and finite Decimal as SQL keywords and numbers,
      and str in single quotes, doubling any `'`. Negative numbers are parenthesized, so `x -{n:l}` cannot
      become a `--` comment.

    Parts may also be parameter, identifier, and literal objects from other libraries, as defined in SPEC.md.

    >>> render_parts(["SELECT ", param(1, "x")], placeholder=lambda name, position: f"@{name}", named=True)
    CompiledSql(sql='SELECT @p0_x', params={'p0_x': 1})
    >>> render_parts(["SELECT ", lit(-1), " AS ", ident('a"b')], placeholder=lambda name, position: "?", named=False)
    CompiledSql(sql='SELECT (-1) AS "a""b"', params=())
    """
    if quote_ident is None:
        quote_ident = _quote_ident_ansi
    if render_lit is None:
        render_lit = _render_lit_ansi
    sql_parts: list[str] = []
    params_items: list[tuple[str, object]] = []

    def add_sql(text: str) -> None:
        sql_parts.append(text.replace("%", "%%") if escape_percent else text)

    def next_name(suffix: str | None = None) -> str:
        # Exact params still count, so we get p0, my_param, p2, p3_x, ...
        base = f"p{len(params_items)}"
        if suffix:
            return f"{base}_{suffix}"
        return base

    for part in parts:
        if isinstance(part, str):
            add_sql(part)
        elif (as_ident := _as_ident(part)) is not None:
            add_sql(quote_ident(as_ident.name))
        elif (as_lit := _as_lit(part)) is not None:
            add_sql(render_lit(as_lit.value))
        elif (as_param := _as_param(part)) is not None:
            if as_param.name is None:
                param_name = next_name()
            elif as_param.exact:
                param_name = as_param.name
            else:
                param_name = next_name(_sanitize_name_suffix(as_param.name))
            assert_param_name_legal(param_name)
            sql_parts.append(placeholder(param_name, len(params_items) + 1))
            params_items.append((param_name, as_param.value))
        else:
            msg = f"Cannot render {type(part).__name__}. Expected SQL text, a Param, an Ident, or a Lit."
            raise TypeError(msg)
    sql = "".join(sql_parts)
    if not named:
        return CompiledSql(sql=sql, params=tuple(value for _, value in params_items))
    param_name_counts = Counter(name for name, _ in params_items)
    dupes = [name for name, count in param_name_counts.items() if count > 1]
    if dupes:
        msg = f"Duplicate parameter names found: {dupes}. Please ensure all parameter names are unique."
        raise ValueError(msg)
    return CompiledSql(sql=sql, params=dict(params_items))


def _quote_ident_ansi(name: str) -> str:
    # str.__str__ reads the characters themselves, ignoring any overrides on a str subclass.
    text = str.__str__(name)
    return '"' + text.replace('"', '""') + '"'


def _quote_ident_mysql(name: str) -> str:
    text = str.__str__(name)
    return "`" + text.replace("`", "``") + "`"


def _render_lit_scalar(value: object) -> str | None:
    """Return the SQL for None, a bool, or a number, or None if value is none of those."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    # Call the base class methods, so a subclass cannot change the text that lands in the SQL.
    if isinstance(value, int):
        text = int.__repr__(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            msg = f"Cannot write {value!r} as a SQL literal. Use a Param instead."
            raise ValueError(msg)
        text = float.__repr__(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            msg = f"Cannot write {value!r} as a SQL literal. Use a Param instead."
            raise ValueError(msg)
        text = Decimal.__str__(value)
    else:
        return None
    return f"({text})" if text.startswith("-") else text


def _lit_text(value: object) -> str:
    """Return value's characters, for a string literal, or raise if value cannot be one."""
    if not isinstance(value, str):
        msg = (
            f"Cannot write {type(value).__name__} as a SQL literal. "
            "Supported types are None, bool, int, float, Decimal, and str. Use a Param instead."
        )
        raise TypeError(msg)
    text = str.__str__(value)
    if "\x00" in text:
        msg = "SQL string literals cannot contain NUL characters. Use a Param instead."
        raise ValueError(msg)
    return text


def _render_lit_ansi(value: object) -> str:
    if (scalar := _render_lit_scalar(value)) is not None:
        return scalar
    return "'" + _lit_text(value).replace("'", "''") + "'"


def _render_lit_postgres(value: object) -> str:
    # Postgres treats backslashes in '...' as escapes if standard_conforming_strings is off.
    # E'...' always treats them as escapes, so it reads the same under either setting.
    if (scalar := _render_lit_scalar(value)) is not None:
        return scalar
    text = _lit_text(value)
    if "\\" in text:
        return "E'" + text.replace("\\", "\\\\").replace("'", "''") + "'"
    return "'" + text.replace("'", "''") + "'"


def _render_lit_mysql(value: object) -> str:
    # MySQL treats backslashes in '...' as escapes, unless the NO_BACKSLASH_ESCAPES SQL mode is set.
    if (scalar := _render_lit_scalar(value)) is not None:
        return scalar
    return "'" + _lit_text(value).replace("\\", "\\\\").replace("'", "''") + "'"


Quoting = Literal["ansi", "postgres", "mysql"]
"""How DbApiRenderer writes Idents and Lits.

| quoting    | Ident    | str Lit                                    | used by                  |
|------------|----------|--------------------------------------------|--------------------------|
| `ansi`     | `"name"` | `'text'`                                   | SQLite, DuckDB, Oracle   |
| `postgres` | `"name"` | `'text'`, or `E'text'` if it has a `\\`     | Postgres                 |
| `mysql`    | `` `name` `` | `'text'`, with `\\` escaped as `\\\\`        | MySQL, MariaDB           |

`mysql` assumes MySQL's default SQL mode. With NO_BACKSLASH_ESCAPES set, backslashes in str Lits come out doubled.
"""

_QUOTING: dict[Quoting, tuple[Callable[[str], str], Callable[[object], str]]] = {
    "ansi": (_quote_ident_ansi, _render_lit_ansi),
    "postgres": (_quote_ident_ansi, _render_lit_postgres),
    "mysql": (_quote_ident_mysql, _render_lit_mysql),
}

_PLACEHOLDERS: dict[ParamStyle, Callable[[str, int], str]] = {
    "dollar": lambda name, _position: f"${name}",
    "named": lambda name, _position: f":{name}",
    "pyformat": lambda name, _position: f"%({name})s",
    "qmark": lambda _name, _position: "?",
    "format": lambda _name, _position: "%s",
    "numeric": lambda _name, position: f":{position}",
    "dollar_numeric": lambda _name, position: f"${position}",
}


@dataclasses.dataclass(frozen=True)
class DbApiRenderer:
    """Renders to a PEP 249 paramstyle, quoting Idents and Lits as `quoting` says. See Quoting.

    >>> template("SELECT ", param(1)).compile(DbApiRenderer(paramstyle="numeric"))
    CompiledSql(sql='SELECT :1', params=(1,))
    >>> template("SELECT * FROM ", ident("order")).compile(DbApiRenderer("format", quoting="mysql"))
    CompiledSql(sql='SELECT * FROM `order`', params=())
    """

    paramstyle: ParamStyle
    quoting: Quoting = "ansi"

    def __post_init__(self) -> None:
        """Ensure paramstyle and quoting are known."""
        if self.paramstyle not in PARAMSTYLES:
            msg = f"Unknown paramstyle: {self.paramstyle!r}. Expected one of {PARAMSTYLES}."
            raise ValueError(msg)
        if self.quoting not in _QUOTING:
            msg = f"Unknown quoting: {self.quoting!r}. Expected one of {tuple(_QUOTING)}."
            raise ValueError(msg)

    def __sql_render__(self, parts: FlatTemplate, /, **future_kwargs: object) -> CompiledSql:
        """Render parts into a final SQL string with parameter placeholders, and the params."""
        quote_ident, render_lit = _QUOTING[self.quoting]
        return render_parts(
            parts,
            placeholder=_PLACEHOLDERS[self.paramstyle],
            named=self.paramstyle in _NAMED_STYLES,
            escape_percent=self.paramstyle in ("pyformat", "format"),
            quote_ident=quote_ident,
            render_lit=render_lit,
        )


# Keyed by the driver's import name, which is what appears in the caller's code, eg `mysql.connector`, not its
# PyPI name `mysql-connector-python`. Each uses the driver's own `paramstyle`, except duckdb, which declares
# `qmark` but also accepts `$name`; `dollar` keeps param names visible in the SQL.
_RENDERERS: dict[str, SqlRenderer] = {
    "duckdb": DbApiRenderer("dollar"),
    "sqlite3": DbApiRenderer("qmark"),
    "psycopg": DbApiRenderer("pyformat", quoting="postgres"),
    "psycopg2": DbApiRenderer("pyformat", quoting="postgres"),
    "pg8000": DbApiRenderer("format", quoting="postgres"),
    "asyncpg": DbApiRenderer("dollar_numeric", quoting="postgres"),
    "pymysql": DbApiRenderer("pyformat", quoting="mysql"),
    "MySQLdb": DbApiRenderer("format", quoting="mysql"),
    "mysql.connector": DbApiRenderer("pyformat", quoting="mysql"),
    "oracledb": DbApiRenderer("named"),
    # Paramstyle names, for drivers not listed above or to pick a driver's non-default style.
    **{paramstyle: DbApiRenderer(paramstyle) for paramstyle in PARAMSTYLES},
}


def register_renderer(name: str, renderer: SqlRenderer, *, overwrite: bool = False) -> None:
    """Register a renderer under a name, so that `compile(obj, name)` uses it.

    The registry is shared by everything in the process, so be polite:
    register only names you own, such as your own package or driver name.

    Raises ValueError if the name is already registered, unless overwrite=True.

    >>> register_renderer("myapp.sqlite3_named", DbApiRenderer("named"))
    >>> compile(["SELECT ", param(1, "x")], "myapp.sqlite3_named")
    CompiledSql(sql='SELECT :p0_x', params={'p0_x': 1})
    """
    if not isinstance(renderer, SqlRenderer):
        msg = f"Expected a SqlRenderer, got {type(renderer).__name__}."
        raise TypeError(msg)
    if name in _RENDERERS and not overwrite:
        msg = f"A renderer is already registered as {name!r}. Pass overwrite=True to replace it."
        raise ValueError(msg)
    _RENDERERS[name] = renderer


def get_renderer(renderer: str | SqlRenderer, /) -> SqlRenderer:
    """Look up a renderer by name in the registry. A SqlRenderer is returned as is.

    The built-in names are:
    - driver import names: duckdb, sqlite3, psycopg, psycopg2, pg8000, asyncpg,
      pymysql, MySQLdb, mysql.connector, and oracledb. See the README for the paramstyle and quoting each uses.
    - paramstyle names, eg `qmark`, each giving `DbApiRenderer(paramstyle)` with ANSI quoting.
    """
    if isinstance(renderer, SqlRenderer):
        return renderer
    if not isinstance(renderer, str):
        msg = f"Expected a renderer name or a SqlRenderer, got {type(renderer).__name__}."
        raise TypeError(msg)
    try:
        return _RENDERERS[renderer]
    except KeyError:
        msg = f"Unknown renderer: {renderer!r}. Registered renderers are {sorted(_RENDERERS)}."
        raise ValueError(msg) from None
