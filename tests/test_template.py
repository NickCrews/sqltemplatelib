"""Pure-python tests that don't require a compiled extension module."""

from __future__ import annotations

import dataclasses
import sys
from unittest.mock import MagicMock, Mock

import pytest

from sqltemplatelib import (
    CompiledSql,
    IntoInterpolation,
    Param,
    ParamInterpolation,
    SqlTemplate,
    SupportsSqlTemplate,
    compile,
    is_compiled_sql,
    is_sql_param,
    param,
    parse_parts,
    render_parts,
    template,
)

# ── helpers ───────────────────────────────────────────────────────────────────


class FakeInterpolation:
    """Minimal object satisfying IntoInterpolation protocol."""

    def __init__(self, value, *, expression=None, conversion=None, format_spec="") -> None:
        self.value = value
        self.expression = expression
        self.conversion = conversion
        self.format_spec = format_spec


class SimpleRelation:
    """A minimal SupportsSqlTemplate implementation returning a string."""

    def __init__(self, sql: str) -> None:
        self._sql = sql

    def __sql_template__(self, **kwargs) -> str:
        return self._sql


class Cafe:
    """Test for ascii(obj) conversion."""

    def __repr__(self) -> str:
        return "Café"

    @classmethod
    def ascii(cls) -> str:
        return r"Caf\xe9"


# ═══════════════════════════════════════════════════════════════════════════════
# Param
# ═══════════════════════════════════════════════════════════════════════════════


class TestParam:
    def test_basic_creation(self):
        p = Param(value=42)
        assert p.value == 42
        assert p.name is None
        assert p.exact is False

    def test_named_param(self):
        p = Param(value="hello", name="greeting")
        assert p.name == "greeting"
        assert p.exact is False

    def test_positional_creation(self):
        p = Param(42, "greeting", True)
        assert p.value == 42
        assert p.name == "greeting"
        assert p.exact is True

    def test_exact_param_requires_name(self):
        with pytest.raises(ValueError, match="exact=True must have a name"):
            Param(value=1, exact=True)

    def test_exact_param_with_name(self):
        p = Param(value=1, name="x", exact=True)
        assert p.name == "x"
        assert p.exact is True

    def test_frozen(self):
        p = Param(value=1)
        with pytest.raises(AttributeError):
            p.value = 2  # ty:ignore[invalid-assignment]

    def test_param_helper_function(self):
        p = param(42, "answer", exact=True)
        assert isinstance(p, Param)
        assert p.value == 42
        assert p.name == "answer"
        assert p.exact is True

    def test_param_repr(self):
        p = Param(value=42, name="x")
        r = repr(p)
        expected = "Param(value=42, name='x', exact=False)"
        assert r == expected

    def test_param_equality(self):
        """Frozen dataclasses support equality by default."""
        assert Param(value=1, name="x") == Param(value=1, name="x")
        assert Param(value=1) != Param(value=2)

    def test_param_various_value_types(self):
        """Params should accept any Python object as a value."""
        for val in [None, 3.14, True, [1, 2], {"a": 1}, b"bytes", object()]:
            p = Param(value=val)
            assert p.value is val

    def test_param_factory_function_defaults(self):
        """param() should allow defaults for name and exact."""
        expected = Param(value=42, name=None, exact=False)
        assert param(42) == expected
        assert param(value=42) == expected

    def test_param_factory_function_named(self):
        p = param(42, name="x")
        assert p.value == 42
        assert p.name == "x"
        assert p.exact is False

    def test_param_factory_function_exact(self):
        p = param(42, name="x", exact=True)
        assert p.value == 42
        assert p.name == "x"
        assert p.exact is True

    def test_param_factory_function_exact_requires_name(self):
        with pytest.raises(ValueError, match="exact=True must have a name"):
            param(42, exact=True)

    def test_param_factory_function_exact_with_name(self):
        with pytest.raises(TypeError):
            param(42, "x", True)  # ty:ignore[too-many-positional-arguments]


# ═══════════════════════════════════════════════════════════════════════════════
# ParamInterpolation
# ═══════════════════════════════════════════════════════════════════════════════


class TestParamInterpolation:
    def test_wraps_param(self):
        p = param(42, "x")
        pi = ParamInterpolation(p)
        assert pi.value is p
        assert pi.expression == "x"
        assert pi.conversion is None
        assert pi.format_spec == ""

    def test_unnamed_param_expression_is_none(self):
        p = param(42)
        pi = ParamInterpolation(p)
        assert pi.expression is None

    def test_satisfies_into_interpolation_protocol(self):
        pi = ParamInterpolation(param(1, "x"))
        assert isinstance(pi, IntoInterpolation)


# ═══════════════════════════════════════════════════════════════════════════════
# parse_parts
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseParts:
    def test_all_strings(self):
        strings, others = parse_parts(["hello", " ", "world"])
        assert strings == ("hello world",)
        assert others == ()

    def test_all_others(self):
        a, b = object(), object()
        strings, others = parse_parts([a, b])
        # two others → need three string spacers: "", "", ""
        assert strings == ("", "", "")
        assert others == (a, b)

    def test_alternating(self):
        a = object()
        strings, others = parse_parts(["before", a, "after"])
        assert strings == ("before", "after")
        assert others == (a,)

    def test_string_then_other(self):
        a = object()
        strings, others = parse_parts(["sql", a])
        assert strings == ("sql", "")
        assert others == (a,)

    def test_other_then_string(self):
        a = object()
        strings, others = parse_parts([a, "sql"])
        assert strings == ("", "sql")
        assert others == (a,)

    def test_adjacent_strings_merged(self):
        strings, others = parse_parts(["a", "b", "c"])
        assert strings == ("abc",)
        assert others == ()

    def test_adjacent_others_get_empty_string_spacers(self):
        a, b, c = object(), object(), object()
        strings, others = parse_parts([a, b, c])
        assert strings == ("", "", "", "")
        assert others == (a, b, c)

    def test_invariant_strings_one_more_than_others(self):
        """The fundamental invariant: len(strings) == len(others) + 1."""
        cases = [
            ["a"],
            ["a", object()],
            [object(), "a"],
            [object(), object()],
            ["a", object(), "b", object(), "c"],
        ]
        for parts in cases:
            strings, others = parse_parts(parts)
            assert len(strings) == len(others) + 1, f"Failed for parts={parts}"

    def test_empty_input(self):
        """Empty input should return a single empty string and no others."""
        strings, others = parse_parts([])
        assert strings == ("",)
        assert others == ()

    def test_single_string(self):
        strings, others = parse_parts(["SELECT 1"])
        assert strings == ("SELECT 1",)
        assert others == ()

    def test_single_other(self):
        a = object()
        strings, others = parse_parts([a])
        assert strings == ("", "")
        assert others == (a,)


# ═══════════════════════════════════════════════════════════════════════════════
# SqlTemplate construction
# ═══════════════════════════════════════════════════════════════════════════════


class TestSqlTemplateConstruction:
    def test_plain_string(self):
        t = SqlTemplate("SELECT 1")
        assert t.strings == ("SELECT 1",)
        assert t.interpolations == ()

    def test_multiple_strings_merged(self):
        t = SqlTemplate("SELECT ", "1")
        assert t.strings == ("SELECT 1",)
        assert t.interpolations == ()

    def test_with_interpolation(self):
        interp = FakeInterpolation(value=42, expression="x")
        t = SqlTemplate("a ", interp, " b")
        assert len(t.interpolations) == 1
        assert t.interpolations[0] is interp

    def test_bare_param_errors(self):
        with pytest.raises(TypeError, match="Unexpected part type"):
            SqlTemplate("SELECT ", param(42))  # ty:ignore[invalid-argument-type]

    def test_wrapped_param(self):
        wrapped = ParamInterpolation(param(42, "x"))
        t = SqlTemplate("SELECT ", wrapped, " FROM t")
        assert len(t.interpolations) == 1

    def test_rejects_invalid_types(self):
        """Items that are not str, IntoInterpolation, or Param should raise TypeError."""
        with pytest.raises(TypeError, match="Unexpected part type"):
            SqlTemplate(42)  # ty:ignore[invalid-argument-type]

    def test_no_args(self):
        """Empty SqlTemplate should produce a single empty string."""
        t = SqlTemplate()
        assert t.strings == ("",)
        assert t.interpolations == ()


# ═══════════════════════════════════════════════════════════════════════════════
# SqlTemplate iteration and repr
# ═══════════════════════════════════════════════════════════════════════════════


class TestSqlTemplateIterRepr:
    def test_iter_plain_string(self):
        t = SqlTemplate("hello")
        parts = list(t)
        assert parts == ["hello"]

    def test_iter_with_interpolations(self):
        t = template("a ", param(1, "x"), " b")
        parts = list(t)
        assert len(parts) == 3
        assert parts[0] == "a "
        assert isinstance(parts[1], IntoInterpolation)
        assert parts[2] == " b"

    def test_str_raises(self):
        t = SqlTemplate("hello")
        with pytest.raises(NotImplementedError):
            str(t)

    def test_repr_plain_string(self):
        t = SqlTemplate("SELECT 1")
        r = repr(t)
        assert "SqlTemplate" in r
        assert "'SELECT 1'" in r


# ═══════════════════════════════════════════════════════════════════════════════
# template() factory — basic cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestTemplateFactory:
    def test_plain_string(self):
        t = template("SELECT 1")
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT 1", {})
        assert expected == compiled

    def test_param(self):
        t = template(param(42, "answer"))
        compiled = t.compile("duckdb")
        expected = CompiledSql("$p0_answer", {"p0_answer": 42})
        assert expected == compiled

    def test_into_interpolation(self):
        interp = FakeInterpolation(value=42, expression="x")
        t = template(interp)
        compiled = t.compile("duckdb")
        expected = CompiledSql("$p0_x", {"p0_x": 42})
        assert expected == compiled

    def test_supports_sql_template_string(self):
        rel = SimpleRelation("SELECT 1")
        t = template(rel)
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT 1", {})
        assert expected == compiled

    def test_supports_sql_template_returns_iterable(self):
        class MultiPart:
            def __sql_template__(self, **kwargs) -> list[str | Param]:
                return ["SELECT * FROM ", param(42, "x")]

        t = template(MultiPart())
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT * FROM $p0_x", {"p0_x": 42})
        assert expected == compiled

    def test_supports_sql_template_returns_interpolation(self):
        class InterpReturner:
            def __sql_template__(self, **kwargs) -> FakeInterpolation:
                return FakeInterpolation(value="hello", expression="val")

        t = template(InterpReturner())
        compiled = t.compile("duckdb")
        expected = CompiledSql("$p0_val", {"p0_val": "hello"})
        assert expected == compiled

    def test_iterable_of_strings(self):
        t = template("SELECT ", "1")
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT 1", {})
        assert expected == compiled

    def test_iterable_with_params(self):
        t = template("SELECT * FROM t WHERE id = ", param(5, "id"))
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE id = $p0_id", {"p0_id": 5})
        assert expected == compiled

    def test_iterable_with_bare_values(self):
        """Bare values in an iterable should be treated as params."""
        t = template("SELECT * FROM t WHERE id = ", 42)
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE id = $p0", {"p0": 42})
        assert expected == compiled

    def test_bare_value_at_top_level_becomes_param(self):
        """A bare value passed directly to template() becomes a param."""
        t = template(42)  # type: ignore[arg-type]
        compiled = t.compile("duckdb")
        expected = CompiledSql("$p0", {"p0": 42})
        assert expected == compiled

    def test_bytes_not_treated_as_iterable(self):
        """Bytes should not be iterated — _is_iterable excludes bytes."""
        t = template(b"hello")  # type: ignore[arg-type]
        compiled = t.compile("duckdb")
        expected = CompiledSql("$p0", params={"p0": b"hello"})
        assert compiled == expected


# ═══════════════════════════════════════════════════════════════════════════════
# template() factory — t-string integration (Python 3.14+)
# ═══════════════════════════════════════════════════════════════════════════════


class TestTemplateWithInterpolations:
    """Tests for template() with interpolations, using FakeInterpolation to simulate t-string behavior."""

    def test_simple_param(self):
        interp = FakeInterpolation(value=123, expression="user_id")
        compiled = compile(["SELECT * FROM users WHERE id = ", interp, ""], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = $p0_user_id", {"p0_user_id": 123})
        assert expected == compiled

    def test_multiple_params(self):
        name_interp = FakeInterpolation(value="Alice", expression="name")
        age_interp = FakeInterpolation(value=30, expression="age")
        t = template("SELECT * FROM users WHERE name = ", name_interp, " AND age = ", age_interp, "")
        compiled = t.compile("duckdb")
        expected = CompiledSql(
            "SELECT * FROM users WHERE name = $p0_name AND age = $p1_age",
            {"p0_name": "Alice", "p1_age": 30},
        )
        assert expected == compiled

    def test_no_params(self):
        compiled = compile("SELECT 1", renderer="duckdb")
        expected = CompiledSql("SELECT 1")
        assert expected == compiled

    def test_string_conversion_s(self):
        """!s should inline the value as raw SQL (no param)."""
        interp = FakeInterpolation(value="users", expression="table", conversion="s")
        compiled = compile(["SELECT * FROM ", interp, ""], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users")
        assert expected == compiled

    def test_repr_conversion_r(self):
        """!r should inline repr(value) as raw SQL."""
        interp = FakeInterpolation(value="hello", expression="val", conversion="r")
        compiled = compile(["SELECT ", interp, ""], renderer="duckdb")
        # repr of "hello" is "'hello'"
        expected = CompiledSql("SELECT 'hello'")
        assert expected == compiled

    def test_ascii_conversion_a(self):
        interp = FakeInterpolation(value=Cafe(), expression="val", conversion="a")
        compiled = compile(["SELECT ", interp], renderer="duckdb")
        expected = CompiledSql("SELECT " + Cafe.ascii())
        assert expected == compiled

    def test_conversion_ignored_for_param(self):
        """A Param is bound even with !s, so a conversion cannot turn it into raw SQL."""
        interp = FakeInterpolation(value=param(5, "x"), expression="p", conversion="s")
        assert compile(["SELECT ", interp], renderer="duckdb") == CompiledSql("SELECT $x", {"x": 5})

    def test_string_value_becomes_param_not_raw_sql(self):
        """A string interpolation WITHOUT conversion should be a param, not raw SQL."""
        interp = FakeInterpolation(value="Alice", expression="name")
        compiled = compile(["SELECT * FROM users WHERE name = ", interp, ""], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE name = $p0_name", {"p0_name": "Alice"})
        assert expected == compiled

    def test_nested_template_via_supports_sql_template(self):
        """An interpolated SupportsSqlTemplate should be expanded inline."""
        inner = SimpleRelation("SELECT * FROM people")
        interp = FakeInterpolation(value=inner, expression="inner")
        compiled = compile(["SELECT name FROM (", interp, ")"], renderer="duckdb")
        expected = CompiledSql("SELECT name FROM (SELECT * FROM people)", {})
        assert expected == compiled

    def test_nested_chaining(self):
        """Chaining templates: inner template params should propagate."""
        inner = template("SELECT * FROM people WHERE age >= ", 18)
        interp = FakeInterpolation(value=inner, expression="inner")
        t = template("SELECT name FROM (", interp, ")")
        compiled = t.compile("duckdb")
        # Skipping CompiledSql equality: exact param name depends on counter offsets across nesting levels
        assert "SELECT * FROM people WHERE age >= $" in compiled.sql
        assert 18 in compiled.params.values()

    def test_param_name_derived_from_expression(self):
        """Param names should be based on the expression in the interpolation."""
        interp = FakeInterpolation(value=99, expression="my_value")
        t = template("SELECT ", interp, "")
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT $p0_my_value", {"p0_my_value": 99})
        assert expected == compiled

    def test_explicit_param_in_interpolation(self):
        """An explicit Param() used in an interpolation should be treated as a param."""
        p = param(42, "answer")
        interp = FakeInterpolation(value=p, expression="p")
        t = template("SELECT ", interp, "")
        compiled = t.compile("duckdb")
        # Skipping exact equality: param name may come from Param.name or interpolation expression
        assert 42 in compiled.params.values()

    def test_format_spec_on_conversion(self):
        """Format spec combined with conversion: conversion first, then format."""
        interp = FakeInterpolation(value=3.14159, expression="val", conversion="s", format_spec=".5")
        t = template("SELECT ", interp, "")
        compiled = t.compile("duckdb")
        # Python semantics: str(3.14159) = "3.14159", then format("3.14159", ".5") = "3.141"
        expected = CompiledSql("SELECT 3.141")
        assert expected == compiled

    def test_format_spec_without_conversion_is_ignored(self):
        """Format spec without conversion should ideally apply, but currently it's silently dropped."""
        interp = FakeInterpolation(value=3.14159, expression="val", format_spec=".2f")
        t = template("SELECT ", interp, "")
        compiled = t.compile("duckdb")
        expected = CompiledSql("SELECT $p0_val", {"p0_val": 3.14159})
        assert expected == compiled


# ═══════════════════════════════════════════════════════════════════════════════
# resolving interpolations at compile time
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompile:
    def test_plain_string_resolves_to_itself(self):
        t = SqlTemplate("SELECT 1")
        assert t.compile("duckdb") == CompiledSql("SELECT 1")

    def test_conversion_s_resolves_to_string(self):
        interp = FakeInterpolation(value=42, expression="x", conversion="s")
        t = SqlTemplate("SELECT ", interp)
        resolved = t.compile("duckdb")
        expected = CompiledSql("SELECT 42")
        assert resolved == expected

    def test_conversion_r_resolves_to_repr(self):
        interp = FakeInterpolation(value="hello", expression="x", conversion="r")
        t = SqlTemplate("SELECT ", interp)
        resolved = t.compile("duckdb")
        expected = CompiledSql("SELECT 'hello'")
        assert resolved == expected

    def test_conversion_a_resolves_to_ascii(self):
        interp = FakeInterpolation(value=Cafe(), expression="x", conversion="a")
        actual = compile(["SELECT ", interp], renderer="duckdb")
        expected = CompiledSql("SELECT " + Cafe.ascii())
        assert actual == expected

    def test_string_value_without_conversion_becomes_param(self):
        """A string value in an interpolation (no conversion) must become a param, not raw SQL."""
        interp = FakeInterpolation(value="DROP TABLE users", expression="val")
        t = SqlTemplate("SELECT ", interp)
        resolved = t.compile("duckdb")
        expected = CompiledSql("SELECT $p0_val", params={"p0_val": "DROP TABLE users"})
        assert resolved == expected

    def test_nested_supports_sql_template(self):
        rel = SimpleRelation("SELECT 1")
        interp = FakeInterpolation(value=rel, expression="rel")
        t = SqlTemplate("SELECT * FROM (", interp, ")")
        resolved = t.compile("duckdb")
        expected = CompiledSql("SELECT * FROM (SELECT 1)")
        assert resolved == expected

    def test_expression_name_preserved_for_simple_param(self):
        """When interpolation resolves to a single param, the expression name should be kept."""
        interp = FakeInterpolation(value=42, expression="my_age")
        t = SqlTemplate("age = ", interp)
        resolved = t.compile("duckdb")
        expected = CompiledSql("age = $p0_my_age", params={"p0_my_age": 42})
        assert resolved == expected


# ═══════════════════════════════════════════════════════════════════════════════
# render_parts
# ═══════════════════════════════════════════════════════════════════════════════


def dollar_render_parts(parts):
    return render_parts(parts, placeholder=lambda name, _position: f"${name}", named=True)


class TestCompileParts:
    def test_all_strings(self):
        result = dollar_render_parts(["SELECT 1"])
        assert result == CompiledSql("SELECT 1")

    def test_single_unnamed_param(self):
        result = dollar_render_parts(["SELECT ", param(42)])
        expected = CompiledSql("SELECT $p0", params={"p0": 42})
        assert result == expected

    def test_single_named_param(self):
        result = dollar_render_parts(["SELECT ", param(42, "x")])
        expected = CompiledSql("SELECT $p0_x", params={"p0_x": 42})
        assert result == expected

    def test_exact_param_uses_literal_name(self):
        result = dollar_render_parts(["SELECT ", param(42, "my_param", exact=True)])
        expected = CompiledSql("SELECT $my_param", params={"my_param": 42})
        assert result == expected

    def test_multiple_params_numbered_sequentially(self):
        result = dollar_render_parts(["SELECT * WHERE a = ", param(1, "a"), " AND b = ", param(2, "b")])
        expected = CompiledSql("SELECT * WHERE a = $p0_a AND b = $p1_b", params={"p0_a": 1, "p1_b": 2})
        assert result == expected

    def test_duplicate_param_names_raises(self):
        with pytest.raises(ValueError, match="Duplicate parameter names"):
            dollar_render_parts([param(1, "x", exact=True), param(2, "x", exact=True)])

    def test_unnamed_params_get_sequential_names(self):
        result = dollar_render_parts(["a = ", param(1), " AND b = ", param(2)])
        expected = CompiledSql("a = $p0 AND b = $p1", params={"p0": 1, "p1": 2})
        assert result == expected

    def test_exact_param_causes_counter_gap(self):
        """Known issue: exact params still increment the counter, causing non-sequential auto names."""
        result = dollar_render_parts(
            [
                "a = ",
                param(1, "x"),  # → p0_x
                " AND b = ",
                param(2, "b", exact=True),  # → b (exact), but counter increments
                " AND c = ",
                param(3, "y"),  # → p2_y (not p1_y!)
            ]
        )
        expected = CompiledSql("a = $p0_x AND b = $b AND c = $p2_y", params={"p0_x": 1, "b": 2, "p2_y": 3})
        assert result == expected

    def test_empty_parts(self):
        result = dollar_render_parts([])
        expected = CompiledSql("")
        assert result == expected

    def test_adjacent_strings(self):
        result = dollar_render_parts(["SELECT ", "1"])
        expected = CompiledSql("SELECT 1")
        assert result == expected

    def test_param_with_none_value(self):
        result = dollar_render_parts(["SELECT ", param(None, "x")])
        expected = CompiledSql("SELECT $p0_x", params={"p0_x": None})
        assert result == expected


# ═══════════════════════════════════════════════════════════════════════════════
# SupportsSqlTemplate protocol
# ═══════════════════════════════════════════════════════════════════════════════


class TestSupportsSqlTemplate:
    def test_protocol_check(self):
        rel = SimpleRelation("SELECT 1")
        assert isinstance(rel, SupportsSqlTemplate)

    def test_non_implementor(self):
        assert not isinstance("hello", SupportsSqlTemplate)
        assert not isinstance(42, SupportsSqlTemplate)

    def test_dunder_called_at_compile_not_template(self):
        class Tracking:
            def __init__(self) -> None:
                self.called = False

            def __sql_template__(self, **kwargs) -> str:
                self.called = True
                return "SELECT 1"

        obj = Tracking()
        t = template(obj)
        assert not obj.called
        t.compile("duckdb")
        assert obj.called

    def test_compile_reflects_state_at_compile_time(self):
        class Table:
            def __init__(self, name: str) -> None:
                self.name = name

            def __sql_template__(self, **kwargs) -> str:
                return f"SELECT * FROM {self.name}"  # noqa: S608

        table = Table("a")
        t = template(table)
        table.name = "b"
        assert t.compile("duckdb") == CompiledSql("SELECT * FROM b")

    def test_dunder_returning_scalar_inside_interpolation_is_param(self):
        class Wrapper:
            def __sql_template__(self, **kwargs) -> int:
                return 5

        interp = FakeInterpolation(value=Wrapper(), expression="w")
        assert compile(["x = ", interp], renderer="duckdb") == CompiledSql("x = $p0_w", {"p0_w": 5})

    def test_dunder_returning_named_param_inside_interpolation_keeps_name(self):
        class Wrapper:
            def __sql_template__(self, **kwargs) -> Param:
                return param(5, "five", exact=True)

        interp = FakeInterpolation(value=Wrapper(), expression="w")
        assert compile(["x = ", interp], renderer="duckdb") == CompiledSql("x = $five", {"five": 5})

    def test_future_kwargs_accepted(self):
        """Implementations should accept **kwargs for future extensibility."""

        class Strict:
            def __sql_template__(self, **kwargs) -> str:
                assert isinstance(kwargs, dict)
                return "SELECT 1"

        template(Strict())

    def test_returns_interpolation(self):
        """__sql_template__ can return an interpolation."""

        class InterpolationReturner:
            def __sql_template__(self, **kwargs) -> FakeInterpolation:
                return FakeInterpolation(value=42, expression="val")

        t = template(InterpolationReturner())
        compiled = t.compile("duckdb")
        expected = CompiledSql("$p0_val", params={"p0_val": 42})
        assert compiled == expected

    def test_supports_sql_template_priority_over_iterable(self):
        class IterableWithTemplate:
            def __sql_template__(self, **kwargs) -> str:
                return "SELECT 1"

            def __iter__(self) -> list[str]:
                return ["SELECT 2"]

        result = template(IterableWithTemplate()).compile("duckdb")
        expected = CompiledSql("SELECT 1")
        assert result == expected


# ═══════════════════════════════════════════════════════════════════════════════
# IntoInterpolation protocol
# ═══════════════════════════════════════════════════════════════════════════════


class TestIntoInterpolation:
    def test_protocol_check_positive(self):
        interp = FakeInterpolation(value=1, expression="x")
        assert isinstance(interp, IntoInterpolation)

    def test_protocol_check_negative(self):
        assert not isinstance("hello", IntoInterpolation)
        assert not isinstance(42, IntoInterpolation)

    def test_param_interpolation_satisfies(self):
        pi = ParamInterpolation(param(1))
        assert isinstance(pi, IntoInterpolation)


# ═══════════════════════════════════════════════════════════════════════════════
# CompiledSql
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompiledSql:
    def test_basic(self):
        c = CompiledSql("SELECT $p0", params={"p0": 42})
        assert c.sql == "SELECT $p0"
        assert c.params == {"p0": 42}

    def test_positional_params(self):
        c = CompiledSql("SELECT $p0", {"p0": 42})
        assert c.sql == "SELECT $p0"
        assert c.params == {"p0": 42}

    def test_empty_params(self):
        c = CompiledSql("SELECT 1")
        assert c.sql == "SELECT 1"
        assert c.params == {}

    def test_optional_params(self):
        c = CompiledSql("SELECT 1")
        assert c.sql == "SELECT 1"
        assert c.params == {}

    def test_frozen(self):
        c = CompiledSql("SELECT 1")
        with pytest.raises(AttributeError):
            c.sql = "SELECT 2"  # ty:ignore[invalid-assignment]

    def test_equality(self):
        a = CompiledSql("SELECT 1")
        b = CompiledSql("SELECT 1")
        assert a == b

    def test_repr(self):
        c = CompiledSql("SELECT 1")
        expected = "CompiledSql(sql='SELECT 1', params={})"
        assert repr(c) == expected

    def test_str(self):
        c = CompiledSql("SELECT 1")
        with pytest.raises(NotImplementedError):
            str(c)


@dataclasses.dataclass
class SavedReport:
    """A row from an app's saved_reports table.

    It has `sql` and `params` fields, but it is an ordinary value that can be bound as a param,
    eg via a psycopg dumper that adapts it to JSON.
    """

    name: str
    sql: str
    params: dict[str, object]


def sales_report() -> SavedReport:
    return SavedReport("weekly sales", "SELECT * FROM sales WHERE week = $week", {"week": 12})


@dataclasses.dataclass(frozen=True)
class BuiltQuery:
    """Compiled SQL from a query builder that does not import sqltemplatelib."""

    __sql_compiled__ = True  # No annotation, so a class attribute rather than a dataclass field.

    sql: str
    params: tuple[object, ...] = ()


class TestIsCompiledSql:
    def test_compiled_sql(self):
        assert is_compiled_sql(CompiledSql("SELECT 1"))

    def test_foreign_compiled(self):
        assert is_compiled_sql(BuiltQuery("SELECT * FROM users WHERE id = ?", (7,)))

    def test_dataclass_with_sql_and_params_fields(self):
        assert not is_compiled_sql(sales_report())

    @pytest.mark.parametrize("mock", [Mock(), MagicMock(), MagicMock(spec=sales_report())])
    def test_mock(self, mock):
        """Mocks answer every attribute, including `sql` and `params`."""
        assert not is_compiled_sql(mock)

    def test_instance_attribute_is_ignored(self):
        report = sales_report()
        report.__sql_compiled__ = True
        assert not is_compiled_sql(report)

    @pytest.mark.parametrize(
        ("marker", "expected"), [(True, True), (1, True), ("yes", True), (False, False), (0, False), (None, False)]
    )
    def test_marker_truthiness_decides(self, marker, expected):
        class MarkedReport(SavedReport):
            __sql_compiled__ = marker

        assert is_compiled_sql(MarkedReport("q", "SELECT 1", {})) is expected

    @pytest.mark.parametrize("obj", [template("SELECT 1"), "SELECT 1", 1, None, param(1)])
    def test_other_objects(self, obj):
        assert not is_compiled_sql(obj)


class TestEmbeddingCompiledSql:
    def test_part_raises(self):
        with pytest.raises(TypeError, match="Cannot embed compiled SQL \\(CompiledSql\\)"):
            template("SELECT * FROM (", compile("SELECT 1"), ")")

    def test_foreign_part_raises(self):
        with pytest.raises(TypeError, match="Cannot embed compiled SQL \\(BuiltQuery\\)"):
            template("SELECT * FROM (", BuiltQuery("SELECT 1"), ")")

    def test_dataclass_part_is_a_param(self):
        report = sales_report()
        compiled = compile(["INSERT INTO report_archive (report) VALUES (", report, ")"], renderer="duckdb")
        assert compiled == CompiledSql("INSERT INTO report_archive (report) VALUES ($p0)", {"p0": report})

    def test_dataclass_interpolation_is_a_param(self):
        report = sales_report()
        interp = FakeInterpolation(report, expression="report")
        compiled = compile(["INSERT INTO report_archive (report) VALUES (", interp, ")"], renderer="duckdb")
        assert compiled == CompiledSql("INSERT INTO report_archive (report) VALUES ($p0_report)", {"p0_report": report})

    def test_mock_interpolation_is_a_param(self):
        user_id = Mock(name="user_id")
        interp = FakeInterpolation(user_id, expression="user_id")
        compiled = compile(["SELECT * FROM users WHERE id = ", interp], renderer="duckdb")
        assert compiled == CompiledSql("SELECT * FROM users WHERE id = $p0_user_id", {"p0_user_id": user_id})

    def test_specced_magic_mock_is_a_param(self):
        """A MagicMock standing in for a SavedReport has `sql` and `params`, and is bound like the real thing."""
        report = MagicMock(spec=sales_report())
        assert compile(["INSERT INTO report_archive (report) VALUES (", report, ")"], renderer="duckdb").params == {
            "p0": report
        }

    def test_interpolation_raises_at_compile(self):
        t = template("SELECT * FROM (", FakeInterpolation(compile("SELECT 1"), expression="sub"), ")")
        with pytest.raises(TypeError, match="Cannot embed compiled SQL"):
            t.compile("duckdb")

    def test_returned_from_sql_template_raises_at_compile(self):
        class Wrapper:
            def __sql_template__(self, **kwargs):
                return compile("SELECT 1")

        t = template("SELECT * FROM (", Wrapper(), ")")
        with pytest.raises(TypeError, match="Cannot embed compiled SQL"):
            t.compile("duckdb")

    def test_param_binds_it_as_a_value(self):
        c = CompiledSql("SELECT 1")
        assert compile(["SELECT ", param(c)], renderer="duckdb").params == {"p0": c}

    def test_param_object_takes_precedence(self):
        class Both(ForeignParam):
            __sql_compiled__ = True

        assert compile(["SELECT ", Both(1, "x")], renderer="duckdb").params == {"p0_x": 1}

    def test_composable_takes_precedence(self):
        class ComposableBuiltQuery(BuiltQuery):
            def __sql_template__(self, **kwargs):
                return "SELECT 1"

        assert compile(ComposableBuiltQuery("ignored"), renderer="duckdb").sql == "SELECT 1"


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end: compile
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndCompile:
    def test_plain_sql(self):
        result = compile("SELECT * FROM users", renderer="duckdb")
        assert result == CompiledSql("SELECT * FROM users", {})

    def test_strings_joined(self):
        result = compile(["SELECT * FROM ", "users"], renderer="duckdb")
        assert result == CompiledSql("SELECT * FROM users", {})

    def test_param_in_list(self):
        result = compile(["SELECT * FROM users WHERE id = ", param(5, "id")], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = $p0_id", {"p0_id": 5})
        assert expected == result

    def test_multiple_params_in_list(self):
        result = compile(
            ["SELECT * FROM users WHERE name = ", param("Alice", "name"), " AND age > ", param(18, "age")],
            renderer="duckdb",
        )
        expected = CompiledSql(
            "SELECT * FROM users WHERE name = $p0_name AND age > $p1_age",
            {"p0_name": "Alice", "p1_age": 18},
        )
        assert expected == result

    def test_supports_sql_template_end_to_end(self):
        class MyTable:
            def __sql_template__(self, **kwargs) -> str:
                return "SELECT * FROM my_table"

        result = compile(MyTable(), renderer="duckdb")
        expected = CompiledSql("SELECT * FROM my_table", {})
        assert expected == result

    def test_unparameterized_strings(self):
        result = compile(["SELECT * FROM users WHERE id = ", 42, " AND name = ", "Alice"], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = $p0 AND name = Alice", {"p0": 42})
        assert expected == result

    def test_parameterized_strings(self):
        result = compile(["SELECT * FROM users WHERE id = ", 42, " AND name = ", param("Alice")], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = $p0 AND name = $p1", {"p0": 42, "p1": "Alice"})
        assert expected == result

    def test_nested_template_relations(self):
        """The key use case: chaining template queries."""
        inner = template("SELECT * FROM people WHERE age >= ", 18)
        outer = template("SELECT name FROM (", inner, ")")
        result = outer.compile("duckdb")
        # Skipping exact equality: param name depends on counter offsets across nesting levels
        assert "SELECT * FROM people WHERE age >=" in result.sql
        assert "SELECT name FROM (" in result.sql
        assert 18 in result.params.values()
        assert len(result.params) == 1

    def test_deeply_nested_tstrings(self):
        """Three levels of nesting."""
        val = 100
        level1 = template("SELECT * FROM t WHERE x = ", val)
        level2 = template("SELECT * FROM (", level1, ") WHERE y = 1")
        level3 = template("SELECT count(*) FROM (", level2, ")")
        result = level3.compile("duckdb")
        # Skipping exact equality: param name depends on counter offsets across nesting levels
        assert "SELECT * FROM t WHERE x = $" in result.sql
        assert 100 in result.params.values()
        assert len(result.params) == 1

    def test_multiple_nested_with_separate_params(self):
        """Two nested templates each with their own params."""
        a = 1
        b = 2
        t1 = template("SELECT * FROM t1 WHERE a = ", a)
        t2 = template("SELECT * FROM t2 WHERE b = ", b)
        outer = template("SELECT * FROM (", t1, ") JOIN (", t2, ")")
        result = outer.compile("duckdb")
        # Skipping exact equality: param names depend on counter offsets across nesting levels
        assert 1 in result.params.values()
        assert 2 in result.params.values()
        assert len(result.params) == 2

    def test_sql_injection_prevented_by_default(self):
        """Without !s, even SQL-like strings become params, not raw SQL."""
        evil = "1; DROP TABLE users; --"
        interp = FakeInterpolation(value=evil, expression="evil")
        result = compile(["SELECT * FROM users WHERE id = ", interp, ""], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = $p0_evil", {"p0_evil": evil})
        assert expected == result

    def test_sql_injection_possible_with_s_conversion(self):
        """With !s, values ARE inlined — this is intentional but dangerous."""
        evil = "1; DROP TABLE users; --"
        interp = FakeInterpolation(value=evil, expression="evil", conversion="s")
        result = compile(["SELECT * FROM users WHERE id = ", interp, ""], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = 1; DROP TABLE users; --", {})
        assert expected == result

    def test_exact_param_name(self):
        result = compile(["SELECT * FROM t WHERE id = ", param(value=42, name="my_id", exact=True)], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE id = $my_id", {"my_id": 42})
        assert expected == result


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases and known issues
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_string_template(self):
        result = compile("", renderer="duckdb")
        expected = CompiledSql("")
        assert result == expected

    def test_param_with_none_value(self):
        result = compile(param(value=None, name="x"), renderer="duckdb")
        expected = CompiledSql("$p0_x", params={"p0_x": None})
        assert result == expected

    def test_param_with_list_value(self):
        result = compile(param(value=[1, 2, 3], name="ids"), renderer="duckdb")
        expected = CompiledSql("$p0_ids", params={"p0_ids": [1, 2, 3]})
        assert result == expected

    def test_param_with_dict_value(self):
        d = {"key": "value"}
        result = compile(param(value=d, name="data"), renderer="duckdb")
        expected = CompiledSql("$p0_data", params={"p0_data": d})
        assert result == expected

    def test_bool_param(self):
        result = compile(["SELECT * FROM t WHERE active = ", True], renderer="duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE active = $p0", params={"p0": True})
        assert result == expected

    def test_float_param(self):
        interp = FakeInterpolation(value=3.14, expression="threshold")
        result = template("SELECT * FROM t WHERE score > ", interp, "").compile("duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE score > $p0_threshold", params={"p0_threshold": 3.14})
        assert result == expected

    def test_none_param(self):
        interp = FakeInterpolation(value=None, expression="val")
        result = template("SELECT * FROM t WHERE x IS ", interp, "").compile("duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE x IS $p0_val", params={"p0_val": None})
        assert result == expected

    def test_param_object_in_interpolation_preserves_name(self):
        """An explicit Param used in an interpolation should keep its name."""
        interp = FakeInterpolation(value=param(42, "custom_name"), expression="p")
        result = template("SELECT ", interp, "").compile("duckdb")
        expected = CompiledSql("SELECT $custom_name", params={"custom_name": 42})
        assert result == expected

    def test_same_expression_used_twice(self):
        """Using the same expression twice should create two separate params."""
        interp1 = FakeInterpolation(value=42, expression="x")
        interp2 = FakeInterpolation(value=42, expression="x")
        result = template("SELECT * FROM t WHERE a = ", interp1, " AND b = ", interp2, "").compile("duckdb")
        expected = CompiledSql("SELECT * FROM t WHERE a = $p0_x AND b = $p1_x", params={"p0_x": 42, "p1_x": 42})
        assert result == expected

    def test_mixed_conversion_and_param(self):
        """Mix of !s (inlined) and regular (parameterized) in same template."""
        table_interp = FakeInterpolation(value="users", expression="table", conversion="s")
        id_interp = FakeInterpolation(value=5, expression="user_id")
        result = template("SELECT * FROM ", table_interp, " WHERE id = ", id_interp, "").compile("duckdb")
        expected = CompiledSql("SELECT * FROM users WHERE id = $p0_user_id", params={"p0_user_id": 5})
        assert result == expected


# ═══════════════════════════════════════════════════════════════════════════════
# Conversion semantics — documenting current (potentially incorrect) behavior
# ═══════════════════════════════════════════════════════════════════════════════


class TestConversionSemantics:
    """Verify conversion + format_spec follows Python f-string semantics."""

    def test_s_conversion_on_int(self):
        interp = FakeInterpolation(value=42, expression="x", conversion="s")
        actual = compile(interp, renderer="duckdb")
        expected = CompiledSql("42")
        assert actual == expected

    def test_r_conversion_on_string(self):
        """repr('hello') = "'hello'"."""
        interp = FakeInterpolation(value="hello", expression="x", conversion="r")
        actual = compile(interp, renderer="duckdb")
        expected = CompiledSql("'hello'")
        assert actual == expected

    def test_r_conversion_on_int(self):
        """repr(42) = '42', no quotes."""
        interp = FakeInterpolation(value=42, expression="x", conversion="r")
        actual = compile(interp, renderer="duckdb")
        expected = CompiledSql("42")
        assert actual == expected

    def test_s_conversion_with_format_spec(self):
        """Conversion first, then format_spec: str(3.14159) then format with '.5' truncates."""
        interp = FakeInterpolation(value=3.14159, expression="x", conversion="s", format_spec=".5")
        actual = compile(interp, renderer="duckdb")
        expected = CompiledSql("3.141")
        assert actual == expected

    def test_r_conversion_with_format_spec(self):
        """Python semantics: repr first, then format."""
        interp = FakeInterpolation(value="hi", expression="x", conversion="r", format_spec=".4")
        actual = compile(interp, renderer="duckdb")
        expected = CompiledSql("'hi'")
        assert actual == expected

    def test_format_spec_ignored_for_parameterized_values(self):
        """When no conversion is specified, format_spec is silently ignored — the value is parameterized as-is."""
        interp = FakeInterpolation(value=3.14159, expression="x", format_spec=".2f")
        actual = compile(interp, renderer="duckdb")
        expected = CompiledSql("$p0_x", params={"p0_x": 3.14159})
        assert actual == expected


# ═══════════════════════════════════════════════════════════════════════════════
# Error notes pointing at where a template was created
# ═══════════════════════════════════════════════════════════════════════════════


class Exploding:
    def __sql_template__(self, **kwargs) -> str:
        msg = "boom"
        raise RuntimeError(msg)


class TestErrorNotes:
    def test_note_names_interpolation_and_creation_site(self):
        interp = FakeInterpolation(value=Exploding(), expression="table")
        t = SqlTemplate("SELECT * FROM ", interp)
        line = sys._getframe().f_lineno - 1
        with pytest.raises(RuntimeError, match="boom") as exc_info:
            t.compile("duckdb")
        assert exc_info.value.__notes__ == [f"while expanding {{table}} in SqlTemplate created at {__file__}:{line}"]

    def test_template_factory_records_caller_not_library(self):
        t = template("SELECT * FROM ", Exploding())
        line = sys._getframe().f_lineno - 1
        with pytest.raises(RuntimeError) as exc_info:
            t.compile("duckdb")
        assert exc_info.value.__notes__ == [
            f"while expanding Exploding object in SqlTemplate created at {__file__}:{line}"
        ]

    def test_nested_templates_add_a_note_per_level(self):
        inner = template("SELECT * FROM ", Exploding())
        inner_line = sys._getframe().f_lineno - 1
        outer = SqlTemplate("SELECT * FROM (", FakeInterpolation(value=inner, expression="inner"), ")")
        outer_line = sys._getframe().f_lineno - 1
        with pytest.raises(RuntimeError) as exc_info:
            outer.compile("duckdb")
        assert exc_info.value.__notes__ == [
            f"while expanding Exploding object in SqlTemplate created at {__file__}:{inner_line}",
            f"while expanding {{inner}} in SqlTemplate created at {__file__}:{outer_line}",
        ]

    def test_compile_error_notes_creation_site(self):
        t = template(param(1, "x", exact=True), param(2, "x", exact=True))
        line = sys._getframe().f_lineno - 1
        with pytest.raises(ValueError, match="Duplicate parameter names") as exc_info:
            t.compile("duckdb")
        assert exc_info.value.__notes__ == [f"while compiling SqlTemplate created at {__file__}:{line}"]


# ═══════════════════════════════════════════════════════════════════════════════
# SPEC.md interop
# ═══════════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class ForeignParam:
    """A parameter object from a library that does not import sqltemplatelib."""

    __sql_param__ = True  # No annotation, so a class attribute rather than a dataclass field.

    value: object
    name: str | None = None
    exact: bool = False


class TestIsSqlParam:
    def test_param(self):
        assert is_sql_param(param(1))

    def test_foreign_param(self):
        assert is_sql_param(ForeignParam(1))

    @pytest.mark.parametrize(("marker", "expected"), [(1, True), ("yes", True), (0, False), (None, False)])
    def test_marker_truthiness_decides(self, marker, expected):
        class Marked(ForeignParam):
            __sql_param__ = marker

        assert is_sql_param(Marked(1)) is expected

    def test_param_type_itself_is_not_a_param(self):
        assert not is_sql_param(Param)
        assert compile(["SELECT ", Param], renderer="duckdb").params == {"p0": Param}

    def test_instance_attribute_is_ignored(self):
        report = sales_report()
        report.__sql_param__ = True
        assert not is_sql_param(report)

    @pytest.mark.parametrize("mock", [Mock(), MagicMock()])
    def test_mock(self, mock):
        """Mocks answer every attribute, including `value`, `name`, and `exact`."""
        assert not is_sql_param(mock)

    @pytest.mark.parametrize("obj", [template("SELECT 1"), "SELECT 1", 1, None, CompiledSql("SELECT 1")])
    def test_other_objects(self, obj):
        assert not is_sql_param(obj)


class TestForeignParam:
    def test_part(self):
        assert compile(["SELECT ", ForeignParam(1, "x")], renderer="duckdb") == CompiledSql("SELECT $p0_x", {"p0_x": 1})

    def test_exact(self):
        assert compile(["SELECT ", ForeignParam(1, "x", exact=True)], renderer="duckdb") == CompiledSql(
            "SELECT $x", {"x": 1}
        )

    def test_list_value_is_one_param(self):
        assert compile(["SELECT ", ForeignParam([1, 2])], renderer="duckdb").params == {"p0": [1, 2]}

    def test_truthy_marker(self):
        class TruthyParam(ForeignParam):
            __sql_param__ = 1

        assert compile(["SELECT ", TruthyParam(1)], renderer="duckdb").params == {"p0": 1}

    def test_missing_value_raises(self):
        class NoValue:
            __sql_param__ = True

        with pytest.raises(TypeError, match="NoValue sets __sql_param__ but has no `value` attribute"):
            template("SELECT ", NoValue())

    def test_returned_from_sql_template(self):
        class Age:
            def __sql_template__(self, **kwargs):
                return ("age >= ", ForeignParam(18, "min_age"))

        assert compile(["WHERE ", Age()], renderer="duckdb") == CompiledSql(
            "WHERE age >= $p0_min_age", {"p0_min_age": 18}
        )


def test_sql_template_is_composable():
    t = template("SELECT ", param(1, "x"))
    assert isinstance(t, SupportsSqlTemplate)
    assert template(*t.__sql_template__()).compile("duckdb") == t.compile("duckdb")
