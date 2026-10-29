"""Tool search: hold heavy tool schemas out of context until the model asks for them.

Two strategies exist, selected by ``HOLMES_TOOL_SEARCH_MODE``:

``load_tools`` (client-side, the default when tool search is enabled)
    Deferrable tool schemas are held back and the model is given a single
    ``load_tools`` function tool. It calls that with a query; HolmesGPT runs the
    search itself and the matched tools appear on the next agent step.

``anthropic`` (server-side, opt-in)
    Anthropic's own tool-search tool is injected and every deferrable tool is
    marked ``defer_loading``, so Claude searches the catalog server-side.

The default is deliberately ``load_tools``, because the server-side path is
mis-handled by the litellm SDK this repo pins (1.89.0): ``server_tool_use`` is
converted into a *client* ``tool_use`` and the ``tool_search_tool_result`` block is
dropped as "internal metadata" (litellm transformation.py). The agent loop then sees
a tool call named ``tool_search_tool_regex`` that no toolset owns, and the search's
result never makes it into history. The fix landed upstream on 2026-10-15
(BerriAI/litellm#46440, #46389) and is only in ``v1.107.0`` pre-releases -- no stable
litellm carries it yet. ``_SERVER_SEARCH_TOOL_NAMES`` below lets the agent loop
recognise and absorb those stray calls so the ``anthropic`` mode degrades into a
wasted hop rather than a hard error; re-test it after the litellm bump.

``load_tools`` is a plain function tool that HolmesGPT executes itself, so it behaves
identically across the Anthropic API, Bedrock, OpenAI and any LiteLLM-fronted gateway
-- no beta header, no server tools.
"""

# Name of the meta-tool the model calls to discover/load held-back tools.
LOAD_TOOLS_NAME = "load_tools"

# Anthropic's server-side tool-search tool. The regex variant is used rather than
# bm25 because bm25 is not supported on Bedrock.
ANTHROPIC_TOOL_SEARCH_TYPE = "tool_search_tool_regex_20251119"
ANTHROPIC_TOOL_SEARCH_NAME = "tool_search_tool_regex"

ANTHROPIC_TOOL_SEARCH_TOOL: dict = {
    "type": ANTHROPIC_TOOL_SEARCH_TYPE,
    "name": ANTHROPIC_TOOL_SEARCH_NAME,
}

# Anthropic requires this beta header for tool search. litellm 1.89.0 adds beta
# headers for web_fetch/memory/advisor tools but NOT for tool search, so Holmes
# sends it itself (verified against litellm 1.89.0's
# update_headers_with_optional_anthropic_beta).
ANTHROPIC_TOOL_SEARCH_BETA_HEADER = "advanced-tool-use-2025-11-20"

# Names a server-side search can surface as a client tool call. litellm converts
# Anthropic's server_tool_use block into a normal tool_use, so the agent loop can be
# handed a call for a tool no toolset owns; it absorbs these instead of erroring.
_SERVER_SEARCH_TOOL_NAMES = frozenset(
    {ANTHROPIC_TOOL_SEARCH_NAME, "tool_search_tool_bm25"}
)


def is_server_search_tool_call(tool_name: str) -> bool:
    """Whether ``tool_name`` is a provider-side tool-search call, not a toolset tool."""
    return tool_name in _SERVER_SEARCH_TOOL_NAMES


# OpenAI function-tool definition for load_tools. It is a normal function tool --
# HolmesGPT intercepts and executes it (see ToolCallingLLM._handle_load_tools).
LOAD_TOOLS_TOOL: dict = {
    "type": "function",
    "function": {
        "name": LOAD_TOOLS_NAME,
        "description": (
            "Search for and load additional tools that are not loaded by default. "
            "To keep the context small, heavy integrations are hidden until needed. "
            "Call this with keywords describing the capability you need (e.g. "
            "'grafana dashboard', 'loki logs', 'buildkite build', 'nexus docs'). The "
            "matching tools become available to call on your next step. If nothing "
            "matches, retry with broader keywords or an integration name. Tools "
            "already loaded in this conversation stay available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords (or a regular expression) describing the capability "
                        "or integration you need, e.g. 'grafana', 'loki', 'buildkite'."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}
