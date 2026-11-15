#!/usr/bin/env bash
# install.sh — check dependencies and put `takeout-photos` on PATH.
#
# Local use:   ./install.sh
# Remote use:  curl -fsSL https://raw.githubusercontent.com/whyname/takeout-photos/main/install.sh | bash

set -euo pipefail

REPO_URL="https://github.com/whyname/takeout-photos.git"
DATA_DIR="${HOME}/.local/share/takeout-photos"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- locate code
here=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -z "$here" || ! -f "$here/restore_from_zips.py" ]]; then
  command -v git >/dev/null 2>&1 || die "git is required for the remote installer"
  say "→ downloading takeout-photos into $DATA_DIR"
  mkdir -p "$(dirname "$DATA_DIR")"
  if [[ -d "$DATA_DIR/.git" ]]; then
    git -C "$DATA_DIR" pull --ff-only --quiet
  else
    git clone --depth 1 --quiet "$REPO_URL" "$DATA_DIR"
  fi
  here="$DATA_DIR"
fi

# ---------------------------------------------------------------- dependencies
command -v python3 >/dev/null 2>&1 || die "python3 not found (macOS: xcode-select --install)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "python3 >= 3.9 required (found $(python3 -V 2>&1))"

if command -v exiftool >/dev/null 2>&1; then
  say "✓ exiftool $(exiftool -ver)"
elif command -v brew >/dev/null 2>&1; then
  say "→ installing exiftool (the only required dependency)"
  brew install exiftool
else
  die "exiftool missing and Homebrew not found. Install Homebrew (https://brew.sh) or exiftool, then rerun."
fi

chmod +x "$here/restore_from_zips.py" "$here/restore_gp_metadata.py"

# ---------------------------------------------------------------- PATH link
bindir="${TAKEOUT_BINDIR:-}"
if [[ -z "$bindir" ]]; then
  for candidate in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/bin"; do
    if [[ -d "$candidate" && -w "$candidate" ]]; then bindir="$candidate"; break; fi
  done
fi
if [[ -z "$bindir" ]]; then
  bindir="$HOME/.local/bin"
  mkdir -p "$bindir"
fi
mkdir -p "$bindir"

ln -sf "$here/restore_from_zips.py" "$bindir/takeout-photos"
ln -sf "$here/restore_gp_metadata.py" "$bindir/takeout-photos-dir"

say ""
say "✓ installed: $bindir/takeout-photos"
say "✓ installed: $bindir/takeout-photos-dir"
case ":$PATH:" in
  *":$bindir:"*) : ;;
  *)
    say ""
    say "! $bindir is not on your PATH. Add this to ~/.zshrc:"
    say "    export PATH=\"$bindir:\$PATH\""
    ;;
esac

say ""
say "下一步 / next:"
say "  1. 把 Takeout 的 zip 放在 ~/Downloads"
say "  2. 运行: takeout-photos --dry-run   # 先看配对率"
say "  3. 运行: takeout-photos             # 正式处理，输出在 ~/Pictures/Takeout"
say "  4. 只把 ~/Pictures/Takeout/待导入 拖进「照片」"
