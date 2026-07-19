"""Frontend structure regressions for the Phase 7 client-feature split."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "js"


def _imports(source: Path):
    pattern = r'^\s*import(?:[\s\S]*?from\s*)?[\'\"]([^\'\"]+)[\'\"]'
    for specifier in re.findall(pattern, source.read_text(), re.MULTILINE):
        if specifier.startswith("."):
            target = (source.parent / specifier).resolve()
            yield target if target.suffix == ".js" else target.with_suffix(".js")


def test_client_feature_has_focused_modules_and_a_single_state_owner():
    feature = WEB / "clients"
    for name in ("store.js", "api.js", "grid.js", "actions.js", "filters.js", "edit-modal.js", "nac-setup.js", "index.js"):
        assert (feature / name).is_file()
    assert "let roster" in (feature / "store.js").read_text()
    assert "from \"./store.js\"" not in (feature / "edit-modal.js").read_text()
    assert "from \"./store.js\"" not in (feature / "grid.js").read_text()


def test_frontend_import_graph_is_acyclic():
    graph = {source.resolve(): list(_imports(source)) for source in WEB.rglob("*.js")}
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise AssertionError(f"circular frontend import involving {node.relative_to(WEB)}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for module in graph:
        visit(module)


def test_modal_receives_its_completion_callback_without_importing_client_state():
    modal = (WEB / "clients" / "edit-modal.js").read_text()
    assert "onComplete" in modal
    assert "../clients.js" not in modal
    assert "./index.js" not in modal
