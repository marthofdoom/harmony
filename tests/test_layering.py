"""Architectural fence: the core must stay frontend-agnostic.

Harmony has two frontends on the roadmap — the GTK desktop app and a hosted web
service — sharing one engine. That only works if the engine never reaches for
GTK. These checks parse the source rather than importing it, so they run on a
machine with no GTK installed and catch the mistake at the point someone adds
the import rather than months later during the web port.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "harmony"

# The only places allowed to touch GTK: the UI package itself and the two
# desktop entry points that construct the application.
GUI_ALLOWED = {
    "app.py",
    "__main__.py",
}
GUI_ALLOWED_PACKAGES = {"ui"}

# ``tasks`` is the single sanctioned bridge between the engine and the GTK main
# loop. It is exempt from the blanket rule below but held to a stricter one:
# every GTK import must sit inside a function body, so the module still imports
# cleanly on a headless server. See test_tasks_gtk_usage_is_confined_to_function_bodies.
GUI_BRIDGE = {"tasks.py"}

GUI_MODULES = {"gi", "gtk", "adw"}


def _core_files() -> list[Path]:
    """Every module that must remain importable without a display server."""
    files = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel.parts[0] in GUI_ALLOWED_PACKAGES:
            continue
        if len(rel.parts) == 1 and rel.name in GUI_ALLOWED:
            continue
        files.append(path)
    return files


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports have no module root to police.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_core_files_are_discovered() -> None:
    """Guard the guard: a bad path glob would make every check below vacuous."""
    names = {p.name for p in _core_files()}
    assert {"models.py", "matching.py", "sync.py", "db.py"} <= names


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: p.name)
def test_core_module_does_not_import_gtk(path: Path) -> None:
    if path.name in GUI_BRIDGE:
        pytest.skip("bridge module, covered by the stricter module-scope check")
    offending = _imported_roots(path) & GUI_MODULES
    assert not offending, (
        f"{path.relative_to(SRC.parent.parent)} imports {sorted(offending)}. "
        "Core modules must run headless — move GUI code into harmony/ui/."
    )


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: p.name)
def test_core_module_does_not_import_ui_package(path: Path) -> None:
    """Dependencies point one way: ui -> core, never core -> ui."""
    source = path.read_text("utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "harmony.ui" in node.module:
            pytest.fail(f"{path.name} imports {node.module}; core must not depend on the UI.")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("harmony.ui"):
                    pytest.fail(f"{path.name} imports {alias.name}; core must not depend on the UI.")


def test_tasks_gtk_usage_is_confined_to_function_bodies() -> None:
    """``tasks`` bridges to GLib, so it is the one core module that may touch it.

    It must do so lazily inside functions — a module-level ``gi`` import would
    make the whole engine unimportable on a headless server.
    """
    tree = ast.parse((SRC / "tasks.py").read_text("utf-8"), filename="tasks.py")
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "gi" for a in node.names), (
                "tasks.py imports gi at module scope; it must be imported inside "
                "the functions that need it so the core stays headless-importable."
            )
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] != "gi", (
                "tasks.py imports from gi at module scope; keep it inside function bodies."
            )
