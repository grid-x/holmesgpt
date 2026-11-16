import base64
import json

from holmes.core.tools import StructuredToolResult, StructuredToolResultStatus
from holmes.plugins.toolsets.grafana.toolset_grafana import (
    GetDashboardByUID,
    GrafanaDashboardConfig,
    GrafanaToolset,
)
from holmes.plugins.toolsets.json_filter_mixin import _truncate_to_depth


def _build_tool(data):
    toolset = GrafanaToolset()
    toolset._grafana_config = GrafanaDashboardConfig(url="http://example.com")
    tool = GetDashboardByUID(toolset)
    tool._make_grafana_request = lambda endpoint, params: StructuredToolResult(
        status=StructuredToolResultStatus.SUCCESS,
        data=data,
        params=params,
        url="http://api",
    )
    return tool


def test_truncate_to_depth_limits_nested_values():
    data = {"a": {"b": {"c": 1}}, "list": [1, {"nested": 2}]}
    truncated = _truncate_to_depth(data, 1)
    assert truncated["a"] == "...truncated at depth 1"
    assert truncated["list"] == "...truncated at depth 1"


def test_jq_filter_applies_before_returning_data():
    data = {"dashboard": {"panels": [{"id": 1, "title": "CPU"}]}}
    tool = _build_tool(data)

    result = tool._invoke(
        {"uid": "abc", "jq": ".dashboard.panels[].title"}, context=None
    )

    assert result.status is StructuredToolResultStatus.SUCCESS
    assert result.data == "CPU"


def test_invalid_jq_returns_error():
    data = {"dashboard": {"panels": [{"id": 1, "title": "CPU"}]}}
    tool = _build_tool(data)

    result = tool._invoke({"uid": "abc", "jq": ".["}, context=None)

    assert result.status is StructuredToolResultStatus.ERROR
    assert "Invalid jq expression" in result.error


def test_jq_runtime_error_is_not_reported_as_invalid_expression():
    """A filter that compiles but fails on the data must not be labelled a syntax
    error -- that tells the LLM to rewrite a correct filter, and each retry costs
    another tool call (for a device command, a ~90s round-trip)."""
    # stdout whose decoded bytes do not start with `[`, as produced by a command
    # wrapper that prepends control/non-UTF-8 bytes.
    data = {"body": {"stdout": base64.b64encode(b"\xe4\xf9\x00\x13[]").decode()}}
    tool = _build_tool(data)

    result = tool._invoke(
        {"uid": "abc", "jq": ".body.stdout | @base64d | fromjson"}, context=None
    )

    assert result.status is StructuredToolResultStatus.ERROR
    assert "Invalid jq expression" not in result.error
    assert "compiled but failed on this response's data" in result.error


def test_stripping_control_bytes_recovers_the_payload():
    """The strip recommended in the runtime-error message must actually work."""
    payload = [{"Names": ["/a"], "State": "running"}]
    raw = b"\xe4\xf9\x00\x13" + json.dumps(payload).encode()
    data = {"body": {"stdout": base64.b64encode(raw).decode()}}
    tool = _build_tool(data)

    result = tool._invoke(
        {
            "uid": "abc",
            "jq": '.body.stdout | @base64d | gsub("[[:cntrl:]]"; "") '
            '| sub("^[^[{]*"; "") | fromjson',
        },
        context=None,
    )

    assert result.status is StructuredToolResultStatus.SUCCESS
    assert result.data == payload


def test_depth_applies_after_filters():
    data = {"dashboard": {"panels": [{"id": 1, "title": "CPU"}]}}
    tool = _build_tool(data)

    result = tool._invoke({"uid": "abc", "max_depth": 1}, context=None)

    assert result.data["dashboard"] == "...truncated at depth 1"


def test_max_depth_zero_returns_error_on_success():
    """max_depth=0 would silently destroy a SUCCESS payload; it must now surface an ERROR."""
    data = {"dashboard": {"panels": [{"id": 1, "title": "CPU"}]}}
    tool = _build_tool(data)

    result = tool._invoke({"uid": "abc", "max_depth": 0}, context=None)

    assert result.status is StructuredToolResultStatus.ERROR
    assert result.error is not None
    assert "max_depth" in result.error
    assert ">= 1" in result.error or "omit" in result.error.lower()


def test_max_depth_negative_returns_error_on_success():
    """Negative max_depth is an undocumented full-response escape hatch; close it and fail loudly."""
    data = {"dashboard": {"panels": [{"id": 1, "title": "CPU"}]}}
    tool = _build_tool(data)

    result = tool._invoke({"uid": "abc", "max_depth": -1}, context=None)

    assert result.status is StructuredToolResultStatus.ERROR
    assert result.error is not None
    assert "max_depth" in result.error


def test_max_depth_zero_preserves_upstream_error():
    """If upstream already failed, the guard must not clobber the original error field."""
    toolset = GrafanaToolset()
    toolset._grafana_config = GrafanaDashboardConfig(url="http://example.com")
    tool = GetDashboardByUID(toolset)
    upstream_error = "HTTP 503: Elasticsearch cluster unreachable"
    tool._make_grafana_request = lambda endpoint, params: StructuredToolResult(
        status=StructuredToolResultStatus.ERROR,
        error=upstream_error,
        data={"status_code": 503, "body": "unreachable"},
        params=params,
        url="http://api",
    )

    result = tool._invoke({"uid": "abc", "max_depth": 0}, context=None)

    assert result.status is StructuredToolResultStatus.ERROR
    assert result.error == upstream_error


def test_max_depth_omitted_returns_full_data():
    """Omitting max_depth must return the full, untouched payload."""
    data = {"dashboard": {"panels": [{"id": 1, "title": "CPU"}]}}
    tool = _build_tool(data)

    result = tool._invoke({"uid": "abc"}, context=None)

    assert result.status is StructuredToolResultStatus.SUCCESS
    assert result.data == data


def test_max_depth_description_does_not_lure_zero():
    """Regression: the LLM-facing description must not suggest 0 as a valid value."""
    from holmes.plugins.toolsets.json_filter_mixin import JsonFilterMixin

    desc = JsonFilterMixin.filter_parameters["max_depth"].description
    assert "0 returns only top-level keys" not in desc
    assert ">= 1" in desc
