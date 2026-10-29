"""Tool search: the load_tools definition, deferral policy, and the executor search."""

from unittest.mock import patch

from holmes.core.tool_search import (
    ANTHROPIC_TOOL_SEARCH_NAME,
    ANTHROPIC_TOOL_SEARCH_TOOL,
    ANTHROPIC_TOOL_SEARCH_TYPE,
    LOAD_TOOLS_NAME,
    LOAD_TOOLS_TOOL,
    is_server_search_tool_call,
)
from holmes.core.tools import (
    StructuredToolResult,
    StructuredToolResultStatus,
    Tool,
    ToolInvokeContext,
    Toolset,
    ToolsetStatusEnum,
    ToolsetType,
)
from holmes.core.tools_utils.tool_executor import ToolExecutor


def test_load_tools_tool_is_a_plain_function_tool():
    # Must be a normal OpenAI function tool so HolmesGPT can execute it itself and so
    # it works on every provider (no Anthropic server-tool / beta dependency).
    assert LOAD_TOOLS_TOOL["type"] == "function"
    assert LOAD_TOOLS_TOOL["function"]["name"] == LOAD_TOOLS_NAME
    assert "query" in LOAD_TOOLS_TOOL["function"]["parameters"]["properties"]
    assert LOAD_TOOLS_TOOL["function"]["parameters"]["required"] == ["query"]


def test_anthropic_search_tool_has_no_function_key():
    # Anthropic's search tool is a provider tool, not a function tool. Anything that
    # walks the tool list by t["function"]["name"] must skip it -- see the
    # type == "function" guards in _get_tools/call_stream.
    assert ANTHROPIC_TOOL_SEARCH_TOOL["type"] == ANTHROPIC_TOOL_SEARCH_TYPE
    assert ANTHROPIC_TOOL_SEARCH_TOOL["name"] == ANTHROPIC_TOOL_SEARCH_NAME
    assert "function" not in ANTHROPIC_TOOL_SEARCH_TOOL


def test_is_server_search_tool_call():
    assert is_server_search_tool_call("tool_search_tool_regex")
    assert is_server_search_tool_call("tool_search_tool_bm25")
    assert not is_server_search_tool_call("kubectl_get")
    assert not is_server_search_tool_call(LOAD_TOOLS_NAME)


class _SearchTool(Tool):
    def _invoke(self, params, context: ToolInvokeContext) -> StructuredToolResult:
        return StructuredToolResult(status=StructuredToolResultStatus.SUCCESS)

    def get_parameterized_one_liner(self, params) -> str:
        return ""


class _SearchToolset(Toolset):
    def __init__(self, name, toolset_type, tool_specs, **kw):
        super().__init__(
            name=name, description=f"{name} toolset", type=toolset_type, **kw
        )
        self.tools = [_SearchTool(name=n, description=d) for n, d in tool_specs]


def _executor_with_search() -> ToolExecutor:
    core = _SearchToolset(
        "kubernetes/core", ToolsetType.BUILTIN, [("kubectl_get", "get k8s resources")]
    )
    grafana = _SearchToolset(
        "grafana", ToolsetType.MCP, [("search_dashboards", "find Grafana dashboards")]
    )
    nexus = _SearchToolset("nexus", ToolsetType.MCP, [("kb_search", "search the KB")])
    for ts in (core, grafana, nexus):
        ts.status = ToolsetStatusEnum.ENABLED
    return ToolExecutor(toolsets=[core, grafana, nexus])


def test_visible_tools_hide_mcp_and_expose_load_tools():
    ex = _executor_with_search()
    names = {t["function"]["name"] for t in ex.get_visible_tools_openai_format(set())}
    assert "kubectl_get" in names  # built-in core stays loaded
    assert LOAD_TOOLS_NAME in names  # meta-tool exposed
    assert "search_dashboards" not in names  # MCP tools held back
    assert "kb_search" not in names


def test_visible_tools_include_a_loaded_mcp_tool():
    ex = _executor_with_search()
    names = {
        t["function"]["name"]
        for t in ex.get_visible_tools_openai_format({"search_dashboards"})
    }
    assert "search_dashboards" in names  # loaded on demand -> now visible
    assert "kb_search" not in names  # the other MCP tool is still hidden


def test_search_deferred_tools_matches_mcp_only():
    ex = _executor_with_search()
    assert ex.search_deferred_tools("dashboards") == (["search_dashboards"], 1)
    # Matches via the owning toolset's name, even though the tool name/description
    # never says "nexus".
    assert ex.search_deferred_tools("nexus") == (["kb_search"], 1)
    assert ex.search_deferred_tools("kubectl") == ([], 0)  # built-in isn't deferrable
    assert ex.search_deferred_tools("no-such-thing") == ([], 0)
    assert ex.search_deferred_tools("") == ([], 0)


def test_search_deferred_tools_falls_back_for_invalid_regex():
    # A malformed regex (models do emit a bare "[" or "(") must not raise; it falls
    # back to a case-insensitive substring match.
    ex = _executor_with_search()
    assert ex.search_deferred_tools("[") == ([], 0)
    assert ex.search_deferred_tools("(") == ([], 0)


def test_search_deferred_tools_reports_truncation():
    """The cap must be reported, not applied silently. A truncated list that reads as
    complete is how a 16-tool MCP server looked like an 8-tool one, leaving the tools
    that were cut unreachable and unmentioned."""
    ex = _executor_with_search()
    names, total = ex.search_deferred_tools(".", limit=1)
    assert len(names) == 1
    assert total == 2  # caller can see 2 matched, so 1 was withheld

    names, total = ex.search_deferred_tools(".")
    assert len(names) == total == 2  # nothing withheld


def test_multi_term_query_is_or_not_a_literal_phrase():
    """A model that names several tools in one query must get them all.

    As a single regex, "grafana kb_search" matches nothing -- no tool's text contains
    those words in that order -- so a multi-word query silently returned zero results
    even though both tools existed. It is also valid regex, so the invalid-regex
    substring fallback never fired.
    """
    ex = _executor_with_search()
    names, total = ex.search_deferred_tools("search_dashboards kb_search")
    assert set(names) == {"search_dashboards", "kb_search"}
    assert total == 2

    # A term that matches nothing doesn't suppress the ones that do.
    names, _ = ex.search_deferred_tools("kb_search no_such_tool")
    assert names == ["kb_search"]


def test_regex_queries_still_work_for_a_single_term():
    ex = _executor_with_search()
    assert ex.search_deferred_tools("search_dash.*ds")[0] == ["search_dashboards"]
    assert ex.search_deferred_tools("kb_(search|lookup)")[0] == ["kb_search"]


def test_deferrable_helpers_report_the_mcp_tools():
    ex = _executor_with_search()
    assert ex.has_deferrable_tools()
    assert set(ex.get_deferrable_tool_names()) == {"search_dashboards", "kb_search"}


def test_nothing_is_deferrable_without_mcp_toolsets():
    core = _SearchToolset(
        "kubernetes/core", ToolsetType.BUILTIN, [("kubectl_get", "get k8s")]
    )
    core.status = ToolsetStatusEnum.ENABLED
    ex = ToolExecutor(toolsets=[core])
    # No MCP toolsets -> nothing to defer, so _get_tools skips tool search entirely
    # rather than paying a search hop that could never return anything.
    assert not ex.has_deferrable_tools()
    assert ex.get_deferrable_tool_names() == []


@patch("holmes.core.tools_utils.tool_executor.TOOL_SEARCH_DEFER_TYPES", "built-in")
def test_defer_types_are_configurable():
    ex = _executor_with_search()
    # With built-ins made deferrable, the MCP tools are the ones that stay loaded.
    assert ex.get_deferrable_tool_names() == ["kubectl_get"]
    names = {t["function"]["name"] for t in ex.get_visible_tools_openai_format(set())}
    assert "kubectl_get" not in names
    assert "search_dashboards" in names
