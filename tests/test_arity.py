"""Argument and parameter counting for call-edge scoring.

These counts only ever *rank* edges, never remove them, so the important property is
not accuracy but honesty: every helper here must answer "unknown" rather than guess.
A wrong number reads as positive evidence downstream, an unknown one reads as no
evidence, and the second failure mode is recoverable while the first is not.
"""

from __future__ import annotations

import pytest

from memo.analyzers.base import (
    PF_EXTENSION,
    PF_UNPARSED,
    UNKNOWN_ARITY,
    VARIADIC_MAX,
    arity_fits,
    param_arity,
    scan_arg_counts,
    split_params,
)


def argc_of(text: str, language: str = "csharp", name: str = "f") -> int:
    """The argument count of the first `name(` call in `text`."""
    counts = scan_arg_counts(text, language)
    open_at = text.index(name + "(") + len(name)
    return counts.get(open_at, UNKNOWN_ARITY)


# --- scan_arg_counts -------------------------------------------------------

def test_counts_top_level_commas_only():
    text = "f(g(x), h(y,z));"
    counts = scan_arg_counts(text, "csharp")
    assert counts[text.index("f(") + 1] == 2
    assert counts[text.index("g(") + 1] == 1
    assert counts[text.index("h(") + 1] == 2


def test_ignores_commas_inside_strings():
    assert argc_of('f(x, "a,b,c");') == 2


def test_ignores_commas_inside_csharp_verbatim_strings():
    assert argc_of('f(x, @"a,b""c,d");') == 2


def test_ignores_commas_inside_lambda_braces():
    assert argc_of("f(a, x => { g(1,2); });") == 2


def test_ignores_commas_inside_generics_and_tuples():
    assert argc_of("f(Dictionary<string,int> m, Func<int,int> cb);") == 2


def test_ignores_commas_inside_collection_initialisers():
    assert argc_of("f(new[] { 1, 2, 3 }, y);") == 2


def test_ignores_type_argument_commas_at_a_call_site():
    assert argc_of("f(new Dictionary<string,int>());") == 1
    assert argc_of("f(Map<string,int>(a), b);") == 2


def test_spaced_relational_is_not_read_as_a_generic():
    assert argc_of("f(i < n, j);") == 2


def test_lambda_arrow_does_not_close_a_generic():
    assert argc_of("f(x => y, z);") == 2
    assert argc_of("f(Func<int,int> cb, x => y);") == 2


def test_unbalanced_angle_in_a_group_yields_unknown():
    # `a<b` could be a comparison or the start of a generic; without a type table it is
    # genuinely ambiguous, so claim nothing.
    assert argc_of("f(a<b, c);") == UNKNOWN_ARITY


def test_zero_args_is_zero_not_unknown():
    assert argc_of("f();") == 0
    assert argc_of("f( );") == 0
    assert argc_of("f(\n);") == 0


def test_unbalanced_span_yields_unknown_not_a_wrong_count():
    # The group never closes, so it is absent from the mapping entirely.
    assert argc_of("f(a, b") == UNKNOWN_ARITY


def test_spread_and_splat_args_yield_unknown():
    assert argc_of("f(...args);", "typescript") == UNKNOWN_ARITY
    assert argc_of("f(*a);", "python") == UNKNOWN_ARITY
    assert argc_of("f(**d);", "python") == UNKNOWN_ARITY


def test_named_and_ref_out_args_still_count():
    assert argc_of("f(b: 1, a: 2);") == 2
    assert argc_of("f(ref x, out var y, in z);") == 3


def test_line_comment_commas_are_ignored():
    assert argc_of("f(a, // b, c\n d);") == 2


def test_block_comment_commas_are_ignored():
    assert argc_of("f(a, /* b, c */ d);") == 2


def test_vb_apostrophe_is_a_comment_not_a_string():
    # Read as a char literal, the `'` would swallow the rest of the line and lose the `)`.
    assert argc_of("f(a, b) ' note, with comma\n", "vbnet") == 2


def test_python_hash_comment_commas_are_ignored():
    assert argc_of("f(a, # b, c\n    d)", "python") == 2


def test_span_longer_than_max_is_unknown():
    text = "f(" + "x" * 50 + ")"
    open_at = text.index("f(") + 1
    assert scan_arg_counts(text, "csharp", max_span=10)[open_at] == UNKNOWN_ARITY


def test_multi_line_call_spans_are_counted():
    assert argc_of("f(\n    a,\n    b,\n    c\n);") == 3


def test_scan_is_linear_in_file_size():
    """Guards against a per-site rescan creeping back in.

    Doubling the input must roughly double the work, not quadruple it. Deeply nested
    calls are the shape that punishes a balanced-scan-per-site implementation.
    """
    import time

    unit = "f(g(h(a, b), c), d);\n"
    def elapsed(n):
        text = unit * n
        t0 = time.perf_counter()
        scan_arg_counts(text, "csharp")
        return time.perf_counter() - t0

    elapsed(2000)  # warm up, so import/JIT costs don't land in the ratio
    small, large = elapsed(2000), elapsed(8000)
    assert large < small * 12, f"4x input took {large / max(small, 1e-9):.1f}x time"


# --- split_params ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("int x, string y", 2),
    ('int x, string y = "a,b", params object[] rest', 3),
    ("Dictionary<string,int> m, Func<int,int> f", 2),
    ("{ userId }: UserCardProps", 1),
    ("", 0),
])
def test_split_params_counts(text, expected):
    assert len(split_params(text)) == expected


def test_split_params_falls_back_when_angles_are_unpaired():
    # `a < b` is relational, not generic; the split must still yield two parameters.
    assert len(split_params("int x = a < b, int y")) == 2


# --- param_arity -----------------------------------------------------------

def test_csharp_required_params():
    assert param_arity("public IActionResult Create(OrderDto dto)", "csharp", "method") == (1, 1, 0)


def test_csharp_no_params_is_zero_zero():
    assert param_arity("protected void LogFailure()", "csharp", "method") == (0, 0, 0)


def test_csharp_default_value_lowers_the_minimum():
    pmin, pmax, _ = param_arity('void A(int x, string y = "a,b")', "csharp", "method")
    assert (pmin, pmax) == (1, 2)


def test_csharp_params_array_is_variadic():
    pmin, pmax, _ = param_arity("void A(int x, params object[] rest)", "csharp", "method")
    assert (pmin, pmax) == (1, VARIADIC_MAX)


def test_csharp_extension_method_is_flagged():
    pmin, pmax, flags = param_arity("public static void Ext(this Svc s, int q)",
                                    "csharp", "method")
    assert (pmin, pmax) == (2, 2)
    assert flags & PF_EXTENSION


def test_csharp_generic_return_tuple_is_not_read_as_the_param_list():
    # The first `(` in the text belongs to `<(int,string)>`, not to the parameters.
    got = param_arity("public Task<(int,string)> B(Dictionary<string,int> m, Func<int,int> f)",
                      "csharp", "method")
    assert got == (2, 2, 0)


def test_csharp_generic_constraint_after_params_is_not_swallowed():
    got = param_arity("public void Foo<T>(int x) where T : new()", "csharp", "method")
    assert got == (1, 1, 0)


def test_vb_optional_and_paramarray_widen_the_range():
    pmin, pmax, _ = param_arity(
        'Public Sub A(ByVal x As Integer, Optional ByVal y As String = "z")',
        "vbnet", "method")
    assert (pmin, pmax) == (1, 2)
    pmin, pmax, _ = param_arity("Public Function B(ParamArray items() As Object) As Integer",
                                "vbnet", "method")
    assert (pmin, pmax) == (0, VARIADIC_MAX)


def test_vb_byval_byref_are_stripped_not_counted():
    assert param_arity("Public Shared Function GetCustomer(ByVal id As Integer) As DataRow",
                       "vbnet", "method") == (1, 1, 0)


def test_ts_optional_and_rest_params_widen_the_range():
    pmin, pmax, _ = param_arity("formatName(first: string, last?: string)",
                                "typescript", "function")
    assert (pmin, pmax) == (1, 2)
    pmin, pmax, _ = param_arity("log(msg: string, ...rest: unknown[])",
                                "typescript", "function")
    assert (pmin, pmax) == (1, VARIADIC_MAX)


def test_ts_destructured_param_counts_as_one():
    assert param_arity("UserCard({ userId }: UserCardProps)",
                       "typescript", "component") == (1, 1, 0)


def test_python_arity_is_unknown_in_this_increment():
    """Pinned: `_args_sig` drops defaults, kwonly and posonly params.

    `def build(entries, cache_root=None)` renders as `def build(entries, cache_root)`,
    so a signature-derived minimum of 2 would contradict memo's own `build(entries)`
    call. Until arity comes from the AST, Python must claim nothing.
    """
    assert param_arity("def build(entries, cache_root)", "python", "function") == (
        UNKNOWN_ARITY, UNKNOWN_ARITY, PF_UNPARSED)


def test_non_callable_kinds_claim_nothing():
    # A class's base list is not a parameter list.
    assert param_arity("class OrdersController : ControllerBase", "csharp", "class") == (
        UNKNOWN_ARITY, UNKNOWN_ARITY, PF_UNPARSED)


def test_signature_without_a_paren_group_is_unparsed():
    assert param_arity("Public Sub Refresh", "vbnet", "method") == (
        UNKNOWN_ARITY, UNKNOWN_ARITY, PF_UNPARSED)


# --- arity_fits ------------------------------------------------------------

def test_arity_fits_is_none_when_either_side_is_unknown():
    assert arity_fits(UNKNOWN_ARITY, 1, 1) is None
    assert arity_fits(2, UNKNOWN_ARITY, UNKNOWN_ARITY) is None


def test_arity_fits_within_range():
    assert arity_fits(1, 1, 2) is True
    assert arity_fits(2, 1, 2) is True
    assert arity_fits(3, 1, 2) is False


def test_arity_fits_accepts_variadic():
    assert arity_fits(7, 1, VARIADIC_MAX) is True


def test_extension_method_accepts_one_fewer_argument():
    # `svc.Ext(1)` against `Ext(this Svc s, int q)`.
    assert arity_fits(1, 2, 2, PF_EXTENSION) is True
    assert arity_fits(2, 2, 2, PF_EXTENSION) is True
    assert arity_fits(1, 2, 2, 0) is False
