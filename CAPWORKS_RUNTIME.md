# CapWorks Grok Runtime Distribution

CapWorks consumes Grok Build as a **pinned external ACP runtime binary**. CapWorks
must not compile or link Grok implementation crates in its own Cargo graph.

## Source of truth

A runtime release is identified by a full 40-character commit from this
repository. The current CapWorks compatibility baseline is:

```text
39be4736bab13c3326cd88097c4979a6978039cf
```

The release workflow checks out that exact commit independently from the commit
that contains the workflow itself. Editing CI automation therefore does not
silently change the Runtime source revision.

The workflow itself should live on the repository default branch so manual
`workflow_dispatch` runs remain discoverable in GitHub Actions. The default
branch is an automation/control plane only: every build job separately checks
out and verifies the requested Runtime source revision before compiling it.

## Build target

The shipped executable is the repository's existing Grok CLI composition root:

```text
package: xai-grok-pager-bin
binary:  xai-grok-pager
```

The Runtime entrypoint used by CapWorks is:

```text
grok --trust --no-auto-update agent --no-leader stdio
```

`--trust` is intentional: CapWorks chooses and scopes the working directory,
while Grok's release build otherwise requires an interactive folder-trust
decision. `--no-auto-update` keeps the pinned runtime immutable even if its
install location is later recognized as a managed Grok install. `--no-leader`
ensures CapWorks owns the concrete Runtime child process instead of attaching to
or spawning Grok's shared leader process.

## Release workflow

Run **CapWorks Grok Runtime Release** from GitHub Actions and provide the exact
source revision. The workflow currently builds the MVP desktop targets:

- `aarch64-apple-darwin`
- `x86_64-pc-windows-msvc`

The fork's pinned `rust-toolchain.toml` is authoritative for the Grok build. The
workflow follows the source tree's distribution configuration by enabling both
the `release-dist` Cargo profile **and** the `release-dist` feature. The profile
provides the hardened/LTO distribution build while the feature enables
release-only runtime behavior (including the pager's release policy and
jemalloc release configuration). The checked-in `Cargo.lock` is enforced with
`--locked`. Incremental compilation is disabled for ephemeral CI, but the
source tree's `release-dist` debug/strip settings are otherwise left unchanged
so the Runtime is built with the same shipping profile semantics as upstream.
macOS uses the repository's DotSlash-pinned `bin/protoc`; the pinned source has
no Windows DotSlash entry, so Windows CI installs the matching official
`protoc 29.3` archive with a fixed SHA-256 and exports it through `PROTOC`.

Each target must pass all of the following before it can be released:

1. the binary version contains the requested CapWorks Runtime version and exact
   source commit;
2. the CLI advertises `agent stdio`;
3. a real external process completes ACP `initialize` with protocol version 1;
4. required `session/load` and HTTP MCP capabilities are advertised;
5. a real ACP `session/new` succeeds in an isolated temporary `GROK_HOME` and
   workspace;
6. no model prompt or real provider credential is used by the smoke test.

## Version and release identity

For source revision `39be4736...` and upstream package version `1.0.24`, the
workflow derives:

```text
runtime version: 1.0.24+capworks.39be4736
release tag:     capworks-runtime-39be4736
```

The release contains one verified binary per target plus:

- `runtime-manifest.json`
- `SHA256SUMS`

The manifest records the full source revision, the workflow/builder revision and
GitHub Actions run id, runtime/upstream versions, build profile/features, ACP
protocol version, entrypoint arguments, and per-target SHA-256/size metadata.
CapWorks should pin and verify this manifest rather than trusting a `grok`
binary found on `PATH`.

## Updating the pinned runtime

When CapWorks needs a newer Grok revision:

1. land and review required compatibility patches in this repository;
2. run the release workflow against the candidate full commit;
3. require both target builds and ACP smoke tests to pass;
4. publish the immutable `capworks-runtime-<short revision>` release;
5. update CapWorks' Runtime lock/manifest reference and checksums;
6. run CapWorks Runtime integration tests before merging the pin update.

A release tag must never be repointed to a different source commit. Re-running
CI for an existing tag may replace assets only when the tag still targets the
same full revision.
