"""The Anthropic tool-search beta header is sent only when the search tool is.

litellm 1.89.0 auto-adds anthropic-beta for web_fetch/memory/advisor tools but NOT
for tool search (see update_headers_with_optional_anthropic_beta in
litellm/llms/anthropic/chat/transformation.py), so Holmes adds it itself. Anthropic
rejects the search tool without the beta header, so this is load-bearing for
HOLMES_TOOL_SEARCH_MODE=anthropic.
"""

from unittest.mock import patch

import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage

from holmes.core.llm import DefaultLLM
from holmes.core.tool_search import (
    ANTHROPIC_TOOL_SEARCH_BETA_HEADER,
    ANTHROPIC_TOOL_SEARCH_TOOL,
)

FUNCTION_TOOL = {
    "type": "function",
    "function": {"name": "kubectl_get", "description": "get", "parameters": {}},
}


def _mock_model_response() -> ModelResponse:
    return ModelResponse(
        id="chatcmpl-test",
        choices=[
            Choices(
                index=0,
                message=Message(role="assistant", content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        model="claude-opus-5-5",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _make_llm(args: dict) -> DefaultLLM:
    """Build a DefaultLLM bypassing __init__/check_llm to control self.args directly."""
    llm = DefaultLLM.__new__(DefaultLLM)
    llm.model = "claude-opus-5-5"
    llm.api_key = None
    llm.api_base = None
    llm.api_version = None
    llm.args = dict(args)
    llm.tracer = None
    llm.name = None
    llm.is_robusta_model = False
    llm.max_context_size = 200000
    llm.allowed_openai_params = None
    return llm


@pytest.fixture
def mock_completion():
    with patch("holmes.core.llm.litellm.completion") as mock:
        mock.return_value = _mock_model_response()
        yield mock


def _headers_for(mock_completion, tools, args=None) -> dict:
    llm = _make_llm(args or {})
    llm.completion(
        messages=[{"role": "user", "content": "hi"}], tools=tools, tool_choice="auto"
    )
    return mock_completion.call_args.kwargs.get("extra_headers")


class TestToolSearchBetaHeader:
    def test_header_added_when_search_tool_present(self, mock_completion):
        headers = _headers_for(
            mock_completion, [ANTHROPIC_TOOL_SEARCH_TOOL, FUNCTION_TOOL]
        )
        assert headers == {"anthropic-beta": ANTHROPIC_TOOL_SEARCH_BETA_HEADER}

    def test_no_header_without_the_search_tool(self, mock_completion):
        # The default (load_tools) mode must not send a beta header at all.
        assert _headers_for(mock_completion, [FUNCTION_TOOL]) is None

    def test_merged_into_an_existing_beta_header(self, mock_completion):
        """EXTRA_HEADERS may already carry anthropic-beta (e.g. context-1m); the
        tool-search value is appended rather than replacing it."""
        headers = _headers_for(
            mock_completion,
            [ANTHROPIC_TOOL_SEARCH_TOOL, FUNCTION_TOOL],
            args={"extra_headers": {"anthropic-beta": "context-1m-2025-08-07"}},
        )
        assert headers["anthropic-beta"] == (
            f"context-1m-2025-08-07,{ANTHROPIC_TOOL_SEARCH_BETA_HEADER}"
        )

    def test_not_duplicated_when_already_configured(self, mock_completion):
        headers = _headers_for(
            mock_completion,
            [ANTHROPIC_TOOL_SEARCH_TOOL, FUNCTION_TOOL],
            args={
                "extra_headers": {"anthropic-beta": ANTHROPIC_TOOL_SEARCH_BETA_HEADER}
            },
        )
        assert headers["anthropic-beta"] == ANTHROPIC_TOOL_SEARCH_BETA_HEADER

    def test_other_extra_headers_are_preserved(self, mock_completion):
        headers = _headers_for(
            mock_completion,
            [ANTHROPIC_TOOL_SEARCH_TOOL, FUNCTION_TOOL],
            args={"extra_headers": {"x-custom": "keep-me"}},
        )
        assert headers["x-custom"] == "keep-me"
        assert headers["anthropic-beta"] == ANTHROPIC_TOOL_SEARCH_BETA_HEADER
