"""Every external door names the person when it calls the trigger service (access-control plan P3 Task 4, lead review).

``primer.trigger.service`` gates a trigger on ``person`` and treats ``person=None`` as an internal (platform) call that is not
gated. So a REST route or a tool that forgets ``person=`` silently skips the access check; three channel-binding tools did.
This static test parses every module under ``primer/api`` and ``primer/toolset`` and fails on any call to a gated service
function that does not pass ``person=`` by keyword (a ``**kwargs`` spread does not count: the check must be visible).

The allowlist names the only doors that may call without a person: token-authenticated routes, where the URL's token is the
credential and there is no signed-in person. Today the webhook routes call only ``get_trigger_by_webhook_token``, which takes no
person, so the allowlist is empty; an entry that no call matches fails too, so it cannot outlive its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "primer.trigger.service"
#: The service functions that take ``person`` and skip the gate without it.
GATED = frozenset({
    "create_trigger", "update_trigger", "delete_trigger", "fire_now", "get_trigger", "list_triggers", "rotate_webhook_token",
    "create_subscription", "update_subscription", "delete_subscription", "get_subscription",
})
#: (path relative to the repo, enclosing function, called function) -> why that door has no person.
ALLOWLIST: dict[tuple[str, str, str], str] = {}


def _calls_without_person(path: Path) -> list[tuple[str, str, int]]:
    """``(enclosing function, called gated function, line)`` for each call in ``path`` that omits ``person=``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: dict[str, str] = {}   # local name -> service function (``from primer.trigger.service import x as y``)
    modules: set[str] = set()    # local names bound to the service module (``import primer.trigger.service as s``)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SERVICE:
            names.update({alias.asname or alias.name: alias.name for alias in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module == "primer.trigger":
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "service")
        elif isinstance(node, ast.Import):
            modules.update(alias.asname for alias in node.names if alias.name == SERVICE and alias.asname)

    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            scope = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else enclosing
            if isinstance(child, ast.Call):
                func = child.func
                target = None
                if isinstance(func, ast.Name):
                    target = names.get(func.id)
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in modules:
                    target = func.attr
                if target in GATED and not any(kw.arg == "person" for kw in child.keywords):
                    found.append((enclosing, target, child.lineno))
            visit(child, scope)

    visit(tree, "<module>")
    return found


def _doors() -> list[Path]:
    return sorted(p for base in ("primer/api", "primer/toolset") for p in (ROOT / base).rglob("*.py"))


def test_every_door_passes_the_person_to_a_gated_trigger_service_call() -> None:
    missing = []
    used = set()
    for path in _doors():
        rel = path.relative_to(ROOT).as_posix()
        for enclosing, target, line in _calls_without_person(path):
            key = (rel, enclosing, target)
            if key in ALLOWLIST:
                used.add(key)
                continue
            missing.append(f"{rel}:{line} {enclosing}() calls {target}() without person=")
    assert not missing, "a door skips the trigger access gate:\n" + "\n".join(missing)
    assert used == set(ALLOWLIST), f"stale allowlist entries: {sorted(set(ALLOWLIST) - used)}"


def test_the_guard_sees_a_call_that_forgets_the_person(tmp_path: Path) -> None:
    """The parser itself: a direct, an aliased and a module-attribute call without ``person=`` are all found; one with it is not."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from primer.trigger.service import get_trigger, delete_subscription as drop\n"
        "from primer.trigger import service\n"
        "async def door(ctx):\n"
        "    await get_trigger(trigger_id='t', deps=None)\n"
        "    await drop(trigger_id='t', subscription_id='s', deps=None)\n"
        "    await service.fire_now(trigger_id='t', deps=None)\n"
        "    await get_trigger(trigger_id='t', person=ctx, deps=None)\n",
        encoding="utf-8",
    )
    assert _calls_without_person(probe) == [("door", "get_trigger", 4), ("door", "delete_subscription", 5), ("door", "fire_now", 6)]
