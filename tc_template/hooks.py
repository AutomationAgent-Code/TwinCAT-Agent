"""
Hook execution engine for lifecycle events.

Hooks are shell commands embedded in template.yaml that run at specific
points: pre_create, post_create, pre_extract, post_extract.
Each hook can have an optional ``condition`` (Jinja2 expression).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2

if TYPE_CHECKING:
    from .models import Hook

_jinja_env = jinja2.Environment(
    loader=jinja2.BaseLoader(),
    autoescape=False,
    undefined=jinja2.StrictUndefined,
)


def run_hooks(
    hooks: list["Hook"],
    context: dict[str, str],
    *,
    dry_run: bool = False,
    cwd: Path | None = None,
    hook_label: str = "",
    allow_execution: bool = False,
) -> list[str]:
    """Execute hook commands in order.

    ``context`` provides variable values for ``{{VAR}}`` substitution.
    Returns a list of log messages.
    Raises ``subprocess.CalledProcessError`` if a hook exits non-zero.
    """
    log: list[str] = []

    for i, hook in enumerate(hooks, start=1):
        label = f"{hook_label}[{i}]" if hook_label else f"hook[{i}]"

        # Evaluate condition
        if hook.condition:
            try:
                tmpl = _jinja_env.from_string("{{ " + hook.condition + " }}")
                result = tmpl.render(**context).strip().lower()
                if result not in ("true", "1", "yes"):
                    log.append(f"  (skip) {label}: condition=False")
                    continue
            except jinja2.TemplateError as e:
                log.append(f"  (warn) {label}: condition error - {e}")
                continue

        # Render command
        try:
            tmpl = _jinja_env.from_string(hook.cmd)
            rendered = tmpl.render(**context)
        except jinja2.TemplateError as e:
            log.append(f"  (warn) {label}: template error - {e}")
            continue

        log.append(f"  -> {label}: {rendered}")

        if dry_run:
            log.append("     (dry-run, not executed)")
            continue

        if not allow_execution:
            raise PermissionError(
                f"Template hook execution is disabled by default ({label}); "
                "only explicitly trusted templates may enable it"
            )

        try:
            import shlex
            argv = shlex.split(rendered, posix=False)
            if not argv:
                raise ValueError(f"Empty hook command: {label}")
            subprocess.run(
                argv, shell=False,
                cwd=str(cwd) if cwd else None,
                check=True, capture_output=True, text=True,
            )
            log.append(f"     done")
        except subprocess.CalledProcessError as e:
            log.append(f"     FAILED (exit {e.returncode}): {e.stderr.strip()}")
            raise
        except Exception as e:
            log.append(f"     error: {e}")
            raise

    return log


def run_hooks_by_key(
    hooks_dict: dict[str, list["Hook"]],
    key: str,
    context: dict[str, str],
    *,
    dry_run: bool = False,
    cwd: Path | None = None,
    allow_execution: bool = False,
) -> list[str]:
    """Convenience: run hooks for lifecycle ``key`` (e.g. ``post_create``)."""
    hooks = hooks_dict.get(key, [])
    if not hooks:
        return []
    return run_hooks(
        hooks, context, dry_run=dry_run, cwd=cwd, hook_label=key,
        allow_execution=allow_execution,
    )
