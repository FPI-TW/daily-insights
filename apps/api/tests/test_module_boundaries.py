import ast
import re
from pathlib import Path

MODULE_ROOT = Path(__file__).parents[1] / "src" / "daily_insights_api" / "modules"
COMPOSITION_ROOT_EXCEPTIONS = {
    ("admin/router.py", "audit"),
    ("admin/router.py", "identity"),
    ("admin/router.py", "markets"),
    ("admin/router.py", "tenancy"),
    ("identity/auth.py", "tenancy"),
    ("identity/router.py", "tenancy"),
    ("operations/service.py", "reports"),
}
REQUIRED_PUBLIC_MODULES = {
    "admin",
    "assets",
    "audit",
    "chat",
    "data_sources",
    "data_management",
    "identity",
    "markets",
    "model_runtime",
    "news",
    "operations",
    "podcasts",
    "reports",
    "tenancy",
}
TABLE_OWNERS = {
    "active_model_configuration": "model_runtime",
    "asset_migration_entries": "assets",
    "asset_migration_manifests": "assets",
    "assets": "assets",
    "audit_events": "audit",
    "conversations": "chat",
    "data_management_runs": "data_management",
    "generation_records": "model_runtime",
    "index_daily_bar_series": "markets",
    "index_daily_bars": "markets",
    "institutional_market_flows": "markets",
    "institutional_stock_flows": "markets",
    "login_throttles": "identity",
    "markets": "markets",
    "memberships": "tenancy",
    "messages": "chat",
    "model_configurations": "model_runtime",
    "news_editions": "news",
    "news_generation_audits": "news",
    "news_items": "news",
    "news_presentations": "news",
    "organization_market_policies": "markets",
    "organizations": "tenancy",
    "podcast_episode_audio_variants": "podcasts",
    "podcast_episode_translations": "podcasts",
    "podcast_episodes": "podcasts",
    "publication_source_runs": "reports",
    "report_pipeline_runs": "operations",
    "report_publications": "reports",
    "sessions": "identity",
    "source_runs": "operations",
    "users": "identity",
}
WRITE_SQL = re.compile(
    r"\b(?:insert\s+into|update|delete\s+from)\s+[\"']?([a-z][a-z0-9_]*)",
    re.IGNORECASE,
)


def _target_module(module_name: str) -> tuple[str, str | None] | None:
    parts = module_name.split(".")
    if "modules" not in parts:
        return None
    index = parts.index("modules")
    if len(parts) <= index + 1:
        return None
    segment = parts[index + 2] if len(parts) > index + 2 else None
    return parts[index + 1], segment


def _attribute_path(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _violations(source: str, *, owner: str, module_relative: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        imported_modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        for imported_module in imported_modules:
            target = _target_module(imported_module)
            if target is None:
                continue
            target_owner, target_segment = target
            if target_owner == owner or target_segment == "api":
                continue
            if (module_relative, target_owner) in COMPOSITION_ROOT_EXCEPTIONS:
                continue
            violations.append(f"private import: {imported_module}")

        if isinstance(node, ast.Subscript):
            attribute = _attribute_path(node.value)
            if attribute is None or not attribute.endswith("Base.metadata.tables"):
                continue
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                table_owner = TABLE_OWNERS.get(node.slice.value)
                if (
                    table_owner is not None
                    and table_owner != owner
                    and (module_relative, table_owner) not in COMPOSITION_ROOT_EXCEPTIONS
                ):
                    violations.append(f"foreign metadata table: {node.slice.value}")

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in WRITE_SQL.finditer(node.value):
                table = match.group(1)
                table_owner = TABLE_OWNERS.get(table)
                if (
                    table_owner is not None
                    and table_owner != owner
                    and (module_relative, table_owner) not in COMPOSITION_ROOT_EXCEPTIONS
                ):
                    violations.append(f"foreign raw SQL write: {table}")
    return violations


def _repository_violations() -> list[tuple[str, str]]:
    violations: list[tuple[str, str]] = []
    for path in MODULE_ROOT.rglob("*.py"):
        relative = path.relative_to(MODULE_ROOT)
        module_relative = "/".join(relative.parts)
        owner = relative.parts[0]
        violations.extend(
            (module_relative, violation)
            for violation in _violations(
                path.read_text(),
                owner=owner,
                module_relative=module_relative,
            )
        )
    return violations


def test_modules_expose_an_explicit_public_application_interface() -> None:
    missing = sorted(
        module
        for module in REQUIRED_PUBLIC_MODULES
        if not (MODULE_ROOT / module / "api.py").is_file()
    )
    assert missing == []


def test_private_cross_module_imports_and_table_writes_are_rejected() -> None:
    assert _repository_violations() == []


def test_architecture_rule_catches_common_bypass_forms() -> None:
    source = """
import daily_insights_api.modules.assets.models
from daily_insights_api.modules.tenancy import Organization

asset_table = Base.metadata.tables["assets"]
database.execute("UPDATE organizations SET name = 'wrong'")
"""
    assert _violations(source, owner="chat", module_relative="chat/service.py") == [
        "private import: daily_insights_api.modules.assets.models",
        "private import: daily_insights_api.modules.tenancy",
        "foreign metadata table: assets",
        "foreign raw SQL write: organizations",
    ]


def test_public_api_imports_remain_allowed() -> None:
    source = "from daily_insights_api.modules.assets.api import AssetForSigning"
    assert _violations(source, owner="podcasts", module_relative="podcasts/service.py") == []
