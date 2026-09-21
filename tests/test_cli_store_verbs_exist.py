"""Every `store.<method>(` the CLI calls must exist on DecisionStore.

`awask resolve` shipped calling `store.resolve()` before the method existed
(2026-09-21): the parser accepted the verb, help listed it, and the first real
use died with AttributeError. CCM001 (check_collaborator_methods.py) resolves
`self.<x>` collaborators, not a typed parameter, so nothing static saw it.
"""
from __future__ import annotations

import ast
from pathlib import Path

from awask.store import DecisionStore

CLI = Path(__file__).resolve().parents[1] / "awask" / "cli.py"


def _store_calls():
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "store"):
            yield node.lineno, node.func.attr


def test_every_cli_store_verb_is_a_store_method():
    calls = list(_store_calls())
    assert calls, "no store.<verb>( calls found -- the scan is broken, not the CLI"
    missing = sorted({(ln, m) for ln, m in calls if not callable(getattr(DecisionStore, m, None))})
    assert not missing, f"cli.py calls DecisionStore methods that do not exist: {missing}"


def test_scan_can_fail():
    src = "def f(store):\n    store.no_such_verb()\n"
    tree = ast.parse(src)
    attrs = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert attrs == ["no_such_verb"] and not hasattr(DecisionStore, "no_such_verb")
