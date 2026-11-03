"""The views layer stays runnable in a plain Python session (spec section 7)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import audit_workbench.dashboard as package

FORBIDDEN = {"streamlit", "plotly"}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_package_modules_do_not_import_streamlit_or_plotly() -> None:
    folder = Path(package.__file__).parent
    modules = sorted(folder.glob("*.py"))
    assert {m.name for m in modules} >= {"__init__.py", "views.py", "source.py"}
    for module in modules:
        assert not _imported_roots(module) & FORBIDDEN, module.name


def test_importing_the_views_does_not_load_streamlit_or_plotly() -> None:
    code = (
        "import sys, audit_workbench.dashboard.views, audit_workbench.dashboard.source;"
        "bad = [m for m in ('streamlit', 'plotly') if m in sys.modules];"
        "print(','.join(bad));"
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_app_folder_is_not_an_importable_package() -> None:
    root = Path(package.__file__).parents[3]
    assert not (root / "dashboard" / "__init__.py").exists()
