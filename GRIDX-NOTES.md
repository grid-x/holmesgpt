# gridx-0.45.0 — what we carry on top of upstream

Rebased from `fix/bash-argv-flag-aware-validation` (which sat on 0.42.0).

| commit | status on 0.45.0 |
|---|---|
| `fix: Handle None timeout values in Prometheus queries` | kept, applied clean |
| `fix: Handle malformed MCP tool schemas gracefully` | kept, applied clean |
| `chore: Add CLI tools to Docker image` | kept, applied clean |
| `fix: Support kubectl flags in bash validation prefix matching` | **dropped**, superseded |
| `fix(bash): make allow/deny prefix matching argv-based and flag-aware` | **dropped**, reimplemented |

## Added on top of 0.45.0 (not from the original rebase)

| commit | why |
|---|---|
| `fix: don't crash on an explicit "tool_calls": null when a turn pauses` | tool approval |
| `fix(jq): stop reporting a data error as an invalid jq expression` | see below |

`json_filter_mixin._apply_jq_filter` labelled every failure "Invalid jq
expression", including failures where the filter compiled fine and the DATA was
at fault. An LLM told its expression is invalid rewrites the expression, which
then fails identically — and `filter_result` discards `data` on error, so each
retry costs a whole tool call. Compile and run are now reported separately.
Voyager's prompt carries the matching decode rule (strip control bytes and
anything before the first `[`/`{` before `fromjson`); the two are independent —
the prompt rule works on any image, this only improves the message when
something still fails.

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

139 pass. For the jq filter change:

    pytest tests/plugins/toolsets/test_json_filter_mixin.py --noconftest -o addopts=""

11 pass. Note the empty `addopts`: the project's default adds `--cov`, which
needs pytest-cov, and `-n auto`, which needs xdist. Four `TestStdio` failures in `tests/test_mcp_toolset.py` are
pre-existing on pristine 0.45.0 (they spawn a real MCP server over `npx`) and
are unrelated to anything here — compare against the upstream tag before
treating one as a regression.

**Not yet verified:** no container image has been built from this branch, so
the Dockerfile commit is carried on inspection only (0.45.0 is still
`python:3.11-alpine`, which is what it assumes).
