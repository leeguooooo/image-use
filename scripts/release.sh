#!/bin/sh
# Release image-use: bump __version__ + SKILL.md + WHATSNEW, test, push to main. CI then runs and
# release.yml tags vX.Y.Z and creates the GitHub Release; after that, sync the plugin marketplace
# so Claude Code plugin installs pick the new version up right away (no token: uses your `gh` login).
#   scripts/release.sh [--dry-run] 0.30.1 "one-line what's new (shown in the update reminder)"
set -eu
run_ok() {  # run_ok <run-id> [-R owner/repo]: wait until the run completes (gh run watch can drop on a network error), then require success
  _r=$1; shift
  until [ "$(gh run view "$_r" "$@" --json status -q .status 2>/dev/null)" = completed ]; do gh run watch "$_r" "$@" >/dev/null 2>&1 || sleep 15; done
  [ "$(gh run view "$_r" "$@" --json conclusion -q .conclusion)" = success ]
}
DRY=
[ "${1:-}" = --dry-run ] && { DRY=1; shift; }
V=${1:?usage: scripts/release.sh [--dry-run] <version> <whatsnew>}
NOTE=${2:?usage: scripts/release.sh [--dry-run] <version> <whatsnew>}
REPO=leeguooooo/image-use
MARKETPLACE=leeguooooo/plugins
cd "$(dirname "$0")/.."

[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "error: not on main" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "error: working tree not clean" >&2; exit 1; }
git pull -q --ff-only
[ -z "$(git ls-remote --tags origin "refs/tags/v$V")" ] || { echo "error: v$V already exists" >&2; exit 1; }

[ -z "$DRY" ] || trap 'git checkout -q -- image-use skills/image-use/SKILL.md skills/image-use/image-use' EXIT
# The three things release.yml checks: CLI __version__, SKILL.md version, a WHATSNEW line for it.
V="$V" NOTE="$NOTE" python3 - <<'PY'
import os, re
from pathlib import Path
v, note = os.environ["V"], os.environ["NOTE"]
cli = Path("image-use").read_text(encoding="utf-8")
cli = re.sub(r'^__version__ = ".*"$', f'__version__ = "{v}"', cli, count=1, flags=re.M)
cli = re.sub(r"^(# WHATSNEW\[)", lambda m: f"# WHATSNEW[{v}]: {note}\n" + m.group(1), cli, count=1, flags=re.M)
Path("image-use").write_text(cli, encoding="utf-8")
Path("skills/image-use/image-use").write_bytes(Path("image-use").read_bytes())
skill = Path("skills/image-use/SKILL.md").read_text(encoding="utf-8")
Path("skills/image-use/SKILL.md").write_text(re.sub(r'^version: ".*"$', f'version: "{v}"', skill, count=1, flags=re.M), encoding="utf-8")
PY
python3 -m py_compile image-use chatgpt-imagegen
python3 -m unittest test_image_use -q

if [ -n "$DRY" ]; then
  git --no-pager diff
  echo "dry run: reverted, nothing committed or pushed"
  exit 0
fi
git commit -qam "chore(release): v$V"
git push -q origin main
SHA=$(git rev-parse HEAD)

# CI on the push must pass; release.yml then tags v$V and creates the Release. Wait for it.
i=0
until RUN=$(gh run list -R "$REPO" -w ci.yml -c "$SHA" -L 1 --json databaseId -q '.[0].databaseId') && [ -n "$RUN" ]; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "error: no CI run for $SHA" >&2; exit 1; }; sleep 5
done
run_ok "$RUN" -R "$REPO" || { echo "error: CI run $RUN failed; no release" >&2; exit 1; }
i=0
until gh release view "v$V" -R "$REPO" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "error: release v$V did not appear; check release.yml" >&2; exit 1; }; sleep 5
done
echo "released v$V"

# The marketplace reads the version from the latest release tag; run its sync now instead of waiting for the hourly cron.
gh workflow run auto-sync-versions.yml -R "$MARKETPLACE"
sleep 5
RUN=$(gh run list -R "$MARKETPLACE" -w auto-sync-versions.yml -e workflow_dispatch -L 1 --json databaseId -q '.[0].databaseId')
run_ok "$RUN" -R "$MARKETPLACE" && echo "marketplace synced" || echo "warn: marketplace sync run $RUN failed; the hourly run will retry"
gh api "repos/$MARKETPLACE/contents/.claude-plugin/marketplace.json" -q .content | base64 -d \
  | python3 -c "import json,sys; print('marketplace image-use:', next(p['version'] for p in json.load(sys.stdin)['plugins'] if p['name']=='image-use'))"
