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
| `chore: add microwave to the image for flight recordings` | see below |

`json_filter_mixin._apply_jq_filter` labelled every failure "Invalid jq
expression", including failures where the filter compiled fine and the DATA was
at fault. An LLM told its expression is invalid rewrites the expression, which
then fails identically — and `filter_result` discards `data` on error, so each
retry costs a whole tool call. Compile and run are now reported separately.
Voyager's prompt carries the matching decode rule (strip control bytes and
anything before the first `[`/`{` before `fromjson`); the two are independent —
the prompt rule works on any image, this only improves the message when
something still fails.

## microwave in the image

`microwave` (private `grid-x/Microwave`) reads gridX flight recordings — the
rbems pipeline's input and output per run — for Voyager's `flight-recordings`
toolset. It is in the image for one reason that nothing else covers: the raw
record JSON carries the `xfra` enums as bare integers (`"Kind": 4`), so the
model cannot tell the battery from the PV inverter without reading
`pkg/xfra/appliance_inventory.go` in a private repo. `microwave -f human`
prints `[eb4303.. (Inv,Battery)]` with the constraint stack in priority order,
which is the readable form of exactly the question the toolset exists to answer.
`-f contracts` additionally runs its own invariant checks over a state.

It does NOT replace the toolset's jq path, for two measured reasons: `-f human`
is built on `MicroObserver`, which takes only an `*xfra.ApplianceState`, so it
structurally cannot show the `Decision`/`Intent` — and at ~44 lines per record
it reads one moment, not a day of ~11,700 runs. The division is microwave for a
moment, jq for a time series.

Built by `scripts/build_go_binaries.sh` alongside kubectl/helm/argocd, but with
two deliberate differences:

* **From a local checkout, not a clone** (`MICROWAVE_SRC`, default
  `~/go/github.com/grid-x/Microwave`). The repo is private, so a clone would
  need credentials on every build host. The build is fully offline and
  credential-free — the repo vendors its deps, including the private
  `grid-x/client` (verified with `GOPROXY=off`), and it is built in place
  without modifying the checkout.
* **Its dependencies are NOT patched here**, unlike every other binary in that
  script. Microwave is a gridX-owned internal repo, so a dep bump belongs in its
  own `go.mod`. Pinning from here would also force a
  `go mod tidy && go mod vendor` on every build — Go refuses a replace that
  `vendor/modules.txt` does not record, verified — turning an offline 7s build
  into a network-dependent 16s one and giving Microwave's module graph two
  sources of truth. **Maintainability over a locally-patched binary, chosen
  knowingly.**

The one thing still enforced is `GOTOOLCHAIN=go1.26.9`: Microwave's `go.mod`
says `go 1.26.3`, below the script's stdlib floor, and building with the pinned
toolchain costs nothing.

What that leaves open, stated rather than waved off: at v1.10.0 Microwave
vendors x/net 0.55.0, x/text 0.37.0 and grpc 1.81.1, below the script's floors.
`x/net/http2`, `x/net/html` and grpc's `internal/transport` **are** linked into
the binary — pulled in through `grid-x/client`'s publisher/derapi packages, not
by the CLI itself — so scanner findings are real, not unreachable dead code. The
practical exposure is small (a CLI reading a local file, no listener, no
untrusted peer) and the fix is one PR in Microwave:

    go get golang.org/x/net@v0.60.0 golang.org/x/text@v0.42.0 \
           google.golang.org/grpc@v1.83.2 && go mod tidy && go mod vendor

Not version-pinned, because a checkout is whatever is checked out; the build
echoes `git describe` so the build log records what went in. Keep the checkout
on a release tag unless deliberately testing newer work.

**Verified:** built from v1.10.0 for both arches, Go 1.26.9, in ~17s with no
network (6.0 MB amd64 / 5.4 MB arm64 gzipped); the checkout is left clean. Both
checksums verify, the Dockerfile smoke test passes inside the 0.45.0 image, and
the shipped binary was run there against a live recording — `-f human` gives
`[eb4303.. (Inv,Battery)]` through `BatteryFuseLimits ph1`, `-f contracts`
returns its `SoftBreach` report. The Dockerfile stanza itself is carried on
inspection, like the rest of that commit.

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
