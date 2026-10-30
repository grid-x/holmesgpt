import logging
import re
from typing import Dict, List, Optional, Tuple

import sentry_sdk

from holmes.common.env_vars import TOOL_SEARCH_DEFER_TYPES, TOOL_SEARCH_RESULT_LIMIT
from holmes.core.init_event import EventCallback, StatusEvent, StatusEventKind
from holmes.core.tool_search import LOAD_TOOLS_TOOL
from holmes.core.tools import (
    Tool,
    Toolset,
    ToolsetStatusEnum,
)
from holmes.core.tools_utils.oauth_tool_connector import OAuthToolConnector

display_logger = logging.getLogger("holmes.display.tool_executor")


def _deferrable_toolset_types() -> frozenset:
    """Toolset types whose tools are held back until loaded on demand.

    Driven by HOLMES_TOOL_SEARCH_DEFER_TYPES (comma-separated ToolsetType values).
    MCP servers dominate tool-schema bloat, so they are the default; built-in
    toolsets (kubernetes, bash, ...) stay loaded so common operations don't pay a
    search hop. Values are matched against ToolsetType values ("mcp", "built-in",
    "custom", "http", "database", "mongodb"); unknown names are ignored.
    """
    return frozenset(
        part.strip().lower()
        for part in TOOL_SEARCH_DEFER_TYPES.split(",")
        if part.strip()
    )


def _mcp_tool_name(tool: "Tool") -> str:
    """Server-side MCP tool name, or "" for non-MCP tools."""
    name = getattr(tool, "mcp_tool_name", "")
    return name if isinstance(name, str) else ""


def resolve_tool_name_collisions(
    toolsets: List[Toolset],
) -> List[Tuple[Toolset, "Tool", str]]:
    """Return (toolset, tool, exposed_name) for every tool.

    MCP tools whose raw name collides across toolsets are namespaced as
    ``{toolset}__{tool}``; all others keep their raw name. Does not mutate.
    """
    entries = []  # (toolset, tool, is_mcp, raw_name)
    counts: Dict[str, int] = {}
    for ts in toolsets:
        for tool in ts.tools:
            mcp_name = _mcp_tool_name(tool)
            raw = mcp_name or tool.name
            entries.append((ts, tool, bool(mcp_name), raw))
            counts[raw] = counts.get(raw, 0) + 1

    for name, count in counts.items():
        if count > 1:
            owners = sorted({ts.name for ts, _, _, r in entries if r == name})
            display_logger.warning(
                "Multiple tools named '%s' across toolsets (%s); "
                "MCP copies are namespaced as '<toolset>__%s'.",
                name,
                ", ".join(owners),
                name,
            )

    resolved: List[Tuple[Toolset, "Tool", str]] = []
    for ts, tool, is_mcp, raw in entries:
        if is_mcp and counts[raw] > 1:
            resolved.append((ts, tool, tool.collision_safe_name))  # type: ignore[attr-defined]
        else:
            resolved.append((ts, tool, raw))
    return resolved


class ToolExecutor:
    def __init__(self, toolsets: List[Toolset], on_event: EventCallback = None):
        # TODO: expose function for this instead of callers accessing directly
        self.toolsets = toolsets

        self.enabled_toolsets: list[Toolset] = [
            ts for ts in toolsets if ts.status == ToolsetStatusEnum.ENABLED
        ]

        toolsets_by_name: dict[str, Toolset] = {}
        for ts in self.enabled_toolsets:
            if ts.name in toolsets_by_name:
                msg = f"Overriding toolset '{ts.name}'!"
                display_logger.warning(msg)
                if on_event is not None:
                    on_event(
                        StatusEvent(
                            kind=StatusEventKind.TOOL_OVERRIDE,
                            name=ts.name,
                            message=msg,
                        )
                    )
            toolsets_by_name[ts.name] = ts

        self.tools_by_name: dict[str, Tool] = {}
        self._tool_to_toolset: dict[str, Toolset] = {}
        for ts, tool, resolved_name in resolve_tool_name_collisions(
            list(toolsets_by_name.values())
        ):
            if tool.icon_url is None and ts.icon_url is not None:
                tool.icon_url = ts.icon_url
            if resolved_name != tool.name:
                # Copy so we never rename the toolset's own tool instance.
                tool = tool.model_copy(update={"name": resolved_name})
            if resolved_name in self.tools_by_name:
                msg = f"Overriding existing tool '{resolved_name} with new tool from {ts.name} at {ts.path}'!"
                display_logger.warning(msg)
                if on_event is not None:
                    on_event(
                        StatusEvent(
                            kind=StatusEventKind.TOOL_OVERRIDE,
                            name=resolved_name,
                            message=msg,
                        )
                    )
            self.tools_by_name[resolved_name] = tool
            self._tool_to_toolset[resolved_name] = ts

        self.oauth_connector = OAuthToolConnector()

    # ── Tool lookup ────────────────────────────────────────────────────

    def get_tool_by_name(
        self, name: str, user_id: Optional[str] = None
    ) -> Optional[Tool]:
        if name in self.tools_by_name:
            return self.tools_by_name[name]
        # Check per-user OAuth tools (registered in _tool_to_toolset but not in tools_by_name)
        user_tool = self.oauth_connector.find_tool(name, user_id)
        if user_tool:
            return user_tool
        logging.warning(f"could not find tool {name}. skipping")
        return None

    def get_toolset_name(
        self, tool_name: str, user_id: Optional[str] = None
    ) -> Optional[str]:
        """Return the toolset name that provides a given tool, or None."""
        ts = self._tool_to_toolset.get(tool_name) or self.oauth_connector.get_toolset(
            tool_name, user_id
        )
        return ts.name if ts else None

    def ensure_toolset_initialized(self, tool_name: str) -> Optional[str]:
        """Ensure the toolset containing the given tool is lazily initialized.

        For toolsets loaded from cache without full initialization, this triggers
        the deferred prerequisite checks (callable and command prerequisites)
        on first tool use.

        Returns None on success, or an error message string on failure.
        """
        toolset = self._tool_to_toolset.get(tool_name)
        if toolset is None:
            return None

        if toolset.needs_initialization:
            if not toolset.lazy_initialize():
                error_msg = (
                    f"Toolset '{toolset.name}' failed to initialize: {toolset.error}"
                )
                logging.error(error_msg)
                return error_msg
        elif toolset.status == ToolsetStatusEnum.FAILED:
            # Toolset was already initialized but failed — don't let tools execute
            error_msg = f"Toolset '{toolset.name}' is unavailable: {toolset.error}"
            logging.error(error_msg)
            return error_msg

        return None

    # ── Cloning ────────────────────────────────────────────────────────

    def _clone_base(self) -> "ToolExecutor":
        """Create a shallow clone sharing toolsets but with independent tool registries."""
        clone = object.__new__(ToolExecutor)
        clone.toolsets = self.toolsets
        clone.enabled_toolsets = self.enabled_toolsets
        clone.tools_by_name = dict(self.tools_by_name)
        clone._tool_to_toolset = dict(self._tool_to_toolset)
        clone.oauth_connector = self.oauth_connector  # Shared reference
        return clone

    def clone_with_extra_tools(self, extra_tools: List[Tool]) -> "ToolExecutor":
        """Create a shallow clone with additional tools registered.

        The clone shares the same toolsets and base tools but adds extra_tools
        on top. The original ToolExecutor is never mutated.

        This is used to inject frontend tools (FrontendPauseTool) on a
        per-request basis without modifying the shared ToolExecutor.
        """
        clone = self._clone_base()

        for tool in extra_tools:
            if tool.name in clone.tools_by_name:
                logging.warning(f"Frontend tool '{tool.name}' overrides existing tool")
            clone.tools_by_name[tool.name] = tool
            # No toolset mapping — frontend tools don't belong to a toolset,
            # so ensure_toolset_initialized() returns None (no-op) for them.

        return clone

    # ── Tool listing ───────────────────────────────────────────────────

    @sentry_sdk.trace
    def get_all_tools_openai_format(
        self,
        user_id: Optional[str] = None,
    ):
        """Get all tools in OpenAI format.

        Args:
            user_id: If provided, replace OAuth _connect placeholders with the
                     user's real tools (loaded after authentication).
        """
        tools = self._get_base_tools()
        return self.oauth_connector.apply_user_tools(
            tools, user_id, self._tool_to_toolset
        )

    def _get_base_tools(self) -> list:
        """Get all tools in OpenAI format (base set, no per-user overrides)."""
        return [tool.get_openai_format() for tool in self.tools_by_name.values()]

    # -- Tool search (progressive disclosure) ---------------------------------

    def _is_deferrable(self, tool_name: str) -> bool:
        """Whether a tool is held back from the default list until loaded on demand.

        An explicit ``defer:`` on the owning toolset wins; otherwise the toolset's
        type is matched against HOLMES_TOOL_SEARCH_DEFER_TYPES. A toolset with no
        type is never deferred by type alone -- built-in overrides loaded from config
        come back with ``type=None``, and inferring "defer it" from a missing type
        would hold back bash/prometheus/kubernetes, which nearly every turn needs.
        Such a toolset can still be deferred, but only by saying ``defer: true``.
        """
        toolset = self._tool_to_toolset.get(tool_name)
        if toolset is None:
            return False
        explicit = getattr(toolset, "defer", None)
        if explicit is not None:
            return bool(explicit)
        if toolset.type is None:
            return False
        ts_type = getattr(toolset.type, "value", toolset.type)
        return str(ts_type).lower() in _deferrable_toolset_types()

    def has_deferrable_tools(self) -> bool:
        """Whether any tool would be held back. Used to skip tool search entirely
        when there is nothing to defer -- exposing load_tools (or Anthropic's search
        tool) with an empty deferred set would only waste a turn."""
        return any(self._is_deferrable(name) for name in self.tools_by_name)

    def get_deferrable_tool_names(self) -> List[str]:
        """Names of every tool that tool search holds back."""
        return [name for name in self.tools_by_name if self._is_deferrable(name)]

    def get_visible_tools_openai_format(
        self,
        loaded_tool_names: Optional[set] = None,
        user_id: Optional[str] = None,
        include_load_tools: bool = True,
    ) -> list:
        """Tool-search mode: core (non-deferrable) tools + the ``load_tools`` meta-tool
        + any deferrable tools already loaded this conversation. Heavy tool schemas stay
        out of context until the model loads them.
        """
        loaded = loaded_tool_names or set()
        tools = []
        for tool in self.tools_by_name.values():
            if self._is_deferrable(tool.name) and tool.name not in loaded:
                continue
            tools.append(tool.get_openai_format())
        if include_load_tools:
            tools.append(LOAD_TOOLS_TOOL)
        return self.oauth_connector.apply_user_tools(
            tools, user_id, self._tool_to_toolset
        )

    def _build_matcher(self, query: str):
        """Build the predicate search_deferred_tools matches each tool's text against.

        A single term is used as a regex (``re.search``), falling back to a
        case-insensitive substring match when it isn't valid regex -- models do emit a
        bare "[" or "(".

        Multiple whitespace-separated terms are treated as OR, not as one literal
        phrase. A query like "buildkite list_jobs read_logs" is the model naming the
        several tools it wants; as a single regex that phrase matches nothing, because
        no tool's text contains all those words in that order. Each term is still
        tried as a regex so "kube.*logs" keeps working.
        """
        terms = query.split()
        if len(terms) == 1:
            terms = [query]

        predicates = []
        for term in terms:
            try:
                pattern = re.compile(term, re.IGNORECASE)
                predicates.append(pattern.search)
            except re.error:
                needle = term.lower()
                predicates.append(lambda text, needle=needle: needle in text.lower())

        def matches(text: str) -> bool:
            return any(predicate(text) for predicate in predicates)

        return matches

    def search_deferred_tools(
        self, query: str, limit: Optional[int] = None
    ) -> Tuple[List[str], int]:
        """Find held-back (deferrable) tools matching ``query``.

        Returns ``(names, total_matched)``. ``names`` is capped at the result limit;
        ``total_matched`` is how many matched before capping, so the caller can tell
        the model its result was truncated. Silently returning a truncated list is
        what made a 16-tool MCP server look like an 8-tool one, with the tools that
        were cut (list_jobs, read_logs, search_logs) unreachable and unmentioned.

        Each tool's name, description and owning toolset's name/description are
        searched, so "grafana" matches every tool in the grafana toolset even when the
        individual tool descriptions don't repeat the word.
        """
        query = (query or "").strip()
        if not query:
            return [], 0
        effective_limit = TOOL_SEARCH_RESULT_LIMIT if limit is None else limit
        matches = self._build_matcher(query)

        results: List[str] = []
        total = 0
        for name, tool in self.tools_by_name.items():
            if not self._is_deferrable(name):
                continue
            ts = self._tool_to_toolset.get(name)
            haystack = " ".join(
                filter(
                    None,
                    [
                        name,
                        tool.description or "",
                        ts.name if ts else "",
                        (ts.description or "") if ts else "",
                    ],
                )
            )
            if matches(haystack):
                total += 1
                if len(results) < effective_limit:
                    results.append(name)
        return results, total
