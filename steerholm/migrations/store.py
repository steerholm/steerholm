"""Document-level access to the on-disk state, for migrations.

`ConfigManager` speaks the *current* schema in validated models. A migration has
to speak *historical* schemas, which those models cannot represent — a schema-0
policy has `agent_name` and name-keyed permissions, so it is not an
`AgentPolicy`, and since the models reject unknown keys it cannot be coerced
into one either. So migrations work in untyped documents: whatever JSON is
actually on disk.

The store also owns the version marker. It lives in `migrations/version` rather
than inside `config.json` because it describes the whole state directory — the
schema-0 to 1 step mostly rewrites `policies/`, barely touching the config — and
because a marker the migration engine owns needs no co-ordination with the
models, which would otherwise have to carry a field for it.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..files import ENCODING, read_text, restrict, write_atomic

SUFFIX = ".json"

_DIGITS = re.compile(r"[0-9]+")


class StateError(Exception):
    """The state directory is not in a shape that can be read safely."""


class MigrationStore:
    """The state directory, addressed as documents rather than models."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.config_file = self.root / "config.json"
        self.policies_dir = self.root / "policies"
        self.meta_dir = self.root / "migrations"

    # ── the version marker ──────────────────────────────────────────

    @property
    def version_file(self) -> Path:
        return self.meta_dir / "version"

    @property
    def version(self) -> Optional[int]:
        """The schema this state is marked as, or None if it carries no marker.

        None rather than a number: what an absent marker *means* is a question
        about which releases wrote which shape, which changes as releases are
        added. That belongs with the migrations — `resolve_version` answers it —
        not here, where it would pin the store to one era.

        A marker that is PRESENT but unreadable is a third case and must not
        collapse into either: treating a permissions fault or a torn write as
        "no marker" would re-run migrations against already migrated data.
        """
        if not self.version_file.exists():
            return None
        try:
            raw = self.version_file.read_text(encoding=ENCODING).strip()
        except OSError as e:
            raise StateError(f"{self.version_file} cannot be read: {e}") from e
        except UnicodeDecodeError as e:
            # Not an OSError: a scrambled block decodes, not reads, badly.
            raise StateError(f"{self.version_file} is not readable text: {e}") from e
        # Not `isdigit()`: that accepts superscripts ("²") that int() rejects, and
        # other-script digits ("٣") that it silently accepts as a version.
        if not _DIGITS.fullmatch(raw):
            raise StateError(
                f"{self.version_file} does not contain a schema version: {raw!r}"
            )
        return int(raw)

    @version.setter
    def version(self, value: int) -> None:
        # Temp file plus rename: a torn write here would read back as garbage,
        # and the marker is what decides whether migrations re-run.
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        restrict(self.meta_dir, 0o700)
        write_atomic(self.version_file, f"{value}\n")

    # ── documents ───────────────────────────────────────────────────

    def read_config(self) -> Dict[str, Any]:
        if not self.config_file.exists():
            return {}
        try:
            return json.loads(read_text(self.config_file))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # A torn write or a bad hand-edit. Raised as a StateError so the
            # engine can say what to do about it: the bare decoder message
            # surfaced through the installer as "Migration 1 failed", which
            # names neither the file nor any way forward.
            raise StateError(
                f"{self.config_file} is not valid JSON ({e}). Migrating cannot "
                "read it. Restore it from a backup, or move it aside and start "
                "from an empty configuration — the servers and agents it "
                "defines would be lost."
            ) from e

    def write_config(self, doc: Dict[str, Any]) -> None:
        write_atomic(self.config_file, json.dumps(doc, indent=2))

    def policy_files(self) -> List[Path]:
        if not self.policies_dir.is_dir():
            return []
        return sorted(
            p for p in self.policies_dir.glob(f"*{SUFFIX}")
            if p.is_file()   # a hand-made directory called `x.json` is not a policy
        )

    @staticmethod
    def policy_name(path: Path) -> str:
        """The agent name a policy filename encodes.

        The exact inverse of the name `write_policy` builds a filename from,
        for every name that one accepts — including "", whose file is a bare
        `.json`. Not `path.stem`, which reports ".json" for that file and an
        empty suffix, handing back a name no agent has.
        """
        return path.name[: -len(SUFFIX)]

    def read_policy(self, path: Path) -> Dict[str, Any]:
        try:
            return json.loads(read_text(path))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 0.1.x wrote policies with a plain truncate-and-write, so a crash
            # or a full disk mid-save leaves one of these behind. Name it: the
            # bare decoder message says nothing about which file to look at.
            raise StateError(f"{path} is not valid JSON ({e}).") from e

    def write_policy(self, name: str, doc: Dict[str, Any]) -> Path:
        self.policies_dir.mkdir(parents=True, exist_ok=True)
        path = self.policies_dir / f"{name}{SUFFIX}"
        write_atomic(path, json.dumps(doc, indent=2))
        return path
