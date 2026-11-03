# inhere-box desktop (Wails v3 + green portable)

All desktop-client code lives here. This is **not** `src/inhere_box/infra/desktop`
(remote desktop streaming).

| Path | Role |
|------|------|
| [`portable/`](portable/) | Green zip packaging (was `scripts/green/`) |
| [`src/`](src/) | Wails v3 shell: load bundled zip, spawn inhere-box, tray/settings |
| [`package-release.sh`](package-release.sh) | Native end-to-end portable + Wails release build |

## Data directory

Same as the inhere-box CLI/server default:

- `INHERE_BOX_HOME` → `~/.inhere-box` (or the existing `INHERE_BOX_HOME` env)
- Green runtime extract → `~/.inhere-box/portable/`
- Shell prefs → `~/.inhere-box/desktop-settings.json`

## Build green zip

From repo root:

```bash
make -f desktop/portable/Makefile green
```

CI: `.github/workflows/inhere-box-desktop.yml` builds native platform/arch variants.
`v*` tags and `workflow_dispatch` (platforms=`all`) run all six; pull requests
that touch `desktop/` build `darwin-*` and `windows-*` (amd64 + arm64). Each job
first creates the green zip and then packages the matching Wails application.

## Build a complete desktop release

Run the end-to-end script on the matching native host. It builds the Dashboard,
creates and verifies the portable runtime, embeds it into Wails, and produces
the final native package:

```bash
desktop/package-release.sh
# Reuse an existing desktop/portable/release/inhere-box-portable-<plat>-<version>.zip:
desktop/package-release.sh darwin-arm64 --reuse-portable
```

Wails requires native packaging, so all six variants are produced by the CI
matrix on macOS, Linux, and Windows runners rather than cross-compiled locally.
Windows packaging also needs [NSIS](https://nsis.sourceforge.io/) (`makensis`)
so the `.exe` is an installer rather than a portable single-file binary.

## Build the Wails shell

Run these from **`desktop/src`** (that directory contains `Taskfile.yml` and
`build/config.yml`). Requires **Go 1.25+**, [Wails v3](https://v3.wails.io/)
`v3.0.0-beta.13`.

```bash
go install github.com/wailsapp/wails/v3/cmd/wails3@v3.0.0-beta.13
cd desktop/src
go mod tidy
wails3 build            # development binary under desktop/src/bin/
wails3 task package ARCH=arm64 VERSION=<version> \
  PORTABLE_ZIP=../portable/release/inhere-box-portable-darwin-arm64-<version>.zip
```

Dev against an already-running inhere-box (skips the bundled green zip):

```bash
cd desktop/src
INHERE_BOX_DESKTOP_URL=http://127.0.0.1:8088 wails3 dev
```

Without `INHERE_BOX_DESKTOP_URL`, first launch uses `~/.inhere-box/portable/` if valid,
otherwise extracts the matching zip shipped with the desktop package (embedded
in the Windows and Linux binaries, under `Contents/Resources` on macOS). The
Wails shell never downloads inhere-box. For local runtime debugging, set
`INHERE_BOX_DESKTOP_PORTABLE_ZIP=/absolute/path/inhere-box-portable-<plat>-<version>.zip`.
On later launches, a newer bundled portable version replaces the extracted
runtime after creating a consistent SQLite backup under `~/.inhere-box/backups/`.
The upgraded inhere-box process then applies the normal database migrations during
startup. Newer extracted runtimes are never downgraded; PostgreSQL remains
externally managed and is not copied by the desktop shell.

### One-time version marker migration

Before upgrading an older extracted runtime or reusing an old portable ZIP, migrate
`VERSION.txt` once. Normal desktop version parsing now reads only
`inhere_box_version=`; it no longer falls back to `octop_version=`.

From the repository root, preview first, then apply to the same path:

```bash
uv run python scripts/migrate_portable_branding.py "/path/to/portable"
uv run python scripts/migrate_portable_branding.py "/path/to/portable" --apply
```

The argument may be an extracted runtime directory, its `VERSION.txt`, or a
portable ZIP. Multiple paths are accepted. Applying creates a content-addressed
`.before-inhere-box-<hash>` backup beside the original, then replaces it atomically.
The JSON result records the backup and original SHA-256. Rerunning a migrated file
returns `current` without changing it. Existing new markers take precedence.

This script only migrates version-marker fields. Runtime files, package metadata,
database records, messages, and checkpoints are not renamed or moved. ZIP entries
retain their contents except for the version marker; the replacement ZIP is CRC
checked before it is published. New portable builds already write the new key.

GitHub Release names follow `inhere-box-<kind>-<os>-<arch>-<version>.<ext>`:

- Desktop GUI: `inhere-box-desktop-<plat>-<version>.dmg` (macOS; open and drag
  `inhere-box.app` into Applications), `.exe` (Windows NSIS installer — copies
  into `Program Files\inhere-box` and creates Start Menu + desktop shortcuts),
  `.tar.gz` (Linux)
- Green runtime zip: `inhere-box-portable-<plat>-<version>.zip`
- PyPI wheels stay `inhere-box-<version>-py3-none-any.whl` (PEP 427)

The Linux tar.gz contains only the GUI binary; it has no separate portable zip or
server terminal process. Runtime upgrades remain owned by inhere-box:
the shell sets `INHERE_BOX_GREEN_PACKAGES`, so `inhere-box update` upgrades the extracted
`packages/` directory through inhere-box's existing `--target` logic.

Linux also needs GTK4 + WebKitGTK 6 to link. macOS 12+.

## Icons

| File | Used for | Rule |
|------|----------|------|
| `src/build/appicon.png` | Windows `.ico`, Linux | Full-bleed 512x512 artwork |
| `src/build/appicon-macos.png` | macOS `.icns` | 1024x1024 canvas, artwork 824x824 centred |
| `src/assets/tray-icon.png` | Tray + app icon on Windows/Linux | Full-bleed |
| `src/assets/tray-icon-template.png` | macOS menu bar | 88px canvas, 64px black-on-transparent glyph |

macOS sizes both surfaces to a fixed box, so the padding has to live in the
artwork: the Dock follows Apple's 824/1024 icon grid, and Wails scales the menu
bar image to the full `NSStatusBar` thickness (22pt) where the glyph should be
~16pt. Full-bleed sources on either surface render a size bigger than every
other app. On macOS the Dock icon comes from the bundle's `icons.icns` only —
see `applyAppIcon` in `src/icons_darwin.go`.
