# Intel Mac installer

This fork adds an Intel (`x86_64`) build of the Hermes setup application.
It is a community build of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), under the original [MIT license](../../LICENSE).

The upstream `Hermes-Setup.dmg` contains an ARM-only launcher. The setup
application and its install script already support Intel; the missing part is
building and distributing the `x86_64-apple-darwin` launcher.

## Install

Use `Hermes-Setup-0.21.7-r2-mac-x64.dmg` on a 64-bit Intel Mac running macOS 12 or
newer. Open the disk image, copy `Hermes.app` to Applications, and launch it.
If Hermes is already installed, back it up before replacing the application.

This is an **online setup application**, not a bundle containing the complete
agent. It downloads the official source and dependencies, then builds Desktop
on the destination Mac. Internet access, disk space, and Xcode Command Line
Tools are required. The installation flow can prompt to install the tools.
The installed app uses the normal Hermes data directory and source-update
mechanism.

The 0.21.7 Intel installer pins installation to upstream commit
`71d34bad1045610252eda02ee79549436896e784` (`v0.21.7`). This pin deliberately
refers to the upstream repository: setup retrieves its install script from
NousResearch, not from a fork-only commit.

The Intel packaging configuration sets its deployment floor to macOS 12 and
uses the existing ICNS icon. This avoids an Xcode 27 Icon Composer compilation
failure and keeps the icon compatible with older macOS versions.

The installer's own upstream deployment floor is macOS 11. The Desktop app's
Electron 40 runtime requires macOS 12; an installer that launches on macOS 11
does not make the complete app compatible with macOS 11.

## Build locally

On macOS, install Node/npm, Rust, and Xcode Command Line Tools. From the repository
root, install only the installer workspace and the Intel Rust target:

```sh
npm ci --workspace apps/bootstrap-installer --include-workspace-root=false --no-audit --no-fund
rustup target add x86_64-apple-darwin
export CARGO_TARGET_DIR="$HOME/.cache/hermes-bootstrap-intel-target"
HERMES_BUILD_PIN_COMMIT=71d34bad1045610252eda02ee79549436896e784 \
HERMES_BUILD_PIN_BRANCH=main \
npm run tauri:build --workspace apps/bootstrap-installer -- \
  --target x86_64-apple-darwin --config src-tauri/tauri.intel.conf.json --bundles app,dmg
```

Outputs are under
`$CARGO_TARGET_DIR/x86_64-apple-darwin/release/bundle/`.
Keep the Cargo target directory outside the source tree so formatters cannot
modify generated assets. Tauri caches Brotli-compressed frontend files using
`.html`, `.js`, and `.css` extensions; they must never be formatted as text.
If these files were modified, use a fresh target directory and rebuild.
For a distributable build, set `APPLE_SIGNING_IDENTITY` to your Developer ID
Application certificate and use [Tauri's macOS signing and notarization process](https://v2.tauri.app/distribute/sign/macos/).
Do not redistribute an unsigned local build as a notarized release.

## Verify

```sh
app="$CARGO_TARGET_DIR/x86_64-apple-darwin/release/bundle/macos/Hermes.app"
lipo -archs "$app/Contents/MacOS/Hermes-Setup"  # must be x86_64
codesign --verify --deep --strict "$app"
xcrun stapler validate "$app"
spctl --assess --type execute --verbose=2 "$app"
```

The `Bootstrap installer build` workflow builds both architectures, preserves
the legacy ARM filename, and uploads the Intel result separately as
`Hermes-Setup-x64.dmg`. The Intel job defaults to the same immutable upstream installation pin; an
explicit `pin_commit` input overrides it. ARM keeps its existing branch-follow
default. Both jobs retain signing, notarization, and an explicit
binary-architecture check. They may cross-compile on Apple Silicon; this does
not establish that installation works on a physical Intel Mac.

For release acceptance, use a clean Intel Mac: run setup through completion,
launch Desktop, open its integrated terminal, and send a chat with a configured
provider. Record the macOS version and result. Architecture inspection,
Gatekeeper acceptance, and a Rosetta launch are narrower checks.

## Installer revisions

Revision r2 replaces r1, which displayed `asset not found: index.html` at startup.
A local formatter had damaged a compressed HTML build artifact; rebuilding
from a fresh external Cargo target directory restores the embedded frontend.
The application version and pinned upstream installation commit are unchanged.

The `Intel installer UI smoke` workflow downloads a published DMG and opens it
on a native Intel macOS runner. It captures the app window and checks the welcome
text and Install button using Vision OCR, rejecting missing assets and capture
failures. This verifies the setup screen only; it does not click Install or
claim that the complete Desktop installation has been tested.
