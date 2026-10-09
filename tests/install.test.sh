#!/bin/sh
# Runs install.sh piped from curl, as a downloaded copy, and from a checkout — with a fake git
# (clone = copy of this repo) and an isolated HOME — and checks the CLI and skill links.
# Nothing outside a temp dir is touched.
#   sh tests/install.test.sh
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/git" <<GIT
#!/bin/sh
for dest; do :; done
case "\$*" in *clone*) mkdir -p "\$dest" && cp -R "$REPO/." "\$dest/" ;; esac
GIT
chmod +x "$TMP/bin/git"
export PATH="$TMP/bin:$PATH"

fail() { echo "FAIL: $1"; exit 1; }
fresh() { rm -rf "$TMP/home"; mkdir -p "$TMP/home"; export HOME="$TMP/home"; }
CO="$TMP/home/.agents/use-family/image-use"

fresh; out=$(sh < "$REPO/install.sh")
[ "$(readlink "$HOME/.local/bin/image-use")" = "$CO/image-use" ] || fail "piped: CLI linked to $(readlink "$HOME/.local/bin/image-use")"
[ "$(readlink "$HOME/.agents/skills/image-use")" = "$CO" ] || fail "piped: skill linked to $(readlink "$HOME/.agents/skills/image-use")"
[ -L "$HOME/.claude/skills/image-use" ] || fail "piped: no Claude Code skill link without the plugin"
"$HOME/.local/bin/image-use" --version | grep -q '^image-use [0-9]' || fail "piped: linked CLI does not run"
echo "$out" | grep -q '^installed image-use [0-9]' || fail "piped: no installed line"

fresh; mkdir -p "$TMP/dl"; cp "$REPO/install.sh" "$TMP/dl/install.sh"; sh "$TMP/dl/install.sh" >/dev/null
[ "$(readlink "$HOME/.local/bin/image-use")" = "$CO/image-use" ] || fail "downloaded copy: CLI linked to $(readlink "$HOME/.local/bin/image-use")"

fresh; mkdir -p "$HOME/.claude/plugins"; echo '{"plugins":{"image-use@leeguooooo-plugins":[]}}' > "$HOME/.claude/plugins/installed_plugins.json"
sh "$REPO/install.sh" >/dev/null
[ "$(readlink "$HOME/.local/bin/image-use")" = "$REPO/image-use" ] || fail "checkout: CLI not linked to the checkout"
[ -e "$HOME/.claude/skills/image-use" ] && fail "checkout: second Claude Code skill copy next to the plugin"

echo "ok install.sh"
