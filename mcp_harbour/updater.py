import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__

logger = logging.getLogger("mcp_harbour.updater")

REPO = "mcpharbour/mcpharbour"
GITHUB_API = f"https://api.github.com/repos/{REPO}"
GITHUB_RELEASES = f"https://github.com/{REPO}/releases/download"


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    asset: ReleaseAsset
    update_available: bool


def normalize_tag(version: str) -> str:
    version = version.strip()
    return version if version.startswith("v") else f"v{version}"


def _version_tuple(version: str) -> tuple[int, ...]:
    version = version.strip().lstrip("v")
    parts = []
    for part in version.split("."):
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits or 0))
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def platform_asset_name(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()

    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "mcp-harbour-linux-x64.tar.gz"
    # Only an arm64 macOS build is published. x86_64/amd64 here means an x86_64
    # Python on Apple Silicon (e.g. under Rosetta), whose hardware runs the arm64
    # binary natively; genuine Intel Macs are not supported.
    if system == "Darwin" and machine in {"arm64", "aarch64", "x86_64", "amd64"}:
        return "mcp-harbour-darwin-arm64.tar.gz"
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "mcp-harbour-windows-x64.zip"

    raise UpdateError(f"Unsupported platform: {system}-{machine}")


def installer_asset_name(system: Optional[str] = None) -> str:
    system = system or platform.system()
    if system in {"Linux", "Darwin"}:
        return "install.sh"
    if system == "Windows":
        return "install.ps1"
    raise UpdateError(f"Unsupported platform: {system}")


def _github_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "mcp-harbour"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "mcp-harbour"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _asset_from_release(release: dict, asset_name: str) -> ReleaseAsset:
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name and asset.get("browser_download_url"):
            return ReleaseAsset(name=asset_name, download_url=asset["browser_download_url"])
    tag = release.get("tag_name", "unknown")
    raise UpdateError(f"Release {tag} does not include asset {asset_name}.")


def _fetch_release(tag: Optional[str]) -> dict:
    url = f"{GITHUB_API}/releases/tags/{normalize_tag(tag)}" if tag else f"{GITHUB_API}/releases/latest"
    release = _github_json(url)
    if not release.get("tag_name"):
        raise UpdateError("GitHub release response did not include a tag name.")
    return release


def fetch_latest_tag(timeout: float = 3.0) -> str:
    """Lightweight fetch of the latest release tag, for the update-available hint.
    Short timeout, no asset validation. Raises on network error."""
    request = urllib.request.Request(
        f"{GITHUB_API}/releases/latest", headers={"User-Agent": "mcp-harbour"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    tag = data.get("tag_name")
    if not tag:
        raise UpdateError("Latest release has no tag_name.")
    return tag


def fetch_release_info(
    current_version: str = __version__,
    tag: Optional[str] = None,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    release: Optional[dict] = None,
) -> ReleaseInfo:
    release = release if release is not None else _fetch_release(tag)
    asset = _asset_from_release(release, platform_asset_name(system, machine))
    return ReleaseInfo(
        tag=release["tag_name"],
        asset=asset,
        update_available=is_newer(release["tag_name"], current_version),
    )


def fetch_installer_asset(
    tag: str,
    system: Optional[str] = None,
    release: Optional[dict] = None,
) -> ReleaseAsset:
    release = release if release is not None else _fetch_release(tag)
    return _asset_from_release(release, installer_asset_name(system))


def checksum_asset_url(tag: str) -> str:
    return f"{GITHUB_RELEASES}/{normalize_tag(tag)}/checksums.txt"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    checksums = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            checksums[parts[-1].lstrip("*")] = parts[0].lower()
    return checksums


def verify_checksum(path: Path, checksums_text: str) -> None:
    checksums = parse_checksums(checksums_text)
    expected = checksums.get(path.name)
    if not expected:
        raise UpdateError(f"checksums.txt does not include {path.name}.")
    actual = sha256_file(path)
    if actual != expected:
        raise UpdateError(f"Checksum verification failed for {path.name}.")


def download_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "mcp-harbour"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def download_installer(
    tag: str,
    destination: Path,
    system: Optional[str] = None,
    release: Optional[dict] = None,
) -> Path:
    asset = fetch_installer_asset(tag, system=system, release=release)
    installer_path = destination / asset.name
    _download(asset.download_url, installer_path)

    try:
        checksums = download_text(checksum_asset_url(tag))
    except urllib.error.URLError:
        checksums = None
    if checksums:
        verify_checksum(installer_path, checksums)
    else:
        logger.warning(
            "checksums.txt not available for %s; skipping installer integrity verification.",
            tag,
        )

    return installer_path


def run_installer(installer_path: Path, system: Optional[str] = None, version: Optional[str] = None) -> None:
    system = system or platform.system()
    if system in {"Linux", "Darwin"}:
        installer_path.chmod(0o755)
        command = ["bash", str(installer_path)]
    elif system == "Windows":
        command = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer_path),
        ]
    else:
        raise UpdateError(f"Unsupported platform: {system}")

    env = None
    if version:
        env = {**os.environ, "MCP_HARBOUR_VERSION": version}

    try:
        subprocess.run(command, check=True, env=env)
    except subprocess.CalledProcessError as e:
        raise UpdateError(f"Installer exited with status {e.returncode}.") from e


def run_update_installer(
    tag: str,
    system: Optional[str] = None,
    release: Optional[dict] = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-harbour-update-") as tmp:
        installer = download_installer(tag, Path(tmp), system=system, release=release)
        run_installer(installer, system, version=tag)


def update_binary(
    tag: Optional[str] = None,
    check_only: bool = False,
    force: bool = False,
    current_version: str = __version__,
) -> ReleaseInfo:
    release = _fetch_release(tag)
    info = fetch_release_info(current_version=current_version, release=release)

    if not info.update_available and not force and normalize_tag(current_version) == normalize_tag(info.tag):
        return info

    if not check_only:
        run_update_installer(info.tag, release=release)

    return info
