"""
Template composition engine.

Merges multiple templates into a single virtual template for scaffolding.
Handles file merging, variable deduplication, GUID renumbering, and
conflict resolution.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TemplateMetadata


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def compose_templates(
    template_names: list[str],
    *,
    repo_dir: str | Path | None = None,
) -> "TemplateMetadata":
    """Merge multiple templates into one.

    The first template is treated as the base; subsequent templates are
    overlayed according to their ``composition.merge_mode``.

    Returns a new (virtual) TemplateMetadata representing the merged result.
    """
    ...


def resolve_dependencies(
    template_names: list[str],
    *,
    repo_dir: str | Path | None = None,
) -> list[str]:
    """Topological sort of template names, resolving depends_on chains."""
    ...


def detect_conflicts(
    templates: list["TemplateMetadata"],
) -> list[str]:
    """Check for conflicts between templates and return a list of issues."""
    ...
