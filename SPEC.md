# SQL template spec

Status: draft.

This spec lets libraries that build SQL and libraries that execute SQL work together without depending on each other or on `sqltemplatelib`. It has three parts:

```
input ──expansion──▶ flat template ──rendering──▶ sql + params for one driver
      └──────────────── compilation ─────────────────┘
```

1. **Input**: what a *producer* may hand to a consumer, eg t-strings, strings, parameters, identifiers, literals, objects that implement `__sql_template__`, and nested trees of these.
2. **Expansion**: the rules that turn any input into a *flat template*, a flat sequence of SQL text, parameters, identifiers, and literals. The rules are deterministic and do not depend on the database or driver, so every consumer gets the same flat template from the same input. This is what lets an object from one library nest inside a query from another library, executed by a third.
3. **Rendering**: how a *renderer* turns a flat template into SQL and params for one database and driver. Renderers choose their own placeholder syntax, parameter names, and quoting, within rules that keep the query's meaning intact.

*Compilation* is expansion followed by rendering.

A *producer* is anything that creates input: application code writing t-strings, or a library whose objects implement `__sql_template__`. A *consumer* is a library that executes SQL. It compiles the input it is given, either itself or with a renderer and an implementation of expansion such as `sqltemplatelib`. Producers never expand anything themselves.

Everything here is duck-typed. No side needs to import `sqltemplatelib`.

## 1. Input

### TemplateLike

A *TemplateLike* is any of:

- a `str`: SQL text, interpreted literally.
- an interpolation: a `string.templatelib.Interpolation`, or any object with `value`, `expression`, `conversion`, and `format_spec` attributes.
- a `string.templatelib.Template` (a t-string), or any other iterable of `str` and interpolations.
- a parameter, identifier, or literal object (see below).
- a composable object (see below).
- a non-string iterable of any of the above.
- any other object except compiled SQL: a parameter value.

### Parameter objects

A *parameter object* sets a parameter's value and name explicitly. It is any object whose type has a truthy `__sql_param__` attribute, and which has:

- `value`: the value to bind. It is never expanded, even if it is a template or an iterable.
- `name` (optional, default `None`): a `str` naming the parameter, or `None`.
- `exact` (optional, default `False`): if true, `name` is the parameter's name in named paramstyles, and it must match `[A-Za-z_][A-Za-z0-9_]*`. If false, `name` is a hint the renderer may use or change, eg to make names unique.

The marker is read from the type, not the instance, so an object whose `__getattr__` answers any name is not a parameter object by accident. The same holds for every marker below.

### Identifier objects

An *identifier object* is a name, eg of a table or column, that the renderer quotes in its dialect's syntax. It is any object whose type has a truthy `__sql_ident__` attribute, and which has:

- `name`: a non-empty `str` without NUL characters. It is one identifier: `a.b` is a single name containing a dot. A qualified name is written as separate identifiers joined by SQL text `"."`.

### Literal objects

A *literal object* is a value written into the SQL as a literal, rather than bound as a parameter. It is for places that do not accept parameters, eg DDL. It is any object whose type has a truthy `__sql_lit__` attribute, and which has:

- `value`: the value to write. It is never expanded.

### Composable objects

An object is *composable* if it has a method:

```python
def __sql_template__(self, /, **kwargs) -> TemplateLike: ...
```

- It MUST accept arbitrary keyword arguments and MAY ignore them. Keyword names are reserved for future versions of this spec.
- It MUST NOT require positional arguments.
- It returns a TemplateLike.

Consumers call `__sql_template__` during expansion, at execution time, not when the enclosing query is built. A producer MUST NOT assume when it is called or how many times.

### Compiled SQL

*Compiled SQL* is a query already rendered for one driver. It is any object whose type has a truthy `__sql_compiled__` attribute, and which has:

- `sql`: a `str` of SQL with placeholders in one driver's syntax, eg `"SELECT * FROM people WHERE age > $1"`.
- `params`: the values to bind, eg a `dict` for named paramstyles or a `tuple` for positional ones, eg `(18,)`.

Compiled SQL is not input. Its placeholders are in one driver's syntax, and its parameter names may collide with the enclosing query's. See [Consumers](#consumers) for when a consumer may execute it directly.

## 2. Expansion

Expansion turns a TemplateLike into a flat template. It is recursive: whenever a rule says "expand", the result is spliced in place.

### Parts

Each part of the input is checked in this order:

1. Composable: call `__sql_template__()` and expand the result.
2. `str`: SQL text, inserted verbatim.
3. Interpolation: expand by the interpolation rules below.
4. Parameter object: a parameter with the object's `value`, `name`, and `exact`.
5. Identifier or literal object: itself.
6. Compiled SQL: raise an error.
7. Non-string iterable, including a t-string: expand each item in order.
8. Anything else: a parameter with that value and no name.

### Interpolations

A t-string's interpolations follow different rules, since `{x}` usually holds a value and only rarely holds SQL text. For an interpolation with `value`, `expression`, `conversion`, and `format_spec`, the first matching rule applies:

1. `value` is a parameter object: that parameter, except that a `name` is treated as exact. Any `conversion` and `format_spec` are ignored.
2. `value` is an identifier or literal object: itself. Any `conversion` and `format_spec` are ignored.
3. `conversion` is `"s"`, `"r"`, or `"a"`: apply `str` / `repr` / `ascii`, then `format(result, format_spec)`, and insert the result as SQL text. This is for SQL that cannot be a parameter, identifier, or literal, eg a keyword, and it is unsafe for untrusted values.
4. `format_spec` is `"i"`: an identifier named `value`, which must be a `str`.
5. `format_spec` is `"l"`: a literal with value `value`.
6. `value` is a `str`: a parameter, with `expression` as its name hint.
7. `value` is composable or a template: expand it in place.
8. `value` is compiled SQL: raise an error.
9. `value` is a non-string iterable: expand it in place.
10. Otherwise: a parameter, with `expression` as its name hint.

The `i` and `l` format specs match psycopg's. Other format specs without a conversion are currently ignored, and reserved for future versions of this spec.

## 3. The flat template

The result of expansion is a *flat template*: a sequence in which each item is one of

- SQL text, a `str`,
- a parameter, with a `value`, a `name` that is a `str` or `None`, and an `exact` flag,
- an identifier, with a `name`, or
- a literal, with a `value`.

Adjacent SQL text items mean the same as their concatenation, and empty SQL text means nothing. Two flat templates are equivalent if they have the same SQL text between other items, the same identifiers and literals, and the same parameter values in the same order, with the same names on exact parameters. Name hints do not affect equivalence, since renderers may change them.

The flat template is the contract between producers and consumers. A producer can rely on its input meaning the same flat template in every consumer, and a renderer can rely on the flat template containing nothing it must expand further.

## 4. Rendering

A *renderer* turns a flat template into compiled SQL for one database and driver. A renderer is any object with a method:

```python
def __sql_render__(self, parts, /, **kwargs) -> CompiledSql: ...
```

- `parts` is a flat template: any iterable of SQL text and parameter, identifier, and literal objects.
- It MUST accept arbitrary keyword arguments and MAY ignore them. Keyword names are reserved for future versions of this spec.
- It returns compiled SQL.

The rendered SQL is the SQL text in order, with each other item replaced:

- **SQL text** is inserted verbatim, except for escaping that the placeholder syntax requires, eg `%` as `%%` in `format` and `pyformat`.
- **A parameter** becomes a placeholder, which binds the parameter's value unchanged. Converting values to database types is the driver's job.
  - In positional paramstyles, the params follow the order of the parameters, and names are ignored.
  - In named paramstyles, every parameter needs a unique name. An exact parameter MUST get exactly its `name`. A parameter with a name hint MAY get any name derived from it, and one without a name MAY get any name. If the names cannot be made unique without renaming an exact parameter, the renderer MUST raise an error, never silently rename it.
- **An identifier** becomes its `name`, quoted so that the database reads it as exactly that name, whatever characters it contains.
- **A literal** becomes its `value`, written so that the database reads it as exactly that value. A renderer MUST support `None`, `bool`, `int`, finite `float`, finite `decimal.Decimal`, and `str` values without NUL characters, and MAY support others. It MUST raise an error for a value it cannot write exactly, never approximate it. A literal must stay one token wherever it is placed, eg a negative number written after `-` must not start a `--` comment.

Any other item is an error.

Renderers choose the placeholder syntax, how to derive names, and how to quote, since only they know what their database and driver accept. Whatever they choose, the rendered query binds the same values at the same positions, and names the same identifiers and literals, in the same SQL as every other renderer's.

## Consumers

A library that executes SQL accepts any TemplateLike, typically a t-string or a composable object. It compiles it: it expands it into a flat template, then renders that.

If the query itself is compiled SQL and is not composable or a template, the consumer MAY skip compilation and pass its `sql` and `params` to the driver unchanged. The caller compiled it for this driver, so its placeholders are already in the right syntax, and there is no enclosing query for its names to collide with.

A consumer does not need `sqltemplatelib`. One that uses it calls `expand(obj)` for expansion, `render(flat, renderer)` for rendering, or `compile(obj, renderer)` for both. A custom renderer can use `render_parts()`.

## Limitations

Expansion does not know the target database, so a composable object cannot produce SQL specific to one dialect. A library that generates SQL text, eg a dataframe or query-builder library, must be told the dialect some other way, such as an argument to its own constructor. Passing the dialect to `__sql_template__` would require every producer and consumer to agree on dialect names and versions, eg `postgres` versus `postgresql`, and this spec defines no such vocabulary.
