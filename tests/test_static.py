"""Static guardrails: shipped scripts must be stdlib-only and contain no mock paths."""
import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "migrate-from-codex" / "scripts"
FORBIDDEN_NAMES = {"mock", "Mock", "MagicMock", "patch", "fake_result", "simulate", "dummy"}
FORBIDDEN_IMPORTS = {"unittest.mock", "mock"}


def test_scripts_have_no_mock_paths():
    for script in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = {a.name for a in node.names}
                assert mod not in FORBIDDEN_IMPORTS and not (names & FORBIDDEN_IMPORTS), (
                    f"{script.name} imports a mock library"
                )
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES, f"{script.name} uses '{node.id}'"
            if isinstance(node, ast.FunctionDef):
                assert not any(w in node.name.lower() for w in ("mock", "fake", "simulate")), (
                    f"{script.name}: {node.name}"
                )


def test_scripts_are_stdlib_only():
    allowed = {
        "__future__", "argparse", "datetime", "hashlib", "json",
        "os", "pathlib", "re", "shutil", "subprocess", "sys", "tomllib",
    }
    for script in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] in allowed, f"{script.name} imports {a.name}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] in allowed, f"{script.name} imports {node.module}"


def test_subprocess_only_used_for_codex_version():
    """The scanner may spawn exactly one thing: `codex --version`."""
    for script in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_sub = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            if not is_sub:
                continue
            assert func.attr == "run", f"{script.name}: only subprocess.run is allowed"
            argv = node.args[0]
            assert isinstance(argv, ast.List) and len(argv.elts) == 2, f"{script.name}: argv must be [binary, '--version']"
            flag = argv.elts[1]
            assert isinstance(flag, ast.Constant) and flag.value == "--version", f"{script.name}: only --version may be invoked"
            kw = {k.arg: k.value for k in node.keywords}
            assert "timeout" in kw, f"{script.name}: subprocess.run must have a timeout"
