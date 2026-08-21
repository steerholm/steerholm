#!/usr/bin/env python3
"""Snapshot the working docs into a frozen, versioned copy on the docs-live tree.

Builds Mintlify multi-version docs (docs.json `navigation.versions`): each released
minor line (vX.Y) gets its own content folder plus a navigation entry, with the newest
version first and marked default. Branding/theme is always taken from the current config
source so every version stays visually consistent; only per-version content and
navigation differ.

Usage:
    snapshot_docs.py --version v0.2 \
        --content-source content/docs \
        --config-source main/docs \
        --dest live/docs

The content source supplies the version's pages and navigation (docs.json or the legacy
mint.json are both accepted, so older release tags can be snapshotted). The config source
supplies shared branding. The dest is the docs root of the docs-live branch.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

# Per-version content directories copied into vX.Y/.
CONTENT_DIRS = ("getting-started", "guides", "concepts", "reference")
# Shared assets copied to the docs root (referenced as /logo, /favicon.svg, ...).
ASSET_PATHS = ("logo", "images", "favicon.svg", "custom.css")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_config(src: Path) -> tuple[dict, str]:
    for name in ("docs.json", "mint.json"):
        path = src / name
        if path.exists():
            return load_json(path), name
    raise SystemExit(f"no docs.json or mint.json found in {src}")


def navigation_groups(cfg: dict) -> list:
    """Flatten to a list of {group, pages} from either schema."""
    nav = cfg.get("navigation")
    if isinstance(nav, list):  # legacy mint.json
        return nav
    if isinstance(nav, dict):
        if "groups" in nav:
            return nav["groups"]
        versions = nav.get("versions") or []
        if versions:  # already-versioned source: take the default/first entry
            return versions[0].get("groups", [])
    return []


def branding(cfg: dict, schema: str) -> dict:
    """Map either schema's top-level branding into docs.json shape."""
    if schema == "docs.json":
        keep = ("$schema", "theme", "name", "colors", "logo", "favicon",
                "appearance", "navbar", "footer")
        out = {k: cfg[k] for k in keep if k in cfg}
        out.setdefault("$schema", "https://mintlify.com/docs.json")
        out.setdefault("theme", "mint")
        return out

    # legacy mint.json -> docs.json
    out: dict = {
        "$schema": "https://mintlify.com/docs.json",
        "theme": "mint",
        "name": cfg.get("name", "Docs"),
    }
    if cfg.get("favicon"):
        out["favicon"] = cfg["favicon"]
    colors = {k: cfg["colors"][k] for k in ("primary", "light", "dark")
              if k in cfg.get("colors", {})}
    if colors:
        out["colors"] = colors
    if cfg.get("logo"):
        out["logo"] = cfg["logo"]
    if cfg.get("appearance"):
        out["appearance"] = cfg["appearance"]

    links = []
    for item in list(cfg.get("anchors", [])) + list(cfg.get("topbarLinks", [])):
        label = item.get("name") or item.get("label")
        href = item.get("url") or item.get("href")
        if label and href:
            link = {"label": label, "href": href}
            if item.get("icon"):
                link["icon"] = item["icon"]
            links.append(link)
    navbar: dict = {}
    if links:
        navbar["links"] = links
    cta = cfg.get("topbarCtaButton")
    if cta:
        navbar["primary"] = {
            "type": "button",
            "label": cta.get("name") or cta.get("label"),
            "href": cta.get("url") or cta.get("href"),
        }
    if navbar:
        out["navbar"] = navbar
    if cfg.get("footerSocials"):
        out["footer"] = {"socials": cfg["footerSocials"]}
    return out


def version_key(entry: dict) -> tuple:
    """Sort key from a 'vX.Y' label; unparseable labels sort lowest."""
    label = str(entry.get("version", "")).lstrip("v")
    try:
        return tuple(int(p) for p in label.split("."))
    except ValueError:
        return (0,)


def prefix_pages(groups: list, version: str) -> list:
    def fix(page):
        if isinstance(page, str):
            return f"{version}/{page}"
        if isinstance(page, dict) and "pages" in page:  # nested group
            return {**page, "pages": [fix(p) for p in page["pages"]]}
        return page

    return [{**g, "pages": [fix(p) for p in g.get("pages", [])]} for g in groups]


def copy_content(content_src: Path, version_dir: Path) -> None:
    if version_dir.exists():
        shutil.rmtree(version_dir)
    version_dir.mkdir(parents=True)
    for name in CONTENT_DIRS:
        src = content_src / name
        if src.is_dir():
            shutil.copytree(src, version_dir / name)
    for mdx in content_src.glob("*.mdx"):  # any root-level pages
        shutil.copy2(mdx, version_dir / mdx.name)


def copy_assets(config_src: Path, dest_root: Path) -> None:
    for name in ASSET_PATHS:
        src = config_src / name
        if src.is_dir():
            shutil.copytree(src, dest_root / name, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, dest_root / name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="version label, e.g. v0.2")
    ap.add_argument("--content-source", required=True,
                    help="docs dir supplying this version's content + navigation")
    ap.add_argument("--config-source", required=True,
                    help="docs dir supplying shared branding (the current docs)")
    ap.add_argument("--dest", required=True, help="docs root of the docs-live branch")
    args = ap.parse_args()

    version = args.version
    content_src = Path(args.content_source)
    config_src = Path(args.config_source)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    # 1. freeze this version's content + 2. refresh shared assets
    copy_content(content_src, dest / version)
    copy_assets(config_src, dest)

    # 3. build this version's navigation from the content source
    content_cfg, content_schema = find_config(content_src)
    entry = {"version": version,
             "groups": prefix_pages(navigation_groups(content_cfg), version)}

    # 4. branding from the config source (keeps all versions consistent)
    config_cfg, config_schema = find_config(config_src)
    out = branding(config_cfg, config_schema)

    # 5. merge versions: replace same label, order by version desc, highest is default
    existing = []
    dest_docs = dest / "docs.json"
    if dest_docs.exists():
        prev_nav = load_json(dest_docs).get("navigation", {}) or {}
        existing = prev_nav.get("versions", []) or []
    merged = [entry] + [v for v in existing if v.get("version") != version]
    versions = sorted(merged, key=version_key, reverse=True)
    for v in versions:
        v.pop("default", None)
    versions[0]["default"] = True
    out["navigation"] = {"versions": versions}

    # 6. point the navbar CTA at the default (highest) version's page
    default_version = versions[0]["version"]
    primary = (out.get("navbar") or {}).get("primary")
    if primary and isinstance(primary.get("href"), str) and primary["href"].startswith("/"):
        primary["href"] = f"/{default_version}{primary['href']}"

    dest_docs.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"snapshotted {version}: {len(entry['groups'])} groups; "
          f"versions now {[v['version'] for v in versions]}")


if __name__ == "__main__":
    main()
