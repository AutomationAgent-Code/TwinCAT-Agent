"""
Data models for template metadata, variables, GUID strategy, hooks, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TemplateError(Exception):
    """Base exception for template errors."""


class TemplateNotFoundError(TemplateError):
    """Template not found in repository."""


class TemplateValidationError(TemplateError):
    """template.yaml validation failed."""


class VariableValidationError(TemplateError):
    """Variable input validation failed."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Variable:
    """An interactive prompt that collects a value from the user.

    Its ``{{NAME}}`` placeholder is replaced during scaffolding.
    """
    name: str
    prompt: str
    description: str = ""
    default: str = ""
    required: bool = True
    pattern: str | None = None
    error_msg: str = ""

    def validate(self, value: str) -> str | None:
        """Return error message or None if valid."""
        if not value and self.required:
            return f"'{self.name}' is required"
        if self.pattern and value:
            if not re.fullmatch(self.pattern, value):
                return self.error_msg or f"'{self.name}' must match: {self.pattern}"
        return None


@dataclass
class ConditionalFile:
    """A group of files included/excluded based on user choice."""
    name: str
    prompt: str
    type: str = "boolean"
    default: Any = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class GUIDStrategy:
    """How GUIDs are handled: placeholder substitution or scan-and-replace."""
    strategy: str = "placeholder"      # "placeholder" | "scan-replace"
    exclude_prefixes: list[str] = field(default_factory=lambda: ["{18071995-"])
    exclude_exact: list[str] = field(default_factory=list)


@dataclass
class CompositionConfig:
    """How this template merges with others during composition."""
    merge_mode: str = "merge"          # "merge" | "overlay" | "ask"
    priority_files: list[str] = field(default_factory=list)


@dataclass
class Hook:
    """Shell command run before/after scaffolding or extraction."""
    cmd: str
    condition: str | None = None


@dataclass
class FileRules:
    """File processing rules for scaffolding."""
    text_extensions: list[str] = field(default_factory=lambda: [
        ".sln", ".tsproj", ".plcproj",
        ".TcPOU", ".TcDUT", ".TcIO", ".TcTTO",
        ".TcGTLO", ".TcVMO", ".TcVIS",
        ".tmc", ".xti", ".xml", ".txt", ".json", ".csv",
    ])
    binary_extensions: list[str] = field(default_factory=lambda: [
        ".library", ".xlsx", ".suo", ".png", ".jpg",
    ])
    skip_files: list[str] = field(default_factory=lambda: [
        "template.yaml", "_GUID_map.json",
    ])
    skip_dirs: list[str] = field(default_factory=lambda: [
        ".git", ".vs", "_CompileInfo",
    ])


@dataclass
class TemplateMetadata:
    """Complete metadata for a single template (deserialized from template.yaml)."""
    name: str
    version: str = "0.1.0"
    display_name: str = ""
    description: str = ""
    author: str = ""
    license: str = "MIT"
    tags: list[str] = field(default_factory=list)
    category: str = "plc"
    template_dir: str = "template"
    depends_on: list[str] = field(default_factory=list)
    conflicts_with: list[str] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    conditional_files: list[ConditionalFile] = field(default_factory=list)
    guid: GUIDStrategy = field(default_factory=GUIDStrategy)
    composition: CompositionConfig = field(default_factory=CompositionConfig)
    hooks: dict[str, list[Hook]] = field(default_factory=dict)
    file_rules: FileRules = field(default_factory=FileRules)

    # ---- YAML serialization ----

    @classmethod
    def from_yaml(cls, path: Path) -> "TemplateMetadata":
        """Load from a template.yaml file."""
        if not path.exists():
            raise TemplateNotFoundError(f"Template file not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if raw is None:
            raise TemplateValidationError(f"Empty template file: {path}")
        return cls._from_dict(raw)

    @classmethod
    def from_yaml_string(cls, content: str) -> "TemplateMetadata":
        """Load from YAML string."""
        raw = yaml.safe_load(content)
        if raw is None:
            raise TemplateValidationError("Empty YAML content")
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, d: dict) -> "TemplateMetadata":
        if "name" not in d:
            raise TemplateValidationError("template.yaml must contain 'name'")

        variables = [Variable(**v) for v in d.get("variables", [])]
        conditional_files = [ConditionalFile(**cf) for cf in d.get("conditional_files", [])]

        guid_raw = d.get("guid", {})
        guid = GUIDStrategy(
            strategy=guid_raw.get("strategy", "placeholder"),
            exclude_prefixes=guid_raw.get("exclude_prefixes", ["{18071995-"]),
            exclude_exact=guid_raw.get("exclude_exact", []),
        )

        comp_raw = d.get("composition", {})
        composition = CompositionConfig(
            merge_mode=comp_raw.get("merge_mode", "merge"),
            priority_files=comp_raw.get("priority_files", []),
        )

        hooks: dict[str, list[Hook]] = {}
        for key in ("pre_create", "post_create", "pre_extract", "post_extract"):
            hooks[key] = [Hook(**h) for h in d.get("hooks", {}).get(key, [])]

        fr_raw = d.get("file_rules", {})
        file_rules = FileRules(
            text_extensions=fr_raw.get("text_extensions", FileRules().text_extensions),
            binary_extensions=fr_raw.get("binary_extensions", FileRules().binary_extensions),
            skip_files=fr_raw.get("skip_files", FileRules().skip_files),
            skip_dirs=fr_raw.get("skip_dirs", FileRules().skip_dirs),
        )

        return cls(
            name=d["name"],
            version=str(d.get("version", "0.1.0")),
            display_name=d.get("display_name", ""),
            description=d.get("description", ""),
            author=d.get("author", ""),
            license=d.get("license", "MIT"),
            tags=d.get("tags", []),
            category=d.get("category", "plc"),
            template_dir=d.get("template_dir", "template"),
            depends_on=d.get("depends_on", []),
            conflicts_with=d.get("conflicts_with", []),
            variables=variables,
            conditional_files=conditional_files,
            guid=guid,
            composition=composition,
            hooks=hooks,
            file_rules=file_rules,
        )

    def to_yaml(self) -> str:
        """Serialize back to YAML string."""
        return yaml.dump(self.to_dict(), default_flow_style=False,
                         allow_unicode=True, sort_keys=False)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "license": self.license,
            "tags": self.tags,
            "category": self.category,
            "template_dir": self.template_dir,
            "depends_on": self.depends_on,
            "conflicts_with": self.conflicts_with,
            "variables": [_v2d(v) for v in self.variables],
            "conditional_files": [_cf2d(cf) for cf in self.conditional_files],
            "guid": {
                "strategy": self.guid.strategy,
                "exclude_prefixes": self.guid.exclude_prefixes,
                "exclude_exact": self.guid.exclude_exact,
            },
            "composition": {
                "merge_mode": self.composition.merge_mode,
                "priority_files": self.composition.priority_files,
            },
            "hooks": {k: [{"cmd": h.cmd, "condition": h.condition} for h in v]
                      for k, v in self.hooks.items()},
            "file_rules": {
                "text_extensions": self.file_rules.text_extensions,
                "binary_extensions": self.file_rules.binary_extensions,
                "skip_files": self.file_rules.skip_files,
                "skip_dirs": self.file_rules.skip_dirs,
            },
        }

    # ---- Validation ----

    def validate(self) -> list[str]:
        """Return list of issues (empty = valid)."""
        issues: list[str] = []
        if not self.name:
            issues.append("name is required")
        elif not re.fullmatch(r"^[a-z][a-z0-9_-]*$", self.name):
            issues.append(f"name '{self.name}' must be kebab-case")
        if not self.version:
            issues.append("version is required")

        names: set[str] = set()
        for v in self.variables:
            if not v.name:
                issues.append("variable has empty name")
            elif not re.fullmatch(r"^[A-Z][A-Z0-9_]*$", v.name):
                issues.append(f"variable '{v.name}' must be SCREAMING_SNAKE_CASE")
            if v.name in names:
                issues.append(f"duplicate variable: {v.name}")
            names.add(v.name)
            if v.pattern:
                try:
                    re.compile(v.pattern)
                except re.error as e:
                    issues.append(f"variable '{v.name}' bad regex: {e}")

        if self.guid.strategy not in ("placeholder", "scan-replace"):
            issues.append(f"guid.strategy must be 'placeholder' or 'scan-replace'")
        if self.composition.merge_mode not in ("merge", "overlay", "ask"):
            issues.append(f"composition.merge_mode must be 'merge', 'overlay', or 'ask'")

        for key in ("pre_create", "post_create", "pre_extract", "post_extract"):
            for h in self.hooks.get(key, []):
                if not h.cmd:
                    issues.append(f"hook in {key} has empty cmd")

        return issues


def _v2d(v: Variable) -> dict:
    d: dict = {"name": v.name, "prompt": v.prompt}
    if v.description:
        d["description"] = v.description
    if v.default:
        d["default"] = v.default
    if not v.required:
        d["required"] = False
    if v.pattern:
        d["pattern"] = v.pattern
    if v.error_msg:
        d["error_msg"] = v.error_msg
    return d


def _cf2d(cf: ConditionalFile) -> dict:
    return {
        "name": cf.name, "prompt": cf.prompt, "type": cf.type,
        "default": cf.default, "include": cf.include, "exclude": cf.exclude,
    }
