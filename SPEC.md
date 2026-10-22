# The `__sql_template__` protocol

Status: draft.

This document defines how an object makes itself embeddable in a SQL template, and how a SQL-executing library accepts templates. Both sides are duck-typed. Neither needs to import `sqltemplatelib`.

## Producers: `__sql_template__`

An object is *composable* if it has a method:

```python
def __sql_template__(self, /, **kwargs) -> TemplateLike: ...
```

- It MUST accept arbitrary keyword arguments and MAY ignore them. Keyword names are reserved for future versions of this spec.
- It MUST NOT require positional arguments.
- It returns a *TemplateLike*, one of:
  - `str`: SQL text, inserted verbatim.
  - `string.templatelib.Template` (a t-string), or any iterable of `str` and interpolations.
  - `string.templatelib.Interpolation`, or any object with `value`, `expression`, `conversion`, and `format_spec` attributes.
  - a parameter object (see below).
  - another composable object.
  - a non-string iterable of any of the above, concatenated in order.
  - any other object except compiled SQL (see below), treated as a single parameter value.

A consumer expands the result recursively.

## Compiled SQL

*Compiled SQL* is two things:

- `sql`: a `str` of SQL, with placeholders in one driver's syntax, eg "SELECT * FROM table WHERE age > $1"
- `params`: the values to bind, eg a `dict` for named paramstyles or a `tuple` for positional ones, eg `(18, )`

An object is compiled SQL if its type has a truthy `__sql_compiled__` attribute. Having `sql` and `params` attributes alone does not make an object compiled SQL, since mocks and objects with a catch-all `__getattr__` have every attribute.

Compiled SQL cannot be embedded in a template, since its placeholders are in one driver's syntax and its parameter names may collide with the enclosing template's. Wherever expansion would otherwise treat compiled SQL as a single parameter value, the consumer raises an error. Composable objects and templates are expanded as usual, and a parameter object's `value` is bound as usual, even if they are also compiled SQL.

## Parameter objects

A *parameter object* sets a parameter's value and name explicitly. It is any object whose type has a truthy `__sql_param__` attribute, and which has:

- `value`: the value to bind. It is never expanded, even if it is a template or an iterable.
- `name` (optional, default `None`): a `str` naming the parameter, or `None`.
- `exact` (optional, default `False`): if true, `name` is the parameter's name in named paramstyles; it must match `[A-Za-z_][A-Za-z0-9_]*`. If false, `name` is a hint the consumer may use or change, eg to make names unique.

## Interpolations

When an interpolation inside a t-string is expanded:

1. If `value` is a parameter object: it is that parameter, except that a `name` is treated as exact. Any `conversion` is ignored.
2. If `conversion` is `"s"`, `"r"`, or `"a"`: apply `str` / `repr` / `ascii`, then `format(result, format_spec)`, and insert the result as SQL text.
3. If `value` is a `str`: it is a parameter, named after `expression`.
4. If `value` is composable or a template: expand it in place.
5. If `value` is compiled SQL: raise an error.
6. If `value` is a non-string iterable: expand it in place.
7. Otherwise `value` is a parameter, named after `expression`.

When `format_spec` is set without a conversion, it is currently ignored. It is reserved for future use, eg identifier or literal quoting.

## Consumers

A library that executes SQL accepts any TemplateLike, typically a t-string or a composable object. It:

1. Expands it, by the rules above, into a flat sequence of SQL text and parameters. Each parameter has a value, an optional name, and an `exact` flag.
2. Renders that sequence in its own syntax: it joins the SQL text, writes a placeholder for each parameter, and binds the values.

The consumer chooses the placeholder syntax and the parameter names, since only it knows what its database and driver accept. A named paramstyle needs unique names: the consumer derives them from the name hints, and raises if two exact names collide.

If the query itself is compiled SQL and not composable or a template, the consumer may skip both steps and pass its `sql` and `params` to the driver unchanged. The caller compiled it for this driver, so its placeholders are already in the right syntax, and there is no enclosing template for its names to collide with.

A consumer does not need this package. One that uses it calls `template(obj).resolve()` for step 1, and `compile_parts()` or a compiler for step 2.
