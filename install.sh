#!/bin/sh
# Install image-use: the `image-use` command and the agent skill for Claude Code / Codex.
#   curl -fsSL https://raw.githubusercontent.com/leeguooooo/image-use/main/install.sh | sh
# Keeps a git checkout under ~/.agents/use-family/image-use like the rest of the *-use family
# (run from a checkout, it uses that one), links the CLI into ~/.local/bin — no sudo — and the
# skill into ~/.agents/skills. Re-run to update; `image-use upgrade` then `git pull`s the checkout.
set -eu
command -v git >/dev/null 2>&1 || { echo "error: git is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 3.10+ is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || { echo "error: image-use needs Python 3.10+, found $(python3 -V 2>&1)" >&2; exit 1; }

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
link "$ROOT/image-use" "$BIN/image-use"
link "$ROOT" "$HOME/.agents/skills/image-use"
# Claude Code plugin installed → it already provides the skill; a second copy would load twice.
if grep -q '"image-use@' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
  echo "skip: $HOME/.claude/skills/image-use (Claude Code plugin image-use already provides the skill)"
else
  link "../../.agents/skills/image-use" "$HOME/.claude/skills/image-use"
fi
[ -d "$HOME/.codex/skills" ] && link "$ROOT" "$HOME/.codex/skills/image-use"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to PATH to run \`image-use\` directly" ;; esac

echo "installed $(USE_NO_UPDATE_CHECK=1 "$ROOT/image-use" --version 2>/dev/null | head -n 1)"
echo "next: image-use doctor"
