"""Read bounded client display metadata, never conversation contents."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sqlite3

from .common import safe_id, timestamp


MAX_METADATA_BYTES = 8_000_000


def metadata_timestamp(value):
    parsed = timestamp(value)
    if parsed is not None or not isinstance(value, str):
        return parsed
    # Python 3.9 accepts only specific fractional lengths; client RFC3339 names
    # use variable precision, including four/five digits and nanoseconds.
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{1,9})(Z|[+-]\d{2}:\d{2})", value)
    if match:
        return timestamp(match[1] + "." + match[2][:6].ljust(6, "0") + match[3])
    return None


def normalized_root(value):
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return None
    return os.path.normpath(value)


def bounded_bytes(path):
    with path.open("rb") as stream:
        value = stream.read(MAX_METADATA_BYTES + 1)
    if len(value) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds limit")
    return value


@dataclass
class CodexNames:
    titles: dict = field(default_factory=dict)
    titles_failed: bool = False
    invalid_titles: set = field(default_factory=set)
    projects: list = field(default_factory=list)
    assignments: dict = field(default_factory=dict)
    invalid_assignments: set = field(default_factory=set)
    projects_available: bool = False
    projects_failed: bool = False

    def project_title(self, thread_id, cwd):
        """Return (covered, name); ambiguous/unreadable sources preserve old data."""
        if not self.projects_available or self.projects_failed or thread_id in self.invalid_assignments:
            return False, None
        grouped = {}
        for project in self.projects:
            grouped.setdefault(project["canonical"], []).append(project)
        candidates = []
        assignment = self.assignments.get(thread_id)
        root = normalized_root(cwd)
        for records in grouped.values():
            latest_at = max(record["updated_at"] for record in records)
            latest = [record for record in records if record["updated_at"] == latest_at]
            aliases = {identity for record in records for identity in record["aliases"]}
            roots = {path for record in latest for path in record["roots"]}
            if (assignment is not None and assignment in aliases) or (assignment is None and root and root in roots):
                names = {record["name"] for record in latest}
                candidates.append((len(names) == 1, next(iter(names)) if len(names) == 1 else None))
        if len(candidates) == 1:
            return candidates[0]
        if candidates or assignment is not None:
            return False, None
        return True, None


def read_codex_names(home, db):
    """Whitelist official name/catalog fields; all source files stay read-only."""
    home = Path(home)
    result = CodexNames()
    try:
        latest = {}
        for position, line in enumerate(bounded_bytes(home / "session_index.jsonl").splitlines()):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError, UnicodeError):
                result.titles_failed = True
                continue
            identity = safe_id(row.get("id")) if isinstance(row, dict) else None
            name = row.get("thread_name") if isinstance(row, dict) else None
            at = metadata_timestamp(row.get("updated_at")) if isinstance(row, dict) else None
            if not identity or "thread_name" not in row or not isinstance(name, (str, type(None))) or at is None:
                result.titles_failed = True
                if identity:
                    result.invalid_titles.add(identity)
                continue
            if identity not in latest or (at, position) >= latest[identity][0]:
                latest[identity] = ((at, position), name)
        result.titles = {identity: value[1] for identity, value in latest.items() if identity not in result.invalid_titles}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, UnicodeError):
        result.titles_failed = True

    migrations = {}
    try:
        document = json.loads(bounded_bytes(home / ".codex-global-state.json"))
        if not isinstance(document, dict) or not isinstance(document.get("local-projects"), dict):
            raise ValueError("unsupported project metadata")
        host_maps = document.get("app-server-project-id-by-legacy-project-id-by-host", {})
        local_map = host_maps.get("local:" + str(home.absolute()), {}) if isinstance(host_maps, dict) else {}
        if isinstance(local_map, dict):
            migrations = {old: new for old, new in local_map.items() if safe_id(old) and safe_id(new)}
        assignments = document.get("thread-project-assignments", {})
        if not isinstance(assignments, dict):
            raise ValueError("unsupported project assignments")
        if isinstance(assignments, dict):
            for identity, assignment in assignments.items():
                if not safe_id(identity):
                    continue
                if (isinstance(assignment, dict) and safe_id(assignment.get("projectId"))
                        and assignment.get("projectKind", "local") in ["local", "chatgpt", "codex"]):
                    result.assignments[identity] = assignment["projectId"]
                else:
                    result.invalid_assignments.add(identity)
        for identity, project in document["local-projects"].items():
            if (not safe_id(identity) or not isinstance(project, dict) or project.get("id") != identity
                    or "name" not in project or not isinstance(project["name"], (str, type(None)))
                    or not isinstance(project.get("rootPaths"), list)
                    or any(normalized_root(value) is None for value in project["rootPaths"])):
                raise ValueError("unsupported project record")
            result.projects.append({"canonical": migrations.get(identity, identity), "aliases": {identity, migrations.get(identity, identity)},
                "name": project["name"], "roots": {root for value in project["rootPaths"] if (root := normalized_root(value))},
                "updated_at": metadata_timestamp(project.get("updatedAt")) or 0})
        result.projects_available = True
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, UnicodeError):
        result.projects_failed = True

    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"projects", "project_roots"} <= tables:
            columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
            root_columns = {row[1] for row in db.execute("PRAGMA table_info(project_roots)")}
            if not {"id", "name"} <= columns or not {"project_id", "path"} <= root_columns:
                raise ValueError("unsupported native project catalog")
            updated = "p.updated_at_ms" if "updated_at_ms" in columns else "NULL"
            native = {}
            for identity, name, at, path in db.execute("SELECT p.id,p.name," + updated + ",r.path FROM projects p LEFT JOIN project_roots r ON r.project_id=p.id"):
                if not safe_id(identity) or not isinstance(name, (str, type(None))):
                    raise ValueError("unsupported native project record")
                project = native.setdefault(identity, {"canonical": identity, "aliases": {identity}, "name": name,
                    "roots": set(), "updated_at": metadata_timestamp(at) or 0})
                root = normalized_root(path)
                if path is not None and root is None:
                    raise ValueError("unsupported native project root")
                if root:
                    project["roots"].add(root)
            result.projects.extend(native.values())
            result.projects_available = True
    except (sqlite3.Error, ValueError, TypeError):
        result.projects_failed = True
    return result
