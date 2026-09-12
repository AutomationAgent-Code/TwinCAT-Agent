"""Git-backed PLC source staging for the open TwinCAT XAE instance.

This module deliberately keeps Git working-tree operations separate from the
XAE project directory.  A repository is pulled into a normal local checkout,
then the caller can push the parsed PLC source through the Automation
Interface.  That is the important distinction for an open XAE: changing
``.TcPOU`` files on disk does not reliably update the in-memory project tree.

The supported source format is the native TwinCAT XML source format:
``.TcPOU``, ``.TcGVL`` and ``.TcDUT``.  Repository scripts are not invoked by
this module; only the Git executable is called with an argument list, never
through a shell.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET


SUPPORTED_SUFFIXES = {".tcpou", ".tcgvl", ".tcdut"}
EXCLUDED_DIRS = {
    ".git", ".vs", "_boot", "boot", "_compileinfo", "_libraries",
    ".twincatagent", ".codex", "bin", "obj",
}
_OBJECT_TAGS = {"pou", "gvl", "dut", "interface"}
_MEMBER_TAGS = {"method", "action", "property", "transition", "get", "set"}
_REMOTE_RE = re.compile(r"^(?:https://|ssh://|git@)", re.IGNORECASE)


class GitSyncError(ValueError):
    """A repository or source manifest cannot be safely synchronized."""


@dataclass(frozen=True)
class GitMember:
    path: str
    name: str
    kind: str
    declaration: str
    implementation: str
    return_type: str = ""


@dataclass(frozen=True)
class GitSource:
    file: Path
    relative_file: str
    name: str
    kind: str
    declaration: str
    implementation: str
    members: tuple[GitMember, ...]
    create_type: str = ""
    return_type: str = ""

    @property
    def hashes(self) -> dict[str, str]:
        return {
            "declaration": sha256_text(self.declaration),
            "implementation": sha256_text(self.implementation),
        }


def normalize_text(value: object) -> str:
    """Match XAE's line-oriented comparison without changing source tokens."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def sha256_text(value: object) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def equivalent(left: object, right: object) -> bool:
    return normalize_text(left) == normalize_text(right)


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].casefold()


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return normalize_text("".join(element.itertext()))


def _child(element: ET.Element, name: str) -> ET.Element | None:
    wanted = name.casefold()
    return next((item for item in list(element) if _local_name(item.tag) == wanted), None)


def _code(element: ET.Element) -> tuple[str, str]:
    declaration = _element_text(_child(element, "Declaration"))
    implementation = ""
    impl = _child(element, "Implementation")
    if impl is not None:
        st = next((item for item in impl.iter() if _local_name(item.tag) == "st"), None)
        implementation = _element_text(st if st is not None else impl)
    return declaration, implementation


def _return_type(declaration: str, name: str) -> str:
    match = re.search(
        rf"(?im)^\s*(?:FUNCTION|METHOD|PROPERTY)\s+{re.escape(name)}\b\s*(?::\s*([A-Za-z_][A-Za-z0-9_.]*))?",
        declaration,
    )
    return str(match.group(1) or "") if match else ""


def _infer_dut_type(declaration: str) -> str:
    body = str(declaration or "")
    if re.search(r"(?is)\bSTRUCT\b", body):
        return "struct"
    if re.search(r"(?is)\bUNION\b", body):
        return "union"
    # Enum declarations are TYPE Name : (...);.  Match the opening parenthesis
    # after the type separator instead of mistaking a comment for an enum.
    if re.search(r"(?is)^\s*TYPE\s+[A-Za-z_]\w*\s*:\s*\(", body):
        return "enum"
    return "alias"


def _flatten_members(parent: ET.Element, prefix: str = "") -> list[GitMember]:
    result: list[GitMember] = []
    for element in list(parent):
        kind = _local_name(element.tag)
        if kind not in _MEMBER_TAGS:
            continue
        name = str(element.attrib.get("Name") or kind.title()).strip()
        path = f"{prefix}.{name}" if prefix else name
        declaration, implementation = _code(element)
        result.append(GitMember(
            path=path, name=name, kind=kind,
            declaration=declaration, implementation=implementation,
            return_type=_return_type(declaration, name),
        ))
        result.extend(_flatten_members(element, path))
    return result


def parse_source_file(file: str | Path, *, repo_root: str | Path | None = None) -> GitSource:
    """Parse one native TwinCAT XML source file into a syncable object."""
    source_file = Path(file).resolve()
    try:
        root = ET.parse(source_file).getroot()
    except (OSError, ET.ParseError) as exc:
        raise GitSyncError(f"无法解析 PLC 源文件 {source_file}: {exc}") from exc

    object_element = next(
        (item for item in root.iter() if _local_name(item.tag) in _OBJECT_TAGS),
        None,
    )
    if object_element is None:
        raise GitSyncError(f"PLC 源文件没有找到 POU/GVL/DUT/Interface 节点: {source_file}")
    name = str(object_element.attrib.get("Name") or "").strip()
    if not name:
        raise GitSyncError(f"PLC 源文件对象缺少 Name 属性: {source_file}")

    declaration, implementation = _code(object_element)
    kind = _local_name(object_element.tag)
    create_type = ""
    return_type = ""
    if kind == "pou":
        if re.search(r"(?im)^\s*PROGRAM\b", declaration):
            create_type = "program"
        elif re.search(r"(?im)^\s*FUNCTION_BLOCK\b", declaration):
            create_type = "fb"
        elif re.search(r"(?im)^\s*FUNCTION\b", declaration):
            create_type = "function"
            return_type = _return_type(declaration, name)
        else:
            raise GitSyncError(
                f"POU '{name}' 的声明无法识别 PROGRAM/FUNCTION_BLOCK/FUNCTION: {source_file}"
            )
    elif kind == "gvl":
        create_type = "gvl"
    elif kind == "dut":
        create_type = _infer_dut_type(declaration)
    elif kind == "interface":
        create_type = "interface"

    root_path = Path(repo_root).resolve() if repo_root else source_file.parent
    try:
        relative = source_file.relative_to(root_path).as_posix()
    except ValueError as exc:
        raise GitSyncError(f"源文件不在仓库目录内: {source_file}") from exc
    return GitSource(
        file=source_file, relative_file=relative, name=name, kind=kind,
        declaration=declaration, implementation=implementation,
        members=tuple(_flatten_members(object_element)),
        create_type=create_type, return_type=return_type,
    )


def _under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def discover_sources(
    repo_dir: str | Path,
    *,
    source_subdir: str = "",
    objects: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[GitSource], list[dict[str, str]]]:
    """Discover and parse native PLC source files below a checked-out repo."""
    root = Path(repo_dir).resolve()
    if not root.is_dir():
        raise GitSyncError(f"Git 仓库目录不存在: {root}")
    base = (root / source_subdir).resolve() if source_subdir else root
    if not _under(root, base) or not base.is_dir():
        raise GitSyncError(f"source_subdir 不在仓库目录内或不存在: {source_subdir}")
    wanted = {str(item).casefold() for item in (objects or []) if str(item).strip()}
    sources: list[GitSource] = []
    errors: list[dict[str, str]] = []
    for file in sorted(base.rglob("*"), key=lambda item: str(item).casefold()):
        if not file.is_file() or file.suffix.casefold() not in SUPPORTED_SUFFIXES:
            continue
        try:
            relative_parts = file.relative_to(root).parts
        except ValueError:
            continue
        if any(part.casefold() in EXCLUDED_DIRS for part in relative_parts):
            continue
        try:
            source = parse_source_file(file, repo_root=root)
        except GitSyncError as exc:
            errors.append({"file": str(file), "error": str(exc)})
            continue
        if wanted and source.name.casefold() not in wanted:
            continue
        sources.append(source)

    seen: dict[str, GitSource] = {}
    for source in sources:
        key = source.name.casefold()
        if key in seen:
            errors.append({
                "file": source.relative_file,
                "error": f"对象名重复: {source.name}（已有 {seen[key].relative_file}）",
            })
        else:
            seen[key] = source
    if not sources and not errors:
        raise GitSyncError(f"仓库中没有可同步的 TwinCAT PLC 源文件: {base}")
    return sources, errors


def is_remote_repository(value: str) -> bool:
    text = str(value or "").strip()
    if not _REMOTE_RE.match(text):
        return False
    if text.lower().startswith("git@"):
        return ":" in text
    parsed = urlsplit(text)
    return parsed.scheme.casefold() in {"https", "ssh"} and bool(parsed.netloc)


def _redact(text: str) -> str:
    value = str(text or "")
    # Do not echo embedded HTTPS credentials or query strings in Git errors.
    value = re.sub(r"(https?://)([^/@\s]+)@", r"\1***@", value, flags=re.IGNORECASE)
    return re.sub(r"([?&](?:token|access_token|password|passwd)=)[^&\s]+", r"\1***", value, flags=re.IGNORECASE)


def _run_git(repo_dir: Path | None, args: list[str], *, timeout: float = 120.0) -> str:
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command, cwd=str(repo_dir) if repo_dir else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GitSyncError("未找到 git 可执行文件，请先安装 Git 并加入 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitSyncError(f"Git 操作超时: git {' '.join(args[:3])}") from exc
    if completed.returncode != 0:
        detail = _redact((completed.stderr or completed.stdout or "").strip())
        raise GitSyncError(
            f"Git 操作失败 (exit {completed.returncode}): {detail or '无错误输出'}"
        )
    return (completed.stdout or "").strip()


def _is_git_dir(path: Path) -> bool:
    return (path / ".git").is_dir() or (path / ".git").is_file()


def _origin_matches(repo_dir: Path, requested: str) -> bool:
    try:
        origin = _run_git(repo_dir, ["remote", "get-url", "origin"], timeout=20)
    except GitSyncError:
        return False
    def clean(value: str) -> str:
        value = value.strip().rstrip("/")
        return value[:-4] if value.casefold().endswith(".git") else value
    return clean(origin).casefold() == clean(requested).casefold()


def _clean_worktree(repo_dir: Path) -> None:
    status = _run_git(repo_dir, ["status", "--porcelain", "--untracked-files=all"], timeout=30)
    if status:
        raise GitSyncError(
            f"Git 仓库存在未提交修改，拒绝 pull 以免覆盖本地内容: {repo_dir}"
        )


def prepare_repository(
    repository: str,
    *,
    local_path: str = "",
    branch: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    """Clone/pull a repository when applying, or inspect an existing checkout."""
    requested = str(repository or "").strip()
    if not requested:
        raise GitSyncError("repository 不能为空")

    if is_remote_repository(requested):
        if not local_path:
            raise GitSyncError("远程 GitHub 仓库必须提供 local_path 作为本地工作目录")
        repo_dir = Path(local_path).expanduser().resolve()
        exists_as_repo = repo_dir.is_dir() and _is_git_dir(repo_dir)
        if not exists_as_repo:
            if repo_dir.exists() and any(repo_dir.iterdir()):
                raise GitSyncError(f"local_path 不是空目录且不是 Git 仓库: {repo_dir}")
            if not apply:
                return {
                    "status": "preview_requires_clone", "repository": requested,
                    "local_path": str(repo_dir), "branch": branch,
                    "next_action": "apply=true 时首次 clone，再执行 PLC 同步。",
                }
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            clone_args = ["clone"]
            if branch:
                clone_args.extend(["--branch", branch])
            clone_args.extend([requested, str(repo_dir)])
            _run_git(None, clone_args, timeout=300)
            operation = "cloned"
        else:
            if not _origin_matches(repo_dir, requested):
                raise GitSyncError(
                    f"local_path 的 origin 与 repository 不一致，拒绝拉取: {repo_dir}"
                )
            operation = "checked_out"
    else:
        repo_dir = Path(requested).expanduser().resolve()
        if not _is_git_dir(repo_dir):
            raise GitSyncError(f"repository 不是 Git 仓库或 GitHub URL: {repo_dir}")
        operation = "checked_out"

    current_branch = _run_git(repo_dir, ["branch", "--show-current"], timeout=20)
    if branch and current_branch != branch:
        raise GitSyncError(
            f"当前 Git 分支为 '{current_branch or '(detached)'}'，请求分支为 '{branch}'；"
            "为避免隐式切换分支，先在该工作目录切换到目标分支。"
        )
    before = _run_git(repo_dir, ["rev-parse", "HEAD"], timeout=20)
    if apply and operation != "cloned":
        _clean_worktree(repo_dir)
        pull_args = ["pull", "--ff-only"]
        if branch:
            pull_args.extend(["origin", branch])
        _run_git(repo_dir, pull_args, timeout=300)
    after = _run_git(repo_dir, ["rev-parse", "HEAD"], timeout=20)
    changed = []
    if before != after:
        changed_text = _run_git(repo_dir, ["diff", "--name-status", before, after], timeout=30)
        changed = [line for line in changed_text.splitlines() if line.strip()]
    return {
        "status": "pulled" if apply and before != after else operation,
        "repository": requested,
        "local_path": str(repo_dir),
        "branch": current_branch,
        "before": before,
        "after": after,
        "changed_files": changed,
    }
