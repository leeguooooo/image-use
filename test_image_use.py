#!/usr/bin/env python3
"""Unit tests for image-use's pure helpers — stdlib `unittest`, no deps.

The CLI ships as a single extension-less script, so we load it as a module via
the SourceFileLoader trick. These cover the browser-free logic (MIME sniffing,
version parsing, prompt building, path defaults, token extraction, the capped
ref download) so a refactor that breaks them fails loudly.

Run:  python3 -m unittest test_image_use -v
"""

import argparse
import base64
import html.parser
import importlib.machinery
import importlib.util
import inspect
import io
import sys
import time
import json
import os
import re
import ssl
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import contextmanager
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path

_loader = importlib.machinery.SourceFileLoader(
    "cig", os.path.join(os.path.dirname(__file__), "image-use"))
_spec = importlib.util.spec_from_loader("cig", _loader)
cig = importlib.util.module_from_spec(_spec)
_loader.exec_module(cig)


@contextmanager
def _in_tmp_cwd():
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            yield Path(d)
        finally:
            os.chdir(prev)


@contextmanager
def _tmp_xdg():
    """Isolate styles.json under a temp XDG_CONFIG_HOME so tests never touch ~/.config."""
    prev = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["XDG_CONFIG_HOME"] = d
        try:
            yield Path(d)
        finally:
            if prev is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prev


class SniffMime(unittest.TestCase):
    def test_png(self):
        self.assertEqual(cig._sniff_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8), "image/png")

    def test_jpeg(self):
        self.assertEqual(cig._sniff_mime(b"\xff\xd8\xff" + b"\x00" * 8), "image/jpeg")

    def test_webp(self):
        self.assertEqual(cig._sniff_mime(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP"), "image/webp")

    def test_unknown(self):
        self.assertIsNone(cig._sniff_mime(b"not an image at all"))


class VersionTuple(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(cig._version_tuple("1.5.23"), (1, 5, 23))
        self.assertEqual(cig._version_tuple("chrome-use 1.5.23"), (1, 5, 23))

    def test_ordering(self):
        self.assertLess(cig._version_tuple("1.4.9"), cig._version_tuple("1.5.0"))
        self.assertLess(cig._version_tuple("1.5.0"), cig._version_tuple("1.5.23"))

    def test_junk(self):
        self.assertEqual(cig._version_tuple("garbage"), (0,))

    def test_min_floor(self):
        self.assertGreaterEqual(cig._version_tuple("1.5.23"),
                                cig._version_tuple(cig.AB_MIN_VERSION))
        self.assertLess(cig._version_tuple("1.4.0"),
                        cig._version_tuple(cig.AB_MIN_VERSION))


_NO_CHECK_ENV = ("CI", "USE_NO_UPDATE_CHECK", "IMAGE_USE_NO_UPDATE_CHECK",
                 "CHATGPT_IMAGEGEN_NO_UPDATE_CHECK")


@contextmanager
def _update_env(**extra):
    """A temp XDG_CACHE_HOME with every opt-out variable cleared (CI sets `CI`),
    plus ``extra``. The network is never reached: callers patch the fetch."""
    with tempfile.TemporaryDirectory() as d:
        env = {k: v for k, v in os.environ.items() if k not in _NO_CHECK_ENV}
        env["XDG_CACHE_HOME"] = d
        env.update(extra)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            yield Path(d)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UpdateCheck(unittest.TestCase):
    """The once-a-day notice: version comparison, 24 h throttle, opt-outs,
    one line on stderr only — and no network (the fetch is patched)."""

    @contextmanager
    def _patched_fetch(self, version, counter=None):
        def fake(timeout=cig.UPDATE_FETCH_TIMEOUT):
            if counter is not None:
                counter.append(timeout)
            return version
        with unittest.mock.patch.object(cig, "_fetch_latest_release", fake):
            yield

    def test_version_comparison(self):
        self.assertTrue(cig._is_newer("0.30.0", "0.29.2"))
        self.assertTrue(cig._is_newer("0.29.10", "0.29.9"))
        self.assertTrue(cig._is_newer("1.0.0", "0.99.99"))
        self.assertFalse(cig._is_newer("0.29.2", "0.29.2"))
        self.assertFalse(cig._is_newer("0.29.1", "0.29.2"))
        self.assertFalse(cig._is_newer(None, "0.29.2"))

    def test_release_tag_parsing(self):
        self.assertEqual(cig._release_version("v0.30.0"), "0.30.0")
        self.assertEqual(cig._release_version("0.30.0"), "0.30.0")
        self.assertIsNone(cig._release_version("nightly"))
        self.assertIsNone(cig._release_version(None))

    def test_fetch_reads_release_api_and_honours_github_token(self):
        seen = []

        def fake_urlopen(req, timeout):
            seen.append((req.full_url, req.get_header("Authorization"), timeout))
            return _FakeResponse(json.dumps(
                {"tag_name": "v9.9.9", "prerelease": False}).encode())

        with _update_env(GITHUB_TOKEN="placeholder-token"), \
             unittest.mock.patch.object(cig.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(cig._fetch_latest_release(2.0), "9.9.9")
        self.assertEqual(seen, [(cig.UPDATE_RELEASES_URL,
                                 "Bearer placeholder-token", 2.0)])

    def test_fetch_failure_is_none(self):
        def boom(*a, **k):
            raise cig.urllib.error.URLError("offline")
        with unittest.mock.patch.object(cig.urllib.request, "urlopen", boom):
            self.assertIsNone(cig._fetch_latest_release(2.0))

    def test_prerelease_is_ignored(self):
        body = json.dumps({"tag_name": "v9.9.9", "prerelease": True}).encode()
        with unittest.mock.patch.object(cig.urllib.request, "urlopen",
                                        lambda *a, **k: _FakeResponse(body)):
            self.assertIsNone(cig._fetch_latest_release(2.0))

    def test_notice_is_one_line_on_stderr_only(self):
        with _update_env(), self._patched_fetch("9.9.9"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                cig._maybe_notify_update()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(
            err.getvalue(),
            f"image-use 9.9.9 is available (you have {cig.__version__}). "
            "Upgrade: image-use upgrade\n")

    def test_silent_when_same_or_older(self):
        with _update_env(), self._patched_fetch(cig.__version__):
            msgs = []
            cig._maybe_notify_update(msgs.append)
        self.assertEqual(msgs, [])

    def test_uses_2s_timeout_and_writes_cache(self):
        with _update_env() as cache_home:
            calls = []
            with self._patched_fetch("9.9.9", calls):
                cig._maybe_notify_update(lambda _m: None)
            self.assertEqual(calls, [2.0])
            data = json.loads(
                (cache_home / "image-use" / "update-check.json").read_text())
        self.assertEqual(data["latest"], "9.9.9")
        self.assertAlmostEqual(data["checked_at"], time.time(), delta=60)

    def test_throttled_within_24h(self):
        with _update_env():
            calls = []
            with self._patched_fetch("9.9.9", calls):
                cig._maybe_notify_update(lambda _m: None)      # hits the network
                msgs = []
                cig._maybe_notify_update(msgs.append)          # served from cache
            self.assertEqual(len(calls), 1)
            self.assertTrue(msgs and "9.9.9" in msgs[0])

    def test_rechecks_after_24h(self):
        with _update_env() as cache_home:
            cache = cache_home / "image-use" / "update-check.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps(
                {"checked_at": time.time() - 86400 - 5, "latest": "0.0.1"}))
            calls = []
            with self._patched_fetch("9.9.9", calls):
                msgs = []
                cig._maybe_notify_update(msgs.append)
        self.assertEqual(len(calls), 1)
        self.assertTrue(msgs and "9.9.9" in msgs[0])

    def test_failed_check_is_silent_and_still_stamps_checked_at(self):
        with _update_env() as cache_home:
            calls = []
            with self._patched_fetch(None, calls):
                msgs = []
                cig._maybe_notify_update(msgs.append)
                cig._maybe_notify_update(msgs.append)          # not retried
            data = json.loads(
                (cache_home / "image-use" / "update-check.json").read_text())
        self.assertEqual((len(calls), msgs), (1, []))
        self.assertIsNone(data["latest"])

    def test_failed_check_keeps_last_known_version(self):
        with _update_env() as cache_home:
            cache = cache_home / "image-use" / "update-check.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"checked_at": 0, "latest": "9.9.9"}))
            with self._patched_fetch(None):
                msgs = []
                cig._maybe_notify_update(msgs.append)
        self.assertTrue(msgs and "9.9.9" in msgs[0])

    def test_opt_out_env_vars(self):
        for var in ("CI", "USE_NO_UPDATE_CHECK", "IMAGE_USE_NO_UPDATE_CHECK",
                    "CHATGPT_IMAGEGEN_NO_UPDATE_CHECK"):
            with self.subTest(var=var), _update_env(**{var: "1"}) as cache_home:
                calls, msgs = [], []
                with self._patched_fetch("9.9.9", calls):
                    cig._maybe_notify_update(msgs.append)
                self.assertEqual((calls, msgs), ([], []))
                self.assertFalse((cache_home / "image-use").exists())

    def test_skipped_for_upgrade_version_and_help(self):
        for argv in (["upgrade"], ["upgrade", "--json"], ["update"], ["doctor"],
                     ["--version"], ["-V"], ["--help"], ["-h"],
                     ["a cat", "--help"], ["style", "list", "-h"]):
            with self.subTest(argv=argv):
                self.assertFalse(cig._should_check_for_update(argv))
        for argv in ([], ["a cat"], ["style", "list"], ["a cat", "--", "--help"]):
            with self.subTest(argv=argv):
                self.assertTrue(cig._should_check_for_update(argv))

    def test_main_upgrade_check_does_not_run_notice(self):
        with _update_env(), \
             unittest.mock.patch.object(cig.sys, "argv",
                                        ["image-use", "upgrade", "--check"]), \
             unittest.mock.patch.object(cig, "_maybe_notify_update") as notify, \
             unittest.mock.patch.object(cig, "_fetch_latest_release",
                                        return_value=cig.__version__), \
             unittest.mock.patch.object(cig, "_find_skill_installs", return_value=[]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cig.main(), 0)
        notify.assert_not_called()

    def test_changes_since_filters_and_orders(self):
        notes = {"0.1.0": "old", "9.9.0": "mid", "9.9.9": "new"}
        self.assertEqual(cig._changes_since(notes, base="9.8.0"),
                         [("9.9.9", "new"), ("9.9.0", "mid")])

    def test_parse_whatsnew_from_real_header(self):
        # `upgrade` reads what changed from the first 8KB of the CLI at the
        # release tag, so __version__ and its WHATSNEW line must sit there.
        head = Path(os.path.join(os.path.dirname(__file__),
                                 "image-use")).read_text(encoding="utf-8")[:8192]
        m = re.search(r'__version__\s*=\s*"([\d.]+)"', head)
        self.assertEqual(m.group(1), cig.__version__)
        notes = cig._parse_whatsnew(head)
        self.assertIn(cig.__version__, notes)            # current release is documented
        self.assertTrue(notes[cig.__version__])


class Upgrade(unittest.TestCase):
    """`upgrade` / `update`: --check, --json, exit codes, install routes and
    skill refresh — all against a temp HOME with the network patched."""

    @contextmanager
    def _home(self):
        with tempfile.TemporaryDirectory() as home, _update_env():
            with unittest.mock.patch.object(cig.Path, "home",
                                            return_value=Path(home)):
                yield Path(home)

    def _run(self, argv, latest):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(cig, "_fetch_latest_release",
                                        return_value=latest), \
             redirect_stdout(out), redirect_stderr(err):
            rc = cig._upgrade_command(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_check_update_available(self):
        with self._home():
            rc, out, _ = self._run(["--check"], "9.9.9")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], f"image-use {cig.__version__} -> 9.9.9")

    def test_check_up_to_date(self):
        with self._home():
            rc, out, _ = self._run(["--check"], cig.__version__)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), f"image-use {cig.__version__} is up to date")

    def test_check_failure_exits_2(self):
        with self._home():
            rc, out, err = self._run(["--check"], None)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("could not read the latest release", err)

    def test_json_shape(self):
        with self._home() as home:
            plugins = home / ".claude" / "plugins"
            plugins.mkdir(parents=True)
            (plugins / "installed_plugins.json").write_text(json.dumps(
                {"version": 2, "plugins": {
                    "image-use@leeguooooo-plugins": [
                        {"installPath": str(home / "cache" / "image-use")}],
                    "other@leeguooooo-plugins": [{"installPath": "/x"}]}}))
            copy = home / ".agents" / "skills" / "image-use"
            copy.mkdir(parents=True)
            (copy / "SKILL.md").write_text("---\nname: image-use\n---\n")
            (home / ".claude" / "skills").mkdir(parents=True)
            (home / ".claude" / "skills" / "image-use").symlink_to(copy)
            with unittest.mock.patch.object(cig, "_git_toplevel", return_value=None):
                rc, out, _ = self._run(["--json"], "9.9.9")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data, {
            "name": "image-use", "current": cig.__version__, "latest": "9.9.9",
            "update_available": True,
            "skills": [
                {"channel": "claude-plugin",
                 "path": str(home / "cache" / "image-use"),
                 "update": "claude plugin update image-use@leeguooooo-plugins"},
                {"channel": "copy", "path": str(copy.resolve()),
                 "update": "npx skills update image-use"},
            ]})

    def test_json_failure_exits_2_with_valid_json(self):
        with self._home():
            rc, out, _ = self._run(["--json"], None)
        data = json.loads(out)
        self.assertEqual(rc, 2)
        self.assertIsNone(data["latest"])
        self.assertFalse(data["update_available"])

    def test_git_checkout_skill_is_detected_only_at_its_root(self):
        with self._home() as home:
            clone = home / "src" / "image-use"
            clone.mkdir(parents=True)
            (clone / "SKILL.md").write_text("x")
            (home / ".agents" / "skills").mkdir(parents=True)
            (home / ".agents" / "skills" / "image-use").symlink_to(clone)
            with unittest.mock.patch.object(cig, "_git_toplevel",
                                            return_value=clone.resolve()):
                found = cig._find_skill_installs()
            # A copy that merely sits inside some other repo is not a clone.
            with unittest.mock.patch.object(cig, "_git_toplevel",
                                            return_value=home.resolve()):
                found_nested = cig._find_skill_installs()
        self.assertEqual(found, [{"channel": "git", "path": str(clone.resolve()),
                                  "update": f"git -C {clone.resolve()} pull --ff-only"}])
        self.assertEqual(found_nested[0]["channel"], "copy")

    def test_upgrade_runs_route_then_refreshes_skills(self):
        skills = [{"channel": "git", "path": "/s/image-use",
                   "update": "git -C /s/image-use pull --ff-only"},
                  {"channel": "claude-plugin", "path": "/p",
                   "update": "claude plugin update image-use@leeguooooo-plugins"},
                  {"channel": "copy", "path": "/c",
                   "update": "npx skills update image-use"}]
        steps = []

        def fake_step(argv, cwd=None):
            steps.append(argv)
            return 0, ""

        with self._home(), \
             unittest.mock.patch.object(cig, "_find_skill_installs", return_value=skills), \
             unittest.mock.patch.object(cig, "_cli_install_route",
                                        return_value=("file", Path("/bin/image-use"))), \
             unittest.mock.patch.object(cig, "_replace_script", return_value=0) as repl, \
             unittest.mock.patch.object(cig, "_fetch_whatsnew",
                                        return_value={"9.9.9": "new thing"}), \
             unittest.mock.patch.object(cig, "_run_step", fake_step), \
             unittest.mock.patch.object(cig.shutil, "which",
                                        lambda n: f"/usr/bin/{n}"):
            rc, out, _ = self._run([], "9.9.9")
        self.assertEqual(rc, 0)
        repl.assert_called_once_with(Path("/bin/image-use"), "9.9.9")
        self.assertEqual(steps, [
            ["git", "-C", "/s/image-use", "pull", "--ff-only"],
            ["/usr/bin/claude", "plugin", "update", "image-use@leeguooooo-plugins"]])
        self.assertIn("9.9.9: new thing", out)
        self.assertIn("run: npx skills update image-use", out)   # printed, not run
        self.assertIn("upgraded to 9.9.9", out)

    def test_plugin_refresh_prints_command_without_claude(self):
        skill = {"channel": "claude-plugin", "path": "/p",
                 "update": "claude plugin update image-use@leeguooooo-plugins"}
        with unittest.mock.patch.object(cig.shutil, "which", return_value=None), \
             unittest.mock.patch.object(cig, "_run_step") as step:
            out = io.StringIO()
            with redirect_stdout(out):
                cig._refresh_skill(skill, "file", Path("/x"))
        step.assert_not_called()
        self.assertIn("run: claude plugin update image-use@leeguooooo-plugins",
                      out.getvalue())

    def test_git_pull_failure_is_reported_not_forced(self):
        skill = {"channel": "git", "path": "/s", "update": "git -C /s pull --ff-only"}
        with unittest.mock.patch.object(cig, "_run_step",
                                        return_value=(1, "Not possible to fast-forward")):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cig._refresh_skill(skill, "file", Path("/x"))
        self.assertEqual(rc, 1)
        self.assertIn("not updated", out.getvalue())
        self.assertIn("Not possible to fast-forward", out.getvalue())

    def test_up_to_date_still_pulls_the_clone(self):
        # With nothing installed in step 1, a clone must not be reported as
        # "pulled above" — it gets its own pull.
        skill = {"channel": "git", "path": "/s", "update": "git -C /s pull --ff-only"}
        with self._home(), \
             unittest.mock.patch.object(cig, "_find_skill_installs", return_value=[skill]), \
             unittest.mock.patch.object(cig, "_cli_install_route",
                                        return_value=("git", Path("/s"))), \
             unittest.mock.patch.object(cig, "_run_step", return_value=(0, "")) as step:
            rc, out, _ = self._run([], cig.__version__)
        self.assertEqual(rc, 0)
        step.assert_called_once_with(["git", "-C", "/s", "pull", "--ff-only"])
        self.assertIn("pulled", out)

    def test_up_to_date_installs_nothing(self):
        with self._home(), \
             unittest.mock.patch.object(cig, "_find_skill_installs", return_value=[]), \
             unittest.mock.patch.object(cig, "_install_cli") as install:
            rc, out, _ = self._run([], cig.__version__)
        self.assertEqual(rc, 0)
        install.assert_not_called()
        self.assertIn("is up to date", out)

    def test_replace_script_swaps_file_for_release(self):
        body = (b'#!/usr/bin/env python3\n__version__ = "9.9.9"\n')
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "image-use"
            target.write_text("old")
            with unittest.mock.patch.object(cig.urllib.request, "urlopen",
                                            lambda *a, **k: _FakeResponse(body)):
                rc = cig._replace_script(target, "9.9.9")
            self.assertEqual(rc, 0)
            self.assertEqual(target.read_bytes(), body)
            self.assertTrue(os.access(target, os.X_OK))

    def test_replace_script_rejects_wrong_payload(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "image-use"
            target.write_text("old")
            with unittest.mock.patch.object(
                    cig.urllib.request, "urlopen",
                    lambda *a, **k: _FakeResponse(b"<html>not found</html>")), \
                 redirect_stderr(io.StringIO()):
                rc = cig._replace_script(target, "9.9.9")
            self.assertEqual(rc, 2)
            self.assertEqual(target.read_text(), "old")

    def test_route_detection(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = Path(d) / ".claude" / "plugins" / "cache" / "x" / "image-use"
            skill = Path(d) / "skills" / "image-use"
            loose = Path(d) / "bin"
            for folder in (plugin, skill, loose):
                folder.mkdir(parents=True)
            (skill / "SKILL.md").write_text("x")
            with unittest.mock.patch.object(cig, "_git_toplevel", return_value=None):
                for folder, route in ((plugin, "claude-plugin"), (skill, "skills"),
                                      (loose, "file")):
                    with unittest.mock.patch.object(cig, "__file__",
                                                    str(folder / "image-use")):
                        self.assertEqual(cig._cli_install_route()[0], route)
            with unittest.mock.patch.object(cig, "_git_toplevel",
                                            return_value=skill.resolve()), \
                 unittest.mock.patch.object(cig, "_git_is_this_repo", return_value=True), \
                 unittest.mock.patch.object(cig, "__file__", str(skill / "image-use")):
                self.assertEqual(cig._cli_install_route()[0], "git")
            # Some other repo that happens to hold SKILL.md + the script:
            # keep the skills route, never pull it.
            with unittest.mock.patch.object(cig, "_git_toplevel",
                                            return_value=skill.resolve()), \
                 unittest.mock.patch.object(cig, "_git_is_this_repo", return_value=False), \
                 unittest.mock.patch.object(cig, "__file__", str(skill / "image-use")):
                self.assertEqual(cig._cli_install_route()[0], "skills")

    def test_lone_script_at_root_of_unrelated_repo_is_a_file_route(self):
        # e.g. ~/bin is a dotfiles repo: never `git pull` it.
        with tempfile.TemporaryDirectory() as d:
            loose = Path(d) / "bin"
            loose.mkdir()
            with unittest.mock.patch.object(cig, "_git_toplevel",
                                            return_value=loose.resolve()), \
                 unittest.mock.patch.object(cig, "_git_is_this_repo", return_value=False), \
                 unittest.mock.patch.object(cig, "__file__", str(loose / "image-use")):
                self.assertEqual(cig._cli_install_route()[0], "file")

    def test_daily_fetch_is_wall_clock_bounded(self):
        release = threading.Event()

        def hang(timeout=cig.UPDATE_FETCH_TIMEOUT):
            release.wait(5)  # e.g. a stalled DNS lookup urlopen can't time out
            return "9.9.9"
        try:
            with unittest.mock.patch.object(cig, "_fetch_latest_release", hang):
                t0 = time.monotonic()
                self.assertIsNone(cig._fetch_latest_release_bounded(0.1))
                self.assertLess(time.monotonic() - t0, 2)
        finally:
            release.set()

    def test_cache_write_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": d}):
            cig._write_update_cache(1.0, "1.2.3")
            cig._write_update_cache(2.0, "1.2.4")
            folder = Path(d) / "image-use"
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ["update-check.json"])
            self.assertEqual(json.loads((folder / "update-check.json").read_text()),
                             {"checked_at": 2, "latest": "1.2.4"})


class SkillsRoute(unittest.TestCase):
    """A skill-folder install upgrades through `skills update`, via npx when
    `skills` itself is not on PATH."""

    def _install(self, which, run_rc=0):
        calls = []

        class _Res:
            returncode = run_rc

        def fake_run(argv, *a, **k):
            calls.append(argv)
            return _Res()

        with unittest.mock.patch.object(cig.shutil, "which", which), \
             unittest.mock.patch.object(cig.subprocess, "run", fake_run), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            rc = cig._install_cli("skills", Path("/s"), "9.9.9")
        return rc, calls, err.getvalue()

    def test_runs_skills_update(self):
        rc, calls, _ = self._install(lambda n: "/usr/bin/skills")
        self.assertEqual((rc, calls), (0, [["/usr/bin/skills", "update", "image-use"]]))

    def test_missing_skills_falls_back_to_npx(self):
        rc, calls, _ = self._install(
            lambda n: "/usr/bin/npx" if n == "npx" else None)
        self.assertEqual(
            (rc, calls), (0, [["/usr/bin/npx", "-y", "skills", "update", "image-use"]]))

    def test_path_skills_wins_over_npx(self):
        _, calls, _ = self._install(lambda n: f"/usr/bin/{n}")
        self.assertEqual(calls, [["/usr/bin/skills", "update", "image-use"]])

    def test_no_runner_at_all_prints_command_and_fails(self):
        rc, calls, err = self._install(lambda n: None)
        self.assertEqual((rc, calls), (1, []))
        self.assertIn("npx -y skills update image-use", err)

    def test_propagates_nonzero_exit(self):
        rc, _, _ = self._install(lambda n: "/usr/bin/skills", run_rc=3)
        self.assertEqual(rc, 3)


class IsUrl(unittest.TestCase):
    def test_true(self):
        self.assertTrue(cig._is_url("http://x.com/a.png"))
        self.assertTrue(cig._is_url("https://x.com/a.png"))

    def test_false(self):
        self.assertFalse(cig._is_url("/local/a.png"))
        self.assertFalse(cig._is_url("a.png"))
        self.assertFalse(cig._is_url("ftp://x.com/a.png"))


class ValidStyleName(unittest.TestCase):
    def test_accepts_slugs(self):
        for ok in ("doodle", "flat-icon", "v2", "a", "my_style"):
            self.assertTrue(cig._valid_style_name(ok), ok)

    def test_rejects_bad(self):
        for bad in ("", "Doodle", "has space", "-leading", "_leading", "with.dot", "藝術"):
            self.assertFalse(cig._valid_style_name(bad), bad)


class ComposePrompt(unittest.TestCase):
    def test_appends_with_comma(self):
        self.assertEqual(cig._compose_prompt("a cat", "watercolor"), "a cat, watercolor")

    def test_none_or_blank_snippet_unchanged(self):
        self.assertEqual(cig._compose_prompt("a cat", None), "a cat")
        self.assertEqual(cig._compose_prompt("a cat", "   "), "a cat")

    def test_strips_one_trailing_punct(self):
        self.assertEqual(cig._compose_prompt("a cat.", "watercolor"), "a cat, watercolor")
        self.assertEqual(cig._compose_prompt("a cat, ", "watercolor"), "a cat, watercolor")

    def test_empty_prompt_yields_snippet(self):
        self.assertEqual(cig._compose_prompt("", "watercolor"), "watercolor")

    def test_snippet_is_trimmed(self):
        self.assertEqual(cig._compose_prompt("a cat", "  watercolor  "), "a cat, watercolor")


class StyleStorage(unittest.TestCase):
    def test_path_honors_xdg(self):
        with _tmp_xdg() as d:
            self.assertEqual(cig._styles_path(),
                             Path(d) / "chatgpt-imagegen" / "styles.json")

    def test_load_starts_empty_when_missing(self):
        # There are no built-in styles: a fresh install has an empty library.
        with _tmp_xdg():
            doc = cig._load_styles()
            self.assertEqual(doc["default"], [])          # v2: default is a list
            self.assertEqual(doc["styles"], {})           # nothing seeded
            self.assertNotIn("seeded", doc)               # machinery gone
            self.assertTrue(cig._styles_path().exists())  # empty doc written

    def test_load_never_injects_styles(self):
        # Loading must never add styles the user didn't put there — the only
        # sources are `style pull` / auto-pull, never the CLI itself.
        with _tmp_xdg():
            cig._save_styles({"version": 2, "default": [],
                              "styles": {"mine": {"kind": "style",
                                                  "snippet": "x", "refs": []}}})
            doc = cig._load_styles()
            self.assertEqual(list(doc["styles"]), ["mine"])  # nothing added

    def test_save_roundtrip_and_atomic(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["custom"] = {"kind": "style", "snippet": "neon glow",
                                       "refs": []}
            doc["default"] = ["custom"]
            cig._save_styles(doc)
            reread = cig._load_styles()
            self.assertEqual(reread["styles"]["custom"]["snippet"], "neon glow")
            self.assertEqual(reread["default"], ["custom"])
            # no leftover temp file beside the target
            self.assertEqual(list(cig._styles_path().parent.glob("*.tmp")), [])

    def test_corrupt_file_raises(self):
        with _tmp_xdg():
            p = cig._styles_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                cig._load_styles()


class ResolveStyleName(unittest.TestCase):
    DOC = {"version": 1, "default": "doodle",
           "styles": {"doodle": "d-snippet", "neon": "n-snippet"}}

    def test_no_style_wins_over_everything(self):
        self.assertIsNone(cig._resolve_style_name(
            self.DOC, style_arg="neon", no_style=True))

    def test_style_arg_overrides_default(self):
        self.assertEqual(cig._resolve_style_name(
            self.DOC, style_arg="neon", no_style=False), "neon")

    def test_falls_back_to_default(self):
        self.assertEqual(cig._resolve_style_name(
            self.DOC, style_arg=None, no_style=False), "doodle")

    def test_empty_default_is_none(self):
        doc = {"default": "", "styles": {"neon": "x"}}
        self.assertIsNone(cig._resolve_style_name(
            doc, style_arg=None, no_style=False))

    def test_unknown_style_arg_raises(self):
        with self.assertRaises(SystemExit):
            cig._resolve_style_name(self.DOC, style_arg="nope", no_style=False)


import io
from contextlib import redirect_stdout

class StyleCommand(unittest.TestCase):
    def test_add_then_show(self):
        with _tmp_xdg():
            self.assertEqual(cig._style_command(["add", "neon", "neon glow"]), 0)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cig._style_command(["show", "neon"]), 0)
            text = out.getvalue()
            self.assertIn("kind: style", text)
            self.assertIn("snippet: neon glow", text)
            self.assertIn("path:", text)

    def test_add_invalid_name_raises(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                cig._style_command(["add", "Bad Name", "x"])

    def test_use_and_clear_default(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["use", "neon"])
            self.assertEqual(cig._load_styles()["default"], ["neon"])
            cig._style_command(["clear"])
            self.assertEqual(cig._load_styles()["default"], [])

    def test_rm_clears_default_if_pointed_there(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["use", "neon"])
            cig._style_command(["rm", "neon"])
            doc = cig._load_styles()
            self.assertNotIn("neon", doc["styles"])
            self.assertEqual(doc["default"], [])

    def test_rm_unknown_raises(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                cig._style_command(["rm", "ghost"])

    def test_list_marks_default(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["use", "neon"])
            out = io.StringIO()
            with redirect_stdout(out):
                cig._style_command(["list"])
            lines = out.getvalue()
            self.assertIn("neon", lines)
            self.assertIn("*", lines)   # default marker

    def test_v2_object_entry_does_not_crash_list_show_compose(self):
        # Regression: a v2 styles.json (entries are {kind,snippet,refs} objects)
        # must not raise "AttributeError: 'dict' object has no attribute 'strip'"
        # on `style list`, `style show`, or the resolve+compose generation path —
        # the crash old (string-only) builds hit against a v2 file.
        with _tmp_xdg():
            cig._save_styles({
                "version": 2, "default": ["xiaohei"],
                "styles": {"xiaohei": {"kind": "style",
                                       "snippet": "hand-drawn 小黑", "refs": []}},
            })
            out = io.StringIO()
            with redirect_stdout(out):
                cig._style_command(["list"])
                cig._style_command(["show", "xiaohei"])
            self.assertIn("xiaohei", out.getvalue())
            doc = cig._load_styles()
            active = cig._resolve_active_styles(doc, style_args=None, no_style=False)
            self.assertEqual(active, ["xiaohei"])
            composed = cig._compose_prompt(
                "a cat", doc["styles"]["xiaohei"]["snippet"])
            self.assertIn("hand-drawn 小黑", composed)

    def test_reset_empties_library(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            self.assertEqual(cig._style_command(["reset", "-y"]), 0)
            doc = cig._load_styles()
            self.assertEqual(doc["styles"], {})      # wiped, nothing re-seeded
            self.assertEqual(doc["default"], [])


class BuildWebText(unittest.TestCase):
    def test_plain_has_no_codex_tool_wording(self):
        t = cig._build_web_text("a red apple", "auto")
        self.assertIn("a red apple", t)
        self.assertNotIn("image_generation tool", t)
        self.assertNotIn("Output format", t)

    def test_size_folded_in(self):
        self.assertIn("1024x1536", cig._build_web_text("x", "1024x1536"))
        self.assertNotIn("auto", cig._build_web_text("x", "auto").lower())

    def test_edit_anchors_on_reference(self):
        # character-only framing (the old is_edit==True case) → counts API.
        t = cig._build_web_text("make it blue", "auto", n_character_refs=1)
        self.assertIn("attached", t.lower())


class BuildUserText(unittest.TestCase):
    def test_codex_keeps_tool_wording(self):
        t = cig._build_user_text("a cat", "auto", "png")
        self.assertIn("image_generation tool", t)
        self.assertIn("png", t)


class DefaultOutPath(unittest.TestCase):
    def test_slugifies_and_numbers(self):
        with _in_tmp_cwd():
            p1 = cig._default_out_path("A Red Apple!!", "png")
            self.assertEqual(p1, Path("assets/generated/a-red-apple.png"))
            p1.parent.mkdir(parents=True, exist_ok=True)
            p1.write_bytes(b"x")
            p2 = cig._default_out_path("A Red Apple!!", "png")
            self.assertEqual(p2.name, "a-red-apple-2.png")

    def test_empty_prompt_fallback(self):
        with _in_tmp_cwd():
            self.assertEqual(cig._default_out_path("!!!", "webp").name, "image.webp")


class AnimationHelpers(unittest.TestCase):
    def test_prompt_locks_grid_and_subject(self):
        text = cig._build_animation_prompt("a dog wags its tail")
        self.assertIn("exactly 4 columns by 2 rows", text)
        self.assertIn("exactly eight", text)
        self.assertIn("body center", text)
        self.assertIn("a dog wags its tail", text)

    def test_png_dimensions_reads_ihdr(self):
        data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
                (1774).to_bytes(4, "big") + (887).to_bytes(4, "big"))
        self.assertEqual(cig._png_dimensions(data), (1774, 887))

    def test_png_dimensions_rejects_non_png(self):
        with self.assertRaises(cig.AnimationError):
            cig._png_dimensions(b"not a png")

    def test_ping_pong_order_avoids_duplicate_endpoints(self):
        self.assertEqual(
            cig._animation_frame_order(),
            [0, 1, 2, 3, 4, 5, 6, 7, 6, 5, 4, 3, 2, 1],
        )
        self.assertEqual(cig._animation_frame_order(ping_pong=False), list(range(8)))

    def test_output_extension_is_authoritative(self):
        path, fmt = cig._animation_output_path("wave", "out/wave.gif", None)
        self.assertEqual((path, fmt), (Path("out/wave.gif"), "gif"))
        with self.assertRaises(cig.AnimationError):
            cig._animation_output_path("wave", "wave.gif", "webp")

    def test_component_parser_filters_background_and_specks(self):
        output = """Objects (id: bounding-box centroid area mean-color):
  0: 443x443+0+0 221.0,221.0 170000 gray(255)
  1: 120x120+100+80 160.5,140.5 9000 gray(255)
  2: 2x2+0+0 1.0,1.0 4 gray(255)
"""
        self.assertEqual(
            cig._connected_component_candidates(output, 443 * 443, 443),
            [(160.5, 140.5, 9000)],
        )

    def test_render_crops_eight_cells_and_encodes_webp_plus_gif(self):
        data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
                (1774).to_bytes(4, "big") + (887).to_bytes(4, "big"))
        commands = []
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(cig.shutil, "which",
                                        side_effect=lambda name: f"/bin/{name}"), \
             unittest.mock.patch.object(cig, "_run_checked",
                                        side_effect=lambda command, label: commands.append((command, label))), \
             unittest.mock.patch.object(cig, "_subject_drift", return_value=(1.0, 0.0)):
            root = Path(d)
            outputs = cig._render_animation(
                root / "sheet.png", data, root / "wave.webp", "webp", 8,
                also_gif=True, keep_frames=False, ping_pong=True,
                max_drift=8.0, allow_drift=False, progress=False,
            )
        self.assertEqual([p.name for p in outputs], ["wave.webp", "wave.gif"])
        self.assertEqual(sum(label.startswith("crop frame") for _, label in commands), 8)
        self.assertEqual(commands[-2][1], "animated WebP encoding")
        self.assertEqual(commands[-1][1], "GIF encoding")

    def test_render_rejects_detected_drift(self):
        data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
                (1600).to_bytes(4, "big") + (800).to_bytes(4, "big"))
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(cig.shutil, "which", return_value="/bin/tool"), \
             unittest.mock.patch.object(cig, "_run_checked"), \
             unittest.mock.patch.object(cig, "_subject_drift", return_value=(20.0, 3.0)):
            with self.assertRaisesRegex(cig.AnimationError, "subject drift"):
                cig._render_animation(
                    Path(d) / "sheet.png", data, Path(d) / "wave.gif", "gif", 8,
                    also_gif=False, keep_frames=False, ping_pong=True,
                    max_drift=8.0, allow_drift=False, progress=False,
                )


class ExtractAccessToken(unittest.TestCase):
    def test_reads_nested_tokens(self):
        auth = {"tokens": {"access_token": "AAA", "account_id": "acc",
                           "refresh_token": "RRR"}}
        access, account, refresh = cig._extract_access_token(auth)
        self.assertEqual((access, refresh), ("AAA", "RRR"))

    def test_missing(self):
        access, _account, _refresh = cig._extract_access_token({})
        self.assertIsNone(access)


class RefreshedAuthPersistence(unittest.TestCase):
    def test_persists_when_posix_fchmod_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td, \
             unittest.mock.patch.object(cig, "AUTH_PATH", Path(td) / "auth.json"), \
             unittest.mock.patch.object(cig.os, "fchmod", None, create=True):
            original = {"tokens": {"access_token": "old"}}
            cig._persist_refreshed_auth(
                original,
                {"access_token": "new", "refresh_token": "refresh"},
            )
            saved = json.loads(cig.AUTH_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["tokens"]["access_token"], "new")
        self.assertEqual(saved["tokens"]["refresh_token"], "refresh")


class DownloadRefCap(unittest.TestCase):
    @contextmanager
    def _fake_urlopen(self, body: bytes):
        import urllib.request
        real = urllib.request.urlopen

        class _Resp:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_, n=-1):
                return body[:n] if n and n > 0 else body

        urllib.request.urlopen = lambda *a, **k: _Resp()
        try:
            yield
        finally:
            urllib.request.urlopen = real

    def test_small_ok(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with self._fake_urlopen(png):
            self.assertEqual(cig._download_ref_url("https://x/a.png"), png)

    def test_over_cap_exits(self):
        big = b"\x00" * (cig.REF_DOWNLOAD_MAX + 10)
        with self._fake_urlopen(big):
            with self.assertRaises(SystemExit):
                cig._download_ref_url("https://x/huge.png")


class WebUnavailableMessage(unittest.TestCase):
    """The diagnostic built when every browser candidate fails (issue #15)."""

    def test_includes_per_candidate_reasons(self):
        msg = cig._web_unavailable_message(
            detected_profiles=["Default"],
            reasons=["current Chrome (relay) — extension not connected",
                     "profile 'Default' — composer never appeared"],
            relay=False,
        )
        # chrome-use's actual errors are surfaced, not swallowed.
        self.assertIn("extension not connected", msg)
        self.assertIn("composer never appeared", msg)

    def test_logged_in_profile_but_no_relay_gives_three_remedies(self):
        # The issue-#15 shape: a login exists on disk, relay isn't connected.
        msg = cig._web_unavailable_message(["Default"], ["x — y"], relay=False)
        self.assertIn("'Default'", msg)
        self.assertIn("connect the relay", msg)
        self.assertIn("quit Chrome", msg)
        self.assertIn("--backend codex", msg)

    def test_relay_up_blames_signed_out_chrome(self):
        msg = cig._web_unavailable_message([], ["relay — composer never appeared"],
                                           relay=True)
        self.assertIn("relay is connected", msg)
        self.assertNotIn("connect the relay", msg)  # already connected

    def test_nothing_detected_no_relay(self):
        msg = cig._web_unavailable_message([], ["relay — extension not connected"],
                                           relay=False)
        self.assertIn("no logged-in Chrome profile was detected", msg)
        self.assertIn("--backend codex", msg)


class AutoCandidates(unittest.TestCase):
    def test_relay_up_tries_relay_first(self):
        self.assertEqual(cig._auto_candidates(["Default", "Profile 1"], True),
                         [None, "Default", "Profile 1"])

    def test_relay_down_tries_profiles_first_relay_last(self):
        # issue #15 shape: relay off, a login detected → don't waste the first
        # attempt on the signed-out throwaway relay launch.
        self.assertEqual(cig._auto_candidates(["Default"], False),
                         ["Default", None])

    def test_no_profiles_is_just_relay_either_way(self):
        self.assertEqual(cig._auto_candidates([], True), [None])
        self.assertEqual(cig._auto_candidates([], False), [None])


class RelayConnected(unittest.TestCase):
    @contextmanager
    def _fake_run(self, stdout: str, raises: bool = False):
        import subprocess
        real = subprocess.run

        def fake(*a, **k):
            if raises:
                raise subprocess.TimeoutExpired(cmd="chrome-use", timeout=10)
            return subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr="")

        subprocess.run = fake
        try:
            yield
        finally:
            subprocess.run = real

    def test_relay_true(self):
        with self._fake_run('{"data":{"relay":true,"sessions":[]},"success":true}'):
            self.assertTrue(cig._relay_connected("chrome-use"))

    def test_relay_false(self):
        with self._fake_run('{"data":{"relay":false},"success":true}'):
            self.assertFalse(cig._relay_connected("chrome-use"))

    def test_garbage_output_is_false(self):
        with self._fake_run("not json"):
            self.assertFalse(cig._relay_connected("chrome-use"))

    def test_subprocess_error_is_false(self):
        with self._fake_run("", raises=True):
            self.assertFalse(cig._relay_connected("chrome-use"))


class DoctorDecisions(unittest.TestCase):
    def test_web_ready(self):
        self.assertTrue(cig._web_ready(True, True, 0))    # relay alone
        self.assertTrue(cig._web_ready(True, False, 2))   # profiles alone
        self.assertFalse(cig._web_ready(True, False, 0))  # installed but nothing to reach
        self.assertFalse(cig._web_ready(False, True, 3))  # not installed

    def test_auto_backend_pick(self):
        self.assertEqual(cig._auto_backend_pick(True, False), "web")
        self.assertEqual(cig._auto_backend_pick(True, True), "web")    # web preferred
        self.assertEqual(cig._auto_backend_pick(False, True), "codex")
        self.assertEqual(cig._auto_backend_pick(False, False), "neither")


class Color(unittest.TestCase):
    class _Stream:
        def __init__(self, tty): self._tty = tty
        def isatty(self): return self._tty

    @contextmanager
    def _env(self, **kv):
        prev = {k: os.environ.get(k) for k in kv}
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            yield
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_color_env_disables(self):
        with self._env(NO_COLOR="1", TERM="xterm"):
            self.assertFalse(cig._use_color(self._Stream(True)))

    def test_non_tty_disables(self):
        with self._env(NO_COLOR=None, IMAGE_USE_NO_COLOR=None, CHATGPT_IMAGEGEN_NO_COLOR=None, TERM="xterm"):
            self.assertFalse(cig._use_color(self._Stream(False)))

    def test_tty_enables(self):
        with self._env(NO_COLOR=None, IMAGE_USE_NO_COLOR=None, CHATGPT_IMAGEGEN_NO_COLOR=None, TERM="xterm"):
            self.assertTrue(cig._use_color(self._Stream(True)))

    def test_paint_plain_when_off(self):
        # Color is off for a non-tty stream → string returned untouched.
        with self._env(NO_COLOR=None, IMAGE_USE_NO_COLOR=None, CHATGPT_IMAGEGEN_NO_COLOR=None):
            out = cig._paint("31", "hi", stream=self._Stream(False))
            self.assertEqual(out, "hi")

    def test_fmt_progress_plain_has_timestamp_no_ansi(self):
        with self._env(NO_COLOR="1"):
            line = cig._fmt_progress(1.5, "generating")
            self.assertIn("1.5s]", line)
            self.assertNotIn("\033[", line)  # no ANSI when color off

    def test_fmt_progress_color_warns(self):
        # When color is on (forced via patched _use_color), a warn word tints.
        real = cig._use_color
        cig._use_color = lambda *a, **k: True
        try:
            line = cig._fmt_progress(2.0, "warning: relay not connected")
            self.assertIn("\033[33m", line)  # yellow
        finally:
            cig._use_color = real


def _write_png(path) -> str:
    """Write a tiny but valid-enough PNG (passes _sniff_mime). Returns the path."""
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return str(path)


def _write_jpeg(path) -> str:
    Path(path).write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)
    return str(path)


class NormalizeDoc(unittest.TestCase):
    """Legacy v1 → v2 normalization: bare-string entries become objects, a
    string `default` becomes a list, version bumps to 2."""

    def test_legacy_migration_on_load(self):
        with _tmp_xdg():
            p = cig._styles_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "version": 1,
                "default": "watercolor",
                "styles": {"watercolor": "soft watercolor"},
            }), encoding="utf-8")
            doc = cig._load_styles()
            self.assertEqual(doc["version"], 2)
            self.assertEqual(doc["default"], ["watercolor"])
            entry = doc["styles"]["watercolor"]
            self.assertEqual(entry, {"kind": "style",
                                     "snippet": "soft watercolor", "refs": []})

    def test_empty_string_default_becomes_empty_list(self):
        self.assertEqual(
            cig._normalize_doc({"default": "", "styles": {}})["default"], [])

    def test_rewrite_bumps_version_on_disk(self):
        with _tmp_xdg():
            p = cig._styles_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "version": 1, "default": "x",
                "styles": {"x": "snip"},
            }), encoding="utf-8")
            cig._style_command(["use", "x"])           # any mutating command
            on_disk = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["version"], 2)
            self.assertEqual(on_disk["default"], ["x"])
            self.assertEqual(on_disk["styles"]["x"]["snippet"], "snip")


class AssetAddRef(unittest.TestCase):
    """add --ref copies the file in, normalizes the filename, and show lists it."""

    def test_add_ref_copies_and_normalizes_name(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as src:
            img = _write_png(os.path.join(src, "Weird Name.PNG"))
            self.assertEqual(
                cig._style_command(["add", "mascot", "a fox", "--ref", img,
                                    "--kind", "character"]), 0)
            doc = cig._load_styles()
            entry = doc["styles"]["mascot"]
            self.assertEqual(entry["kind"], "character")
            self.assertEqual(entry["refs"], ["ref-1.png"])     # normalized
            self.assertTrue((cig._asset_dir("mascot") / "ref-1.png").is_file())

    def test_add_requires_snippet_or_ref(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                cig._style_command(["add", "empty"])

    def test_add_ref_then_rm_ref(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as src:
            a = _write_png(os.path.join(src, "a.png"))
            b = _write_jpeg(os.path.join(src, "b.jpg"))
            cig._style_command(["add", "mascot", "--ref", a, "--kind", "character"])
            cig._style_command(["add-ref", "mascot", b])
            entry = cig._load_styles()["styles"]["mascot"]
            self.assertEqual(entry["refs"], ["ref-1.png", "ref-2.jpg"])
            self.assertTrue((cig._asset_dir("mascot") / "ref-2.jpg").is_file())
            cig._style_command(["rm-ref", "mascot", "ref-1.png"])
            entry = cig._load_styles()["styles"]["mascot"]
            self.assertEqual(entry["refs"], ["ref-2.jpg"])
            self.assertFalse((cig._asset_dir("mascot") / "ref-1.png").exists())

    def test_rm_ref_unknown_file_raises(self):
        with _tmp_xdg():
            cig._style_command(["add", "mascot", "snip"])
            with self.assertRaises(SystemExit):
                cig._style_command(["rm-ref", "mascot", "nope.png"])


class ResolveActiveStyles(unittest.TestCase):
    def _doc(self):
        return cig._normalize_doc({
            "default": ["a", "b"],
            "styles": {"a": {"kind": "character", "snippet": "", "refs": []},
                       "b": "snip-b", "c": "snip-c"},
        })

    def test_falls_back_to_default_list(self):
        self.assertEqual(
            cig._resolve_active_styles(self._doc(), style_args=None,
                                       no_style=False), ["a", "b"])

    def test_explicit_styles_override_default_in_order(self):
        self.assertEqual(
            cig._resolve_active_styles(self._doc(), style_args=["c", "a"],
                                       no_style=False), ["c", "a"])

    def test_no_style_empties(self):
        self.assertEqual(
            cig._resolve_active_styles(self._doc(), style_args=["a"],
                                       no_style=True), [])

    def test_unknown_raises(self):
        with self.assertRaises(SystemExit):
            cig._resolve_active_styles(self._doc(), style_args=["ghost"],
                                       no_style=False)


class CollectRefsOrdering(unittest.TestCase):
    """Partition + ordering: character group first (assets then ad-hoc), then
    the style group."""

    def test_partition_and_order(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as src:
            ch = _write_png(os.path.join(src, "ch.png"))
            st = _write_png(os.path.join(src, "st.png"))
            cig._style_command(["add", "ch", "--ref", ch, "--kind", "character"])
            cig._style_command(["add", "st", "--ref", st, "--kind", "style"])
            doc = cig._load_styles()
            # active order deliberately style-first to prove repartition.
            ordered = cig._collect_refs(doc, ["st", "ch"], ["/adhoc.png"])
            groups = [r["group"] for r in ordered]
            self.assertEqual(groups, ["character", "character", "style"])
            ch_path = str(cig._asset_dir("ch") / "ref-1.png")
            st_path = str(cig._asset_dir("st") / "ref-1.png")
            self.assertEqual([r["ref"] for r in ordered],
                             [ch_path, "/adhoc.png", st_path])

    def test_missing_ref_file_raises_naming_asset_and_path(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as src:
            img = _write_png(os.path.join(src, "x.png"))
            cig._style_command(["add", "ch", "--ref", img, "--kind", "character"])
            # delete the copied byte on disk → load still lists the ref name
            (cig._asset_dir("ch") / "ref-1.png").unlink()
            doc = cig._load_styles()
            with self.assertRaises(SystemExit) as ctx:
                cig._collect_refs(doc, ["ch"], None)
            msg = str(ctx.exception)
            self.assertIn("ch", msg)
            self.assertIn("ref-1.png", msg)


class RefCap(unittest.TestCase):
    def test_caps_and_reports_dropped(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as src:
            refs = [_write_png(os.path.join(src, f"r{i}.png")) for i in range(6)]
            cmd = ["add", "many", "--kind", "character"]
            for r in refs:
                cmd += ["--ref", r]
            cig._style_command(cmd)
            doc = cig._load_styles()
            ordered = cig._collect_refs(doc, ["many"], None)
            self.assertEqual(len(ordered), 6)
            kept, dropped = cig._cap_refs(ordered, cig.REF_ATTACH_CAP)
            self.assertEqual(len(kept), cig.REF_ATTACH_CAP)
            self.assertEqual(len(dropped), 6 - cig.REF_ATTACH_CAP)
            # the dropped ones are the trailing refs, identifiable by label
            self.assertTrue(all("many/ref-" in r["label"] for r in dropped))


class ResetWipesAssets(unittest.TestCase):
    def test_reset_deletes_asset_tree(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as src:
            img = _write_png(os.path.join(src, "x.png"))
            cig._style_command(["add", "mascot", "--ref", img, "--kind", "character"])
            self.assertTrue(cig._asset_dir("mascot").is_dir())
            self.assertEqual(cig._style_command(["reset", "-y"]), 0)
            self.assertFalse(cig._asset_dir("mascot").exists())
            self.assertFalse(cig._assets_root().exists())
            doc = cig._load_styles()
            self.assertEqual(doc["styles"], {})        # empty library after reset


class FromLast(unittest.TestCase):
    def test_record_then_read_roundtrip(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as d:
            img = Path(_write_png(os.path.join(d, "out.png")))
            cig._record_last_output(img)
            self.assertEqual(cig._read_last_output(), img.resolve())

    def test_pin_from_last(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as d:
            img = Path(_write_png(os.path.join(d, "out.png")))
            cig._record_last_output(img)
            self.assertEqual(
                cig._style_command(["add", "pinned", "--from-last",
                                    "--kind", "character"]), 0)
            entry = cig._load_styles()["styles"]["pinned"]
            self.assertEqual(entry["refs"], ["ref-1.png"])
            self.assertTrue((cig._asset_dir("pinned") / "ref-1.png").is_file())

    def test_from_last_absent_errors(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                cig._style_command(["add", "x", "--from-last"])

    def test_from_last_file_gone_errors(self):
        with _tmp_xdg(), tempfile.TemporaryDirectory() as d:
            img = Path(_write_png(os.path.join(d, "out.png")))
            cig._record_last_output(img)
            img.unlink()
            with self.assertRaises(SystemExit):
                cig._style_command(["add", "x", "--from-last"])


class PromptWordingByKind(unittest.TestCase):
    """The three instruction framings keyed off (n_character_refs, n_style_refs)."""

    def test_style_only(self):
        t = cig._build_web_text("a cat", "auto", n_character_refs=0, n_style_refs=2)
        self.assertIn("Match the visual style", t)
        self.assertIn("do NOT copy", t)
        self.assertNotIn("recurring character", t)
        u = cig._build_user_text("a cat", "auto", "png",
                                 n_character_refs=0, n_style_refs=2)
        self.assertIn("visual style", u)
        self.assertIn("do not copy their content", u)
        self.assertNotIn("recurring character", u)

    def test_character_only(self):
        t = cig._build_web_text("a cat", "auto", n_character_refs=1, n_style_refs=0)
        self.assertIn("canonical subject", t)
        self.assertNotIn("recurring character", t)
        u = cig._build_user_text("a cat", "auto", "png",
                                 n_character_refs=1, n_style_refs=0)
        self.assertIn("canonical subject", u)

    def test_mixed(self):
        t = cig._build_web_text("a cat", "auto", n_character_refs=1, n_style_refs=2)
        self.assertIn("recurring character", t)
        self.assertIn("style references", t)
        u = cig._build_user_text("a cat", "auto", "png",
                                 n_character_refs=1, n_style_refs=2)
        self.assertIn("recurring character", u)
        self.assertIn("style references", u)

    def test_payload_tool_choice_required_for_style_only(self):
        payload = cig._build_payload(
            "a cat", "auto", "png", "gpt-5.5",
            refs=[("Zm9v", "image/png")], n_character_refs=0, n_style_refs=1)
        self.assertEqual(payload["tool_choice"], "required")


class CodexModelFallback(unittest.TestCase):
    def test_default_is_a_fast_cheap_driver(self):
        self.assertEqual(cig.CODEX_MODEL_DEFAULT, "gpt-5.6-luna")

    def test_detects_account_allowlist_rejection(self):
        e = cig.GatewayError(
            "HTTP 400: {\"detail\":\"The 'gpt-5-nano' model is not supported "
            "when using Codex with a ChatGPT account.\"}", status=400)
        self.assertTrue(cig._model_unsupported_error(e))
        self.assertFalse(cig._model_unsupported_error(
            cig.GatewayError("HTTP 400: invalid payload", status=400)))
        self.assertFalse(cig._model_unsupported_error(
            cig.GatewayError("HTTP 500: boom", status=500)))


class CodexToolOverrideWarning(unittest.TestCase):
    """Codex rewrites the image tool server-side; a dropped knob must be named."""

    def _capture(self, args, effective):
        err = io.StringIO()
        with redirect_stderr(err):
            cig._warn_codex_tool_overrides(args, effective)
        return err.getvalue()

    def test_warns_on_rewritten_knobs(self):
        ns = argparse.Namespace(image_model="gpt-image-2.5-sunburst",
                                quality="xhigh", background=None, compression=None)
        out = self._capture(ns, {"model": "gpt-image-2-codex", "quality": "auto"})
        self.assertIn("model=gpt-image-2.5-sunburst→gpt-image-2-codex", out)
        self.assertIn("quality=xhigh→auto", out)

    def test_silent_when_nothing_requested(self):
        ns = argparse.Namespace(image_model=None, quality=None,
                                background=None, compression=None)
        self.assertEqual(self._capture(ns, {"model": "gpt-image-2-codex"}), "")

    def test_silent_when_value_survived(self):
        ns = argparse.Namespace(image_model="gpt-image-2-codex", quality=None,
                                background=None, compression=None)
        self.assertEqual(self._capture(ns, {"model": "gpt-image-2-codex"}), "")


class CodexEffectiveConfigCapture(unittest.TestCase):
    """`_post_for_image` keeps the server's effective tool config and usage."""

    def test_captures_effective_tool_and_usage(self):
        blob = cig.base64.b64encode(b"image-bytes").decode()
        events = [
            {"type": "response.created", "response": {"tools": [
                {"type": "image_generation", "model": "gpt-image-2-codex",
                 "quality": "auto"}]}},
            {"type": "response.output_item.done", "item": {
                "type": "image_generation_call", "result": blob,
                "size": "1024x1024", "quality": "low"}},
            {"type": "response.completed", "response": {"usage": {
                "input_tokens": 2316, "output_tokens": 41,
                "total_tokens": 2357}}},
        ]
        now = time.monotonic()
        with unittest.mock.patch.object(cig, "_stream",
                                        lambda *a, **k: iter(events)):
            data, meta = cig._post_for_image({}, {}, now + 60, now, 60.0, False)
        self.assertEqual(data, b"image-bytes")
        self.assertEqual(meta["effective_tool"]["model"], "gpt-image-2-codex")
        self.assertEqual(meta["usage"]["total_tokens"], 2357)
        self.assertEqual(meta["size"], "1024x1024")


class CodexImageToolOptions(unittest.TestCase):
    """The GPT Image 2.5 knobs live inside the image_generation tool, and unset
    ones are omitted so payloads from existing callers stay byte-identical."""

    def _tool(self, size="auto", **kw):
        return cig._build_payload(
            "a cat", size, "png", "gpt-5.5", **kw)["tools"][0]

    def test_unset_omits_every_knob(self):
        tool = self._tool()
        for field in ("model", "quality", "background",
                      "output_compression", "action", "partial_images"):
            self.assertNotIn(field, tool)
        self.assertEqual(tool, {"type": "image_generation", "output_format": "png"})

    def test_each_knob_lands_in_the_tool(self):
        tool = self._tool(
            image_model="gpt-image-2.5-sunburst", quality="xhigh",
            background="transparent", output_compression=90,
            action="edit", partial_images=2)
        self.assertEqual(tool["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(tool["quality"], "xhigh")
        self.assertEqual(tool["background"], "transparent")
        self.assertEqual(tool["output_compression"], 90)
        self.assertEqual(tool["action"], "edit")
        self.assertEqual(tool["partial_images"], 2)

    def test_size_auto_omitted_real_size_forwarded(self):
        self.assertNotIn("size", self._tool(size="auto"))
        self.assertEqual(self._tool(size="1024x1024")["size"], "1024x1024")

    def test_image_opt_collapses_auto_values(self):
        for v in (None, "", "  ", "auto", "AUTO", "default"):
            self.assertIsNone(cig._image_opt(v))
        self.assertEqual(cig._image_opt(" gpt-image-2.5-flare "),
                         "gpt-image-2.5-flare")

    def test_codex_only_options_reports_set_flags(self):
        ns = argparse.Namespace(image_model=None, quality="high",
                                background=None, compression=None,
                                action=None, partial_images=None)
        self.assertEqual(cig._codex_only_options(ns), ["--quality"])
        ns.quality = None
        self.assertEqual(cig._codex_only_options(ns), [])


class StylesAlias(unittest.TestCase):
    def test_use_accepts_multiple(self):
        with _tmp_xdg():
            cig._style_command(["add", "a", "x"])
            cig._style_command(["add", "b", "y"])
            cig._style_command(["use", "a", "b"])
            self.assertEqual(cig._load_styles()["default"], ["a", "b"])


class OriginRoundTrip(unittest.TestCase):
    def test_origin_preserved(self):
        e = cig._normalize_entry({
            "kind": "style", "snippet": "s", "refs": ["a.png"],
            "origin": {"platform": "drawstyle", "slug": "pip", "version": 3}})
        self.assertEqual(e["origin"],
                         {"platform": "drawstyle", "slug": "pip", "version": 3})

    def test_platform_defaults_when_missing(self):
        e = cig._normalize_entry({
            "kind": "style", "snippet": "s", "refs": [],
            "origin": {"slug": "pip", "version": 1}})
        self.assertEqual(e["origin"]["platform"], "drawstyle")

    def test_bad_origin_dropped(self):
        for bad in ("str", 7, ["x"], {"platform": "drawstyle"},  # missing slug/version
                    {"slug": "x", "version": True}):  # bool is not a version
            e = cig._normalize_entry({"kind": "style", "snippet": "s",
                                      "refs": [], "origin": bad})
            self.assertNotIn("origin", e)

    def test_absent_origin_absent(self):
        e = cig._normalize_entry({"kind": "style", "snippet": "s", "refs": []})
        self.assertNotIn("origin", e)


class PlatformRequest(unittest.TestCase):
    def test_base_default_and_env(self):
        prev = os.environ.pop("DRAWSTYLE_API", None)
        try:
            self.assertEqual(cig._platform_base(),
                             "https://drawstyle.leeguoo.com")
            os.environ["DRAWSTYLE_API"] = "http://localhost:8787/"
            self.assertEqual(cig._platform_base(), "http://localhost:8787")
        finally:
            if prev is None:
                os.environ.pop("DRAWSTYLE_API", None)
            else:
                os.environ["DRAWSTYLE_API"] = prev

    def test_error_payload_surfaced(self):
        import urllib.error
        body = json.dumps({"error": {"code": "not_found",
                                     "message": "no such style"}}).encode()
        err = urllib.error.HTTPError("u", 404, "Not Found", {},
                                     io.BytesIO(body))
        with unittest.mock.patch.object(cig, "_urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                cig._platform_request("GET", "/api/styles/nope")
            self.assertIn("no such style", str(cm.exception))

    def test_offline_hint(self):
        import urllib.error
        with unittest.mock.patch.object(
                cig, "_urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(SystemExit) as cm:
                cig._platform_request("GET", "/api/styles")
            self.assertIn("unaffected", str(cm.exception))
            self.assertIn("retry when online", str(cm.exception))

    @staticmethod
    def _fake_urlopen(body: bytes):
        resp = unittest.mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return unittest.mock.Mock(return_value=resp)

    def test_happy_path_json(self):
        fake = self._fake_urlopen(b'{"ok": true}')
        with unittest.mock.patch.object(cig, "_urlopen", fake):
            self.assertEqual(cig._platform_request("GET", "/api/styles"),
                             {"ok": True})

    def test_reference_download_sends_user_agent(self):
        fake = self._fake_urlopen(_PNG)
        with unittest.mock.patch.object(cig, "_urlopen", fake):
            self.assertEqual(cig._download_bytes("https://example.test/ref.png"),
                             _PNG)
        req = fake.call_args[0][0]
        self.assertIn("chatgpt-imagegen/", req.get_header("User-agent"))

    def test_non_json_200_exits(self):
        fake = self._fake_urlopen(b"<html>")
        with unittest.mock.patch.object(cig, "_urlopen", fake):
            with self.assertRaises(SystemExit) as cm:
                cig._platform_request("GET", "/api/styles")
            self.assertIn("not JSON", str(cm.exception))


_SEARCH_PAYLOAD = {"styles": [
    {"slug": "pip", "name": "Pip the fox", "kind": "character",
     "category": "avatar-ip", "likes_count": 12, "pulls_count": 90,
     "snippet": "a round orange fox named Pip, thick outlines"},
]}


class StyleSearch(unittest.TestCase):
    def test_renders_rows_and_pull_hint(self):
        with unittest.mock.patch.object(
                cig, "_platform_request", return_value=_SEARCH_PAYLOAD) as m:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cig._style_command(["search", "fox", "--category",
                                         "avatar-ip", "--tag", "cute"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("pip", out)
        self.assertIn("character", out)
        self.assertIn("style pull pip", out)
        path = m.call_args[0][1]
        self.assertIn("q=fox", path)
        self.assertIn("category=avatar-ip", path)
        self.assertIn("tag=cute", path)

    def test_empty_result(self):
        with unittest.mock.patch.object(
                cig, "_platform_request", return_value={"styles": []}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cig._style_command(["search", "nothing"])
        self.assertEqual(rc, 0)
        self.assertIn("no styles found", buf.getvalue())


_PKG = {"slug": "pip", "name": "Pip the fox", "kind": "character",
        "snippet": "a round orange fox", "version": 3,
        "refs": [{"url": "https://drawstyle.leeguoo.com/img/abc",
                  "content_type": "image/png"}]}
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class ResolveOnlineStyles(unittest.TestCase):
    def test_fetches_snippet_and_refs_without_persisting(self):
        with _tmp_xdg():
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value=_PKG), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                snippets, refs = cig._resolve_online_styles(["pip"])
            # snippet folded, one ref downloaded to a temp path, grouped by kind
            self.assertEqual(snippets, ["a round orange fox"])
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["group"], "character")  # _PKG kind=character
            self.assertTrue(Path(refs[0]["ref"]).is_file())
            self.assertIn("online:pip", refs[0]["label"])
            # provenance: the gallery source URL rides along for the run log
            self.assertEqual(refs[0]["source"],
                             "https://drawstyle.leeguoo.com/img/abc")
            # nothing was written to the local style library
            doc = cig._load_styles()
            self.assertNotIn("pip", doc["styles"])

    def test_invalid_slug_and_malformed_package_exit(self):
        with self.assertRaises(SystemExit):
            cig._resolve_online_styles(["../../etc"])
        with unittest.mock.patch.object(cig, "_platform_request",
                                        return_value={"no": "kind"}):
            with self.assertRaises(SystemExit):
                cig._resolve_online_styles(["pip"])


class RefProvenance(unittest.TestCase):
    """Issue #18: the run log must name each attached ref's source, not a count."""

    def test_online_ref_shows_source_url_and_sha(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ref-1.jpg"
            f.write_bytes(b"hello-bytes")
            lines = cig._format_ref_provenance([{
                "ref": str(f), "group": "style",
                "label": "online:snoopy/ref-1.jpg",
                "source": "https://drawstyle.leeguoo.com/img/deadbeef.jpg"}])
        self.assertEqual(len(lines), 1)
        self.assertIn("[style]", lines[0])
        self.assertIn("online:snoopy/ref-1.jpg", lines[0])
        self.assertIn("source=https://drawstyle.leeguoo.com/img/deadbeef.jpg", lines[0])
        # sha256 of b"hello-bytes", first 16 hex
        import hashlib as _h
        self.assertIn(_h.sha256(b"hello-bytes").hexdigest()[:16], lines[0])

    def test_local_ref_shows_path_not_source(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ref-1.png"
            f.write_bytes(b"x")
            lines = cig._format_ref_provenance([{
                "ref": str(f), "group": "character", "label": "leo/ref-1.png"}])
        self.assertIn(f"path={f}", lines[0])
        self.assertNotIn("source=", lines[0])

    def test_unreadable_ref_degrades_gracefully(self):
        lines = cig._format_ref_provenance([{
            "ref": "/no/such/file.png", "group": "style", "label": "x"}])
        self.assertIn("sha256=?", lines[0])


class StylePull(unittest.TestCase):
    def _pull(self, argv, pkg=None, blobs=None):
        def fake_request(method, path, **kw):
            return pkg or _PKG

        def fake_download(url):
            if blobs is not None and url in blobs:
                raise OSError("boom")
            return _PNG

        with unittest.mock.patch.object(cig, "_platform_request", fake_request), \
             unittest.mock.patch.object(cig, "_download_bytes", fake_download):
            return cig._style_command(argv)

    def test_happy_path_writes_entry_refs_and_origin(self):
        with _tmp_xdg():
            rc = self._pull(["pull", "pip"])
            self.assertEqual(rc, 0)
            doc = cig._load_styles()
            e = doc["styles"]["pip"]
            self.assertEqual(e["kind"], "character")
            self.assertEqual(e["origin"],
                             {"platform": "drawstyle", "slug": "pip", "version": 3})
            self.assertEqual(len(e["refs"]), 1)
            self.assertTrue((cig._asset_dir("pip") / e["refs"][0]).exists())

    def test_collision_aborts_with_as_hint(self):
        with _tmp_xdg():
            self._pull(["pull", "pip"])
            with self.assertRaises(SystemExit) as cm:
                self._pull(["pull", "pip"])
            self.assertIn("--as", str(cm.exception))

    def test_as_renames_locally_keeps_origin_slug(self):
        with _tmp_xdg():
            self._pull(["pull", "pip", "--as", "fox2"])
            doc = cig._load_styles()
            self.assertIn("fox2", doc["styles"])
            self.assertEqual(doc["styles"]["fox2"]["origin"]["slug"], "pip")

    def test_failed_ref_download_leaves_no_entry(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                self._pull(["pull", "pip"],
                           blobs={"https://drawstyle.leeguoo.com/img/abc"})
            doc = cig._load_styles()
            self.assertNotIn("pip", doc["styles"])
            self.assertFalse(cig._asset_dir("pip").exists())

    def test_non_image_payload_refused(self):
        with _tmp_xdg():
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value=_PKG), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=b"<html>nope</html>"):
                with self.assertRaises(SystemExit) as cm:
                    cig._style_command(["pull", "pip"])
            self.assertIn("not an image", str(cm.exception))

    def test_invalid_slug_exits_without_request(self):
        with _tmp_xdg():
            called = []

            def fake_request(method, path, **kw):
                called.append(path)
                return _PKG

            with unittest.mock.patch.object(cig, "_platform_request",
                                            fake_request):
                with self.assertRaises(SystemExit) as cm:
                    cig._style_command(["pull", "../../etc"])
            self.assertIn("invalid slug", str(cm.exception))
            self.assertEqual(called, [])


class EnsureStylesLocal(unittest.TestCase):
    """`--style NAME` auto-pulls a missing name from the gallery and persists it."""

    def test_local_name_no_network(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["mine"] = {"kind": "style", "snippet": "x", "refs": []}
            def boom(*a, **k):  # any network call would fail the test
                raise AssertionError("should not hit the platform")
            with unittest.mock.patch.object(cig, "_platform_request", boom):
                self.assertFalse(cig._ensure_styles_local(doc, ["mine"]))

    def test_missing_name_is_pulled_and_persisted(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value=_PKG), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                changed = cig._ensure_styles_local(doc, ["pip"])
            self.assertTrue(changed)
            self.assertIn("pip", doc["styles"])                 # in the live doc
            self.assertEqual(doc["styles"]["pip"]["origin"]["slug"], "pip")
            reread = cig._load_styles()                         # and on disk
            self.assertIn("pip", reread["styles"])

    def test_not_on_gallery_dies_with_guidance(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            def not_found(*a, **k):
                raise SystemExit("error: platform: not found")
            with unittest.mock.patch.object(cig, "_platform_request", not_found):
                with self.assertRaises(SystemExit) as cm:
                    cig._ensure_styles_local(doc, ["ghost"])
            msg = str(cm.exception)
            self.assertIn("unknown style", msg)
            self.assertIn("style search ghost", msg)            # points at gallery
            self.assertNotIn("ghost", doc["styles"])            # nothing persisted

    def test_invalid_name_dies_without_network(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            def boom(*a, **k):
                raise AssertionError("should not hit the platform")
            with unittest.mock.patch.object(cig, "_platform_request", boom):
                with self.assertRaises(SystemExit):
                    cig._ensure_styles_local(doc, ["Bad Name"])


_SNOOPY_PKG = {"slug": "snoopy", "name": "Snoopy Comic", "kind": "style",
               "snippet": "peanuts newspaper look", "version": 1,
               "refs": [{"url": "https://drawstyle.leeguoo.com/img/s",
                         "content_type": "image/jpeg"}]}


class StaleLegacyBuiltin(unittest.TestCase):
    """0.16-era text-only doodle/xiaohei/snoopy self-heal to the gallery version."""

    def test_predicate(self):
        stale = {"kind": "style", "snippet": "x", "refs": []}
        self.assertTrue(cig._is_stale_legacy_builtin("snoopy", stale))
        self.assertTrue(cig._is_stale_legacy_builtin("doodle", stale))
        # has pinned refs → user-customized, leave it
        self.assertFalse(cig._is_stale_legacy_builtin(
            "xiaohei", {"snippet": "x", "refs": ["ref-1.jpg"]}))
        # already pulled (has origin) → not stale
        self.assertFalse(cig._is_stale_legacy_builtin(
            "snoopy", {"snippet": "x", "refs": [],
                       "origin": {"platform": "drawstyle", "slug": "snoopy",
                                  "version": 1}}))
        # not a legacy name → never touched
        self.assertFalse(cig._is_stale_legacy_builtin("mine", stale))

    def test_upgraded_in_place_to_gallery_version(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["snoopy"] = {"kind": "style",
                                       "snippet": "old text-only", "refs": []}
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value=_SNOOPY_PKG), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                changed = cig._ensure_styles_local(doc, ["snoopy"])
            self.assertTrue(changed)
            e = doc["styles"]["snoopy"]
            self.assertEqual(e["origin"]["slug"], "snoopy")   # now gallery-backed
            self.assertEqual(len(e["refs"]), 1)               # now has a ref image

    def test_offline_keeps_old_copy(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            old = {"kind": "style", "snippet": "old", "refs": []}
            doc["styles"]["snoopy"] = dict(old)
            def offline(*a, **k):
                raise SystemExit("error: cannot reach gallery")
            with unittest.mock.patch.object(cig, "_platform_request", offline):
                changed = cig._ensure_styles_local(doc, ["snoopy"])
            self.assertFalse(changed)
            self.assertEqual(doc["styles"]["snoopy"], old)    # untouched, no crash

    def test_customized_legacy_name_not_touched(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["xiaohei"] = {"kind": "style", "snippet": "mine",
                                        "refs": ["ref-1.jpg"]}
            def boom(*a, **k):
                raise AssertionError("should not hit the platform")
            with unittest.mock.patch.object(cig, "_platform_request", boom):
                self.assertFalse(cig._ensure_styles_local(doc, ["xiaohei"]))

    def test_pull_replaces_stale_legacy_instead_of_colliding(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["snoopy"] = {"kind": "style",
                                       "snippet": "old text-only", "refs": []}
            cig._save_styles(doc)
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value=_SNOOPY_PKG), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                rc = cig._style_command(["pull", "snoopy"])
            self.assertEqual(rc, 0)
            e = cig._load_styles()["styles"]["snoopy"]
            self.assertEqual(e["origin"]["slug"], "snoopy")
            self.assertEqual(len(e["refs"]), 1)


class StyleUpdate(unittest.TestCase):
    def test_version_check_uses_detail_not_package(self):
        calls = []

        def fake_request(method, path, **kw):
            calls.append(path)
            if path.endswith("/package"):
                return dict(_PKG, version=4)
            return {"slug": "pip", "version": 4}

        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["pip"] = {
                "kind": "character", "snippet": "old", "refs": [],
                "origin": {"platform": "drawstyle", "slug": "pip", "version": 3}}
            cig._save_styles(doc)
            with unittest.mock.patch.object(cig, "_platform_request", fake_request), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                rc = cig._style_command(["update", "pip"])
            updated_version = (
                cig._load_styles()["styles"]["pip"]["origin"]["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], "/api/styles/pip")
        self.assertIn("/api/styles/pip/package", calls[1])
        self.assertEqual(updated_version, 4)

    def test_up_to_date_skips_package(self):
        calls = []

        def fake_request(method, path, **kw):
            calls.append(path)
            if path.endswith("/package"):
                return _PKG
            return {"slug": "pip", "version": 3}

        with _tmp_xdg():
            with unittest.mock.patch.object(cig, "_platform_request", fake_request), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                cig._style_command(["pull", "pip"])
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cig._style_command(["update"])
        self.assertEqual(rc, 0)
        self.assertIn("up to date", buf.getvalue())
        self.assertEqual([p for p in calls if p.endswith("/package")],
                         ["/api/styles/pip/package"])

    def test_entry_without_origin_skipped(self):
        with _tmp_xdg():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cig._style_command(["update"])
        self.assertEqual(rc, 0)
        self.assertIn("no pulled styles", buf.getvalue())

    def test_named_entry_without_origin_errors(self):
        with _tmp_xdg():
            cig._style_command(["add", "local", "local only"])
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["update", "local"])
        self.assertIn("has no platform origin", str(cm.exception))


class OidcPkce(unittest.TestCase):
    def test_challenge_is_s256_of_verifier(self):
        import base64
        import hashlib
        verifier, challenge = cig._pkce_pair()
        want = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, want)
        self.assertGreaterEqual(len(verifier), 43)

    def test_token_cache_roundtrip_and_mode(self):
        with _tmp_xdg():
            cig._save_platform_auth({"access_token": "at", "refresh_token": "rt",
                                     "expires_at": 9999999999})
            p = cig._platform_auth_path()
            if os.name != "nt":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cig._load_platform_auth()["access_token"], "at")

    def test_expired_token_triggers_refresh(self):
        with _tmp_xdg():
            cig._save_platform_auth({"access_token": "old", "refresh_token": "rt",
                                     "expires_at": 1})
            with unittest.mock.patch.object(
                    cig, "_oidc_token_request",
                    return_value={"access_token": "new", "refresh_token": "rt2",
                                  "expires_in": 3600}) as m:
                token = cig._platform_access_token(interactive=False)
        self.assertEqual(token, "new")
        self.assertEqual(m.call_args[0][0]["grant_type"], "refresh_token")

    def test_login_errors_when_fixed_callback_port_unavailable(self):
        with unittest.mock.patch.object(cig.http.server, "HTTPServer",
                                        side_effect=OSError("in use")):
            with self.assertRaises(SystemExit) as cm:
                cig._oidc_login_interactive()
        self.assertIn("127.0.0.1:45898", str(cm.exception))


class StylePublish(unittest.TestCase):
    def _setup_local(self):
        cig._style_command(["add", "mylook", "soft watercolor"])

    def test_category_required(self):
        with _tmp_xdg():
            self._setup_local()
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mylook", "--example", "x.png"])
            self.assertIn("--category", str(cm.exception))
            self.assertIn("report", str(cm.exception))

    def test_example_required(self):
        with _tmp_xdg():
            self._setup_local()
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mylook", "--category", "cute"])
            self.assertIn("--example", str(cm.exception))

    def test_no_example_hint_suggests_from_last(self):
        # the friendly error should show the one-liner using --from-last
        with _tmp_xdg():
            self._setup_local()
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mylook", "--category", "cute"])
            self.assertIn("--from-last", str(cm.exception))

    def test_prints_preupload_summary(self):
        import io as _io
        from contextlib import redirect_stderr
        with _tmp_xdg() as root:
            self._setup_local()
            ex = Path(root) / "ex.png"
            ex.write_bytes(_PNG)
            buf = _io.StringIO()
            with unittest.mock.patch.object(cig, "_platform_access_token",
                                            return_value="tok"), \
                 unittest.mock.patch.object(
                     cig, "_platform_request",
                     return_value={"slug": "mylook", "status": "pending"}), \
                 redirect_stderr(buf):
                cig._style_command(["publish", "mylook", "--category", "cute",
                                    "--tag", "watercolor", "--example", str(ex)])
            out = buf.getvalue()
            self.assertIn("publishing 'mylook'", out)
            self.assertIn("category: cute", out)
            self.assertIn("examples: 1", out)
            self.assertIn("track approval", out)

    def test_republish_own_origin_errors(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["mine"] = {"kind": "style", "snippet": "s", "refs": [],
                                     "origin": {"platform": "drawstyle",
                                                "slug": "mine", "version": 1}}
            cig._save_styles(doc)
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mine", "--category", "cute",
                                    "--example", "x.png"])
            self.assertIn("edit", str(cm.exception))

    def test_happy_path_posts_multipart(self):
        with _tmp_xdg() as root:
            self._setup_local()
            ex = Path(root) / "ex.png"
            ex.write_bytes(_PNG)
            with unittest.mock.patch.object(cig, "_platform_access_token",
                                            return_value="tok"), \
                 unittest.mock.patch.object(
                     cig, "_platform_request",
                     return_value={"slug": "mylook", "status": "pending"}) as m:
                rc = cig._style_command(["publish", "mylook", "--category",
                                         "cute", "--example", str(ex)])
        self.assertEqual(rc, 0)
        method, path = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual((method, path), ("POST", "/api/styles"))
        hdrs = m.call_args[1]["headers"]
        self.assertEqual(hdrs["Authorization"], "Bearer tok")
        self.assertIn("multipart/form-data", hdrs["Content-Type"])
        body = m.call_args[1]["data"]
        self.assertIn(b'name="category"', body)
        self.assertIn(b"cute", body)
        self.assertIn(_PNG, body)

    def test_publish_records_origin_and_blocks_second_publish(self):
        with _tmp_xdg() as root:
            self._setup_local()
            ex = Path(root) / "ex.png"
            ex.write_bytes(_PNG)
            with unittest.mock.patch.object(cig, "_platform_access_token",
                                            return_value="tok"), \
                 unittest.mock.patch.object(
                     cig, "_platform_request",
                     return_value={"slug": "mylook", "status": "pending"}):
                rc = cig._style_command(["publish", "mylook", "--category",
                                         "cute", "--example", str(ex)])
            self.assertEqual(rc, 0)
            entry = cig._load_styles()["styles"]["mylook"]
            self.assertEqual(
                entry["origin"],
                {"platform": "drawstyle", "slug": "mylook", "version": 1})
            # a second publish now hits the friendly already-published guard
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mylook", "--category", "cute",
                                    "--example", str(ex)])
            self.assertIn("edit", str(cm.exception))

    def test_too_many_examples_errors_before_auth(self):
        with _tmp_xdg() as root:
            self._setup_local()
            paths = []
            for n in range(4):
                p = Path(root) / f"ex{n}.png"
                p.write_bytes(_PNG)
                paths.extend(["--example", str(p)])
            with unittest.mock.patch.object(cig, "_platform_access_token") as auth:
                with self.assertRaises(SystemExit) as cm:
                    cig._style_command(["publish", "mylook", "--category",
                                        "cute", *paths])
        self.assertIn("at most 3", str(cm.exception))
        auth.assert_not_called()

    def test_pinned_refs_are_uploaded_as_ref_parts(self):
        with _tmp_xdg() as root:
            self._setup_local()
            ref = Path(root) / "ref.png"
            ref.write_bytes(_PNG)
            ex = Path(root) / "ex.png"
            ex.write_bytes(_PNG)
            cig._style_command(["add-ref", "mylook", str(ref)])
            with unittest.mock.patch.object(cig, "_platform_access_token",
                                            return_value="tok"), \
                 unittest.mock.patch.object(
                     cig, "_platform_request",
                     return_value={"slug": "mylook", "status": "pending"}) as m:
                rc = cig._style_command(["publish", "mylook", "--category",
                                         "cute", "--example", str(ex)])
        self.assertEqual(rc, 0)
        body = m.call_args[1]["data"]
        self.assertIn(b'name="example[]"', body)
        self.assertIn(b'name="ref[]"', body)


class GalleryUpload(unittest.TestCase):
    """Anonymous POST /api/uploads — the standalone `upload` subcommand."""

    def test_machine_id_default_and_override(self):
        with unittest.mock.patch.object(cig.socket, "gethostname",
                                        return_value="box.local"):
            self.assertEqual(cig._upload_machine_id(), "box.local")
        with unittest.mock.patch.dict(
                os.environ, {"CHATGPT_IMAGEGEN_MACHINE_ID": "fleet-7"}):
            self.assertEqual(cig._upload_machine_id(), "fleet-7")

    def test_read_upload_accepts_gif(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "anim.gif"
            p.write_bytes(b"GIF89a" + b"\x00" * 32)
            _, mime, _ = cig._read_upload_image(str(p))
            self.assertEqual(mime, "image/gif")

    def test_read_upload_rejects_non_image(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.txt"
            p.write_text("nope")
            with self.assertRaises(SystemExit):
                cig._read_upload_image(str(p))

    def test_posts_multipart_with_machine_header(self):
        captured = {}

        def fake(method, path, *, data=None, headers=None):
            captured.update(method=method, path=path, data=data,
                            headers=headers or {})
            return {"upload": {"url": "https://drawstyle.leeguoo.com/img/x.png",
                               "remaining_today": 7}}

        with tempfile.TemporaryDirectory() as d:
            img = _write_png(os.path.join(d, "out.png"))
            with unittest.mock.patch.object(cig, "_platform_request", fake), \
                 unittest.mock.patch.object(cig, "_upload_machine_id",
                                            return_value="m-1"):
                up = cig._upload_gallery_image(img, "candid-vacation-photography")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/api/uploads")
        self.assertEqual(captured["headers"]["X-Drawstyle-Machine-Id"], "m-1")
        self.assertIn(b"candid-vacation-photography", captured["data"])
        self.assertIn(b'name="image"', captured["data"])
        self.assertEqual(up["remaining_today"], 7)

    def test_oversize_without_sips_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n"
                          + b"\x00" * (cig.UPLOAD_MAX_BYTES + 1))
            with unittest.mock.patch.object(cig, "_downscale_for_upload",
                                            return_value=None):
                with self.assertRaises(SystemExit) as cm:
                    cig._upload_gallery_image(str(p), None)
            self.assertIn("upload cap", str(cm.exception))

    def test_oversize_is_downscaled_then_uploaded(self):
        captured = {}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n"
                          + b"\x00" * (cig.UPLOAD_MAX_BYTES + 1))
            tiny = b"\xff\xd8\xff" + b"\x00" * 16

            def fake(method, path, *, data=None, headers=None):
                captured["data"] = data
                return {"upload": {"url": "u", "remaining_today": 1}}

            with unittest.mock.patch.object(cig, "_downscale_for_upload",
                                            return_value=tiny), \
                 unittest.mock.patch.object(cig, "_platform_request", fake):
                cig._upload_gallery_image(str(p), None)
        self.assertIn(b"image/jpeg", captured["data"])

    def test_unexpected_response_errors(self):
        with tempfile.TemporaryDirectory() as d:
            img = _write_png(os.path.join(d, "o.png"))
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value={"nope": 1}):
                with self.assertRaises(SystemExit):
                    cig._upload_gallery_image(img, None)

    def test_upload_command(self):
        with tempfile.TemporaryDirectory() as d:
            img = _write_png(os.path.join(d, "o.png"))
            err = io.StringIO()
            with unittest.mock.patch.object(
                    cig, "_platform_request",
                    return_value={"upload": {"url": "https://x/img/y.png",
                                             "remaining_today": 6}}), \
                 redirect_stderr(err):
                rc = cig._upload_command([img, "--style", "foo"])
            self.assertEqual(rc, 0)
            self.assertIn("remaining today: 6", err.getvalue())
            self.assertIn("https://x/img/y.png", err.getvalue())
            self.assertIn("/en/s/foo/generations", err.getvalue())


class PlatformAccessTokenRefreshFallback(unittest.TestCase):
    def _expired(self):
        cig._save_platform_auth({"access_token": "old", "refresh_token": "rt",
                                 "expires_at": 1})

    @staticmethod
    def _boom(form):
        raise SystemExit("error: drawstyle login token exchange failed (HTTP 400)")

    def test_interactive_falls_back_to_login_when_refresh_fails(self):
        with _tmp_xdg():
            self._expired()
            with unittest.mock.patch.object(cig, "_oidc_token_request",
                                            self._boom), \
                 unittest.mock.patch.object(
                     cig, "_oidc_login_interactive",
                     return_value={"access_token": "fresh"}) as login:
                token = cig._platform_access_token(interactive=True)
            self.assertEqual(token, "fresh")
            login.assert_called_once()

    def test_non_interactive_still_fails_fast_on_refresh_failure(self):
        with _tmp_xdg():
            self._expired()
            with unittest.mock.patch.object(cig, "_oidc_token_request",
                                            self._boom), \
                 unittest.mock.patch.object(
                     cig, "_oidc_login_interactive") as login:
                with self.assertRaises(SystemExit):
                    cig._platform_access_token(interactive=False)
            login.assert_not_called()


class OidcCallbackMatch(unittest.TestCase):
    def test_matching_cb_with_state_and_code(self):
        ok, code = cig._oidc_callback_match("/cb?state=S&code=XYZ", "S")
        self.assertTrue(ok)
        self.assertEqual(code, "XYZ")

    def test_non_cb_path_ignored(self):
        # a stray favicon/prefetch must not consume the handshake
        self.assertEqual(
            cig._oidc_callback_match("/favicon.ico?state=S&code=XYZ", "S"),
            (False, ""))

    def test_state_mismatch_ignored(self):
        self.assertEqual(
            cig._oidc_callback_match("/cb?state=other&code=XYZ", "S"),
            (False, ""))

    def test_missing_code_ignored(self):
        self.assertEqual(cig._oidc_callback_match("/cb?state=S", "S"),
                         (False, ""))


class StyleUpdatePartial(unittest.TestCase):
    def test_mid_list_failure_saves_earlier_entry(self):
        def fake_request(method, path, **kw):
            if path.endswith("/package"):
                slug = path.split("/")[3]
                return dict(_PKG, slug=slug, version=4,
                            refs=[{"url": f"https://drawstyle.leeguoo.com/img/{slug}",
                                   "content_type": "image/png"}])
            slug = path.rsplit("/", 1)[-1]
            return {"slug": slug, "version": 4}

        def fake_download(url):
            if url.endswith("/bb"):
                raise OSError("boom")
            return _PNG

        with _tmp_xdg():
            doc = cig._load_styles()
            for slug in ("aa", "bb"):
                doc["styles"][slug] = {
                    "kind": "character", "snippet": "old", "refs": [],
                    "origin": {"platform": "drawstyle", "slug": slug,
                               "version": 3}}
            cig._save_styles(doc)
            with unittest.mock.patch.object(cig, "_platform_request",
                                            fake_request), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            fake_download):
                with self.assertRaises(SystemExit):
                    cig._style_command(["update"])
            reloaded = cig._load_styles()["styles"]
            # first entry fully updated AND persisted before the failure
            self.assertEqual(reloaded["aa"]["origin"]["version"], 4)
            self.assertEqual(len(reloaded["aa"]["refs"]), 1)
            self.assertTrue(
                (cig._asset_dir("aa") / reloaded["aa"]["refs"][0]).exists())
            # second entry untouched (still old version, no refs on disk)
            self.assertEqual(reloaded["bb"]["origin"]["version"], 3)
            self.assertEqual(reloaded["bb"]["refs"], [])
            self.assertFalse(cig._asset_dir("bb").exists())


class ApplyPackageMalformed(unittest.TestCase):
    def test_missing_keys_exits_cleanly(self):
        with self.assertRaises(SystemExit) as cm:
            cig._apply_package({"styles": {}}, "x", {"refs": []})
        self.assertIn("malformed package response", str(cm.exception))

    def test_non_dict_payload_exits_cleanly(self):
        with self.assertRaises(SystemExit) as cm:
            cig._apply_package({"styles": {}}, "x", ["not", "a", "dict"])
        self.assertIn("malformed package response", str(cm.exception))


def _composer_state(text="", attachments=0, ready=None, **kw):
    """Build a simulated ChatGPT composer state with optional field overrides."""
    return {"exists": True, "text": text, "attachments": attachments,
            "ready": attachments if ready is None else ready, "busy": False,
            "send_ready": True, "user_turns": 0, **kw}


class UploadReferences(unittest.TestCase):
    """The composer's file input moved (issue #19) — the selector cascade is what
    keeps img2img working across that reshuffle, so pin its behaviour."""

    @staticmethod
    def _run(fail_selectors):
        """Drive _upload_references with a fake _ab that rejects some selectors.

        Returns (selectors tried, emitted lines) or raises whatever it raises.
        """
        tried, emitted = [], []
        uploaded = False

        def fake_ab(ab, *args, session=None, timeout=None, profile=None):
            """Simulate missing upload selectors and report successful attachments."""
            nonlocal uploaded
            if args[0] == "upload":
                tried.append(args[1])
                if args[1] in fail_selectors:
                    raise cig.GatewayError(f"element not found: {args[1]}")
                uploaded = True
                return ""
            return json.dumps(json.dumps(_composer_state(attachments=int(uploaded))))

        with unittest.mock.patch.object(cig, "_ab", fake_ab), \
                unittest.mock.patch.object(cig.time, "sleep", lambda *_: None):
            cig._upload_references("ab", "s", ["/tmp/a.png"],
                                   lambda: 90.0, emitted.append)
        return tried, emitted

    def test_uses_current_composer_input_first(self):
        """Prefer the current ChatGPT file input and confirm its upload."""
        tried, emitted = self._run(fail_selectors=())
        self.assertEqual(tried, ["#upload-files"])
        self.assertIn("all 1 reference image(s) uploaded", emitted)

    def test_falls_back_to_next_selector(self):
        """Try the next upload selector when the preferred input is missing."""
        tried, _ = self._run(fail_selectors=("#upload-files",))
        self.assertEqual(tried, ["#upload-files", "form input[type=file]"])

    def test_reraises_last_error_when_all_selectors_fail(self):
        """Expose the final selector error when no upload input can be found."""
        with self.assertRaises(cig.GatewayError) as cm:
            self._run(fail_selectors=tuple(cig._UPLOAD_SELECTORS))
        self.assertIn(cig._UPLOAD_SELECTORS[-1], str(cm.exception))

    def test_legacy_selector_is_still_last_resort(self):
        """Keep the image-only input selector available for older composers."""
        # Old DOMs exposed only input[accept="image/*"]; keep them working.
        self.assertIn('input[accept="image/*"]', cig._UPLOAD_SELECTORS)

    def test_waits_for_all_five_server_backed_images(self):
        """Wait for all five uploads and pending processing without re-uploading."""
        states = [_composer_state(), _composer_state(attachments=1, ready=0),
                  _composer_state(attachments=5, ready=1),
                  _composer_state(attachments=5, ready=4),
                  _composer_state(attachments=5, busy=True), _composer_state(attachments=5)]
        with unittest.mock.patch.object(cig, "_web_composer_state", side_effect=states), \
             unittest.mock.patch.object(cig, "_ab") as ab, \
             unittest.mock.patch.object(cig.time, "sleep"):
            refs = [f"/ref{i}.png" for i in range(5)]
            cig._upload_references("ab", "s", refs, lambda: 90, lambda _: None)
        ab.assert_called_once_with("ab", "upload", "#upload-files", *refs,
                                   session="s", timeout=90.0)

    def test_partial_or_unconfirmed_upload_never_proceeds(self):
        """Reject batches with missing thumbnails or unfinished server uploads."""
        for state in (_composer_state(), _composer_state(attachments=1),
                      _composer_state(attachments=5, ready=4)):
            with self.subTest(state=state), \
                 unittest.mock.patch.object(cig, "_web_composer_state",
                     side_effect=[_composer_state()] + [state] * 90), \
                 unittest.mock.patch.object(cig, "_ab") as ab, \
                 unittest.mock.patch.object(cig.time, "sleep"):
                with self.assertRaisesRegex(cig.GatewayError, "upload incomplete.*not submitted"):
                    cig._upload_references("ab", "s", ["/ref.png"] * 5,
                                           lambda: 90, lambda _: None)
                ab.assert_called_once()

    def test_upload_error_after_dispatch_does_not_duplicate_images(self):
        """Observe an upload that started before an error instead of repeating it."""
        with unittest.mock.patch.object(cig, "_web_composer_state", side_effect=[
                 _composer_state(), _composer_state(attachments=2), _composer_state(attachments=5)]), \
             unittest.mock.patch.object(cig, "_ab", side_effect=cig.GatewayError("timed out")) as ab:
            cig._upload_references("ab", "s", ["/ref.png"] * 5, lambda: 90, lambda _: None)
        ab.assert_called_once()

    def test_uncertain_upload_with_no_thumbnails_is_not_replayed(self):
        """Avoid replaying a timed-out upload whose outcome cannot be verified."""
        with unittest.mock.patch.object(cig, "_web_composer_state", return_value=_composer_state()), \
             unittest.mock.patch.object(cig, "_ab", side_effect=cig.GatewayError("timed out")) as ab:
            with self.assertRaisesRegex(cig.GatewayError, "outcome is uncertain"):
                cig._upload_references("ab", "s", ["/ref.png"], lambda: 90, lambda _: None)
        ab.assert_called_once()

    def test_preexisting_and_duplicate_attachments_are_rejected(self):
        """Reject existing or excess attachments to prevent mixed reference sets."""
        for states in ([_composer_state(attachments=1)],
                       [_composer_state(), _composer_state(attachments=6)]):
            with self.subTest(states=states), \
                 unittest.mock.patch.object(cig, "_web_composer_state", side_effect=states), \
                 unittest.mock.patch.object(cig, "_ab") as ab:
                with self.assertRaises(cig.GatewayError):
                    cig._upload_references("ab", "s", ["/ref.png"] * 5, lambda: 90, lambda _: None)
                self.assertLessEqual(ab.call_count, 1)

    def test_existing_draft_stops_before_upload(self):
        """Preserve an existing draft by stopping before any reference upload."""
        with unittest.mock.patch.object(cig, "_web_composer_state",
                 return_value=_composer_state("existing draft")), \
             unittest.mock.patch.object(cig, "_ab") as ab:
            with self.assertRaisesRegex(cig.GatewayError, "references not uploaded"):
                cig._upload_references("ab", "s", ["/ref.png"], lambda: 90, lambda _: None)
        ab.assert_not_called()


class WebPromptSubmission(unittest.TestCase):
    """Verify exact prompt entry and one confirmed send using simulated Chrome."""

    def _run(self, states, prompt="first\n\n猫 🦊\nlast\n", send_error=False):
        """Run submission against supplied states and record browser commands."""
        self.calls = []

        def ab(*args, **kw):
            """Record browser calls and optionally simulate an uncertain Send."""
            self.calls.append((args, kw))
            if send_error and args[1] == "click":
                raise cig.GatewayError("click outcome unknown")

        state_iter = iter(states)
        with unittest.mock.patch.object(cig, "_web_composer_state",
                 side_effect=lambda *a: next(state_iter, states[-1])), \
             unittest.mock.patch.object(cig, "_ab", side_effect=ab), \
             unittest.mock.patch.object(cig.time, "sleep"):
            cig._submit_web_prompt("ab", "s", prompt, 5, lambda: 90, lambda _: None)

    def test_long_multiline_prompt_uses_stdin_and_one_click(self):
        """Preserve a long Unicode prompt through stdin and click Send once."""
        prompt = (" \tParagraph — 猫, 'quotes', $(), `code`.\n\n" * 4000) + "end\n"
        self._run([_composer_state(attachments=5),
                   _composer_state(prompt, attachments=5, send_ready=False),
                   _composer_state(prompt, attachments=5),
                   _composer_state(user_turns=1)], prompt)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], ("ab", "fill", cig.WEB_COMPOSER_SEL, "--stdin"))
        self.assertEqual(self.calls[0][1]["input_text"], prompt)
        self.assertEqual(self.calls[1][0][1:], ("click", cig.WEB_SEND_CLICK_SEL))

    def test_changed_text_attachments_or_early_submission_prevents_send(self):
        """Stop before Send if prompt text, reference state, or user turns change."""
        prompt = "first\n\n猫 🦊\nlast\n"
        bad = [_composer_state(prompt.replace("\n", ""), attachments=5),
               _composer_state(prompt, attachments=4),
               _composer_state(prompt, attachments=5, ready=4),
               _composer_state(prompt, attachments=5, user_turns=1)]
        for state in bad:
            with self.subTest(state=state), self.assertRaises(cig.GatewayError):
                self._run([_composer_state(attachments=5), state], prompt)
            # The second fill clears the draft we pasted; Send is never clicked.
            self.assertEqual([a[1] for a, _ in self.calls], ["fill", "fill"])

    def test_editor_whitespace_equivalents_still_send(self):
        """Accept CRLF and NBSP the editor substitutes, but no other drift."""
        prompt = "  indented\r\nnext\rlast\n"
        shown = "\u00a0 indented\nnext\nlast\n"
        self._run([_composer_state(attachments=5),
                   _composer_state(shown, attachments=5),
                   _composer_state(shown, attachments=5),
                   _composer_state(user_turns=1)], prompt)
        self.assertEqual([a[1] for a, _ in self.calls], ["fill", "click"])
        with self.assertRaises(cig.GatewayError):
            self._run([_composer_state(attachments=5),
                       _composer_state(shown.replace("next", "nxet"), attachments=5)], prompt)

    def test_existing_draft_is_not_overwritten(self):
        """Leave an existing composer draft untouched without issuing commands."""
        with self.assertRaisesRegex(cig.GatewayError, "not empty"):
            self._run([_composer_state("existing draft", attachments=5)])
        self.assertEqual(self.calls, [])

    def test_disabled_send_button_never_gets_clicked(self):
        """Fail without clicking when the Send button remains disabled."""
        prompt = "first\n\n猫 🦊\nlast\n"
        with self.assertRaisesRegex(cig.GatewayError, "did not become ready"):
            self._run([_composer_state(attachments=5),
                       _composer_state(prompt, attachments=5, send_ready=False)])
        self.assertNotIn("click", [a[1] for a, _ in self.calls])

    def test_ambiguous_send_is_checked_without_replaying(self):
        """Confirm a message after an uncertain click without sending again."""
        prompt = "first\n\n猫 🦊\nlast\n"
        self._run([_composer_state(attachments=5),
                   _composer_state(prompt, attachments=5),
                   _composer_state(prompt, attachments=5),
                   _composer_state(user_turns=1)], send_error=True)
        self.assertEqual(len(self.calls), 2)

    def test_empty_composer_alone_is_not_proof_of_submission(self):
        """Require a new user message as well as an empty composer after Send."""
        prompt = "first\n\n猫 🦊\nlast\n"
        with self.assertRaisesRegex(cig.GatewayError, "Send was not repeated"):
            self._run([_composer_state(attachments=5),
                       _composer_state(prompt, attachments=5), _composer_state()])
        self.assertEqual(len(self.calls), 2)

    def test_multiple_user_turns_are_reported_without_another_send(self):
        """Report unexpected multiple messages without attempting another send."""
        prompt = "first\n\n猫 🦊\nlast\n"
        with self.assertRaisesRegex(cig.GatewayError, "multiple ChatGPT user messages"):
            self._run([_composer_state(attachments=5),
                       _composer_state(prompt, attachments=5), _composer_state(user_turns=2)])
        self.assertEqual(len(self.calls), 2)

    def test_failed_send_clears_the_pasted_draft(self):
        """Undo the paste when nothing was sent, so no draft blocks later runs."""
        prompt = "first\n\n猫 🦊\nlast\n"
        with self.assertRaisesRegex(cig.GatewayError, "did not become ready"):
            self._run([_composer_state(attachments=5),
                       _composer_state(prompt, attachments=5, send_ready=False)])
        self.assertEqual([a[1] for a, _ in self.calls], ["fill", "fill"])
        self.assertEqual(self.calls[-1][1]["input_text"], "")

    def test_confirmed_send_does_not_clear_the_composer(self):
        """Never issue a clearing fill once the prompt actually went out."""
        prompt = "first\n\n猫 🦊\nlast\n"
        self._run([_composer_state(attachments=5),
                   _composer_state(prompt, attachments=5),
                   _composer_state(user_turns=1)], prompt)
        self.assertEqual([a[1] for a, _ in self.calls], ["fill", "click"])

    def test_existing_draft_error_explains_how_to_recover(self):
        """Point at the ChatGPT composer so a stuck draft can be cleared."""
        with self.assertRaisesRegex(cig.GatewayError, "chatgpt.com"):
            self._run([_composer_state("existing draft", attachments=5)])
        self.assertEqual(self.calls, [])

    def test_ab_passes_prompt_on_stdin_without_adding_it_to_argv(self):
        """Pass prompt text as subprocess input rather than a command argument."""
        prompt = "long\n\n猫\n"
        result = argparse.Namespace(returncode=0, stdout="done")
        with unittest.mock.patch.object(cig.subprocess, "run", return_value=result) as run:
            cig._ab("ab", "fill", "#prompt-textarea", "--stdin", input_text=prompt,
                    session="s", timeout=60)
        self.assertEqual(run.call_args.kwargs["input"], prompt)
        self.assertNotIn(prompt, run.call_args.args[0])

    def test_failed_upload_cleans_downloads_and_never_types(self):
        """Remove temporary reference downloads and skip typing after upload failure."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "download.png"
            path.write_bytes(b"test")
            with unittest.mock.patch.object(cig, "_resolve_ref_path", return_value=(str(path), str(path))), \
                 unittest.mock.patch.object(cig, "_upload_references", side_effect=cig.GatewayError("incomplete")), \
                 unittest.mock.patch.object(cig, "_submit_web_prompt") as submit:
                with self.assertRaises(cig.GatewayError):
                    cig._generate_in_browser("ab", "s", "prompt", argparse.Namespace(),
                        time.monotonic() + 90, lambda: 90, lambda _: None, refs=["https://x/ref.png"])
            self.assertFalse(path.exists())
            submit.assert_not_called()


class SslContext(unittest.TestCase):
    """The gallery's Cloudflare chain trips Python 3.13+'s VERIFY_X509_STRICT
    (issue #20). We clear that ONE flag — and nothing else."""

    def test_strict_x509_is_cleared(self):
        self.assertFalse(cig._ssl_ctx().verify_flags & ssl.VERIFY_X509_STRICT)

    def test_certificate_verification_stays_on(self):
        # The whole point: relax RFC-strictness, never verification itself.
        ctx = cig._ssl_ctx()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_only_strict_differs_from_the_default_context(self):
        # Guards against a future edit quietly widening this into "trust all".
        default = ssl.create_default_context().verify_flags
        self.assertEqual(cig._ssl_ctx().verify_flags,
                         default & ~ssl.VERIFY_X509_STRICT)


class GeminiPromptFraming(unittest.TestCase):
    """Style references must not become the subject.

    Regression guard for a real failure: an agy run that described every
    attached reference as "the subject" answered a prompt about a green glass
    bottle with a four-panel comic strip of the style reference's cartoon dog.
    """

    def test_style_refs_are_not_the_subject(self):
        t = cig._build_reference_framing(0, 2)
        self.assertIn("do NOT copy their content", t)
        self.assertNotIn("as the subject", t)

    def test_character_refs_are_reproduced(self):
        self.assertIn("canonical subject", cig._build_reference_framing(1, 0))

    def test_mixed_refs_spell_out_the_split(self):
        t = cig._build_reference_framing(2, 3)
        self.assertIn("first 2", t)
        self.assertIn("remaining 3", t)

    def test_no_refs_yields_no_framing(self):
        self.assertEqual(cig._build_reference_framing(0, 0), "")

    def test_gemini_text_carries_the_prompt_and_style_framing(self):
        t = cig._build_gemini_text("a green glass bottle", "1024x1024",
                                   n_character_refs=0, n_style_refs=1)
        self.assertIn("a green glass bottle", t)
        self.assertIn("do NOT copy their content", t)
        self.assertIn("square 1:1", t)

    def test_gemini_asks_for_a_ratio_even_on_auto(self):
        # Silence is not neutral here: with no ratio requested Gemini defaults
        # to 16:9 (five probe runs, all 1024x559). Asking for square is the
        # less surprising default for a caller who specified no shape.
        self.assertIn("square 1:1", cig._build_gemini_text("x", "auto"))

    def test_gemini_honours_each_named_size(self):
        for size, want in (("1024x1024", "square 1:1"),
                           ("1024x1536", "portrait 2:3"),
                           ("1536x1024", "landscape 3:2")):
            self.assertIn(want, cig._build_gemini_text("x", size))


class GeminiImageToolLabels(unittest.TestCase):
    """The image-tool switch matches on localised menu text."""

    def test_the_observed_chinese_label_is_covered(self):
        # This is the exact string read off the live menu; it is the one label
        # verified to work, so it must not be dropped in a future tidy-up.
        self.assertIn("制作图片", cig.GEMINI_IMAGE_TOOL_LABELS)

    def test_english_label_is_covered(self):
        self.assertIn("Create image", cig.GEMINI_IMAGE_TOOL_LABELS)


class GeminiImageSrcMatching(unittest.TestCase):
    """The result regex has to reject Google's own avatars.

    gemini.google.com serves the generated image AND the account avatar off
    googleusercontent.com; matching the bare host grabs the avatar instead.
    """

    def _m(self, src):
        return re.search(cig.GEMINI_IMG_SRC_RE, src) is not None

    def test_blob_result_matches(self):
        self.assertTrue(self._m("blob:https://gemini.google.com/dd0d-3e3f"))

    def test_account_avatar_is_rejected(self):
        self.assertFalse(self._m(
            "https://lh3.googleusercontent.com/a/ACg8ocJ2KygVENyOIg=s64-c-v1-rj"))

    def test_profile_chip_is_rejected(self):
        self.assertFalse(self._m(
            "https://lh3.google.com/u/0/ogw/AF2bZyhScQBt51px0SmUvlC8=s64-c-mo"))


class AgyOutputHandling(unittest.TestCase):
    """agy reports where it saved the file; it does not obey a path we pick."""

    def test_path_is_read_from_a_file_url(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as fh:
            out = f"Saved it as [teapot.png](file://{fh.name}) for you."
            self.assertEqual(cig._agy_extract_path(out), fh.name)

    def test_bare_absolute_path_on_the_last_line(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as fh:
            self.assertEqual(cig._agy_extract_path(f"done\n{fh.name}\n"), fh.name)

    def test_nonexistent_path_is_not_accepted(self):
        self.assertIsNone(cig._agy_extract_path("/nope/does/not/exist.png"))

    def test_prose_only_reply_yields_nothing(self):
        self.assertIsNone(cig._agy_extract_path("I could not generate that."))


class GeminiBackendsAreOptIn(unittest.TestCase):
    """auto must never route to a different vendor's account on its own."""

    def test_auto_pick_never_returns_gemini_or_agy(self):
        for web, codex in ((True, True), (True, False),
                           (False, True), (False, False)):
            self.assertIn(cig._auto_backend_pick(web, codex),
                          ("web", "codex", "neither"))

    def test_both_backends_are_selectable_by_name(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertIn('"auto", "web", "codex", "gemini", "agy"', src)


class GoogleProfileDetection(unittest.TestCase):
    """The Google scan must differ from the chatgpt.com one in the cookie only."""

    def test_it_queries_google_session_cookies(self):
        seen = {}

        def fake(host_like, name_clause):
            seen["host"] = host_like
            seen["name"] = name_clause
            return ["Profile 12"]

        with unittest.mock.patch.object(cig, "_detect_logged_in_profiles", fake):
            self.assertEqual(cig._detect_google_profiles(), ["Profile 12"])
        self.assertIn("google.com", seen["host"])
        self.assertIn("__Secure-1PSID", seen["name"])

    def test_chatgpt_detection_defaults_are_unchanged(self):
        import inspect
        sig = inspect.signature(cig._detect_logged_in_profiles)
        self.assertEqual(sig.parameters["host_like"].default, "%chatgpt.com%")
        self.assertIn("__Secure-next-auth.session-token",
                      sig.parameters["name_clause"].default)


class RefRoles(unittest.TestCase):
    """issue #26 — an ad-hoc --ref must be expressible as style/composition,
    not only as the canonical subject."""

    def test_ref_role_rehomes_adhoc_refs(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            for role in ("character", "style", "composition"):
                ordered = cig._collect_refs(doc, [], ["/a.png"], adhoc_role=role)
                self.assertEqual([r["group"] for r in ordered], [role])

    def test_role_shorthands_order_character_style_composition(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            ordered = cig._collect_refs(
                doc, [], ["/subj.png"],
                adhoc_style_refs=["/st.png"],
                adhoc_composition_refs=["/comp.png"])
            self.assertEqual([r["group"] for r in ordered],
                             ["character", "style", "composition"])
            self.assertEqual([r["ref"] for r in ordered],
                             ["/subj.png", "/st.png", "/comp.png"])

    def test_composition_only_never_claims_the_reference_as_subject(self):
        for text in (
            cig._build_web_text("a man", "auto", n_composition_refs=1),
            cig._build_user_text("a man", "auto", "png", n_composition_refs=1),
            cig._build_gemini_text("a man", "auto", n_composition_refs=1),
            cig._build_reference_framing(0, 0, 1),
        ):
            self.assertIn("composition reference", text)
            self.assertIn("do NOT reproduce", text)
            self.assertNotIn("canonical subject", text)

    def test_zero_composition_refs_leaves_existing_wording_untouched(self):
        self.assertEqual(cig._composition_clause(0, True), "")
        self.assertEqual(
            cig._build_web_text("a cat", "auto", n_character_refs=1),
            cig._build_web_text("a cat", "auto", n_character_refs=1,
                                n_composition_refs=0))

    def test_ref_counts_falls_back_to_subject_without_the_asset_machinery(self):
        self.assertEqual(
            cig._ref_counts(argparse.Namespace(), ["/a.png", "/b.png"]), (2, 0, 0))


class AgyStallTimeout(unittest.TestCase):
    """issue #24 — --stall-timeout was documented but only --timeout enforced."""

    def test_silent_child_is_killed_at_the_idle_deadline(self):
        t0 = time.monotonic()
        # Shrink the startup grace too, or the never-speaks child would sit out
        # the full grace window and make the suite that much slower.
        with unittest.mock.patch.object(cig, "_AGY_STARTUP_GRACE", 0.6):
            with self.assertRaises(cig.GatewayError) as ctx:
                cig._run_agy_watched(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    tempfile.gettempdir(), total_timeout=30.0, stall_timeout=0.6)
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertIn("stalled", str(ctx.exception))

    def test_a_slow_boot_is_not_mistaken_for_a_stall(self):
        # agy is silent while it boots. Timing that silence from Popen() rather
        # than from the first chunk killed healthy runs under a short
        # --stall-timeout — and made the chatty test below flaky whenever the
        # interpreter took longer than the window just to start.
        rc, out, _err = cig._run_agy_watched(
            [sys.executable, "-c",
             "import time; time.sleep(1.2); print('late', flush=True)"],
            tempfile.gettempdir(), total_timeout=30.0, stall_timeout=0.3)
        self.assertEqual((rc, out.strip()), (0, "late"))

    def test_the_startup_grace_is_a_positive_default(self):
        self.assertGreaterEqual(cig._AGY_STARTUP_GRACE, 1.0)

    def test_chatty_child_survives_a_shorter_stall_window(self):
        rc, out, _err = cig._run_agy_watched(
            [sys.executable, "-c",
             "import sys,time\n"
             "for _ in range(6):\n"
             "    print('tick', flush=True); time.sleep(0.2)"],
            tempfile.gettempdir(), total_timeout=30.0, stall_timeout=0.6)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("tick"), 6)

    def test_stdout_and_exit_code_are_preserved(self):
        rc, out, err = cig._run_agy_watched(
            [sys.executable, "-c",
             "import sys; print('o'); print('e', file=sys.stderr); sys.exit(3)"],
            tempfile.gettempdir(), total_timeout=10.0, stall_timeout=5.0)
        self.assertEqual((rc, out.strip(), err.strip()), (3, "o", "e"))

    def test_run_agy_accepts_a_stall_timeout(self):
        self.assertIn("stall_timeout", inspect.signature(cig.run_agy).parameters)


class ContentTypeMismatch(unittest.TestCase):
    """issue #25 — a .jpg must not silently contain PNG bytes."""

    def test_fmt_from_mime(self):
        self.assertEqual(cig._fmt_from_mime("image/png"), "png")
        self.assertEqual(cig._fmt_from_mime("image/jpeg; charset=binary"), "jpeg")
        self.assertIsNone(cig._fmt_from_mime("application/octet-stream"))
        self.assertIsNone(cig._fmt_from_mime(None))

    def test_mismatch_warns_naming_both_formats(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            cig._warn_content_type_mismatch(Path("a.jpg"), "jpeg", "image/png")
        msg = buf.getvalue()
        self.assertIn("--format=jpeg", msg)
        self.assertIn("png", msg)

    def test_matching_or_unknown_type_stays_silent(self):
        for ctype in ("image/jpeg", "application/octet-stream", None):
            buf = io.StringIO()
            with redirect_stderr(buf):
                cig._warn_content_type_mismatch(Path("a.jpg"), "jpeg", ctype)
            self.assertEqual(buf.getvalue(), "")

    def test_default_out_path_follows_the_sniffed_type(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertIn("_default_out_path(args.prompt, _actual_fmt)", src)


class GeminiBlobDownload(unittest.TestCase):
    """issue #27 — prefer the byte-exact blob route so Google's C2PA
    provenance manifest survives; fall back to the canvas, never fail."""

    def test_non_blob_src_is_not_attempted(self):
        self.assertIsNone(cig._gemini_blob_download(
            "chrome-use", "s", "https://x/y.png", lambda: 30.0))

    def test_blob_bytes_are_returned_with_the_sniffed_type(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)

        def fake_ab(ab, *a, **kw):
            self.assertEqual(a[0], "download-url")
            Path(a[2]).write_bytes(png)
            return ""

        with unittest.mock.patch.object(cig, "_ab", fake_ab):
            data, meta = cig._gemini_blob_download(
                "chrome-use", "s", "blob:https://gemini.google.com/x",
                lambda: 30.0)
        self.assertEqual(data, png)
        self.assertEqual(meta["content_type"], "image/png")
        self.assertEqual(meta["provenance"], "preserved")

    def test_old_chrome_use_falls_back_instead_of_raising(self):
        def boom(*a, **kw):
            raise cig.GatewayError("Invalid download URL: expected http://")

        with unittest.mock.patch.object(cig, "_ab", boom):
            self.assertIsNone(cig._gemini_blob_download(
                "chrome-use", "s", "blob:https://gemini.google.com/x",
                lambda: 30.0))

    def test_non_image_payload_is_treated_as_a_miss(self):
        def fake_ab(ab, *a, **kw):
            Path(a[2]).write_bytes(b"<html>nope</html>")
            return ""

        with unittest.mock.patch.object(cig, "_ab", fake_ab):
            self.assertIsNone(cig._gemini_blob_download(
                "chrome-use", "s", "blob:https://gemini.google.com/x",
                lambda: 30.0))

    def test_canvas_path_still_labels_itself_as_provenance_stripped(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertIn('"transport": "canvas", "provenance": "stripped"', src)
        # The C2PA warning must not fire on the byte-exact path.
        self.assertIn('if meta.get("provenance") == "stripped":', src)


class StallTimeoutZero(unittest.TestCase):
    """issue #30 — the stall message told users to pass 0, argparse refused it."""

    def test_non_negative_int_accepts_zero_and_rejects_negatives(self):
        self.assertEqual(cig._non_negative_int("0"), 0)
        self.assertEqual(cig._non_negative_int("7"), 7)
        for bad in ("-1", "abc", ""):
            with self.assertRaises(argparse.ArgumentTypeError):
                cig._non_negative_int(bad)

    def test_positive_int_still_rejects_zero(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cig._positive_int("0")

    def test_the_stall_timeout_flag_uses_the_permissive_validator(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertIn('"--stall-timeout",\n        type=_non_negative_int,', src)
        # --timeout must stay strictly positive: 0 there means "give up at once".
        self.assertIn('"--timeout",\n        type=_positive_int,', src)

    def test_zero_survives_the_clamp_against_total_timeout(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertIn("0.0 if args.stall_timeout <= 0", src)

    def test_zero_disables_the_agy_idle_kill(self):
        # A child silent for longer than the old default must still finish.
        rc, out, _err = cig._run_agy_watched(
            [sys.executable, "-c", "import time; time.sleep(1); print('done')"],
            tempfile.gettempdir(), total_timeout=20.0, stall_timeout=0.0)
        self.assertEqual((rc, out.strip()), (0, "done"))

    def _stream_read_timeout(self, stall_timeout: float) -> float:
        seen = {}

        class FakeResp:
            def __iter__(self):
                return iter([b"data: [DONE]\n", b"\n"])

            def close(self):
                pass

        with unittest.mock.patch.object(cig.urllib.request, "urlopen",
                                        lambda *a, **kw: FakeResp()), \
             unittest.mock.patch.object(
                 cig, "_loosen_read_timeout",
                 lambda resp, t: seen.__setitem__("t", t)):
            list(cig._stream("https://x", {}, {},
                             time.monotonic() + 300.0, stall_timeout))
        return seen["t"]

    def test_zero_falls_back_to_the_remaining_budget_not_a_zero_socket(self):
        # A 0 socket timeout means non-blocking: every read would fail at once.
        self.assertGreater(self._stream_read_timeout(0.0), 200.0)

    def test_a_positive_stall_timeout_still_bounds_the_read(self):
        self.assertAlmostEqual(self._stream_read_timeout(12.0), 12.0, places=3)

    def test_the_agy_stall_message_still_advertises_zero(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertIn("drop it to 0 to wait out the", src)


class ConcurrencySlot(unittest.TestCase):
    """The cross-process gate must survive platforms without ``fcntl``.

    Importing ``fcntl`` at module scope used to abort the whole CLI on Windows
    (PR #31) — even ``--version``. It is optional now, and Windows gets a real
    lock via ``msvcrt`` rather than silently losing the gate that issue #7
    added to stop parallel web runs cross-contaminating each other.
    """

    def test_the_posix_import_is_optional(self):
        src = Path(cig.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\nimport fcntl\n", src)   # bare import would crash
        self.assertIn("except ImportError:", src)

    def test_a_second_holder_is_made_to_queue(self):
        # Two nested entries with limit=1: the inner one can't get the only
        # slot, so it polls. Assert it does by watching the sleep it polls on.
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        kind = "test-%d" % os.getpid()
        slept = []
        with cig._concurrency_slot(kind, 1, False, time.monotonic()):
            real_sleep = time.sleep

            def fake_sleep(s):          # let the inner attempt spin once, then
                slept.append(s)         # free the slot so it can proceed
                raise _Escape()

            with unittest.mock.patch.object(time, "sleep", fake_sleep):
                with self.assertRaises(_Escape):
                    with cig._concurrency_slot(kind, 1, False, time.monotonic()):
                        pass
            real_sleep(0)
        self.assertTrue(slept, "second holder took the slot instead of queueing")

    def test_the_slot_is_reusable_once_released(self):
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        kind = "test-reuse-%d" % os.getpid()
        for _ in range(3):              # a released slot must be retakeable
            with cig._concurrency_slot(kind, 1, False, time.monotonic()):
                pass

    def test_it_degrades_to_a_no_op_without_any_lock_primitive(self):
        # Neither fcntl nor msvcrt: run ungated rather than hang or crash.
        with unittest.mock.patch.object(cig, "_HAVE_FILE_LOCK", False):
            with unittest.mock.patch.object(time, "sleep",
                                            lambda s: self.fail("queued")):
                with cig._concurrency_slot("nolock", 1, False, time.monotonic()):
                    pass

    def test_the_windows_path_locks_one_byte_at_offset_zero(self):
        # msvcrt.locking() acts on the CURRENT offset, so _try_lock must pin it
        # to 0 — otherwise two runs lock different bytes and both "win".
        calls = []
        fake_msvcrt = unittest.mock.Mock(LK_NBLCK=1, LK_UNLCK=0)
        fake_msvcrt.locking.side_effect = lambda fd, mode, n: calls.append(
            (mode, n, handle.tell()))
        with tempfile.TemporaryDirectory() as td:
            handle = open(Path(td) / "slot.lock", "a+")
            handle.write("padding")     # a non-zero offset to be corrected
            with unittest.mock.patch.object(cig, "fcntl", None), \
                 unittest.mock.patch.object(cig, "msvcrt", fake_msvcrt):
                self.assertTrue(cig._try_lock(handle))
                cig._unlock(handle)
            handle.close()
        self.assertEqual(calls, [(1, 1, 0), (0, 1, 0)])

    def test_the_windows_path_reports_contention_as_false(self):
        fake_msvcrt = unittest.mock.Mock(LK_NBLCK=1, LK_UNLCK=0)
        fake_msvcrt.locking.side_effect = OSError(36, "already locked")
        with tempfile.TemporaryDirectory() as td:
            handle = open(Path(td) / "slot.lock", "a+")
            with unittest.mock.patch.object(cig, "fcntl", None), \
                 unittest.mock.patch.object(cig, "msvcrt", fake_msvcrt):
                self.assertFalse(cig._try_lock(handle))
                cig._unlock(handle)     # must swallow the error, not raise
            handle.close()

    def test_slot_files_are_opened_without_truncating(self):
        # "w" truncates, and truncating a byte-range-locked file is a sharing
        # violation on Windows — the open mode must stay "a+".
        src = inspect.getsource(cig._concurrency_slot)
        self.assertIn('.lock"), "a+")', src)
        self.assertNotIn('.lock"), "w")', src)


class ChatgptWebTurnLock(unittest.TestCase):
    """The cross-TOOL lock on the chatgpt.com page surface.

    ``_concurrency_slot`` only serializes this program against itself. It never
    saw chatgpt-use (leeguooooo/chatgpt-use), which held a lock of its own at a
    different path — so both projects could drive one account at once, which
    concatenates prompts in a shared composer and leaks a sibling tab's image
    into this run (issue #7). Both projects now agree on ~/.chatgpt-web.lock,
    held for a whole generation.
    """

    @contextmanager
    def _lock_at(self, path):
        with unittest.mock.patch.dict(
            os.environ, {"IMAGE_USE_WEB_LOCK": str(path)}
        ):
            yield

    def test_the_default_path_is_owned_by_neither_project(self):
        # Not ~/.chatgpt-imagegen/... and not ~/.chatgpt-use/... — a lock owned
        # by one tool is one the other has no reason to honour.
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMAGE_USE_WEB_LOCK", None)
            os.environ.pop("CHATGPT_IMAGEGEN_WEB_LOCK", None)
            path = cig._chatgpt_web_lock_path()
        self.assertEqual(path, Path.home() / ".chatgpt-web.lock")
        self.assertEqual(cig.CHATGPT_WEB_LOCK, ".chatgpt-web.lock")

    def test_a_second_holder_is_made_to_queue(self):
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        slept = []
        with tempfile.TemporaryDirectory() as td:
            with self._lock_at(Path(td) / "chatgpt-web.lock"):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    real_sleep = time.sleep

                    def fake_sleep(s):
                        slept.append(s)
                        raise _Escape()

                    with unittest.mock.patch.object(time, "sleep", fake_sleep):
                        with self.assertRaises(_Escape):
                            with cig._chatgpt_web_turn(False, time.monotonic()):
                                pass
                    real_sleep(0)
        self.assertTrue(slept, "a second turn ran concurrently instead of queueing")

    def test_the_lock_is_reusable_once_released(self):
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        with tempfile.TemporaryDirectory() as td:
            with self._lock_at(Path(td) / "chatgpt-web.lock"):
                for _ in range(3):
                    with cig._chatgpt_web_turn(False, time.monotonic()):
                        pass

    def test_it_tracks_whether_the_lock_is_really_held(self):
        # The session name depends on this: sharing one tab is only safe while
        # turns are genuinely serialized, so "held" must never be optimistic.
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        self.assertFalse(cig._chatgpt_web_lock_held)
        with tempfile.TemporaryDirectory() as td:
            with self._lock_at(Path(td) / "chatgpt-web.lock"):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    self.assertTrue(cig._chatgpt_web_lock_held)
        self.assertFalse(cig._chatgpt_web_lock_held,
                         "the flag outlived the lock")

    def test_an_unopenable_lock_file_warns_and_still_generates(self):
        # Refusing to generate because a lock file is unreachable would be a
        # worse failure than losing serialization. It must not claim the lock.
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no-such-dir" / "chatgpt-web.lock"
            err = io.StringIO()
            with self._lock_at(missing), redirect_stderr(err):
                with unittest.mock.patch.object(
                    time, "sleep", lambda s: self.fail("queued on a broken lock")
                ):
                    with cig._chatgpt_web_turn(False, time.monotonic()):
                        ran = True
        self.assertTrue(ran)
        self.assertIn("warning", err.getvalue().lower())
        self.assertFalse(cig._chatgpt_web_lock_held)

    def test_without_a_lock_primitive_it_warns_instead_of_pretending(self):
        err = io.StringIO()
        with unittest.mock.patch.object(cig, "_HAVE_FILE_LOCK", False), \
             redirect_stderr(err):
            with unittest.mock.patch.object(time, "sleep",
                                            lambda s: self.fail("queued")):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    pass
        self.assertIn("warning", err.getvalue().lower())
        self.assertFalse(cig._chatgpt_web_lock_held)

    def test_the_waiter_announces_itself_on_stderr(self):
        # A silent multi-minute stall is indistinguishable from a hang, and the
        # user reads stderr.
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with self._lock_at(Path(td) / "chatgpt-web.lock"):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    real_sleep = time.sleep

                    def fake_sleep(s):
                        raise _Escape()

                    with unittest.mock.patch.object(time, "sleep", fake_sleep), \
                         redirect_stderr(err):
                        with self.assertRaises(_Escape):
                            # progress=True — this is the announcing path
                            with cig._chatgpt_web_turn(True, time.monotonic()):
                                pass
                    real_sleep(0)
        self.assertIn("to finish its chatgpt turn", err.getvalue())

    def test_the_holder_writes_its_name_for_the_other_side(self):
        # chatgpt-use reads this to say WHO it is waiting for. Format agreed as
        # `<tool> <pid>`.
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chatgpt-web.lock"
            with self._lock_at(p):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    first = p.read_text(encoding="utf-8").splitlines()[0].split()
        self.assertEqual(first[0], "image-use")
        self.assertEqual(int(first[1]), os.getpid())

    def test_the_waiter_names_the_holder(self):
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with self._lock_at(Path(td) / "chatgpt-web.lock"):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    real_sleep = time.sleep

                    def fake_sleep(s):
                        raise _Escape()

                    with unittest.mock.patch.object(time, "sleep", fake_sleep), \
                         redirect_stderr(err):
                        with self.assertRaises(_Escape):
                            with cig._chatgpt_web_turn(True, time.monotonic()):
                                pass
                    real_sleep(0)
        self.assertIn("image-use", err.getvalue())
        self.assertIn(str(os.getpid()), err.getvalue())

    def test_an_unnamed_or_foreign_holder_is_described_not_crashed_on(self):
        # The warn-and-proceed paths leave the file EMPTY on purpose, and an old
        # holder may predate the convention — neither is an error.
        with tempfile.TemporaryDirectory() as td:
            for content, expected in [
                ("", "another chatgpt tool"),
                ("\n", "another chatgpt tool"),
                ("chatgpt-use 4321\n", "chatgpt-use (pid 4321)"),
                ("chatgpt-use\n", "chatgpt-use"),          # bare tool name
                ("garbage not-a-pid\n", "garbage"),        # first field only
            ]:
                p = Path(td) / "h.lock"
                p.write_text(content, encoding="utf-8")
                with open(p, "r+") as f:
                    self.assertEqual(cig._chatgpt_web_holder(f), expected,
                                     f"for {content!r}")

    def test_a_longer_previous_holder_cannot_corrupt_our_line(self):
        # We do not truncate (Windows sharing violation), so a longer remnant
        # must land on line 2 rather than trailing our own name.
        if not cig._HAVE_FILE_LOCK:
            self.skipTest("no file-locking primitive on this platform")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chatgpt-web.lock"
            p.write_text("some-very-long-previous-holder-name 999999999\n",
                         encoding="utf-8")
            with self._lock_at(p):
                with cig._chatgpt_web_turn(False, time.monotonic()):
                    with open(p, "r+") as f:
                        holder = cig._chatgpt_web_holder(f)
        self.assertEqual(holder, f"image-use (pid {os.getpid()})")

    def test_the_lock_file_is_never_truncated(self):
        # Truncating a byte-range-locked file is a sharing violation on Windows.
        src = inspect.getsource(cig._chatgpt_web_turn)
        self.assertNotIn("truncate()", src)

    def test_the_lock_file_is_opened_without_truncating_or_appending(self):
        # O_TRUNC would be a Windows sharing violation on a locked file; O_APPEND
        # would make seek(0) a no-op, so the holder name would be appended after
        # a previous holder's line and readers would report a stale holder.
        src = inspect.getsource(cig._chatgpt_web_turn)
        self.assertIn("os.O_RDWR | os.O_CREAT", src)
        self.assertNotIn("os.O_TRUNC", src)
        self.assertNotIn("os.O_APPEND", src)
        self.assertNotIn('open(path, "a+")', src)
        self.assertNotIn('open(path, "w")', src)

    def test_the_whole_generation_is_inside_the_lock(self):
        # A lock held per HTTP call is worse than none: it looks safe while the
        # composer is still shared. run_web must be called INSIDE the turn, and
        # the turn must wrap the inner per-process slot, not the reverse.
        src = inspect.getsource(cig._dispatch)
        turn = src.index("_chatgpt_web_turn")
        slot = src.index('_concurrency_slot("web"')
        self.assertLess(turn, slot, "the cross-tool lock must be the OUTER gate")
        self.assertIn("run_web(args, progress, start, refs=refs)", src)

    def test_the_session_name_is_shared_and_stable(self):
        # One tab for the machine: this tool and chatgpt-use both drive
        # `chatgpt-web`. A pid suffix would defeat reuse and pile up tabs.
        self.assertEqual(cig.CHATGPT_WEB_SESSION, "chatgpt-web")
        self.assertNotIn("{os.getpid()}", cig.CHATGPT_WEB_SESSION)
        src = inspect.getsource(cig.run_web)
        # The stable name applies whenever the lock really serializes us, even
        # if the user lifted the per-process concurrency cap.
        self.assertIn("_chatgpt_web_lock_held or _backend_limit", src)
        self.assertIn("session = CHATGPT_WEB_SESSION", src)

    def test_gemini_is_not_gated_by_the_chatgpt_lock(self):
        # gemini.google.com is a different surface on a different account —
        # serializing it behind chatgpt.com would slow it down for nothing.
        src = inspect.getsource(cig._dispatch)
        gemini = src.index('_concurrency_slot("gemini"')
        self.assertNotIn("_chatgpt_web_turn", src[:gemini])


class EnvNames(unittest.TestCase):
    """IMAGE_USE_* is the name; CHATGPT_IMAGEGEN_* keeps working as a fallback."""

    def _clean(self, **kv):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("IMAGE_USE_", "CHATGPT_IMAGEGEN_"))}
        env.update(kv)
        return unittest.mock.patch.dict(os.environ, env, clear=True)

    def test_new_name_is_read(self):
        with self._clean(IMAGE_USE_BACKEND="codex"):
            self.assertEqual(cig._env("IMAGE_USE_BACKEND", "auto"), "codex")

    def test_legacy_name_still_works(self):
        with self._clean(CHATGPT_IMAGEGEN_BACKEND="web"):
            self.assertEqual(cig._env("IMAGE_USE_BACKEND", "auto"), "web")

    def test_new_name_wins_when_both_set(self):
        with self._clean(IMAGE_USE_BACKEND="codex", CHATGPT_IMAGEGEN_BACKEND="web"):
            self.assertEqual(cig._env("IMAGE_USE_BACKEND", "auto"), "codex")

    def test_new_name_set_empty_still_wins(self):
        # `--project ""` means "plain chat"; an empty new var must not be
        # silently overridden by an old one left in the shell.
        with self._clean(IMAGE_USE_PROJECT="", CHATGPT_IMAGEGEN_PROJECT="old"):
            self.assertEqual(cig._env("IMAGE_USE_PROJECT", "imagegen"), "")

    def test_default_when_neither_set(self):
        with self._clean():
            self.assertEqual(cig._env("IMAGE_USE_BACKEND", "auto"), "auto")
            self.assertIsNone(cig._env("IMAGE_USE_QUALITY"))

    def test_helpers_use_the_fallback(self):
        with self._clean(CHATGPT_IMAGEGEN_KEEP_CONVERSATION="1",
                         CHATGPT_IMAGEGEN_COMPRESSION="80",
                         CHATGPT_IMAGEGEN_WEB_CONCURRENCY="3",
                         CHATGPT_IMAGEGEN_MACHINE_ID="box-1"):
            self.assertTrue(cig._env_bool("IMAGE_USE_KEEP_CONVERSATION"))
            self.assertEqual(cig._opt_int_env("IMAGE_USE_COMPRESSION"), "80")
            self.assertEqual(cig._backend_limit("web", 1), 3)
            self.assertEqual(cig._upload_machine_id(), "box-1")
        with self._clean(IMAGE_USE_WEB_CONCURRENCY="2",
                         CHATGPT_IMAGEGEN_WEB_CONCURRENCY="3"):
            self.assertEqual(cig._backend_limit("web", 1), 2)

    def test_no_code_reads_a_legacy_name_directly(self):
        # Every read goes through _env, so no option can lose its fallback.
        src = Path(os.path.join(os.path.dirname(__file__), "image-use")).read_text(
            encoding="utf-8")
        body = "\n".join(ln for ln in src.splitlines()
                         if not ln.startswith("# WHATSNEW["))
        self.assertIsNone(re.search(r'"CHATGPT_IMAGEGEN_[A-Z]', body))
        self.assertNotIn('os.environ.get("IMAGE_USE_', body)


class SkillName(unittest.TestCase):
    """`update` must name the skill the way the skills manager recorded it."""

    def _at(self, path):
        return unittest.mock.patch.object(cig, "__file__", path)

    def test_legacy_skill_dir_keeps_the_old_name(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "skills" / "chatgpt-imagegen"
            d.mkdir(parents=True)
            with self._at(str(d / "image-use")):
                self.assertEqual(cig._installed_skill_name(), "chatgpt-imagegen")

    def test_new_skill_dir_and_checkouts_use_the_new_name(self):
        with tempfile.TemporaryDirectory() as td:
            for rel in ("skills/image-use", "src/chatgpt-imagegen", "bin"):
                d = Path(td) / rel
                d.mkdir(parents=True)
                with self._at(str(d / "image-use")):
                    self.assertEqual(cig._installed_skill_name(), "image-use", rel)


class LegacyAlias(unittest.TestCase):
    """`chatgpt-imagegen` is a thin alias that runs `image-use`."""

    HERE = os.path.dirname(os.path.abspath(__file__))
    SHIM = os.path.join(HERE, "chatgpt-imagegen")

    def test_alias_runs_image_use(self):
        import subprocess
        out = subprocess.run([sys.executable, self.SHIM, "--version"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), f"image-use {cig.__version__}")

    def test_alias_passes_args_stdio_and_exit_code(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            shim = Path(td) / "chatgpt-imagegen"
            shutil.copy(self.SHIM, shim)
            (Path(td) / "image-use").write_text(
                "import sys\nprint(\"|\".join(sys.argv[1:]))\n"
                "sys.stderr.write(sys.stdin.read())\nsys.exit(7)\n",
                encoding="utf-8")
            out = subprocess.run([sys.executable, str(shim), "a cat", "-o", "x.png"],
                                 input="piped", capture_output=True, text=True,
                                 timeout=60)
        self.assertEqual(out.returncode, 7)
        self.assertEqual(out.stdout.strip(), "a cat|-o|x.png")
        self.assertEqual(out.stderr, "piped")

    def test_alias_advertises_an_upgrade_to_pre_rename_installs(self):
        # Pre-rename CLIs poll this file's URL for __version__/WHATSNEW; it has
        # to read as newer than the last pre-rename release or they never move.
        head = Path(self.SHIM).read_text(encoding="utf-8")[:8192]
        m = re.search(r'^__version__\s*=\s*"([\d.]+)"', head, re.MULTILINE)
        self.assertTrue(m)
        self.assertGreater(cig._version_tuple(m.group(1)), (0, 27, 0))
        self.assertLessEqual(cig._version_tuple(m.group(1)),
                             cig._version_tuple(cig.__version__))
        self.assertIn(m.group(1), cig._parse_whatsnew(head))


class _Escape(Exception):
    """Sentinel used to break out of the slot poll loop under test."""


class SkillManifestTests(unittest.TestCase):
    def _frontmatter(self):
        text = (Path(__file__).parent / "SKILL.md").read_text(encoding="utf-8")
        return text.split("---", 2)[1]

    def test_description_within_pi_limit(self):
        m = re.search(r"^description: >-\n((?:  .*\n)+)", self._frontmatter(), re.M)
        self.assertIsNotNone(m)
        desc = " ".join(line.strip() for line in m.group(1).splitlines())
        self.assertLessEqual(len(desc), 1024)

    def test_version_matches_cli(self):
        m = re.search(r'^version: "([^"]+)"', self._frontmatter(), re.M)
        self.assertEqual(m.group(1), cig.__version__)


# ---------- chatgpt.com DOM fixtures (issue #41) ----------
#
# The web backend's page-side JS is only as good as its selectors, and ChatGPT
# rolls redesigns out per account. These tests hold the selector constants to
# DOM snapshots of BOTH layouts in tests/fixtures/, using a tiny stdlib CSS
# matcher (descendant combinator, tag/#id/.class, [attr], = *= ^= with the `i`
# flag, :not() and :has()) — enough for the selectors the CLI uses, no deps.

_FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "source", "track", "wbr"}


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    def iter(self):
        for c in self.children:
            yield c
            yield from c.iter()

    def closest(self, sel):
        n = self
        while n is not None and n.tag != "#root":
            if _matches(n, sel):
                return n
            n = n.parent
        return None


class _TreeBuilder(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent


def _parse(markup):
    b = _TreeBuilder()
    b.feed(markup)
    return b.root


def _split_top(s, sep):
    """Split on `sep` outside (), [] and quotes."""
    out, depth, quote, buf = [], 0, None, ""
    for ch in s:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and (ch == sep or (sep == " " and ch.isspace())):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


_ATTR_RE = re.compile(r'\[\s*([\w-]+)\s*(?:([*^]?=)\s*(?:"([^"]*)"|\'([^\']*)\'|([^\]\s]+))\s*(i)?\s*)?\]')


def _match_compound(node, comp):
    if node.tag == "#root":
        return False
    i = 0
    m = re.match(r"[a-zA-Z][\w-]*", comp)
    if m:
        if node.tag != m.group(0).lower():
            return False
        i = m.end()
    while i < len(comp):
        ch = comp[i]
        if ch in "#.":
            m = re.match(r"[\w-]+", comp[i + 1:])
            name = m.group(0)
            if ch == "#" and node.attrs.get("id") != name:
                return False
            if ch == "." and name not in node.attrs.get("class", "").split():
                return False
            i += 1 + m.end()
        elif ch == "[":
            m = _ATTR_RE.match(comp, i)
            name, op = m.group(1), m.group(2)
            val = next((g for g in m.group(3, 4, 5) if g is not None), None)
            have = node.attrs.get(name)
            if have is None:
                return False
            if op:
                a, b = (have.lower(), val.lower()) if m.group(6) else (have, val)
                if (op == "=" and a != b) or (op == "*=" and b not in a) \
                        or (op == "^=" and not a.startswith(b)):
                    return False
            i = m.end()
        elif comp.startswith(":not(", i) or comp.startswith(":has(", i):
            depth, j = 0, i + 4
            while True:
                depth += comp[j] == "("
                depth -= comp[j] == ")"
                if depth == 0:
                    break
                j += 1
            inner = comp[i + 5:j]
            if comp.startswith(":not(", i) and _matches(node, inner):
                return False
            if comp.startswith(":has(", i) and not any(_matches(d, inner) for d in node.iter()):
                return False
            i = j + 1
        else:
            raise ValueError(f"unsupported selector syntax at {comp[i:]!r}")
    return True


def _match_complex(node, parts):
    if not _match_compound(node, parts[-1]):
        return False
    rest, anc = parts[:-1], node.parent
    while rest and anc is not None:
        if _match_compound(anc, rest[-1]) and _match_complex(anc, rest):
            return True
        anc = anc.parent
    return not rest


def _matches(node, selector):
    return any(_match_complex(node, _split_top(c, " ")) for c in _split_top(selector, ","))


def _select(root, selector):
    return [n for n in root.iter() if _matches(n, selector)]


def _fixture(name):
    return _parse((_FIXTURES / name).read_text(encoding="utf-8"))


class SelectorEngineSelfTest(unittest.TestCase):
    """The mini matcher must agree with CSS on the constructs the CLI uses."""

    def test_constructs(self):
        root = _parse('<form><div class="ProseMirror x" contenteditable="true"></div>'
                      '<button type="submit" aria-label="Start VOICE"></button>'
                      '<button type="submit" aria-label="Send"></button></form>'
                      '<form><button type="submit" aria-label="Go"></button></form>')
        self.assertEqual(len(_select(root, 'form div.ProseMirror[contenteditable="true"]')), 1)
        self.assertEqual(len(_select(root, 'button[aria-label*="voice" i]')), 1)
        self.assertEqual(len(_select(root, 'button[aria-label*="voice"]')), 0)
        self.assertEqual(
            [n.attrs["aria-label"] for n in _select(
                root, 'form:has(div.ProseMirror) button[type="submit"]:not([aria-label*="voice" i])')],
            ["Send"])


class ChatGPTDomSelectors(unittest.TestCase):
    """Hold the web backend's selectors to both chatgpt.com layouts (issue #41)."""

    LAYOUTS = ("chatgpt-thread-legacy.html", "chatgpt-thread-redesign.html")

    def _user_turns(self, root):
        """Mirror of the distinct-turn count in _JS_WEB_COMPOSER_STATE."""
        seen = set()
        for m in _select(root, cig.WEB_USER_MSG_SEL):
            if m.parent is not None and m.parent.closest(cig.WEB_USER_MSG_SEL):
                continue
            t = m.closest(cig.WEB_TURN_SEL)
            seen.add(("k", t.attrs["data-turn-key"]) if t else id(m))
        return len(seen)

    def test_composer_and_send_resolve_in_both_layouts(self):
        for name in self.LAYOUTS:
            with self.subTest(layout=name):
                root = _fixture(name)
                composers = _select(root, cig.WEB_COMPOSER_SEL)
                self.assertEqual(len(composers), 1)
                form = composers[0].closest("form")
                self.assertIsNotNone(form)
                sends = _select(form, cig.WEB_SEND_SEL)
                self.assertEqual(len(sends), 1)
                self.assertEqual(sends[0].attrs.get("type"), "submit")
                # The page-wide click selector hits that same button, only it.
                self.assertEqual(_select(root, cig.WEB_SEND_CLICK_SEL), sends)

    def test_redesign_has_none_of_the_legacy_markers(self):
        """The reason #41 broke: every legacy hook is gone from the redesign."""
        root = _fixture("chatgpt-thread-redesign.html")
        for sel in ("#prompt-textarea", 'button[data-testid="send-button"]',
                    "[data-message-author-role]"):
            self.assertEqual(_select(root, sel), [], sel)

    def test_one_user_turn_even_though_the_redesign_renders_the_thread_twice(self):
        for name in self.LAYOUTS:
            with self.subTest(layout=name):
                self.assertEqual(self._user_turns(_fixture(name)), 1)
        root = _fixture("chatgpt-thread-redesign.html")
        self.assertEqual(len(_select(root, "[data-user-message-bubble]")), 2)

    def test_nested_user_markers_count_once(self):
        root = _parse('<div data-message-author-role="user">'
                      '<div data-user-message-bubble="true">hi</div></div>')
        self.assertEqual(self._user_turns(root), 1)

    def test_generated_image_is_found_and_user_images_are_not(self):
        re_src = re.compile(cig.WEB_IMG_SRC_RE)
        for name in self.LAYOUTS:
            with self.subTest(layout=name):
                root = _fixture(name)
                user = {i.attrs["src"] for i in _select(
                    root, ", ".join(s.strip() + " img" for s in cig.WEB_USER_MSG_SEL.split(",")))}
                fresh = [i.attrs["src"] for i in _select(root, "main img")
                         if (re_src.search(i.attrs.get("src", "")) or _matches(i, cig.WEB_GEN_IMG_SEL))
                         and i.attrs["src"] not in user]
                self.assertTrue(fresh)
                if name.endswith("redesign.html"):
                    # blob: src — the regex alone would never have matched it.
                    self.assertTrue(all(s.startswith("blob:https://chatgpt.com/") for s in fresh))
                    self.assertFalse(any(re_src.search(s) for s in fresh))
                else:
                    self.assertTrue(all("file_00000000000000000000000000000002" in s for s in fresh))

    def test_redesign_reply_detected_only_once_it_has_content(self):
        root = _fixture("chatgpt-thread-redesign.html")
        turn = _select(root, "main " + cig.WEB_TURN_SEL)[-1]
        self.assertTrue(_select(turn, '[data-conversation-role="assistant"]'))
        self.assertTrue(_select(turn, cig.WEB_ASSISTANT_BODY_SEL))
        pending = _parse('<main><div data-turn-key="t"><div data-user-message-bubble="true">x</div>'
                         '<h4 data-conversation-role="assistant">ChatGPT</h4></div></main>')
        self.assertEqual(_select(pending, cig.WEB_ASSISTANT_BODY_SEL), [])

    def test_send_never_matches_voice_or_dictation(self):
        root = _parse('<form><div class="ProseMirror" contenteditable="true"></div>'
                      '<button type="submit" aria-label="Start Voice"></button>'
                      '<button type="submit" aria-label="开始语音"></button>'
                      '<button type="submit" aria-label="Start dictation"></button></form>')
        self.assertEqual(_select(root, cig.WEB_SEND_SEL), [])
        self.assertEqual(_select(root, cig.WEB_SEND_CLICK_SEL), [])

    def test_stop_button_matches_localized_labels(self):
        for label in ("Stop streaming", "停止生成", "中止"):
            root = _parse(f'<button aria-label="{label}"></button>')
            self.assertEqual(len(_select(root, cig.WEB_STOP_SEL)), 1, label)

    def test_page_js_templates_are_fully_formatted(self):
        """Every selector bundle lands in the JS; no stray %s is left behind."""
        filled = [
            cig._JS_STATE % (json.dumps([]), json.dumps(cig.WEB_IMG_SRC_RE),
                             cig._js_state_selectors()),
            cig._JS_BASELINE % (json.dumps(cig.WEB_IMG_SRC_RE), json.dumps(cig.WEB_GEN_IMG_SEL)),
            cig._JS_WEB_COMPOSER_STATE % cig._js_composer_selectors(),
            cig._JS_COMPOSER % json.dumps(cig.WEB_COMPOSER_SEL),
            cig._JS_FETCH % json.dumps("blob:https://chatgpt.com/x"),
        ]
        for js in filled:
            self.assertNotIn("%s", js)
        self.assertIn(json.dumps(cig.WEB_GEN_IMG_SEL), filled[0])
        self.assertIn(json.dumps(cig.WEB_SEND_SEL, ensure_ascii=False), filled[2])

    def test_blob_src_is_fetched_in_page_with_a_decoded_img_fallback(self):
        """A blob: URL only resolves inside the page, so the download runs there
        and can fall back to re-encoding the already-decoded <img>."""
        js = cig._JS_FETCH % json.dumps("blob:https://chatgpt.com/x")
        self.assertIn("startsWith('blob:')", js)
        self.assertIn("toBlob", js)
        self.assertIn("credentials: 'include'", js)


class WebGenerateBlobDownload(unittest.TestCase):
    """End-to-end over simulated Chrome: a blob: result is detected and saved."""

    def test_blob_result_is_downloaded_through_the_page(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        blob = "blob:https://chatgpt.com/00000000-0000-0000-0000-000000000001"
        evals = []

        def fake_eval(ab, js, session, timeout):
            evals.append(js)
            if js.startswith(cig._JS_BASELINE[:40]):
                return []
            if "credentials" in js and "fetch(src" in js:
                return {"ok": True, "type": "image/png", "b64": base64.b64encode(png).decode()}
            return {"stop": False, "last": blob, "assistant": True, "limited": False, "atext": ""}

        args = argparse.Namespace(timeout=60)
        with unittest.mock.patch.object(cig, "_ab_eval", side_effect=fake_eval), \
             unittest.mock.patch.object(cig, "_submit_web_prompt"), \
             unittest.mock.patch.object(cig.time, "sleep"):
            data, meta = cig._generate_in_browser(
                "ab", "s", "prompt", args, time.monotonic() + 60, lambda: 60, lambda _: None)
        self.assertEqual(data, png)
        self.assertEqual(meta["content_type"], "image/png")
        self.assertIn(json.dumps(blob), evals[-1])


class _FakeClock:
    """One monotonic clock driving monotonic(), sleep() and remaining().

    No wall-clock time passes and no browser/provider is touched: the poll loop
    in ``_generate_in_browser`` advances only by the sleeps it requests, so a
    test can assert exactly *when* (in fake seconds) a branch fired.
    """

    def __init__(self, deadline: float) -> None:
        """Start the fake run at t=0 with ``deadline`` fake seconds of budget."""
        self.now = 0.0
        self.deadline = deadline

    def monotonic(self) -> float:
        """Stand in for ``time.monotonic``: fake seconds since the run started."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake clock; the poll loop's only source of time."""
        self.now += seconds

    def remaining(self) -> float:
        """Mirror production's ``max(2.0, deadline - now)`` read-timeout budget."""
        return max(2.0, self.deadline - self.now)


class WebGenerateWait(unittest.TestCase):
    """The poll loop in ``_generate_in_browser`` after a prompt is submitted.

    Assistant text without an image is not a reliable terminal signal. These
    pin the original deadline, the interruption reset, and the
    post-submit error routing that keeps auto mode off the Codex bucket.
    """

    def setUp(self):
        """Fresh call recorders; success tests set ``fetch_result`` themselves."""
        self.submitted = []
        self.fetched = []
        # Success tests provide the page's download response.
        self.fetch_result = None
        self.conv_url = "https://chatgpt.com/c/6aa20b3f-c6fc-83e8-9007-e1e3d3a8eb3d"

    def _run(self, states, *, deadline):
        """Drive ``_generate_in_browser`` over ``states`` with a fake clock.

        ``states`` is a list of return values for the ``_JS_STATE`` poll; an
        entry may be an exception raised by ``_ab_eval``. The baseline and
        ``_JS_FETCH`` evals are handled separately so neither consumes a state poll.

        The fake clock starts at zero and ``args.timeout`` is derived from the
        deadline, mirroring production where the two describe the same budget
        (``deadline = monotonic() + args.timeout``).
        """
        clock = _FakeClock(deadline)
        self.clock = clock
        state_iter = iter(states)

        def fake_eval(ab, js, session, timeout):
            """Route each page eval: baseline, download, or the next poll state.

            This is the mocked browser boundary — ``_ab_eval`` never runs JS, so
            the test feeds the exact state dicts (or raised errors) the poll loop
            would have observed, and records any download attempt in ``fetched``.
            """
            if js.startswith(cig._JS_BASELINE.split("%s")[0]):
                return []  # nothing on the page before we submitted
            if js.startswith(cig._JS_FETCH.split("%s")[0]):
                self.fetched.append(js)
                return self.fetch_result
            if js == cig._JS_CONV_URL:
                return self.conv_url  # the tab's /c/<id>, for the recover hint
            self.assertTrue(js.startswith(cig._JS_STATE.split("%s")[0]))
            try:
                item = next(state_iter)
            except StopIteration:
                item = states[-1]
            if isinstance(item, BaseException):
                raise item
            return item

        def fake_submit(*a, **kw):
            """Stand in for the one-shot composer submission; records the call."""
            self.submitted.append((a, kw))

        args = argparse.Namespace(timeout=deadline)
        with unittest.mock.patch.object(cig, "_ab_eval", side_effect=fake_eval), \
             unittest.mock.patch.object(cig, "_submit_web_prompt", side_effect=fake_submit), \
             unittest.mock.patch.object(cig.time, "sleep", clock.sleep), \
             unittest.mock.patch.object(cig.time, "monotonic", clock.monotonic):
            result = cig._generate_in_browser(
                "ab", "s", "prompt", args, clock.deadline, clock.remaining,
                lambda _: None)
        return clock, result

    def test_ten_idle_polls_then_two_equal_fresh_blobs_succeed(self):
        """Ten no-image polls must not fail early; two equal fresh reads settle it."""
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        blob = "blob:https://chatgpt.com/00000000-0000-0000-0000-0000000000aa"
        self.fetch_result = {
            "ok": True, "type": "image/png",
            "b64": base64.b64encode(png).decode(),
        }
        idle = {"stop": False, "last": None, "assistant": True,
                "limited": False, "atext": "编辑"}
        fresh = dict(idle, last=blob)
        # Ten consecutive no-image polls would trip the old IDLE_LIMIT=8 error
        # long before the two fresh reads that actually settle the image.
        clock, (data, meta) = self._run([idle] * 10 + [fresh, fresh], deadline=300.0)

        self.assertEqual(data, png)
        self.assertEqual(meta, {"content_type": "image/png", "source": "web"})
        # 12 sleeps (ten idle + the two reads that accept the src) at 2s each.
        self.assertEqual(clock.now, 24.0)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(len(self.fetched), 1)
        self.assertIn(json.dumps(blob), self.fetched[0])

    def _assert_timeout(self, atext, label):
        """Assert an image-less run errors on the ORIGINAL deadline, never earlier.

        The message must be a plain GatewayError naming the budget, the
        already-submitted reminder and the conversation, with no download.
        """
        with self.subTest(assistant_text=label):
            # Isolate call counts for each spelling.
            self.submitted, self.fetched = [], []
            idle = {"stop": False, "last": None, "assistant": True,
                    "limited": False, "atext": atext}
            # An integer 300s budget — exactly what argparse hands production —
            # and 2s polls ⇒ 150 sleeps, landing exactly on 300.0.
            with self.assertRaises(cig.GatewayError) as ctx:
                self._run([idle] * 200, deadline=300)
            # The timeout must fire on the ORIGINAL deadline — not an idle
            # early-error — and report that budget verbatim.
            self.assertEqual(self.clock.now, 300.0)
            self.assertNotIsInstance(ctx.exception, cig.WebUnavailable)
            msg = str(ctx.exception)
            self.assertIn("timed out after 300s", msg)
            self.assertRegex(msg, r"(?i)already submitted")
            self.assertIn("conversation", msg.lower())
            self.assertEqual(len(self.submitted), 1)
            self.assertEqual(self.fetched, [])

    def test_always_no_image_times_out_at_the_original_deadline(self):
        """A page that never yields an image errors only when the budget runs out."""
        self._assert_timeout("", "empty")

    def test_ordinary_quota_wording_is_not_broadly_detected(self):
        """A quoted/incidental quota mention must not be read as a terminal reply.

        The narrow terminal contract covers a leading English quota prefix, not
        any body text that merely *mentions* a limit. ``limited=False`` models
        a page without a recognized rate-limit dialog.
        """
        self._assert_timeout(
            'The help page says, "You\'ve reached your image generation '
            'limit", so try again later.',
            "quoted quota mention")

    def test_first_poll_rate_limit_raises_plain_gateway_error(self):
        """The existing limited=True dialog still fails on the first poll."""
        limited = {"stop": False, "last": None, "assistant": True,
                   "limited": True, "atext": ""}
        with self.assertRaises(cig.GatewayError) as ctx:
            self._run([limited], deadline=300.0)
        # Immediately after the first 2s sleep — no second poll.
        self.assertEqual(self.clock.now, 2.0)
        self.assertNotIsInstance(ctx.exception, cig.WebUnavailable)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(self.fetched, [])

    def _assert_terminal_body(self, body, label, *, needle, invented_reset=False):
        """A recognized terminal reply must fail on the first poll at 2s.

        Plain GatewayError (never WebUnavailable), exactly one submission and no
        download, ChatGPT's own (bounded) wording echoed back, and the
        already-submitted / check-conversation reminder.
        """
        state = {"stop": False, "last": None, "assistant": True,
                 "limited": False, "atext": body}
        with self.subTest(assistant_text=label):
            # Several bodies share this method; isolate each subtest's calls.
            self.submitted, self.fetched = [], []
            with self.assertRaises(cig.GatewayError) as ctx:
                self._run([state], deadline=300)
            # Immediately after the first 2s sleep — no second poll.
            self.assertEqual(self.clock.now, 2.0)
            self.assertNotIsInstance(ctx.exception, cig.WebUnavailable)
            msg = str(ctx.exception)
            self.assertIn(needle, msg)
            self.assertRegex(msg, r"(?i)already submitted")
            self.assertIn("conversation", msg.lower())
            if invented_reset:
                # The body names no reset, so the error must not invent one.
                self.assertNotIn("reset", msg.lower())
            self.assertEqual(len(self.submitted), 1)
            self.assertEqual(self.fetched, [])

    def test_first_poll_quota_body_raises_plain_gateway_error(self):
        """A narrow English quota prefix in the reply is terminal on poll one.

        Straight and curly apostrophes and "image creation" wording are covered;
        ChatGPT's own (bounded) words ride along and no reset timing is invented.
        """
        for body, label, needle in (
            ("You've hit the image generation limit. Upgrade to Plus for more.",
             "hit-limit straight apostrophe", "hit the image generation limit"),
            ("You’ve reached your image generation limit for now.",
             "reached-limit curly apostrophe", "reached your image generation limit"),
            ("You’ve hit your image creation limit.",
             "image creation wording", "image creation limit"),
        ):
            self._assert_terminal_body(
                body, label, needle=needle, invented_reset=True)

    def test_first_poll_refusal_body_raises_plain_gateway_error(self):
        """Explicit refusals are terminal on the first poll, same routing."""
        for body, label, needle in (
            ("I can't generate that image.", "cannot-generate",
             "can't generate that image"),
            ("I'm sorry, but I cannot create this image because it may violate "
             "our content policy.", "sorry-cannot-create",
             "I cannot create this image"),
            ("I can't help with that request.", "cannot-help",
             "can't help with that request"),
        ):
            self._assert_terminal_body(body, label, needle=needle)

    def test_ambiguous_replies_stay_non_terminal(self):
        """Only a narrow leading prefix is terminal — these bodies are not.

        "try again later" alone, a body that stops short ("...yet"), and text
        carrying no leading prefix must all fall through to the original-deadline
        timeout rather than erroring early.
        """
        self._assert_timeout("Please try again later.", "try-again-later only")
        self._assert_timeout("I can't generate that image yet; give me a moment.",
                             "refusal prefix with a trailing clause")
        self._assert_timeout("I have created the image you asked for.",
                             "ordinary prose mentioning creation")

    def test_terminal_wording_on_a_non_assistant_turn_is_ignored(self):
        """Terminal-looking text only counts on the current assistant turn.

        ``assistant=False`` means nothing from the model has rendered yet, so the
        body must not be read as a terminal reply — the run falls through to the
        unchanged original-deadline timeout.
        """
        idle = {"stop": False, "last": None, "assistant": False,
                "limited": False,
                "atext": "You've hit the image generation limit."}
        with self.assertRaises(cig.GatewayError) as ctx:
            self._run([idle] * 200, deadline=300)
        self.assertEqual(self.clock.now, 300.0)
        self.assertNotIsInstance(ctx.exception, cig.WebUnavailable)
        self.assertIn("timed out after 300s", str(ctx.exception))
        # The conversation is named with the command that collects its image
        # later, so a slow image costs a download rather than a second prompt.
        self.assertIn("image-use recover " + self.conv_url, str(ctx.exception))

    def test_streaming_and_fresh_image_take_precedence_over_body_text(self):
        """A body heuristic never fires while the turn is streaming or has an image.

        First the turn is still streaming (``stop=True``) with terminal-looking
        text; then a fresh image is present (``last`` set) with the same text.
        Neither may be terminal — the run succeeds once the blob is confirmed twice.
        """
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        self.fetch_result = {
            "ok": True, "type": "image/png",
            "b64": base64.b64encode(png).decode(),
        }
        blob = "blob:https://chatgpt.com/00000000-0000-0000-0000-0000000000bb"
        terminal_text = "You've hit the image generation limit."
        streaming = {"stop": True, "last": None, "assistant": True,
                     "limited": False, "atext": terminal_text}
        fresh = {"stop": False, "last": blob, "assistant": True,
                 "limited": False, "atext": terminal_text}
        clock, (data, meta) = self._run([streaming, fresh, fresh], deadline=300.0)
        self.assertEqual(data, png)
        self.assertEqual(meta, {"content_type": "image/png", "source": "web"})
        # streaming + two fresh reads = three 2s sleeps; never a terminal error.
        self.assertEqual(clock.now, 6.0)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(len(self.fetched), 1)
        self.assertIn(json.dumps(blob), self.fetched[0])

    def test_delayed_success_beyond_90_seconds_is_not_cut_short(self):
        """A slow generation settling past 90s still needs its two fresh reads.

        There is no fixed idle cutoff: 46 no-image polls (92s) must not error,
        and the run succeeds only when the same new blob is seen twice in a row.
        """
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        self.fetch_result = {
            "ok": True, "type": "image/png",
            "b64": base64.b64encode(png).decode(),
        }
        blob = "blob:https://chatgpt.com/00000000-0000-0000-0000-0000000000cc"
        idle = {"stop": False, "last": None, "assistant": True,
                "limited": False, "atext": ""}
        fresh = dict(idle, last=blob)
        clock, (data, meta) = self._run([idle] * 46 + [fresh, fresh], deadline=300.0)
        self.assertEqual(data, png)
        self.assertEqual(meta, {"content_type": "image/png", "source": "web"})
        # 46 idle + 2 accepting polls = 48 sleeps, i.e. 96s — beyond any 90s cutoff.
        self.assertEqual(clock.now, 96.0)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(len(self.fetched), 1)

    def test_timeout_reports_last_nonempty_assistant_text(self):
        """The timeout diagnostic carries the last nonempty assistant text.

        That text must survive empty polls, a malformed state, a failed page read
        and a non-string ``atext`` (which must not crash) — and reappear in the
        final message so the user can see what the model actually said.
        """
        kept = "Here is a description of the image I made earlier."
        states = [
            {"stop": False, "last": None, "assistant": True,
             "limited": False, "atext": kept},
            {"stop": False, "last": None, "assistant": True,
             "limited": False, "atext": ""},
            "malformed-state",
            cig.GatewayError("transient eval failure"),
            {"stop": False, "last": None, "assistant": True,
             "limited": False, "atext": None},
            {"stop": False, "last": None, "assistant": True,
             "limited": False, "atext": 12345},
        ] + [{"stop": False, "last": None, "assistant": True,
              "limited": False, "atext": ""}] * 200
        with self.assertRaises(cig.GatewayError) as ctx:
            self._run(states, deadline=300)
        self.assertEqual(self.clock.now, 300.0)
        self.assertNotIsInstance(ctx.exception, cig.WebUnavailable)
        msg = str(ctx.exception)
        self.assertIn("timed out after 300s", msg)
        self.assertIn(kept, msg)

    def test_timeout_assistant_text_is_bounded(self):
        """The echoed assistant body in the timeout diagnostic is capped at 240 chars."""
        long_text = "x" * 300
        states = [{"stop": False, "last": None, "assistant": True,
                   "limited": False, "atext": long_text}] \
            + [{"stop": False, "last": None, "assistant": True,
                "limited": False, "atext": ""}] * 200
        with self.assertRaises(cig.GatewayError) as ctx:
            self._run(states, deadline=300)
        msg = str(ctx.exception)
        self.assertIn("x" * 240, msg)
        self.assertNotIn("x" * 241, msg)

    def test_stop_interruption_resets_then_accepts_new_src(self):
        """A still-streaming (stop=True) read breaks confirmation of the old src."""
        self._check_interruption({"stop": True, "last": None, "assistant": True,
                                  "limited": False, "atext": ""}, "stop=True")

    def test_no_image_interruption_resets_then_accepts_new_src(self):
        """A no-image read breaks confirmation, so a later src is re-read twice."""
        self._check_interruption({"stop": False, "last": None, "assistant": True,
                                  "limited": False, "atext": ""}, "no-image")

    def test_gateway_error_interruption_resets_then_accepts_new_src(self):
        """A failed page read counts as an interruption, not a confirmed src."""
        self._check_interruption(cig.GatewayError("transient eval failure"),
                                 "GatewayError")

    def test_invalid_non_dict_state_interruption_resets_then_accepts(self):
        """A malformed state read counts as an interruption, not a confirmed src."""
        self._check_interruption("not-a-dict", "invalid-state")

    def _check_interruption(self, interruption, label):
        """An A -> interruption -> A -> B -> B run must accept B, never A."""
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        self.fetch_result = {
            "ok": True, "type": "image/png",
            "b64": base64.b64encode(png).decode(),
        }
        a = "blob:https://chatgpt.com/aaaaaaaa"
        b = "blob:https://chatgpt.com/bbbbbbbb"
        base = {"stop": False, "assistant": True, "limited": False, "atext": ""}
        states = [dict(base, last=a), interruption, dict(base, last=a),
                  dict(base, last=b), dict(base, last=b)]
        with self.subTest(interruption=label):
            clock, (data, meta) = self._run(states, deadline=300.0)
            self.assertEqual(clock.now, 10.0)
            self.assertEqual(data, png)
            self.assertEqual(meta, {"content_type": "image/png", "source": "web"})
            self.assertEqual(len(self.submitted), 1)
            self.assertEqual(len(self.fetched), 1)
            self.assertIn(json.dumps(b), self.fetched[0])
            self.assertNotIn(json.dumps(a), self.fetched[0])

    def test_auto_does_not_fall_back_to_codex_after_submit(self):
        """auto + a real post-submit error must exit, never run codex.

        End-to-end over the real ``_generate_in_browser``: ``run_web`` is the only
        provider seam mocked, and it invokes ``self._run`` over the chosen states
        so the *actual* GatewayError (timeout, direct quota body, or direct
        refusal body) propagates into the real ``_dispatch``. Locks/slots and the
        codex-token probe are stubbed so no disk/network/account is touched.

        Even with a Codex token "available", the prompt was already submitted, so
        falling back would silently double-spend the metered bucket auto mode
        exists to protect.
        """
        idle = {"stop": False, "last": None, "assistant": True,
                "limited": False, "atext": ""}
        quota_body = "You've hit the image generation limit. Upgrade to Plus for more."
        refusal_body = ("I'm sorry, but I cannot create this image because it may "
                        "violate our content policy.")

        def _noop_lock(*a, **k):
            """Replace the cross-tool/slot lock with an uncontended stub."""
            @contextmanager
            def _cm():
                """Yield at once, standing in for a lock that is free."""
                yield
            return _cm()

        # (label, poll states, expected fake failure second, message needle)
        cases = (
            ("timeout", [idle] * 200, 300.0, "timed out after 300s"),
            ("direct quota body",
             [dict(idle, atext=quota_body)], 2.0, quota_body),
            ("direct refusal body",
             [dict(idle, atext=refusal_body)], 2.0, refusal_body),
        )
        for label, states, expected_at, needle in cases:
            with self.subTest(after_submit=label):
                # Fresh per-subtest call record; `_run` installs the fake clock.
                self.submitted, self.fetched = [], []
                self.fetch_result = None
                args = argparse.Namespace(
                    backend="auto", resolved_refs=None, ref=None,
                    quality=None, background=None, moderation=None)
                with unittest.mock.patch.object(cig, "_codex_only_options", return_value=[]), \
                     unittest.mock.patch.object(cig, "_chatgpt_web_turn", _noop_lock), \
                     unittest.mock.patch.object(cig, "_concurrency_slot", _noop_lock), \
                     unittest.mock.patch.object(cig, "_backend_limit", return_value=1), \
                     unittest.mock.patch.object(cig, "_codex_token_present", return_value=True), \
                     unittest.mock.patch.object(
                         cig, "run_web",
                         side_effect=lambda *a, **k: self._run(states, deadline=300)), \
                     unittest.mock.patch.object(cig, "run_codex") as run_codex:
                    with self.assertRaises(SystemExit) as ctx:
                        cig._dispatch(args, False, 300.0, 0.0)
                run_codex.assert_not_called()
                msg = str(ctx.exception)
                # The real failure reached _dispatch and its original text survived.
                self.assertIn(needle, msg)
                self.assertRegex(msg, r"(?i)already submitted")
                self.assertIn("conversation", msg.lower())
                self.assertIn("did not fall back to codex", msg)
                # One prompt went in, no download, and the poll stopped exactly at
                # the real deadline (300s timeout, or the first 2s terminal poll).
                self.assertEqual(self.clock.now, expected_at)
                self.assertEqual(len(self.submitted), 1)
                self.assertEqual(self.fetched, [])



class WebRecoverHint(unittest.TestCase):
    """Post-submit failures name the conversation only when the tab has one."""

    def test_no_conversation_url_means_no_hint(self):
        """A tab still on / (nothing sent) or an unreadable tab adds nothing."""
        for value in (None, cig.GatewayError("eval failed")):
            with self.subTest(value=value), unittest.mock.patch.object(
                    cig, "_ab_eval",
                    side_effect=value if isinstance(value, Exception) else None,
                    return_value=value):
                self.assertEqual(cig._recover_hint("ab", "s"), "")


class SiteCall(unittest.TestCase):
    """``_site_call`` reads chrome-use's --json envelope, success or not."""

    def _proc(self, stdout, rc=0, stderr=""):
        """A finished chrome-use process with the given output."""
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

    def test_returns_the_adapter_result(self):
        """The adapter's value lives at data.result, after any stray lines."""
        env = {"success": True, "data": {"result": {"count": 1}}}
        with unittest.mock.patch.object(
                cig.subprocess, "run",
                return_value=self._proc("site adapters for chatgpt.com\n" + json.dumps(env))) as run:
            self.assertEqual(cig._site_call("ab", "chatgpt/images", ["x"], "chatgpt-web", 60), {"count": 1})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["ab", "site", "chatgpt/images", "x"])
        self.assertIn("--json", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "chatgpt-web")

    def test_adapter_error_is_carried_into_the_exception(self):
        """exit 1 with {success:false, error} keeps the adapter's own words."""
        env = {"success": False, "error": "chatgpt.com rate-limited this account (HTTP 429) — do not poll"}
        with unittest.mock.patch.object(cig.subprocess, "run", return_value=self._proc(json.dumps(env), rc=1)):
            with self.assertRaises(cig.GatewayError) as ctx:
                cig._site_call("ab", "chatgpt/images", ["x"], "s", 60)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_missing_adapter_points_at_site_update(self):
        """An adapter pack that predates chatgpt/images says how to get it."""
        with unittest.mock.patch.object(
                cig.subprocess, "run",
                return_value=self._proc("", rc=1, stderr="error: unknown adapter chatgpt/images")):
            with self.assertRaises(cig.GatewayError) as ctx:
                cig._site_call("ab", "chatgpt/images", ["x"], "s", 60)
        self.assertIn("chrome-use site update", str(ctx.exception))


class RecoverCommand(unittest.TestCase):
    """``image-use recover``: collect an existing image, never prompt again."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
    URL = "https://chatgpt.com/c/6aa20b3f-c6fc-83e8-9007-e1e3d3a8eb3d"

    def setUp(self):
        """Run in a scratch dir with the lock, browser and adapter all faked."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.evals, self.ab_calls = [], []

        @contextmanager
        def free_lock(*a, **k):
            """An uncontended cross-tool chatgpt.com lock."""
            yield

        def fake_ab(ab, *a, **k):
            """Record chrome-use subcommands (only `close` is expected)."""
            self.ab_calls.append(a)
            return ""

        def fake_eval(ab, js, session, timeout):
            """Answer the in-page download of a signed URL with PNG bytes."""
            self.evals.append((js, session))
            return {"ok": True, "type": "application/octet-stream",
                    "b64": base64.b64encode(self.PNG).decode()}

        for name, value in (("_find_agent_browser", lambda: "ab"),
                            ("_chatgpt_web_turn", free_lock),
                            ("_ab", fake_ab),
                            ("_ab_eval", fake_eval),
                            ("_record_last_output", lambda p: None)):
            patcher = unittest.mock.patch.object(cig, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _recover(self, result, *argv):
        """Run the subcommand against a canned chatgpt/images result."""
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(cig, "_site_call", return_value=result) as site, \
             redirect_stdout(out), redirect_stderr(err):
            rc = cig._recover_command([self.URL, *argv])
        return rc, out.getvalue(), err.getvalue(), site

    def _image(self, file_id):
        """One generated image as chatgpt/images reports it."""
        return {"file_id": file_id, "source": "generated",
                "download_url": "https://chatgpt.com/backend-api/estuary/content?id=" + file_id}

    def test_downloads_each_image_and_prints_the_paths(self):
        """Bytes are fetched in the shared tab, typed by sniffing, then the tab closes."""
        res = {"finished": True, "title": "fox", "images": [self._image("file_a"), self._image("file_b")]}
        rc, out, err, site = self._recover(res, "-o", "fox.png", "--quiet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.split(), ["fox.png", "fox-2.png"])
        self.assertEqual(Path("fox-2.png").read_bytes(), self.PNG)
        self.assertEqual(site.call_args[0][1:4], ("chatgpt/images", [self.URL], "chatgpt-web"))
        self.assertTrue(all(s == "chatgpt-web" for _, s in self.evals))
        self.assertIn("file_b", self.evals[1][0])
        self.assertEqual(self.ab_calls, [("close",)])

    def test_default_name_comes_from_the_title_and_the_bytes(self):
        """octet-stream content still lands as .png, named after the chat."""
        res = {"finished": True, "title": "Red Fox", "images": [self._image("file_a")]}
        rc, out, _, _ = self._recover(res, "--quiet")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(Path("assets/generated/red-fox.png")))

    def test_last_is_passed_to_the_adapter(self):
        """--last narrows to the latest turn on the server side."""
        res = {"finished": True, "images": [self._image("file_a")]}
        _, _, _, site = self._recover(res, "--last", "--quiet")
        self.assertEqual(site.call_args[0][2], [self.URL, "--last", "true"])

    def test_unfinished_conversation_says_to_wait(self):
        """No image yet while ChatGPT works is "try later", not "no image"."""
        rc, out, err, _ = self._recover({"finished": False, "images": []})
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("still working", err)
        self.assertEqual(self.ab_calls, [("close",)])

    def test_finished_conversation_without_image_fails(self):
        """A finished chat with nothing generated is a plain failure."""
        rc, _, err, _ = self._recover({"finished": True, "images": []})
        self.assertEqual(rc, 1)
        self.assertIn("no generated image", err)

    def test_adapter_failure_is_reported_and_the_tab_still_closes(self):
        """A failing adapter never leaves the shared tab open."""
        with unittest.mock.patch.object(cig, "_site_call",
                                        side_effect=cig.GatewayError("HTTP 429")), \
             redirect_stderr(io.StringIO()) as err:
            rc = cig._recover_command([self.URL])
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 429", err.getvalue())
        self.assertEqual(self.ab_calls, [("close",)])


class WebProjectRouting(unittest.TestCase):
    """Exact URL targets bypass name creation; required routing never degrades."""

    ID = "g-p-0123456789abcdef0123456789abcdef"
    URL = "https://chatgpt.com/g/" + ID + "/project"

    def test_project_url_decorations_resolve_to_one_stable_id(self):
        """Display slugs, localization, and share decorations are not identity."""
        for suffix in ("", "-image-art", "-画图", "-%E7%94%BB%E5%9B%BE"):
            for tail in ("/project", "/project/", "/project?source=share#top"):
                with self.subTest(suffix=suffix, tail=tail):
                    self.assertEqual(cig._project_url_id(
                        "https://chatgpt.com/g/" + self.ID + suffix + tail), self.ID)
        self.assertIsNone(cig._project_url_id("My art project"))
        self.assertIsNone(cig._project_url_id("Art: concepts"))

    def test_url_like_invalid_targets_are_never_names(self):
        """Reject malformed, external, GPT, and conversation links before creation."""
        bad = [self.URL.replace("https:", "http:"),
               self.URL.replace("chatgpt.com", "example.com"),
               self.URL.replace("chatgpt.com", "chatgpt.com.evil.example"),
               self.URL.replace("chatgpt.com", "user@chatgpt.com"),
               self.URL.replace("chatgpt.com", "chatgpt.com:443"),
               self.URL.replace(self.ID, self.ID + "0"),
               self.URL.replace(self.ID, "g-p-short"),
               self.URL.replace("/project", "/c/01234567-89ab-cdef"),
               self.URL.replace("/project", "/project/extra"),
               self.URL.replace("https://", "//"),
               self.URL.replace("https://", "chatgpt.com/https://"),
               "https://chatgpt.com/g/g-example/project"]
        for target in bad:
            with self.subTest(target=target), \
                 unittest.mock.patch.object(cig, "_ab_eval") as page, \
                 unittest.mock.patch.object(cig, "_ab") as ab:
                with self.assertRaisesRegex(cig.GatewayError, "Project URL"):
                    cig._enter_project("ab", "s", target, lambda: 90, lambda _: None)
                page.assert_not_called()
                ab.assert_not_called()

    def test_url_opens_canonical_target_without_sidebar_or_create(self):
        """An existing URL opens directly, including projects not in the sidebar."""
        with unittest.mock.patch.object(cig, "_ab_eval") as page, \
             unittest.mock.patch.object(cig, "_ab") as ab, \
             unittest.mock.patch.object(cig, "_wait_composer", return_value=True):
            self.assertEqual(cig._enter_project("ab", "s",
                self.URL.replace("/project", "-画图/project?share=1"),
                lambda: 90, lambda _: None), self.ID)
        page.assert_not_called()
        self.assertEqual(ab.call_args.args, ("ab", "open", self.URL))

    def test_name_lookup_and_create_behavior_remains_available(self):
        """Names keep the authenticated lookup/create flow and return its ID."""
        for created in (False, True):
            messages = []
            with self.subTest(created=created), \
                 unittest.mock.patch.object(cig, "_ab_eval",
                     return_value={"ok": True, "id": self.ID, "created": created}) as page, \
                 unittest.mock.patch.object(cig, "_ab"), \
                 unittest.mock.patch.object(cig, "_wait_composer", return_value=True):
                self.assertEqual(cig._enter_project("ab", "s", "Art",
                    lambda: 90, messages.append), self.ID)
                self.assertIn(json.dumps("Art"), page.call_args.args[1])
                self.assertEqual("(created)" in messages[0], created)

    def test_optional_failure_restores_plain_chat_but_required_failure_stops(self):
        """Only legacy best-effort routing warns and opens a plain chat."""
        for required in (False, True):
            messages = []
            with self.subTest(required=required), \
                 unittest.mock.patch.object(cig, "_ab") as ab, \
                 unittest.mock.patch.object(cig, "_wait_composer", return_value=False):
                if required:
                    with self.assertRaisesRegex(cig.GatewayError, "required"):
                        cig._enter_project("ab", "s", self.URL,
                            lambda: 90, messages.append, require=True)
                    self.assertEqual(ab.call_count, 1)
                else:
                    self.assertIsNone(cig._enter_project("ab", "s", self.URL,
                        lambda: 90, messages.append))
                    self.assertEqual(ab.call_args.args, ("ab", "open", cig.WEB_NEW_CHAT_URL))
                    self.assertIn("using a plain chat", messages[-1])

    def test_required_entry_verifies_identity_after_composer_appears(self):
        """A visible composer on a redirected page is not a resolved Project."""
        for actual in (None, self.ID[:-1] + "0"):
            with self.subTest(actual=actual), \
                 unittest.mock.patch.object(cig, "_ab_eval", return_value=actual), \
                 unittest.mock.patch.object(cig, "_ab") as ab, \
                 unittest.mock.patch.object(cig, "_wait_composer", return_value=True):
                with self.assertRaisesRegex(cig.GatewayError, "required"):
                    cig._enter_project("ab", "s", self.URL,
                        lambda: 90, lambda _: None, require=True)
                self.assertEqual(ab.call_count, 1)

    def test_required_resolution_open_and_composer_errors_never_restore_plain_chat(self):
        """Every entry failure propagates before generation or plain-chat recovery."""
        cases = [("Art", {"ok": False, "error": "HTTP 403"}, None, True),
                 ("Art", {"ok": True, "id": "not-a-project"}, None, True),
                 (self.URL, self.ID, cig.GatewayError("open failed"), True),
                 (self.URL, self.ID, None, False),
                 (self.URL, cig.GatewayError("route unreadable"), None, True)]
        for target, page, open_error, composer in cases:
            with self.subTest(target=target, page=page, open_error=open_error), \
                 unittest.mock.patch.object(cig, "_ab_eval",
                     **({"side_effect": page} if isinstance(page, Exception) else {"return_value": page})), \
                 unittest.mock.patch.object(cig, "_ab", side_effect=open_error) as ab, \
                 unittest.mock.patch.object(cig, "_wait_composer", return_value=composer):
                with self.assertRaisesRegex(cig.GatewayError, "required"):
                    cig._enter_project("ab", "s", target, lambda: 90, lambda _: None, require=True)
            self.assertNotIn(cig.WEB_NEW_CHAT_URL, [c.args[2] for c in ab.call_args_list])


class RequiredProjectSubmission(unittest.TestCase):
    """Preserve drafts and never replay an uncertain guarded native Send."""

    def _run(self, project=None, arm=None, guarded=None, click_error=False, states=None):
        """Simulate only transport boundaries; execute the real submit control flow."""
        self.calls, self.evals = [], []
        project_id = WebProjectRouting.ID
        states = states or [_composer_state(), _composer_state("prompt"),
                            _composer_state(user_turns=1)]

        def page(ab, js, **kw):
            """Return route and guard receipts while recording evaluated scripts."""
            self.evals.append(js)
            if js == cig._JS_PROJECT_ID:
                return project_id if project is None else project
            result = guarded if js.startswith("JSON.stringify(window[") else arm
            if isinstance(result, Exception):
                raise result
            return result if result is not None else {"ok": True}

        def command(*args, **kw):
            """Record native transport calls and optionally lose the click response."""
            self.calls.append((args, kw))
            if click_error and args[1] == "click":
                raise cig.GatewayError("native click outcome unknown")

        state_iter = iter(states)
        with unittest.mock.patch.object(cig, "_ab_eval", side_effect=page), \
             unittest.mock.patch.object(cig, "_web_composer_state",
                 side_effect=lambda *a: next(state_iter, states[-1])), \
             unittest.mock.patch.object(cig, "_ab", side_effect=command), \
             unittest.mock.patch.object(cig.time, "sleep"):
            cig._submit_web_prompt("ab", "s", "prompt", 0, lambda: 90, lambda _: None,
                                   required_project_id=project_id)

    def test_wrong_project_stops_before_paste(self):
        """A mismatched Project never receives the prompt or a Send click."""
        with self.assertRaisesRegex(cig.GatewayError, "required"):
            self._run(project="g-p-ffffffffffffffffffffffffffffffff")
        self.assertEqual(self.calls, [])

    def test_guard_refusal_or_unreadable_arm_prevents_native_click_and_preserves_draft(self):
        """Failed guard setup retains the pasted draft and never clicks Send."""
        for result in ({"ok": False, "error": "Project route changed"},
                       {}, cig.GatewayError("guard setup outcome unknown")):
            with self.subTest(result=result), self.assertRaisesRegex(cig.GatewayError, "Draft retained"):
                self._run(arm=result)
            self.assertEqual([a[1] for a, _ in self.calls], ["fill"])
            self.assertTrue(any(js.startswith("JSON.stringify(window[") for js in self.evals))

    def test_click_time_guard_refusal_stops_without_draft_cleanup_or_replay(self):
        """A blocked native event leaves the current draft and is never retried."""
        with self.assertRaisesRegex(cig.GatewayError, "Draft retained"):
            self._run(guarded={"ok": False, "error": "Project route changed"})
        self.assertEqual([a[1] for a, _ in self.calls], ["fill", "click"])

    def test_readiness_failure_preserves_current_composer(self):
        """Changed text can belong to another context; do not erase it."""
        with self.assertRaisesRegex(cig.GatewayError, "text differs"):
            self._run(states=[_composer_state(), _composer_state("someone else's draft")])
        self.assertEqual([a[1] for a, _ in self.calls], ["fill"])

    def test_correct_project_has_one_native_send_to_the_marked_button(self):
        """One native click targets only the button carrying this guard token."""
        self._run()
        self.assertEqual([a[1] for a, _ in self.calls], ["fill", "click"])
        selector = self.calls[-1][0][2]
        self.assertRegex(selector, r'^button\[data-image-use-project-send="imageUseProjectSend_[0-9a-f]{32}"\]$')
        arm_js = next(js for js in self.evals if js.startswith(cig._JS_ARM_PROJECT_SEND.split("%s")[0]))
        self.assertIn(selector.split('"')[1], arm_js)

    def test_uncertain_native_click_observes_receipt_without_replay(self):
        """A lost click response can be confirmed without a second Send."""
        self._run(click_error=True)
        self.assertEqual([a[1] for a, _ in self.calls], ["fill", "click"])

    def test_uncertain_send_without_receipt_stops_without_replay_or_draft_cleanup(self):
        """Missing receipts cannot justify resending or clearing the current draft."""
        for outcome in ({}, cig.GatewayError("guard receipt unknown")):
            with self.subTest(outcome=outcome), \
                 self.assertRaisesRegex(cig.GatewayError, "Send was not repeated"):
                self._run(guarded=outcome, states=[_composer_state(), _composer_state("prompt")])
            self.assertEqual([a[1] for a, _ in self.calls], ["fill", "click"])

    def test_existing_draft_is_untouched_in_required_mode(self):
        """An existing draft prevents both prompt paste and Send."""
        with self.assertRaisesRegex(cig.GatewayError, "not empty"):
            self._run(states=[_composer_state("existing draft")])
        self.assertEqual(self.calls, [])

    def test_long_prompt_guard_uses_stdin_instead_of_browser_command_arguments(self):
        """Embedding exact text in the guard must not reintroduce argv size limits."""
        prompt = "first\n\nUnicode 猫 — $() `text`\n" * 6000
        calls = []
        def ab(*args, **kw):
            """Emulate encoded browser responses and record long-script stdin use."""
            calls.append((args, kw))
            if args[1] == "click":
                return ""
            result = {"ok": True} if (args[2] == "--stdin" or
                args[2].startswith("JSON.stringify(window[")) else WebProjectRouting.ID
            return json.dumps(json.dumps(result))
        with unittest.mock.patch.object(cig, "_ab", side_effect=ab), \
             unittest.mock.patch.object(cig, "_web_composer_state", side_effect=[
                 _composer_state(), _composer_state(prompt), _composer_state(user_turns=1)]):
            cig._submit_web_prompt("ab", "s", prompt, 0, lambda: 90, lambda _: None,
                                   required_project_id=WebProjectRouting.ID)
        self.assertEqual([a[1] for a, _ in calls], ["eval", "fill", "eval", "click", "eval", "eval"])
        self.assertEqual(calls[2][0], ("ab", "eval", "--stdin"))
        self.assertIn(json.dumps(prompt), calls[2][1]["input_text"])


@unittest.skipUnless(cig.shutil.which("node"), "Node is needed only to execute page-JS fixtures")
class RequiredProjectPageJS(unittest.TestCase):
    """Execute the real capture guard, including changes between arm and native click."""

    def test_native_click_is_guarded_at_the_event_and_cleanup_is_bounded(self):
        """Wrong routes/state block the app handler; expiry removes only our marker."""
        project_id, token = WebProjectRouting.ID, "imageUseProjectSend_fixture"
        script = cig._JS_ARM_PROJECT_SEND % (
            json.dumps(project_id), json.dumps(token), cig._JS_PROJECT_ID,
            cig._JS_WEB_COMPOSER_STATE % cig._js_composer_selectors(),
            json.dumps("prompt"), 0, 0,
            json.dumps(cig.WEB_COMPOSER_SEL), json.dumps(cig.WEB_SEND_SEL))
        path = "/g/" + project_id + "/project"
        cases = [
            (path, {}, 1),
            ("/g/" + project_id + "-画图/c/12345678-90ab", {}, 1),
            ("/g/" + project_id + "-%E7%94%BB%E5%9B%BE/project/", {}, 1),
            ("/", {}, 0), ("/c/12345678-90ab", {}, 0),
            ("/g/" + project_id + "0/project", {}, 0),
            ("/g/g-p-ffffffffffffffffffffffffffffffff/project", {}, 0),
            (path, {"origin": "https://example.com"}, 0),
            (path, {"text": "changed"}, 0), (path, {"attachments": 1}, 0),
            (path, {"turns": 1}, 0), (path, {"disabled": True}, 0),
            (path, {"busy": True}, 0), (path, {"dialog": True}, 0),
            (path, {"composer": False}, 0),
            (path, {"latePath": "/"}, 0),
            (path, {"latePath": "/g/g-p-ffffffffffffffffffffffffffffffff/project"}, 0),
            (path, {"lateText": "another draft"}, 0),
            (path, {"lateAttachments": 1}, 0), (path, {"lateTurns": 1}, 0),
            (path, {"lateDialog": True}, 0), (path, {"expired": True}, 0),
            (path, {"newDocument": True}, 0),
        ]
        harness = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const results = input.cases.map(([pathname, cfg]) => {
  let clicks = 0, listener = null, expiry = null;
  const attributes = {'data-owner': 'preserve'};
  const window = {
    addEventListener: (name, fn) => {listener = fn;},
    removeEventListener: () => {listener = null;},
  };
  const setTimeout = fn => {expiry = fn; return 1;};
  const clearTimeout = () => {expiry = null;};
  const send = {
    disabled: !!cfg.disabled,
    setAttribute: (key, value) => {attributes[key] = value;},
    removeAttribute: key => {delete attributes[key];},
    getAttribute: key => attributes[key] || null,
    closest: () => send,
    click: () => {
      let blocked = false;
      const event = {target: send, preventDefault: () => {blocked = true;},
        stopImmediatePropagation: () => {blocked = true;}};
      if (listener) listener(event);
      if (!blocked) clicks++;  // stand-in for the app's Send handler
    },
  };
  const form = {
    querySelector: sel => sel === input.send ? send : (cfg.busy ? {} : null),
    querySelectorAll: () => Array.from({length: cfg.attachments || 0},
      () => ({src: 'https://chatgpt.com/backend-api/estuary/content?id=fixture'})),
  };
  const composer = {value: cfg.text || 'prompt', childNodes: [], closest: () => form};
  const document = {
    querySelector: sel => sel === input.composer ? (cfg.composer === false ? null : composer)
      : (cfg.dialog ? {} : null),
    querySelectorAll: () => Array.from({length: cfg.turns || 0}, () => ({
      parentElement: {closest: () => null}, closest: () => null,
    })),
  };
  const location = {pathname, origin: cfg.origin || 'https://chatgpt.com'};
  const armed = JSON.parse(eval(input.script));
  if (armed.ok) {
    if (cfg.latePath) location.pathname = cfg.latePath;
    if (cfg.lateText) composer.value = cfg.lateText;
    if (cfg.lateAttachments) cfg.attachments = cfg.lateAttachments;
    if (cfg.lateTurns) cfg.turns = cfg.lateTurns;
    if (cfg.lateDialog) cfg.dialog = true;
    if (cfg.expired) expiry();
    if (cfg.newDocument) { // a new document cannot carry the armed marker
      delete attributes['data-image-use-project-send'];
      delete window[input.token]; listener = null;
    }
    if (attributes['data-image-use-project-send'] === input.token) send.click();
  }
  const receipt = window[input.token] ? window[input.token].finish() : null;
  return {clicks, marker: attributes['data-image-use-project-send'] || null,
          listener: !!listener, owner: attributes['data-owner'], text: composer.value};
});
process.stdout.write(JSON.stringify(results));
"""
        proc = subprocess.run([cig.shutil.which("node"), "-e", harness], text=True,
            input=json.dumps({"script": script, "cases": cases, "token": token,
                              "composer": cig.WEB_COMPOSER_SEL, "send": cig.WEB_SEND_SEL}),
            capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for case, result in zip(cases, json.loads(proc.stdout)):
            with self.subTest(case=case):
                cfg = case[1]
                self.assertEqual(result, {"clicks": case[2], "marker": None, "listener": False,
                    "owner": "preserve", "text": cfg.get("lateText", cfg.get("text", "prompt"))})


class RequiredProjectIntegration(unittest.TestCase):
    """Carry resolved identity through run_web, with retention independently controlled."""

    def test_run_web_passes_required_identity_and_respects_retention_flags(self):
        """Resolved identity reaches generation while retention controls deletion."""
        from contextlib import ExitStack
        for required, keep, keep_tab in ((True, False, False), (True, True, False),
                                        (True, False, True), (False, False, False)):
            args = argparse.Namespace(backend="web", project=WebProjectRouting.URL,
                require_project=required, keep_conversation=keep, keep_tab=keep_tab,
                profile="relay", session="fixture", timeout=90, gen_prompt="image",
                size="auto", web_model="")
            with self.subTest(required=required, keep=keep, keep_tab=keep_tab), ExitStack() as stack:
                for name, result in (("_find_agent_browser", "ab"), ("_relay_connected", True),
                                     ("_wait_composer", True), ("_ref_counts", (0, 0, 0))):
                    stack.enter_context(unittest.mock.patch.object(cig, name, return_value=result))
                for name in ("_warn_if_chrome_use_old", "_ensure_web_model", "_ab"):
                    stack.enter_context(unittest.mock.patch.object(cig, name))
                entry = stack.enter_context(unittest.mock.patch.object(cig, "_enter_project",
                                             return_value=WebProjectRouting.ID))
                generate = stack.enter_context(unittest.mock.patch.object(cig, "_generate_in_browser",
                                                return_value=(b"image", {})))
                delete = stack.enter_context(unittest.mock.patch.object(cig, "_delete_conversation"))
                cig.run_web(args, False, 0)
                self.assertEqual(entry.call_args.kwargs["require"], required)
                self.assertEqual(generate.call_args.kwargs["required_project_id"],
                                 WebProjectRouting.ID if required else None)
                self.assertEqual(delete.call_count, int(not keep and not keep_tab))

    def test_required_route_failure_stops_before_reference_resolution_or_upload(self):
        """Route rejection prevents reference work and prompt submission."""
        with unittest.mock.patch.object(cig, "_ab_eval", return_value=None), \
             unittest.mock.patch.object(cig, "_resolve_ref_path") as resolve, \
             unittest.mock.patch.object(cig, "_upload_references") as upload, \
             unittest.mock.patch.object(cig, "_submit_web_prompt") as submit:
            with self.assertRaisesRegex(cig.GatewayError, "required"):
                cig._generate_in_browser("ab", "s", "prompt", argparse.Namespace(),
                    90, lambda: 90, lambda _: None, refs=["fixture.png"],
                    required_project_id=WebProjectRouting.ID)
        resolve.assert_not_called()
        upload.assert_not_called()
        submit.assert_not_called()

    def test_cli_env_flag_and_retention_are_independent(self):
        """The real parser consumes env/flags before styles or backend work."""
        parse_args = argparse.ArgumentParser.parse_args
        for env, flags, expected, keep in (
            ({"IMAGE_USE_REQUIRE_PROJECT": "1"}, [], True, False),
            ({"CHATGPT_IMAGEGEN_REQUIRE_PROJECT": "1"}, [], True, False),
            ({"IMAGE_USE_REQUIRE_PROJECT": "0", "CHATGPT_IMAGEGEN_REQUIRE_PROJECT": "1"}, [], False, False),
            ({"IMAGE_USE_REQUIRE_PROJECT": "1"}, ["--no-require-project"], False, False),
            ({"CHATGPT_IMAGEGEN_REQUIRE_PROJECT": "1"}, ["--no-require-project"], False, False),
            ({"IMAGE_USE_REQUIRE_PROJECT": "0"}, ["--require-project"], True, False),
            ({}, ["--require-project", "--no-require-project"], False, False),
            ({}, ["--no-require-project", "--require-project"], True, False),
            ({"IMAGE_USE_REQUIRE_PROJECT": "1"}, ["--no-require-project", "--keep-conversation"], False, True),
            ({}, ["--require-project", "--keep-conversation"], True, True),
        ):
            args_seen = []
            def record_args(parser, argv):
                """Record real parser output before any styles or backend work."""
                args = parse_args(parser, argv)
                args_seen.append(args)
                return args
            with self.subTest(env=env, flags=flags), \
                 unittest.mock.patch.dict(os.environ, env, clear=True), \
                 unittest.mock.patch.object(sys, "argv", ["image-use", "prompt", *flags]), \
                 unittest.mock.patch.object(cig, "_should_check_for_update", return_value=False), \
                 unittest.mock.patch.object(argparse.ArgumentParser, "parse_args",
                     autospec=True, side_effect=record_args), \
                 unittest.mock.patch.object(cig, "_load_styles",
                     side_effect=RuntimeError("parser checkpoint")):
                with self.assertRaisesRegex(RuntimeError, "parser checkpoint"):
                    cig.main()
            self.assertEqual(args_seen[0].require_project, expected)
            self.assertEqual(args_seen[0].keep_conversation, keep)

    def test_cli_opt_out_allows_plain_chat_or_non_web_backend_without_changing_env(self):
        """A per-run opt-out passes validation while the exported default stays on."""
        parse_args = argparse.ArgumentParser.parse_args
        for env_name in ("IMAGE_USE_REQUIRE_PROJECT", "CHATGPT_IMAGEGEN_REQUIRE_PROJECT"):
            for backend, project in (("web", ""), ("auto", ""), ("codex", "Art"),
                                     ("gemini", "Art"), ("agy", "Art")):
                args_seen = []
                def record_args(parser, argv):
                    """Observe the actual CLI parser without bypassing validation."""
                    args = parse_args(parser, argv)
                    args_seen.append(args)
                    return args
                with self.subTest(env=env_name, backend=backend, project=project), \
                     unittest.mock.patch.dict(os.environ, {env_name: "1"}, clear=True), \
                     unittest.mock.patch.object(sys, "argv", ["image-use", "prompt",
                         "--no-require-project", "--backend", backend, "--project", project]), \
                     unittest.mock.patch.object(cig, "_should_check_for_update", return_value=False), \
                     unittest.mock.patch.object(argparse.ArgumentParser, "parse_args",
                         autospec=True, side_effect=record_args), \
                     unittest.mock.patch.object(cig, "_load_styles",
                         side_effect=RuntimeError("parser checkpoint")), \
                     unittest.mock.patch.object(cig, "_dispatch") as dispatch:
                    with self.assertRaisesRegex(RuntimeError, "parser checkpoint"):
                        cig.main()
                    self.assertEqual(os.environ[env_name], "1")
                    self.assertFalse(args_seen[0].require_project)
                    self.assertEqual(args_seen[0].backend, backend)
                    self.assertEqual(args_seen[0].project, project)
                    dispatch.assert_not_called()

    def test_cli_invalid_route_stops_before_styles_or_backend(self):
        """Invalid required targets and backends fail before downstream work."""
        for flags in (["--require-project", "--project", ""],
                      ["--require-project", "--backend", "codex"],
                      ["--project", "https://example.com/project"]):
            with self.subTest(flags=flags), \
                 unittest.mock.patch.dict(os.environ, {}, clear=True), \
                 unittest.mock.patch.object(sys, "argv", ["image-use", "prompt", *flags]), \
                 unittest.mock.patch.object(cig, "_should_check_for_update", return_value=False), \
                 unittest.mock.patch.object(cig, "_load_styles") as styles, \
                 unittest.mock.patch.object(cig, "_dispatch") as dispatch, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cig.main()
                self.assertEqual(error.exception.code, 2)
            styles.assert_not_called()
            dispatch.assert_not_called()


class RequiredProjectDispatch(unittest.TestCase):
    """The real dispatch boundary rejects incompatible backends and fallbacks."""

    def setUp(self):
        """Provide a valid required-Project request for dispatch boundary tests."""
        self.args = argparse.Namespace(backend="auto", project="Art", require_project=True,
                                      ref=None, resolved_refs=None)

    @contextmanager
    def _backends(self, failure=None):
        """Record every backend/token boundary without opening a browser or lock."""
        from contextlib import ExitStack, nullcontext
        with ExitStack() as stack:
            stack.enter_context(unittest.mock.patch.object(cig, "_codex_only_options", return_value=[]))
            for lock in ("_chatgpt_web_turn", "_concurrency_slot"):
                stack.enter_context(unittest.mock.patch.object(cig, lock, side_effect=lambda *a: nullcontext()))
            calls = {name: stack.enter_context(unittest.mock.patch.object(cig, name))
                     for name in ("run_web", "run_codex", "run_gemini", "run_agy", "_codex_token_present")}
            calls["run_web"].side_effect = failure
            calls["run_web"].return_value = (b"image", {})
            yield calls

    def test_required_unavailable_web_never_reads_codex_token_or_falls_back(self):
        """Unavailable web stops required mode before Codex credentials or calls."""
        with self._backends(cig.WebUnavailable("relay offline")) as calls:
            with self.assertRaisesRegex(SystemExit, "required"):
                cig._dispatch(self.args, False, 300, 0)
        calls["run_web"].assert_called_once()
        calls["run_codex"].assert_not_called()
        calls["_codex_token_present"].assert_not_called()

    def test_required_non_web_backends_and_empty_target_stop_before_work(self):
        """Reject explicit incompatible routes, even when their clients are ready."""
        for backend, project in (("codex", "Art"), ("gemini", "Art"),
                                 ("agy", "Art"), ("auto", ""), ("web", "  ")):
            with self.subTest(backend=backend, project=project), self._backends() as calls:
                self.args.backend, self.args.project = backend, project
                with self.assertRaises(SystemExit):
                    cig._dispatch(self.args, False, 300, 0)
                for call in calls.values():
                    call.assert_not_called()

    def test_required_generation_error_does_not_suggest_codex(self):
        """Required-mode failures never recommend switching to Codex."""
        with self._backends(cig.GatewayError("required Project changed")) as calls:
            with self.assertRaises(SystemExit) as error:
                cig._dispatch(self.args, False, 300, 0)
        self.assertNotIn("rerun with --backend codex", str(error.exception))
        calls["run_codex"].assert_not_called()

    def test_optional_auto_fallback_is_unchanged(self):
        """Existing users retain fallback when they have not required a Project."""
        self.args.require_project = False
        with self._backends(cig.WebUnavailable("relay offline")) as calls:
            calls["_codex_token_present"].return_value = True
            cig._dispatch(self.args, False, 300, 0)
        calls["run_codex"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
