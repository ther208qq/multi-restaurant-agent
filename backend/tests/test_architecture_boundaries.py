"""Static import-boundary checks for every layer (spec §6 / §8 D4).

Per spec §6 and §8 (D4), dependency direction is enforced by scanning module
imports rather than by abstract base classes.

Acceptance criteria encoded here (spec §6):

* Repo  - only touches the DB; never calls Service / API / Agent.
* Service - reaches data only through Repo; never depends on FastAPI.
* API   - only calls Service; never touches Repo, the DB, or Agent *tools*.
* Agent - never touches Repo / API / the DB; reaches business logic through
  Service (directly or via a thin Tool).

``routes/chat.py`` is allowed to import ``agent.graph`` / ``agent.checkpointer``
because that endpoint *is* the agent's HTTP entry point; the ban is on the
business layer under ``agent.tools``.
"""

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
REPO_DIR = APP_DIR / "repo"
SERVICE_DIR = APP_DIR / "service"
ROUTES_DIR = APP_DIR / "routes"
AGENT_DIR = APP_DIR / "agent"

# layer -> (forbidden single module-name parts,
#           forbidden top-level modules,
#           forbidden dotted module prefixes)
LAYER_RULES = {
    "repo": ({"service", "routes", "agent"}, {"fastapi"}, set()),
    "service": ({"routes", "agent"}, {"fastapi"}, set()),
    "routes": (set(), set(), {"backend.app.repo", "backend.app.agent.tools"}),
    "agent": (set(), set(), {"backend.app.repo", "backend.app.routes"}),
}

# Raw DB access, regardless of how it was imported. Presence of any of these in
# a layer that must not own data access is a violation of §6.
DB_ACCESS_MARKERS = ("AsyncSessionLocal", "select(", ".execute(", ".commit(")


def _modules_in_source(source: str):
    """Yield every module name referenced by import statements in ``source``."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            # Relative imports keep only the tail (e.g. ``from ..service import``
            # yields ``service``), which is enough to detect a layer crossing.
            if node.module:
                yield node.module


def violations_in_source(
    source: str,
    forbidden_parts: set,
    forbidden_roots: set,
    forbidden_prefixes: set = frozenset(),
):
    """Return the module names in ``source`` that cross a forbidden boundary."""
    found = []
    for module in _modules_in_source(source):
        parts = module.split(".")
        if parts[0] in forbidden_roots or forbidden_parts.intersection(parts):
            found.append(module)
            continue
        # A dotted prefix must match a whole segment (``backend.app.repo`` is a
        # violation, ``backend.app.repofoo`` is not).
        for prefix in forbidden_prefixes:
            if module == prefix or module.startswith(prefix + "."):
                found.append(module)
                break
    return found


def scan_layer(
    layer_dir: Path,
    forbidden_parts: set,
    forbidden_roots: set,
    forbidden_prefixes: set = frozenset(),
):
    """Return ``{relative_path: [violating_modules]}`` for a layer directory."""
    report = {}
    for path in sorted(layer_dir.rglob("*.py")):
        found = violations_in_source(
            path.read_text(encoding="utf-8"),
            forbidden_parts,
            forbidden_roots,
            forbidden_prefixes,
        )
        if found:
            report[str(path.relative_to(APP_DIR))] = found
    return report


def raw_db_access_in_layer(layer_dir: Path):
    """Return ``{relative_path: [markers]}`` for files touching the DB directly."""
    report = {}
    for path in sorted(layer_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        found = [m for m in DB_ACCESS_MARKERS if m in source]
        if found:
            report[str(path.relative_to(APP_DIR))] = found
    return report


def test_repo_layer_boundaries():
    forbidden_parts, forbidden_roots, forbidden_prefixes = LAYER_RULES["repo"]
    assert scan_layer(REPO_DIR, forbidden_parts, forbidden_roots, forbidden_prefixes) == {}


def test_service_layer_boundaries():
    forbidden_parts, forbidden_roots, forbidden_prefixes = LAYER_RULES["service"]
    assert scan_layer(SERVICE_DIR, forbidden_parts, forbidden_roots, forbidden_prefixes) == {}


def test_routes_layer_boundaries():
    """API must not reach into the Repo layer or the Agent's business tools."""
    forbidden_parts, forbidden_roots, forbidden_prefixes = LAYER_RULES["routes"]
    assert scan_layer(ROUTES_DIR, forbidden_parts, forbidden_roots, forbidden_prefixes) == {}


def test_agent_layer_boundaries():
    """Agent must not reach into the Repo layer or the API layer."""
    forbidden_parts, forbidden_roots, forbidden_prefixes = LAYER_RULES["agent"]
    assert scan_layer(AGENT_DIR, forbidden_parts, forbidden_roots, forbidden_prefixes) == {}


def test_routes_have_no_direct_db_access():
    """§6: API only calls Service -- no ORM/session usage in ``routes/``."""
    assert raw_db_access_in_layer(ROUTES_DIR) == {}


def test_agent_has_no_direct_db_access():
    """§6: Agent never touches the DB -- no ORM/session usage in ``agent/``."""
    assert raw_db_access_in_layer(AGENT_DIR) == {}


def test_scanner_detects_violations():
    """Guard against a vacuously-passing scan by proving detection works."""
    source = "import fastapi\nfrom backend.app.routes import auth_router\n"
    found = violations_in_source(source, {"service", "routes", "agent"}, {"fastapi"})
    assert set(found) == {"fastapi", "backend.app.routes"}


def test_scanner_detects_prefix_violations():
    """Dotted prefixes are enforced as whole segments, not substrings."""
    source = (
        "from backend.app.repo.branch import BranchRepo\n"
        "from backend.app.agent.tools import menu_tools\n"
        "from backend.app.repofoo import typo\n"
    )
    found = violations_in_source(
        source, set(), set(), {"backend.app.repo", "backend.app.agent.tools"}
    )
    assert set(found) == {"backend.app.repo.branch", "backend.app.agent.tools"}


def test_scanner_detects_raw_db_access():
    """The raw-DB scan must flag direct ORM/session use."""
    assert raw_db_access_in_layer(REPO_DIR) != {}


def test_scanner_allows_layering_imports():
    """Service may import repo; repo may import models/infra -- no false positives."""
    source = (
        "from backend.app.repo.branch import BranchRepo\n"
        "from backend.app.models.branch import Branch\n"
        "from backend.app.database import transactional_session\n"
        "from backend.app.exceptions import NotFoundError\n"
    )
    assert violations_in_source(source, {"service", "routes", "agent"}, {"fastapi"}) == []


def test_scanner_allows_routes_agent_entrypoints():
    """The chat endpoint may import the agent graph; Service imports stay legal."""
    source = (
        "from backend.app.agent.graph import build_restaurant_graph\n"
        "from backend.app.agent.checkpointer import get_checkpointer\n"
        "from backend.app.service.branch import BranchService\n"
        "from backend.app.database import get_db\n"
    )
    assert (
        violations_in_source(
            source, set(), set(), {"backend.app.repo", "backend.app.agent.tools"}
        )
        == []
    )
