"""Atomic JSON persistence of drawing profiles, one file per stable id."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.profiles.models import DrawingProfile

_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")


class ProfileNotFoundError(KeyError):
    pass


def _write_atomic(target: Path, payload: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp", delete=False
        ) as temporary:
            temporary.write(payload)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class ProfileRepository:
    """Profiles plus user preferences (default profile, last use).

    Preferences live in their own file: choosing or using a profile never
    rewrites the profile, so neither its revision nor its date changes.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._preferences_path = self.root / "meta" / "preferences.json"
        self._lock = threading.Lock()

    def _path(self, profile_id: str) -> Path:
        if not _SAFE_ID.fullmatch(profile_id):
            raise ProfileNotFoundError(profile_id)
        return self.root / f"{profile_id}.json"

    def save(self, profile: DrawingProfile, *, touch: bool = True) -> DrawingProfile:
        """Store the profile; its revision increases when its rules change."""

        with self._lock:
            try:
                stored = self.get(profile.id)
            except ProfileNotFoundError:
                stored = None
            update: dict[str, object] = {}
            if stored is not None:
                changed = stored.content_hash != profile.content_hash
                update["revision"] = stored.revision + 1 if changed else stored.revision
            if touch:
                update["updated_at"] = datetime.now(timezone.utc)
            profile = profile.model_copy(update=update)
            _write_atomic(self._path(profile.id), profile.model_dump_json(indent=2))
        return profile

    def get(self, profile_id: str) -> DrawingProfile:
        path = self._path(profile_id)
        try:
            return DrawingProfile.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProfileNotFoundError(profile_id) from exc
        except ValueError as exc:
            raise ProfileNotFoundError(profile_id) from exc

    def list(self) -> tuple[list[DrawingProfile], int]:
        """Valid profiles, most recently updated first, and how many were unreadable."""

        if not self.root.exists():
            return [], 0
        profiles: list[DrawingProfile] = []
        unreadable = 0
        for path in self.root.glob("*.json"):
            try:
                profiles.append(DrawingProfile.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                unreadable += 1
        profiles.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return profiles, unreadable

    def delete(self, profile_id: str) -> None:
        path = self._path(profile_id)
        with self._lock:
            if not path.is_file():
                raise ProfileNotFoundError(profile_id)
            path.unlink()
            preferences = self._read_preferences()
            if preferences["default"] == profile_id:
                preferences["default"] = None
            preferences["last_used"].pop(profile_id, None)
            self._write_preferences(preferences)

    def duplicate(self, profile_id: str) -> DrawingProfile:
        source = self.get(profile_id)
        now = datetime.now(timezone.utc)
        copy = source.model_copy(
            update={
                "id": uuid4().hex,
                "name": f"{source.name} (copie)"[:120],
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
        )
        return self.save(copy, touch=False)

    # Preferences -----------------------------------------------------------

    def _read_preferences(self) -> dict:
        try:
            raw = json.loads(self._preferences_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        default = raw.get("default") if isinstance(raw, dict) else None
        used = raw.get("last_used") if isinstance(raw, dict) else None
        last_used = {}
        for key, value in (used or {}).items() if isinstance(used, dict) else ():
            try:
                if _SAFE_ID.fullmatch(key):
                    last_used[key] = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
        return {
            "default": default if isinstance(default, str) and _SAFE_ID.fullmatch(default) else None,
            "last_used": last_used,
        }

    def _write_preferences(self, preferences: dict) -> None:
        payload = {
            "default": preferences["default"],
            "last_used": {key: value.isoformat() for key, value in sorted(preferences["last_used"].items())},
        }
        _write_atomic(self._preferences_path, json.dumps(payload, indent=2, sort_keys=True))

    def default_id(self) -> str | None:
        return self._read_preferences()["default"]

    def set_default(self, profile_id: str | None) -> None:
        if profile_id is not None:
            self.get(profile_id)
        with self._lock:
            preferences = self._read_preferences()
            preferences["default"] = profile_id
            self._write_preferences(preferences)

    def last_used(self) -> dict[str, datetime]:
        return self._read_preferences()["last_used"]

    def mark_used(self, profile_id: str) -> None:
        if not _SAFE_ID.fullmatch(profile_id):
            raise ProfileNotFoundError(profile_id)
        with self._lock:
            preferences = self._read_preferences()
            preferences["last_used"][profile_id] = datetime.now(timezone.utc)
            self._write_preferences(preferences)
