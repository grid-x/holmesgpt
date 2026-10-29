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
