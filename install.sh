#!/bin/sh
# Install image-use: the `image-use` command and the agent skill for Claude Code / Codex.
#   curl -fsSL https://raw.githubusercontent.com/leeguooooo/image-use/main/install.sh | sh
# Keeps a git checkout under ~/.agents/use-family/image-use like the rest of the *-use family
# (run from a checkout, it uses that one), links the CLI into ~/.local/bin — no sudo — and the
# skill into ~/.agents/skills. Re-run to update; `image-use upgrade` then `git pull`s the checkout.
set -eu
command -v git >/dev/null 2>&1 || { echo "error: git is required" >&2; exit 1; }

# image-use needs Python 3.10+. A stock Mac's python3 is Apple's 3.9, so when python3 is older
# look for a newer interpreter and run the CLI with it through a small wrapper (below).
new_enough() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }
PY=""
if command -v python3 >/dev/null 2>&1 && new_enough python3; then
  PY=python3
else
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    new_enough "$p" && { PY=$p; break; }
  done
  if [ -z "$PY" ] && command -v uv >/dev/null 2>&1; then
    p=$(uv python find '>=3.10' 2>/dev/null) && new_enough "$p" && PY=$p
  fi
fi
if [ -z "$PY" ]; then
  echo "error: image-use needs Python 3.10+; found $(python3 -V 2>&1 || echo 'no python3')." >&2
  echo "  Install one, then re-run:  brew install python   (or: uv python install 3.12)" >&2
  exit 1
fi

SELF_DIR=""
case "$0" in */install.sh) SELF_DIR=$(cd "$(dirname "$0")" && pwd) ;; esac
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/SKILL.md" ] && [ -f "$SELF_DIR/image-use" ]; then
  ROOT=$SELF_DIR
else
  ROOT="${IMAGE_USE_HOME:-${USE_FAMILY_DIR:-$HOME/.agents/use-family}/image-use}"
  if [ -d "$ROOT/.git" ]; then
    git -C "$ROOT" pull -q --ff-only || echo "warn: $ROOT not updated (local changes?)"
  else
    mkdir -p "$(dirname "$ROOT")"
    git clone -q --depth 1 https://github.com/leeguooooo/image-use.git "$ROOT"
  fi
fi

link() {  # link <target> <link-path>; never replaces a real file or directory
  if [ -e "$2" ] && [ ! -L "$2" ]; then
    echo "skip: $2 exists and is not a symlink (move it aside, then re-run)"
  else
    ln -sfn "$1" "$2" && echo "linked: $2 -> $1"
  fi
}

BIN="${IMAGE_USE_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills" "$BIN"
chmod +x "$ROOT/image-use"
WRAP_MARK="# image-use wrapper written by install.sh"
if [ -f "$BIN/image-use" ] && [ ! -L "$BIN/image-use" ] && grep -qF "$WRAP_MARK" "$BIN/image-use"; then
  rm -f "$BIN/image-use"  # our own wrapper from an earlier run: rewrite it or swap it for a link
fi
if [ "$PY" = python3 ]; then
  link "$ROOT/image-use" "$BIN/image-use"
elif [ -e "$BIN/image-use" ]; then
  echo "skip: $BIN/image-use exists and is not ours (move it aside, then re-run)"
else
  # The script's `#!/usr/bin/env python3` would pick the old python3, so run it with $PY.
  # It still executes from the checkout, so `image-use upgrade` keeps its git route.
  printf '#!/bin/sh\n%s\nexec "%s" "%s" "$@"\n' "$WRAP_MARK ($PY)" "$PY" "$ROOT/image-use" > "$BIN/image-use"
  chmod +x "$BIN/image-use"
  echo "wrapped: $BIN/image-use runs $ROOT/image-use with $PY ($("$PY" -V 2>&1))"
fi
link "$ROOT" "$HOME/.agents/skills/image-use"
# Claude Code plugin installed → it already provides the skill; a second copy would load twice.
if grep -q '"image-use@' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
  echo "skip: $HOME/.claude/skills/image-use (Claude Code plugin image-use already provides the skill)"
else
  link "../../.agents/skills/image-use" "$HOME/.claude/skills/image-use"
fi
[ -d "$HOME/.codex/skills" ] && link "$ROOT" "$HOME/.codex/skills/image-use"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to PATH to run \`image-use\` directly" ;; esac

echo "installed $(USE_NO_UPDATE_CHECK=1 "$PY" "$ROOT/image-use" --version 2>/dev/null | head -n 1)"
echo "next: image-use doctor"
