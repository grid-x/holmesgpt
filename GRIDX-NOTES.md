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

BerriAI/litellm#46440 (2026-10-15) fixes only the first half. Verified by running a
tool-search response through `litellm==1.107.0rc2`: the `tool_search_tool_result`
block now survives into `provider_specific_fields.tool_results`, but
`server_tool_use` is **still** converted into a client tool call, so the stray
`tool_search_tool_regex` call reaches the agent loop either way. And there is no
stable 1.107.0 at all — latest stable is 1.106.6, which still has the skip, so
1.107.0 exists only as `dev`/`rc`. Moving the pin there would mean shipping a
release candidate (~250 commits past 1.106.6) for a partial fix, and the absorb
shim below would still be required.

Note this is the **SDK inside Holmes**, not the LiteLLM proxy version the gateway
runs; a working `claude` CLI against `gateway.ai.gridx.de` says nothing about it.

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

`HOLMES_TOOL_SEARCH_RESULT_LIMIT` defaults to 40, which must stay comfortably above
any single MCP server's tool count. At the initial 8 (taken from upstream #2108), a
`load_tools("buildkite")` against the 16-tool buildkite server returned the first 8
alphabetically and reported "Loaded 8 tool(s)" with no indication it had truncated,
leaving `list_jobs`/`read_logs`/`search_logs` unreachable. A withheld match is now
always reported. Relatedly, a multi-word query is matched as OR across terms rather
than as one regex: the model's natural recovery
(`load_tools("buildkite list_jobs read_logs")`) is valid regex, so it was searched
for as a literal phrase and matched nothing -- and being valid meant the
invalid-regex substring fallback never fired.

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

**Live-model status.** `load_tools` mode has been exercised through
`gateway.ai.gridx.de` (a buildkite investigation loaded tools on demand), which is
how both search bugs above were found. The token saving is still measured on
serialized schemas rather than a real turn's prompt-token count, and `anthropic` mode
has never been run against a live model.

### Choosing what gets deferred

`type:` is never written by hand; `toolset_manager.py` assigns it. Everything under
`mcp_servers:` is stamped `mcp` (line 103), and any `toolsets:` entry whose name is
not a built-in gets `custom` (line 425). Resolved against
`holmesgpt-config/config.yaml`:

| toolset | type |
|---|---|
| `aws`, `redis`, `reporting`, `artifacts`, `kubernetes` | `custom` |
| `grafana`, `buildkite`, `nexus` | `mcp` |
| `bash`, `prometheus/metrics`, `grafana/tempo`, `kubernetes/*` | `None` (built-in override) |

Two consequences. A built-in overridden from config carries **no** type, so type
matching deliberately requires a real type -- inferring "defer it" from a missing one
would hold back bash/prometheus/tempo, which nearly every turn needs. And
`HOLMES_TOOL_SEARCH_DEFER_TYPES=mcp,custom` is blunt here: it would sweep in
`kubernetes` (used constantly) alongside `aws` (situational), because both are
`custom`.

So a toolset can carry `defer: true` / `defer: false` to override the type-wide
default for itself alone. `defer: true` on `aws` defers it without touching
`kubernetes`; `defer: false` pins a toolset loaded whatever the env var says. A
deferred toolset keeps its `llm_instructions` in the system prompt -- those render
from the toolset list, not the tool list -- so it stays discoverable; only the
parameter schemas are withheld. A toolset with no `llm_instructions` would be
genuinely invisible once deferred, which is worth remembering when adding MCP
servers.
