"""The credentials loader: what the portal actually hands out must parse, the
nearest and newest file must win, and a real environment variable must never be
overwritten by a file on disk."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import envfile                 # noqa: E402

PORTAL = """# Challenge credentials only - never use a real cloud key
export EXOSCALE_API_KEY=EXOabc123
export EXOSCALE_API_SECRET=s3cr3t-value
export GREENOPS_CLOUD_ENDPOINT=https://host/runs/abc/cloud/v2
export GREENOPS_GRID_SECRET=grid-secret
export GREENOPS_GRID_ENDPOINT=https://host/runs/abc/grid/v4
export GREENOPS_WORK_ENDPOINT=https://host/runs/abc/work/v1
"""


class Parse(unittest.TestCase):
    def test_portal_download_format(self):
        d = envfile.parse(PORTAL)
        self.assertEqual(set(d), set(envfile.VARS))
        self.assertEqual(d["EXOSCALE_API_KEY"], "EXOabc123")
        self.assertEqual(d["GREENOPS_GRID_ENDPOINT"], "https://host/runs/abc/grid/v4")

    def test_quotes_crlf_blanks_and_comments(self):
        d = envfile.parse("\r\n# note\r\nexport A='one'\r\nB=\"two\"\r\n\r\nC=three \r\n")
        self.assertEqual(d, {"A": "one", "B": "two", "C": "three"})

    def test_hash_inside_a_value_is_kept(self):
        self.assertEqual(envfile.parse("S=ab#cd")["S"], "ab#cd")

    def test_junk_lines_are_skipped(self):
        self.assertEqual(envfile.parse("not a pair\nsource foo.sh\nA=1\n"), {"A": "1"})


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.deep = self.root / "a" / "b" / "c"
        self.deep.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def write(self, path, text, mtime):
        path.write_text(text)
        os.utime(path, (mtime, mtime))
        return path

    def test_a_parent_serves_every_directory_below_it(self):
        want = self.write(self.root / "greenops.env", "A=1\n", 1000)
        self.assertEqual(envfile.find(self.deep), want)

    def test_nearer_beats_farther(self):
        self.write(self.root / "greenops.env", "A=1\n", 9000)
        near = self.write(self.deep / "greenops.env", "A=2\n", 1000)
        self.assertEqual(envfile.find(self.deep), near)

    def test_newest_beats_older_in_the_same_directory(self):
        self.write(self.deep / "greenops-old.env", "A=1\n", 1000)
        new = self.write(self.deep / "greenops-new.env", "A=2\n", 9000)
        self.assertEqual(envfile.find(self.deep), new)

    def test_nothing_found_is_not_an_error(self):
        self.assertEqual(envfile.load(start=self.deep, environ={}), (None, []))

    def test_named_file_that_is_absent_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            envfile.load(self.root / "nope.env", environ={})


class Loading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.file = self.dir / "greenops.env"
        self.file.write_text(PORTAL)
        self.addCleanup(self.tmp.cleanup)

    def test_sets_every_variable(self):
        env = {}
        used, applied = envfile.load(start=self.dir, environ=env)
        self.assertEqual(used, self.file)
        self.assertEqual(set(applied), set(envfile.VARS))
        self.assertEqual(envfile.missing(env), [])

    def test_a_real_variable_wins_over_the_file(self):
        env = {"GREENOPS_GRID_SECRET": "from-the-shell"}
        _, applied = envfile.load(start=self.dir, environ=env)
        self.assertEqual(env["GREENOPS_GRID_SECRET"], "from-the-shell")
        self.assertNotIn("GREENOPS_GRID_SECRET", applied)

    def test_an_empty_variable_counts_as_unset(self):
        env = {"GREENOPS_GRID_SECRET": ""}
        envfile.load(start=self.dir, environ=env)
        self.assertEqual(env["GREENOPS_GRID_SECRET"], "grid-secret")

    def test_status_never_prints_a_secret(self):
        env = {}
        envfile.load(start=self.dir, environ=env)
        rows = dict(envfile.status(env))
        for key in envfile.MASK:
            self.assertNotIn(env[key], rows[key])
            self.assertIn("chars", rows[key])
        self.assertEqual(rows["GREENOPS_CLOUD_ENDPOINT"], env["GREENOPS_CLOUD_ENDPOINT"])

    def test_missing_names_only_what_a_live_run_needs(self):
        self.assertEqual(envfile.missing({}), list(envfile.REQUIRED))


class RunWiring(unittest.TestCase):
    """`run` must reach the cloud with credentials that came only from the file:
    no exports, no --config, nothing touched by hand."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "greenops.env").write_text(PORTAL)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path.cwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.cwd)
        self.env = dict(os.environ)
        for key in envfile.VARS:
            os.environ.pop(key, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    def test_file_alone_is_enough_to_start_a_run(self):
        from unittest import mock
        from orchestrator import run as runmod
        with mock.patch("orchestrator.cloud.CloudEnv") as cloud, \
             mock.patch.object(runmod, "run_loop") as loop:
            cloud.return_value.grid = "https://host/runs/abc/grid/v4"
            runmod.main(["run"])
        account = cloud.call_args[0][0]
        self.assertEqual(account["key"], "EXOabc123")
        self.assertEqual(account["endpoint"], "https://host/runs/abc/cloud/v2")
        self.assertEqual(cloud.call_args[0][1], "https://host/runs/abc/grid/v4")
        self.assertEqual(cloud.call_args[0][2], "grid-secret")
        self.assertTrue(loop.called)

    def test_no_env_file_without_exports_refuses_instead_of_crashing(self):
        from orchestrator import run as runmod
        with self.assertRaises(SystemExit):
            runmod.main(["run", "--no-env-file"])


if __name__ == "__main__":
    unittest.main()
