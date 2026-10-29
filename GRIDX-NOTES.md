# gridx-0.45.0 — what we carry on top of upstream

Rebased from `fix/bash-argv-flag-aware-validation` (which sat on 0.42.0).

| commit | status on 0.45.0 |
|---|---|
| `fix: Handle None timeout values in Prometheus queries` | kept, applied clean |
| `fix: Handle malformed MCP tool schemas gracefully` | kept, applied clean |
| `chore: Add CLI tools to Docker image` | kept, applied clean |
| `fix: Support kubectl flags in bash validation prefix matching` | **dropped**, superseded |
| `fix(bash): make allow/deny prefix matching argv-based and flag-aware` | **dropped**, reimplemented |

## Why the two bash commits were not replayed

0.45.0 rewrote `validation.py` and replaced bashlex with tree-sitter
(upstream `c9d28a9c`). A plain `git rebase` of all five commits succeeds with
zero conflicts and is still wrong:

* `segment_argv` called `bashlex.errors.ParsingError`, and bashlex is no longer
  imported — `NameError` on the first validated command.
* `prefix_tokens_appear_in_order` would be reinstated on top of the upstream
  replacement, reintroducing a production bug 0.45.0 had fixed.

Git cannot see either, because both touch lines upstream did not.

They are replaced by one commit that reimplements the same intent against
`ParsedCommand.command_argvs`, and additionally fixes the anti-fabrication
check — 0.45.0 repaired one of its two failure modes (`$(kubectl ...)`) but
not the other (a flag splitting the prefix words).

## Verifying

The full dependency set does not install on Python 3.14 (`pyproject.toml`
requires `<3.14`) and the root `conftest.py` imports the whole tree. To run
just the bash suite:

    pytest tests/test_bash_toolset_validation.py --noconftest --no-cov

139 pass. Four `TestStdio` failures in `tests/test_mcp_toolset.py` are
pre-existing on pristine 0.45.0 (they spawn a real MCP server over `npx`) and
are unrelated to anything here — compare against the upstream tag before
treating one as a regression.

**Not yet verified:** no container image has been built from this branch, so
the Dockerfile commit is carried on inspection only (0.45.0 is still
`python:3.11-alpine`, which is what it assumes).

## Tool search (`HOLMES_TOOL_SEARCH_ENABLED`)

Ported from upstream's two unmerged PRs — HolmesGPT/holmesgpt#2108 (client-side
`load_tools`) and #2242 (Anthropic's server-side tool search) — because with the
grafana/nexus/buildkite MCP servers enabled, the whole tool catalog is serialized
into every turn. Off by default; `HOLMES_TOOL_SEARCH_MODE` picks the strategy.

### Why `load_tools` is the default and not Anthropic's own tool search

The server-side path is the better design — Claude searches the catalog itself, no
extra round trip through Holmes — but it does not work on the litellm SDK this repo
pins (`litellm = "1.89.0"`). Checked against the installed package, not just the
docs:

* `_map_tools` serializes `tool_search_tool_regex_20251119` and `defer_loading`
  correctly, so the **request** is fine.
* The **response** is not. `transform_response` converts Anthropic's
  `server_tool_use` block into an ordinary client `tool_use` and drops the
  `tool_search_tool_result` block as "internal metadata". The agent loop is then
  handed a tool call named `tool_search_tool_regex` that no toolset owns, and the
  search result never enters history.

Fixed upstream on 2026-10-15 (BerriAI/litellm#46440, #46389), but that is only in
`v1.107.0` pre-releases — no stable litellm carries it (latest stable at the time
of writing: 1.106.6, still has the skip). Note this is the **SDK inside Holmes**,
not the LiteLLM proxy version the gateway runs; a working `claude` CLI against
`gateway.ai.gridx.de` says nothing about it.

So `anthropic` mode is wired up and tested but gated behind the env var, and
`_handle_server_search_tool_call` absorbs a stray search call so the mode degrades
into one wasted hop instead of a hard "Failed to find tool" error. Re-test it when
litellm is bumped past the fix, then flip the default.

Two further constraints found in the litellm docs and encoded here:

* Anthropic 400s on *"All tools cannot be deferred"*, so `_get_tools_anthropic_search`
  never defers the entire list — only the deferrable (MCP) tools, and if those are
  everything, one stays eagerly loaded.
* litellm 1.89.0 does not add the `anthropic-beta: advanced-tool-use-2025-11-20`
  header for tool search (it does for web_fetch/memory/advisor), so `llm.py` adds it
  when the search tool is in the request, merging into any configured `EXTRA_HEADERS`.

### What it does

Only MCP toolsets are deferred by default (`HOLMES_TOOL_SEARCH_DEFER_TYPES`), so
kubernetes/bash stay loaded and common operations skip the search hop. On a catalog
shaped like ours (6 built-in + 42 MCP tools) the visible schema shrinks ~87%.
If nothing is deferrable, tool search is skipped entirely rather than exposing
`load_tools` with an empty deferred set.

`_loaded_tool_names` is per-conversation and is carried across `with_executor()`,
which is how an approval resume rebuilds the agent — otherwise a resumed turn would
lose the tools it had already loaded.

### Verifying

    pytest tests/core/test_tool_search.py tests/core/test_llm_tool_search_beta_header.py \
           tests/test_tool_calling_llm.py tests/core/test_tool_executor.py --no-cov

The 10 failures in a full `pytest tests/ --ignore=tests/llm` run are pre-existing on
this branch (MCP stdio tests needing a `python` binary, plus four unrelated toolset
tests) — confirmed by re-running them with the change stashed.

**Not verified against a live model.** No turn has been run through
`gateway.ai.gridx.de` with either mode enabled, so the token saving above is measured
on serialized schemas, not on a real turn's prompt tokens.
