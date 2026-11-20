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
| `chore: add fr-decode, raise the Go toolchain pin to 1.27.3` | see below |
| `chore: add clickhousectl to the image for ClickHouse Cloud queries` | see below |

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

## fr-decode in the image, and the 1.27.3 toolchain pin

`decode` (private `grid-x/fr-decode`) turns a flight recording into
newline-delimited JSON, which Voyager's `flight-recordings` toolset pipes into
jq. It is here to **delete a fork**: Voyager previously carried a Python port of
the same binary format, written because this image had no Go toolchain and is
vendored rather than built by us. Both produced byte-identical output on a real
recording (11,712 records, `cmp` clean), so the port was never wrong — it was
simply a second implementation of a binary format that could drift. One
implementation, owned by the team that owns the format.

Performance was not the reason and does not favour either: decode alone is 63ms
(Go) vs 89ms (Python), but end-to-end with jq both land at ~2.1s, because jq
dominates. Built as-is from a local checkout (`FR_DECODE_SRC`), same reasoning
as Microwave. Its whole module graph is `github.com/klauspost/compress` — no
x/net, x/crypto, x/text or grpc at all (verified with `go list -m all`), so the
replaces in that script have nothing to apply to. It is not vendored, so its
build is the one that still wants a module cache or the proxy.

**The GOTOOLCHAIN pin rose from 1.26.9 to 1.27.3 for this.** fr-decode's
`go.mod` declares `go 1.27.3`, and a toolchain below a module's own go
directive fails outright (`go.mod requires go >= 1.27.3`) — a directive *below*
the pin is fine, which is why Microwave's `go 1.26.3` never needed this. The
pin was raised for **all** binaries rather than making fr-decode an exception,
so there is one floor to reason about rather than per-tool ones. 1.27.3 is a
strict superset of 1.26.9's stdlib fixes: Go backports each security fix to the
two most recent minor lines together, so the 1.27 line never trails 1.26.

**Verified before raising it:** argocd v3.3.15, helm v3.22.0 and kubectl
v1.37.1 were each rebuilt from source on Go 1.27.3 with their full replace sets
applied, and all three compile clean. fr-decode itself builds in ~3s for both
arches (974 KB amd64 / 880 KB arm64 gzipped), leaves the checkout clean, and
its smoke test (`decode 2>&1 | grep -q "usage: decode"` — a bare call exits 2
with usage on stderr) passes inside the 0.45.0 image. The full toolset was then
run there against a live recording through all three of its formats.

**Known gap, deliberate:** only `decode` is actually built with 1.27.3. The
four binaries already committed (`argocd`, `helm`, `kubectl`, `microwave`)
remain as built with 1.26.9 — rebuilding them was deferred to the next CVE pass
rather than enlarging this change. So the script's pin is ahead of four of the
five shipped artifacts. That is cosmetic for now (1.26.9 carries every fix this
script documents, and all three externals were verified to compile on 1.27.3),
but **the next run of the script will rebuild all five on 1.27.3**, which is
when the tree catches up. Do not read the committed binaries as evidence of
what the pin produces until then.

## clickhousectl in the image

`clickhousectl` (ClickHouse's official CLI) is how Voyager queries ClickHouse
Cloud: `cloud clickhouse query` sends one SQL statement to a service's Query API
endpoint, authenticated with a single Cloud API key
(`CLICKHOUSE_CLOUD_API_KEY`/`_SECRET`) instead of a database user per service,
and run as whatever database role that endpoint grants (`sql_console_read_only`
for Voyager). It replaces Voyager's previous ClickHouse path through Grafana's
`query_sql`. Voyager uses it like `psql`: a bash allowlist prefix plus a prompt
section, no toolset code here.

Not built by `scripts/build_go_binaries.sh` — it is Rust, not Go, and none of
that script's CVE replaces apply. Taken from the upstream release tarball and
checked against the release's `SHA256SUMS`, the same way `bk` is. The release
assets are named by Rust target triple (`x86_64`/`aarch64-unknown-linux-musl`),
so the stanza maps `TARGETARCH`; both are static-pie binaries and need nothing
from Alpine.

`DO_NOT_TRACK=1` and `CLICKHOUSECTL_NO_UPDATE_CHECK=1` are set image-wide.
Without them it sends anonymous usage telemetry, checks GitHub for updates, and
prints a multi-line telemetry notice to stderr on first run — which the bash
toolset would hand to the model as part of a tool result.

Verified: the two stanzas, extracted into a minimal `python:3.11-alpine`
Dockerfile, build and smoke-test on both `linux/amd64` and `linux/arm64`
(`clickhousectl 0.6.0`; with no credentials a query exits with a clean
`auth_required` error, nothing else written). Not yet verified in a full image
build from this branch, like the rest of the Dockerfile commits.

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
