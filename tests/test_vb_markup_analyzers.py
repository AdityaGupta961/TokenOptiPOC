"""VB.NET and Razor/ASPX markup constructs.

These matter disproportionately for legacy .NET stacks — properties and events are much
of a VB class's public surface, and a Blazor component's entire behaviour lives in its
`@code` block, which previously produced no symbols whatsoever.
"""

from __future__ import annotations

import pytest

from memo.summarizer import summarize

VB_SRC = """\
Imports System

Public Class OrderHelper

    Public Property CustomerName As String

    Public ReadOnly Property Total As Decimal
        Get
            Return 0
        End Get
    End Property

    Public Event OrderPlaced As EventHandler
    Public Event Progress(ByVal pct As Integer)

    Public Shared Function Lookup(ByVal id As Integer) As DataRow
        Return Nothing
    End Function

    Public Sub Refresh()
        Console.WriteLine("x")
    End Sub

    Public Function MultiLine(
        ByVal a As Integer,
        ByVal b As Integer) As Integer
        Return a + b
    End Function

End Class
"""

RAZOR_SRC = """\
@page "/orders"
@using MyApp.Models
@inject IOrderService Orders

<h3>Orders</h3>
<EditForm Model="@model">
  <InputText @bind-Value="model.Name" />
</EditForm>

@code {
    private OrderModel model = new();

    public string Title { get; set; } = "Orders";

    protected override async Task OnInitializedAsync()
    {
        model = await Orders.LoadAsync();
    }

    private void Save() { }
}
"""

ASPX_SRC = """\
<%@ Page Language="C#" AutoEventWireup="true" %>
<html>
<body>
<script runat="server">
    void Page_Init(object sender, EventArgs e)
    {
        DoWork();
    }
    void DoWork() { }
</script>
<form id="form1" runat="server">
  <asp:Label ID="lblName" runat="server" />
</form>
</body>
</html>
"""


# --- VB.NET -----------------------------------------------------------------

@pytest.fixture(scope="module")
def vb():
    return {s.name: s for s in summarize(VB_SRC, "vbnet").symbols}


@pytest.mark.parametrize("name", [
    "OrderHelper", "CustomerName", "Total",
    "OrderPlaced", "Progress",
    "Lookup", "Refresh", "MultiLine",
])
def test_vb_symbol_found(vb, name):
    assert name in vb


def test_vb_properties_and_events_have_distinct_kinds(vb):
    assert vb["CustomerName"].kind == "property"
    assert vb["Total"].kind == "property"
    assert vb["OrderPlaced"].kind == "event"
    assert vb["Progress"].kind == "event"


def test_vb_auto_property_has_no_extent(vb):
    """It has no `End Property`; searching for one runs into the NEXT property's."""
    assert vb["CustomerName"].end_line == 0


def test_vb_block_property_extent_stops_at_its_own_end_property(vb):
    total = vb["Total"]
    assert (total.line, total.end_line) == (7, 11)


def test_vb_multiline_signature_is_found(vb):
    ml = vb["MultiLine"]
    assert ml.end_line > ml.line


def test_vb_class_spans_all_members(vb):
    cls = vb["OrderHelper"]
    for name in ("CustomerName", "Total", "Lookup", "MultiLine"):
        assert cls.line <= vb[name].line <= cls.end_line


def test_vb_lines_point_at_declarations(vb):
    lines = VB_SRC.splitlines()
    for s in vb.values():
        assert s.name in lines[s.line - 1], f"{s.name} at :{s.line}"


# --- Razor ------------------------------------------------------------------

@pytest.fixture(scope="module")
def razor():
    return {s.name: s for s in summarize(RAZOR_SRC, "markup").symbols}


@pytest.mark.parametrize("name", ["OnInitializedAsync", "Save", "Title"])
def test_razor_code_block_symbols_found(razor, name):
    """Regression: a .razor component produced zero symbols, so nothing it defined was
    routable or present in the call graph."""
    assert name in razor


def test_razor_line_numbers_are_rebased_into_the_outer_file(razor):
    lines = RAZOR_SRC.splitlines()
    for s in razor.values():
        assert s.name in lines[s.line - 1], \
            f"{s.name} reported at :{s.line} -> {lines[s.line - 1]!r}"


def test_razor_override_is_captured_as_a_method(razor):
    assert razor["OnInitializedAsync"].kind == "method"
    assert razor["Title"].kind == "property"


def test_razor_without_code_block_still_works():
    s = summarize("@page \"/x\"\n<h1>hi</h1>\n", "markup")
    assert s is not None


# --- classic ASPX -----------------------------------------------------------

@pytest.fixture(scope="module")
def aspx():
    return {s.name: s for s in summarize(ASPX_SRC, "markup").symbols}


def test_aspx_inline_handlers_and_controls_found(aspx):
    assert "Page_Init" in aspx
    assert "lblName" in aspx


MVC_VIEW_SRC = """\
@model eviCore.Web.BusinessObjects.PatientBO
@using eviCore.Web.Helpers
@inject IPatientService PatientSvc

<h2>@Model.Name</h2>
@Html.Partial("_PatientHeader")
@await Html.PartialAsync("_Address")
@Html.ActionLink("Edit", "Edit", "Patient")

@section Scripts {
  <script src="~/js/patient.js"></script>
}
@section Styles {
}
"""


def test_mvc_razor_view_yields_a_usable_summary():
    """Regression: MVC views have no page directive and often no @code block, so they
    produced zero symbols and the purpose "ASP.NET markup fragment" — nothing routable."""
    s = summarize(MVC_VIEW_SRC, "markup")
    by_name = {x.name: x for x in s.symbols}

    assert "eviCore.Web.BusinessObjects.PatientBO" in by_name
    assert by_name["eviCore.Web.BusinessObjects.PatientBO"].kind == "model"
    assert by_name["PatientSvc"].kind == "injected"
    assert {"Scripts", "Styles"} <= set(by_name)
    assert by_name["Scripts"].kind == "section"
    assert "PatientBO" in s.purpose, s.purpose


def test_mvc_razor_partials_and_actions_become_dependencies():
    """These are real edges between views that nothing else in memo could see."""
    deps = summarize(MVC_VIEW_SRC, "markup").dependencies
    assert "_PatientHeader" in deps
    assert "_Address" in deps
    assert "eviCore.Web.Helpers" in deps
    assert any("Edit" in d for d in deps)


def test_mvc_razor_lines_point_at_declarations():
    lines = MVC_VIEW_SRC.splitlines()
    for s in summarize(MVC_VIEW_SRC, "markup").symbols:
        assert s.name in lines[s.line - 1], f"{s.name} at :{s.line}"


def test_view_with_no_model_still_summarized():
    s = summarize("@section Foot {\n}\n", "markup")
    assert "Razor view" in s.purpose


MULTILINE_TAG_SRC = """\
<%@ Page Language="VB" %>
<form runat="server">
  <asp:Label
      ID="lblSpanning"
      runat="server"
      Text="x" />
  <asp:Button ID="btnSameLine" runat="server" />
</form>
"""


def test_multiline_server_tag_line_points_at_the_id_not_the_tag_open():
    """Regression: real .aspx routinely spreads a control across several lines, so the
    `<asp:Label` line holds no trace of the control's name. Reporting the tag-open line
    sent agents to a coordinate that never mentions what they were looking for."""
    lines = MULTILINE_TAG_SRC.splitlines()
    syms = {s.name: s for s in summarize(MULTILINE_TAG_SRC, "markup").symbols}
    assert "lblSpanning" in syms and "btnSameLine" in syms
    for s in syms.values():
        assert s.name in lines[s.line - 1], f"{s.name} at :{s.line} -> {lines[s.line-1]!r}"
    assert syms["lblSpanning"].line == 4  # the ID= line, not line 3


def test_aspx_inline_handler_line_is_offset_from_the_body_not_the_tag(aspx):
    """Regression: offsets were added to the `<script>` tag position instead of the
    captured body, putting handlers a whole tag too early (often line 1)."""
    lines = ASPX_SRC.splitlines()
    pi = aspx["Page_Init"]
    assert "Page_Init" in lines[pi.line - 1], f"reported :{pi.line} -> {lines[pi.line-1]!r}"


# --- classic-ASP server-side includes ---------------------------------------
#
# `<!-- #include -->` is *the* composition mechanism in classic ASP: no imports, no
# module system, so a page's real behaviour is assembled from its include chain. A field
# investigation had to trace `JournalPage.asp -> SecurityCheck.asp` by hand because these
# were invisible to the index.

SSI_SRC = """\
<!--#include file="SecurityCheck.asp"-->
<!-- #include virtual="/preauth/IOSource/SECFunctions.asp" -->
<%@ Page Language="VB" %>
<!--  #INCLUDE FILE="..\Common\Header.inc"  -->
<p>not an include: #include file="quoted in prose"</p>
"""


def test_server_side_includes_become_dependencies():
    deps = summarize(SSI_SRC, "markup").dependencies
    assert "SecurityCheck.asp" in deps
    assert "/preauth/IOSource/SECFunctions.asp" in deps, "virtual= form must be caught too"


def test_include_directive_is_case_insensitive_and_normalises_slashes():
    deps = summarize(SSI_SRC, "markup").dependencies
    assert "../Common/Header.inc" in deps, "uppercase #INCLUDE and backslashes must work"


def test_includes_coexist_with_directive_dependencies():
    """The include scan must not displace tag-prefix / namespace dependencies."""
    src = '<%@ Register TagPrefix="uc" Namespace="My.Controls" %>\n' + SSI_SRC
    deps = summarize(src, "markup").dependencies
    assert "SecurityCheck.asp" in deps
    assert any("My.Controls" in d or "uc" in d for d in deps)


def test_config_files_are_indexed():
    """Regression: `.config` was excluded, so `memo find "customErrors"` returned nothing
    and a Web.config `defaultRedirect` root cause was invisible to the index."""
    from memo.config import DEFAULT_LANGUAGES
    assert ".config" in DEFAULT_LANGUAGES
