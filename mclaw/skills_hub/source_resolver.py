# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve and materialize external Skill sources for install_prepare."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import requests

from mclaw.skills_hub.search import CLAW_HUB_API_BASE, ClawHubSearcher, ClawHubSearchError


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
    local_path: Path | None = None


class SourceResolutionError(ValueError):
    """Raised when install_prepare cannot resolve or copy a source."""


def resolve_source(source: str) -> ResolvedSource:
    """Resolve a user-provided Skill source into a trusted fetch/copy plan."""
    value = str(source or "").strip().strip('"')
    if not value:
        raise SourceResolutionError("source is required.")

    local = Path(value).expanduser()
    if local.exists():
        if local.is_symlink():
            raise SourceResolutionError("Symlinks are not allowed in Skill source packages.")
        if not local.is_dir():
            raise SourceResolutionError("local source must be a directory.")
        return ResolvedSource(type="local", original=value, fetch_url=str(local), local_path=local)

    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host in {"github.com", "www.github.com"}:
        return _resolve_github(value, parsed)
    if host in {"clawhub.ai", "www.clawhub.ai"}:
        return _resolve_clawhub(value, parsed)
    raise SourceResolutionError("Unsupported skill source. Use GitHub, ClawHub, or a local directory.")


def _resolve_clawhub(original: str, parsed) -> ResolvedSource:
    """Resolve ClawHub URLs through the API so owner and slug are canonical."""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise SourceResolutionError("ClawHub URL must be https://clawhub.ai/<owner>/<slug>.")
    owner_hint, slug_hint = parts[0], parts[1]

    try:
        detail = ClawHubSearcher().get_skill_detail(slug_hint, raise_on_error=True)
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

    fetch_url = f"{CLAW_HUB_API_BASE}/download?slug={quote(slug)}"
    return ResolvedSource(
        type="clawhub",
        original=original,
        fetch_url=fetch_url,
        owner=owner or owner_hint,
        slug=slug,
    )


def _resolve_github(original: str, parsed) -> ResolvedSource:
    """Resolve GitHub repo/tree/blob URLs into codeload archive metadata."""
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
        branch = _github_default_branch(owner, repo) or "main"
    fetch_url = f"https://codeload.github.com/{owner}/{repo}/zip/{quote(branch, safe='')}"
    return ResolvedSource(
        type="github",
        original=original,
        fetch_url=fetch_url,
        package_root=package_root,
        owner=owner,
        repo=repo,
    )


def _github_default_branch(owner: str, repo: str) -> str:
    """Best-effort default-branch lookup; callers fall back when unavailable."""
    try:
        response = requests.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=15)
        if response.ok:
            data = response.json()
            return str(data.get("default_branch") or "").strip()
    except (requests.RequestException, ValueError):
        return ""
    return ""


def materialize_source(resolved: ResolvedSource, target_dir: Path) -> None:
    """Copy/download a resolved source into target_dir, preserving its package layout."""
    target_dir.mkdir(parents=True, exist_ok=True)
    if resolved.type == "local":
        if resolved.local_path is None:
            raise SourceResolutionError("local_path is missing.")
        _copy_dir_contents(resolved.local_path, target_dir)
        return
    archive_path = _download_archive(resolved.fetch_url)
    try:
        with tempfile.TemporaryDirectory(prefix="mclaw_skill_src_") as tmp_name:
            tmp = Path(tmp_name)
            with zipfile.ZipFile(archive_path) as zf:
                _safe_extract_archive(zf, tmp)
            package = _find_archive_package_root(tmp, resolved.package_root)
            _copy_dir_contents(package, target_dir)
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass


def _find_archive_package_root(extracted_root: Path, package_root: str = "") -> Path:
    """Find the Skill package root in either GitHub-style or flat archives."""
    package_root = str(package_root or "").strip("/")
    roots = [p for p in extracted_root.iterdir() if p.is_dir()]

    if package_root:
        direct = extracted_root / package_root
        if direct.is_dir():
            return direct
        for root in roots:
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


def _download_archive(url: str) -> Path:
    """Download an archive to a temporary file and delete partial files on errors."""
    fd, tmp_path = tempfile.mkstemp(prefix="mclaw_skill_download_", suffix=".zip")
    try:
        import os
        os.close(fd)
    except OSError:
        pass
    path = Path(tmp_path)
    try:
        with requests.get(url, timeout=60, stream=True) as response:
            if not response.ok:
                raise SourceResolutionError(f"Download failed: HTTP {response.status_code}")
            with open(path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        handle.write(chunk)
        return path
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _safe_extract_archive(zf: zipfile.ZipFile, target_root: Path) -> None:
    """Extract zip members while rejecting absolute paths and traversal."""
    root = target_root.resolve()
    for member in zf.infolist():
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
            shutil.copyfileobj(src, dst)


def _copy_dir_contents(src: Path, dst: Path) -> None:
    """Copy package contents while rejecting symlinks and local cache artifacts."""
    ignore_names = {".git", "__pycache__", ".DS_Store"}
    for item in src.iterdir():
        if item.name in ignore_names:
            continue
        target = dst / item.name
        if item.is_symlink():
            raise SourceResolutionError("Symlinks are not allowed in Skill source packages.")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_dir_contents(item, target)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
