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
  - another composable object.
  - a non-string iterable of any of the above, concatenated in order.
  - any other object, treated as a single parameter value.

A consumer expands the result recursively.

## Interpolations

When an interpolation inside a t-string is expanded:

1. If `conversion` is `"s"`, `"r"`, or `"a"`: apply `str` / `repr` / `ascii`, then `format(result, format_spec)`, and insert the result as SQL text.
2. If `value` is a `str`: it is a parameter.
3. If `value` is composable, a template, or a non-string iterable: expand it in place.
4. Otherwise `value` is a parameter.

When `format_spec` is set without a conversion, it is currently ignored. It is reserved for future use, eg identifier or literal quoting.

## Consumers

A library that executes SQL accepts a template by:

1. If the object has `.sql` and `.params`, using them as-is.
2. Else if the object has `.compile(*, paramstyle=...)`, calling it with the library's paramstyle and using the result's `.sql` and `.params`.
3. Else if the object is composable or a `string.templatelib.Template`, compiling it (eg with `sqltemplatelib.compile(obj, paramstyle=...)`).

`paramstyle` values follow PEP 249 (`qmark`, `numeric`, `named`, `format`, `pyformat`), plus `dollar` (`$name`) and `dollar_numeric` (`$1`). Named styles produce a `dict` of params, positional styles a `tuple`.
