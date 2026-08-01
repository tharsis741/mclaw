# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve and materialize external Skill sources for install_prepare."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

import httpx

from mclaw.agent.skill_utils import parse_frontmatter
from mclaw.skills_hub.search import CLAW_HUB_API_BASE, ClawHubSearcher, ClawHubSearchError
from mclaw.tools.cancellation import cancellation_checkpoint


@dataclass(frozen=True)
class ResolvedSource:
    """Normalized source descriptor consumed by install materialization."""

    type: str
    original: str
    fetch_url: str
    package_root: str = ""
    owner: str = ""
    repo: str = ""
    slug: str = ""
    catalog_id: str = ""
    local_path: Path | None = None


class SourceResolutionError(ValueError):
    """Raised when install_prepare cannot resolve or copy a source."""


def _run_http(coro, *, parent_agent, timeout: float, operation: str):
    from mclaw.tools.dispatch import _run_async

    return _run_async(
        coro,
        parent_agent=parent_agent,
        diagnostic_name=f"skill_manage_{operation}_http",
        timeout_seconds=timeout,
        raise_on_stop=True,
    )


async def _get_json_async(url: str, *, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with asyncio.timeout(max(0.001, timeout)):
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Remote source returned a non-object response.")
    return data


def resolve_source(
    source: str,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> ResolvedSource:
    """Resolve a user-provided Skill source into a trusted fetch/copy plan."""
    cancellation_checkpoint(cancel_event)
    value = str(source or "").strip().strip('"')
    if not value:
        raise SourceResolutionError("source is required.")

    local = Path(value).expanduser()
    if local.exists():
        cancellation_checkpoint(cancel_event)
        if local.is_symlink():
            raise SourceResolutionError("Symlinks are not allowed in Skill source packages.")
        if not local.is_dir():
            raise SourceResolutionError("local source must be a directory.")
        result = ResolvedSource(type="local", original=value, fetch_url=str(local), local_path=local)
        cancellation_checkpoint(cancel_event)
        return result

    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host in {"github.com", "www.github.com"}:
        return _resolve_github(
            value,
            parsed,
            cancel_event=cancel_event,
            parent_agent=parent_agent,
        )
    if host in {"clawhub.ai", "www.clawhub.ai"}:
        return _resolve_clawhub(
            value,
            parsed,
            cancel_event=cancel_event,
            parent_agent=parent_agent,
        )
    if host in {"skills.sh", "www.skills.sh"}:
        return _resolve_skills_sh(
            value,
            parsed,
            cancel_event=cancel_event,
            parent_agent=parent_agent,
        )
    raise SourceResolutionError(
        "Unsupported skill source. Use skills.sh, GitHub, ClawHub, or a local directory."
    )


_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SKILL_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})$")


def _resolve_skills_sh(
    original: str,
    parsed,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> ResolvedSource:
    """Resolve a skills.sh catalog page to its public GitHub repository."""

    cancellation_checkpoint(cancel_event)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 3:
        raise SourceResolutionError(
            "skills.sh URL must be https://skills.sh/<owner>/<repo>/<skill>."
        )
    owner, repo, slug = parts
    if not _GITHUB_OWNER_RE.fullmatch(owner):
        raise SourceResolutionError("skills.sh URL contains an invalid GitHub owner.")
    if repo in {".", ".."} or not _GITHUB_REPO_RE.fullmatch(repo):
        raise SourceResolutionError("skills.sh URL contains an invalid GitHub repository.")
    if slug in {".", ".."} or not _SKILL_SLUG_RE.fullmatch(slug):
        raise SourceResolutionError("skills.sh URL contains an invalid Skill slug.")

    cancellation_checkpoint(cancel_event)
    return ResolvedSource(
        type="skills_sh",
        original=original,
        # GitHub resolves HEAD to the repository's default branch.  This avoids
        # consuming the unauthenticated GitHub API rate limit merely to learn a
        # branch name, while keeping public installs credential-free.
        fetch_url=f"https://github.com/{owner}/{repo}/archive/HEAD.zip",
        owner=owner,
        repo=repo,
        slug=slug,
        catalog_id=f"{owner}/{repo}/{slug}",
    )


def _resolve_clawhub(
    original: str,
    parsed,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> ResolvedSource:
    """Resolve legacy and current ClawHub URLs through owner-qualified APIs."""
    cancellation_checkpoint(cancel_event)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) == 2:
        owner_hint, slug_hint = parts
    elif len(parts) == 3 and parts[1].lower() == "skills":
        owner_hint, slug_hint = parts[0], parts[2]
    else:
        raise SourceResolutionError(
            "ClawHub URL must be https://clawhub.ai/<owner>/skills/<slug> "
            "or the legacy https://clawhub.ai/<owner>/<slug> form."
        )
    if any(
        not value or value in {".", ".."} or "/" in value or "\\" in value
        for value in (owner_hint, slug_hint)
    ):
        raise SourceResolutionError("ClawHub URL contains an invalid owner or Skill slug.")

    try:
        detail = ClawHubSearcher().get_skill_detail(
            slug_hint,
            owner_handle=owner_hint,
            raise_on_error=True,
            cancel_event=cancel_event,
            parent_agent=parent_agent,
        )
    except ClawHubSearchError as exc:
        raise SourceResolutionError(f"ClawHub detail lookup failed for slug '{slug_hint}'.") from exc
    skill = detail.get("skill") if isinstance(detail, dict) else {}
    if not isinstance(skill, dict):
        skill = {}
    if not skill:
        raise SourceResolutionError(f"ClawHub skill not found: {slug_hint}")
    slug = str(skill.get("slug") or slug_hint).strip()
    if not slug:
        raise SourceResolutionError(f"ClawHub skill not found: {slug_hint}")

    owner_data = detail.get("owner") if isinstance(detail, dict) else {}
    if not isinstance(owner_data, dict):
        owner_data = {}
    owner = str(owner_data.get("handle") or skill.get("ownerHandle") or owner_hint).strip()
    if owner and owner_hint and owner.lower() != owner_hint.lower():
        raise SourceResolutionError(
            f"ClawHub URL owner mismatch: URL owner '{owner_hint}' does not match API owner '{owner}'."
        )

    fetch_url = (
        f"{CLAW_HUB_API_BASE}/download?slug={quote(slug)}"
        f"&ownerHandle={quote(owner or owner_hint)}"
    )
    cancellation_checkpoint(cancel_event)
    return ResolvedSource(
        type="clawhub",
        original=original,
        fetch_url=fetch_url,
        owner=owner or owner_hint,
        slug=slug,
    )


def _resolve_github(
    original: str,
    parsed,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> ResolvedSource:
    """Resolve GitHub repo/tree/blob URLs into codeload archive metadata."""
    cancellation_checkpoint(cancel_event)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise SourceResolutionError("GitHub URL must include owner and repo.")
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    package_root = ""
    branch = ""
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
        package_root = "/".join(parts[4:])
    elif len(parts) >= 4 and parts[2] == "blob":
        branch = parts[3]
        blob_path = parts[4:]
        if blob_path and blob_path[-1].lower() == "skill.md":
            blob_path = blob_path[:-1]
        package_root = "/".join(blob_path)
    if not branch:
        branch = _github_default_branch(
            owner,
            repo,
            cancel_event=cancel_event,
            parent_agent=parent_agent,
        ) or "main"
    cancellation_checkpoint(cancel_event)
    fetch_url = f"https://codeload.github.com/{owner}/{repo}/zip/{quote(branch, safe='')}"
    return ResolvedSource(
        type="github",
        original=original,
        fetch_url=fetch_url,
        package_root=package_root,
        owner=owner,
        repo=repo,
    )


def _github_default_branch(
    owner: str,
    repo: str,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> str:
    """Best-effort default-branch lookup; callers fall back when unavailable."""
    cancellation_checkpoint(cancel_event)
    try:
        data = _run_http(
            _get_json_async(
                f"https://api.github.com/repos/{owner}/{repo}",
                timeout=15,
            ),
            parent_agent=parent_agent,
            timeout=15,
            operation="github_resolve",
        )
        cancellation_checkpoint(cancel_event)
        return str(data.get("default_branch") or "").strip()
    except (httpx.HTTPError, TimeoutError, ValueError):
        cancellation_checkpoint(cancel_event)
        return ""
    return ""


def materialize_source(
    resolved: ResolvedSource,
    target_dir: Path,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> None:
    """Copy/download a resolved source into target_dir, preserving its package layout."""
    cancellation_checkpoint(cancel_event)
    target_dir.mkdir(parents=True, exist_ok=True)
    cancellation_checkpoint(cancel_event)
    if resolved.type == "local":
        if resolved.local_path is None:
            raise SourceResolutionError("local_path is missing.")
        _copy_dir_contents(resolved.local_path, target_dir, cancel_event=cancel_event)
        cancellation_checkpoint(cancel_event)
        return
    download_kwargs = {"cancel_event": cancel_event}
    if parent_agent is not None:
        download_kwargs["parent_agent"] = parent_agent
    archive_path = _download_archive(resolved.fetch_url, **download_kwargs)
    try:
        cancellation_checkpoint(cancel_event)
        with tempfile.TemporaryDirectory(prefix="mclaw_skill_src_") as tmp_name:
            tmp = Path(tmp_name)
            with zipfile.ZipFile(archive_path) as zf:
                _safe_extract_archive(zf, tmp, cancel_event=cancel_event)
            cancellation_checkpoint(cancel_event)
            if resolved.type == "skills_sh":
                package = _find_skill_package_root(
                    tmp,
                    resolved.slug,
                    cancel_event=cancel_event,
                )
            else:
                package = _find_archive_package_root(
                    tmp,
                    resolved.package_root,
                    cancel_event=cancel_event,
                )
            cancellation_checkpoint(cancel_event)
            _copy_dir_contents(package, target_dir, cancel_event=cancel_event)
            cancellation_checkpoint(cancel_event)
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass


def _find_archive_package_root(
    extracted_root: Path,
    package_root: str = "",
    *,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Find the Skill package root in either GitHub-style or flat archives."""
    cancellation_checkpoint(cancel_event)
    package_root = str(package_root or "").strip("/")
    roots = []
    for path in extracted_root.iterdir():
        cancellation_checkpoint(cancel_event)
        if path.is_dir():
            roots.append(path)

    if package_root:
        direct = extracted_root / package_root
        if direct.is_dir():
            return direct
        for root in roots:
            cancellation_checkpoint(cancel_event)
            nested = root / package_root
            if nested.is_dir():
                return nested
        raise SourceResolutionError(f"Package root not found in archive: {package_root}")

    if (extracted_root / "SKILL.md").is_file():
        return extracted_root
    if len(roots) == 1:
        return roots[0]
    if not roots:
        raise SourceResolutionError("Downloaded archive did not contain SKILL.md or a package directory.")
    raise SourceResolutionError("Downloaded archive contains multiple package directories; provide a package root.")


def _normalize_skill_selector(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _skill_frontmatter_name(skill_md: Path) -> str:
    """Read only the YAML frontmatter name needed for package selection."""

    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    frontmatter, _ = parse_frontmatter(text)
    return str(frontmatter.get("name") or "").strip()


def _find_skill_package_root(
    extracted_root: Path,
    skill_slug: str,
    *,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Find one unambiguous SKILL.md package selected by a skills.sh slug."""

    cancellation_checkpoint(cancel_event)
    selector = _normalize_skill_selector(skill_slug)
    if not selector:
        raise SourceResolutionError("skills.sh Skill slug is missing.")

    name_matches: list[Path] = []
    directory_matches: list[Path] = []
    for skill_md in sorted(extracted_root.rglob("SKILL.md")):
        cancellation_checkpoint(cancel_event)
        if not skill_md.is_file():
            continue
        package = skill_md.parent
        frontmatter_name = _normalize_skill_selector(_skill_frontmatter_name(skill_md))
        directory_name = _normalize_skill_selector(package.name)
        if frontmatter_name == selector:
            name_matches.append(package)
        elif directory_name == selector:
            directory_matches.append(package)

    matches = name_matches or directory_matches
    unique_matches = list(dict.fromkeys(path.resolve() for path in matches))
    cancellation_checkpoint(cancel_event)
    if not unique_matches:
        raise SourceResolutionError(
            f"Skill '{skill_slug}' was not found in the skills.sh source repository."
        )
    if len(unique_matches) > 1:
        relative = []
        root = extracted_root.resolve()
        for path in unique_matches[:5]:
            try:
                relative.append(path.relative_to(root).as_posix())
            except ValueError:
                relative.append(path.as_posix())
        raise SourceResolutionError(
            f"Skill '{skill_slug}' is ambiguous in the source repository: {', '.join(relative)}"
        )
    return unique_matches[0]


async def _download_archive_async(
    url: str,
    path: Path,
    *,
    cancel_event: threading.Event | None,
    timeout: float,
) -> Path:
    completed = False
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with asyncio.timeout(max(0.001, timeout)):
                async with client.stream("GET", url) as response:
                    if not response.is_success:
                        raise SourceResolutionError(
                            f"Download failed: HTTP {response.status_code}"
                        )
                    with path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 128):
                            cancellation_checkpoint(cancel_event)
                            if chunk:
                                handle.write(chunk)
                        cancellation_checkpoint(cancel_event)
        completed = True
        return path
    finally:
        if not completed:
            try:
                path.unlink()
            except OSError:
                pass


def _download_archive(
    url: str,
    *,
    cancel_event: threading.Event | None = None,
    parent_agent=None,
) -> Path:
    """Download an archive to a temporary file and delete partial files on errors."""
    cancellation_checkpoint(cancel_event)
    fd, tmp_path = tempfile.mkstemp(prefix="mclaw_skill_download_", suffix=".zip")
    try:
        import os
        os.close(fd)
    except OSError:
        pass
    path = Path(tmp_path)
    try:
        return _run_http(
            _download_archive_async(
                url,
                path,
                cancel_event=cancel_event,
                timeout=60,
            ),
            parent_agent=parent_agent,
            timeout=60,
            operation="archive_download",
        )
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _safe_extract_archive(
    zf: zipfile.ZipFile,
    target_root: Path,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    """Extract zip members while rejecting absolute paths and traversal."""
    root = target_root.resolve()
    for member in zf.infolist():
        cancellation_checkpoint(cancel_event)
        raw_name = str(member.filename or "").replace("\\", "/")
        parts = PurePosixPath(raw_name).parts
        if (
            not raw_name
            or raw_name.startswith("/")
            or ".." in parts
            or any(part.endswith(":") for part in parts)
        ):
            raise SourceResolutionError(f"Unsafe archive path: {member.filename}")
        target = (root / Path(*parts)).resolve()
        if not target.is_relative_to(root):
            raise SourceResolutionError(f"Unsafe archive path: {member.filename}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member, "r") as src, target.open("wb") as dst:
            while True:
                cancellation_checkpoint(cancel_event)
                chunk = src.read(1024 * 128)
                if not chunk:
                    break
                dst.write(chunk)
        cancellation_checkpoint(cancel_event)


def _copy_dir_contents(
    src: Path,
    dst: Path,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    """Copy package contents while rejecting symlinks and local cache artifacts."""
    ignore_names = {".git", "__pycache__", ".DS_Store"}
    for item in src.iterdir():
        cancellation_checkpoint(cancel_event)
        if item.name in ignore_names:
            continue
        target = dst / item.name
        if item.is_symlink():
            raise SourceResolutionError("Symlinks are not allowed in Skill source packages.")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_dir_contents(item, target, cancel_event=cancel_event)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        cancellation_checkpoint(cancel_event)
