import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def sg(*args):
    return subprocess.run([sys.executable, "-m", "sg", *args], cwd=HERE, capture_output=True, text=True, timeout=120)


class CommandLine(unittest.TestCase):
    """Every subcommand is wired to a function (an edit once dropped two
    without any other test noticing), and the offline ones run."""

    def test_every_subcommand_is_wired(self):
        for cmd in ("plan", "run", "status", "adopt", "sync", "matrix", "gaps", "report", "doctor"):
            with self.subTest(cmd=cmd):
                r = sg(cmd, "--help")
                self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_offline_commands_run(self):
        r = sg("plan", "--tiles", "N41.75E12.25", "--products", "wind")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        r = sg("status", "--products", "wind")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_dispatch_names_exist(self):
        import ast
        src = (HERE / "sg" / "__main__.py").read_text()
        tree = ast.parse(src)
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        called = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id.startswith("cmd_")}
        self.assertEqual(called - defined, set())


if __name__ == "__main__":
    unittest.main()
