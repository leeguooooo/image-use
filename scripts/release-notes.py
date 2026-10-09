#!/usr/bin/env python3
"""Print the GitHub Release notes for one version, in Markdown.

    scripts/release-notes.py 0.31.0 [TARGET] [PREVIOUS_TAG]

TARGET defaults to HEAD; PREVIOUS_TAG to the newest tag reachable from
TARGET's parent. Most releases here are pushed straight to main, so GitHub's
--generate-notes (which lists merged PRs) comes out as a bare "Full
Changelog" link. This builds the notes from the repo itself instead: the
WHATSNEW line the update reminder shows, the commits in the range, and the
outside contributors, credited by GitHub handle.
"""
import re
import subprocess
import sys

REPO = "leeguooooo/image-use"
OWNERS = {"leeguooooo", "leeguoo"}
# The update reminder's one-liner for each version (see the top of image-use).
WHATSNEW_RE = r"^# WHATSNEW\[{}\]:\s*(.+)$"
NOREPLY_RE = re.compile(r"(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com", re.I)


def md(text: str) -> str:
    """Keep `<conversation-url>`-style placeholders visible: GitHub drops unknown tags."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def notes(version: str, target: str = "HEAD", previous: str | None = None) -> str:
    if previous is None:
        previous = git("describe", "--tags", "--abbrev=0", f"{target}^").strip()
    try:
        cli = git("show", f"{target}:image-use")
    except subprocess.CalledProcessError:  # before the rename (v0.28)
        cli = git("show", f"{target}:chatgpt-imagegen")
    m = re.search(WHATSNEW_RE.format(re.escape(version)), cli, re.M)
    summary = md(m.group(1).strip()) if m else ""

    # %x1f/%x1e separate fields/records; bodies hold Co-authored-by trailers.
    log = git("log", "--format=%h%x1f%s%x1f%ae%x1f%b%x1e", f"{previous}..{target}")
    changes, thanks = [], []

    def credit(login: str) -> None:
        if login.lower() not in OWNERS and login not in thanks:
            thanks.append(login)

    for rec in filter(None, (r.strip("\n") for r in log.split("\x1e"))):
        sha, subject, email, body = (rec.split("\x1f") + [""] * 4)[:4]
        merged = re.match(r"Merge pull request #\d+ from ([^/\s]+)/", subject)
        if merged:
            credit(merged.group(1))
            continue
        if subject.startswith("chore(release)") or subject.startswith("Merge "):
            continue
        for addr in [email] + re.findall(r"^Co-authored-by:.*<([^>]+)>", body, re.M):
            hit = NOREPLY_RE.fullmatch(addr.strip())
            if hit:
                credit(hit.group(1))
        subject = re.sub(r"\(#(\d+)\)", rf"([#\1](https://github.com/{REPO}/pull/\1))", md(subject))
        changes.append(f"- {subject} ({sha})")

    out = []
    if summary:
        out += [summary, ""]
    if changes:
        out += ["## Changes", "", *changes, ""]
    if thanks:
        out += ["Thanks " + ", ".join(f"@{t}" for t in thanks) + " for contributing.", ""]
    out.append(f"**Full Changelog**: https://github.com/{REPO}/compare/{previous}...v{version}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 4:
        sys.exit(__doc__)
    sys.stdout.write(notes(*sys.argv[1:]))
