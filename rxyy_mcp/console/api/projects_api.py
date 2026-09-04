"""开源壳项目管理：扫描 / 增删 / 启用，不含公司汇报管线。"""
from __future__ import annotations

from typing import Any

from src.config import desktop_root
from api.workflow_db import Project, wfdb


def _scan_root() -> str:
    return str(desktop_root())


def refresh_projects_git_meta() -> int:
    try:
        from api.git_utils import repo_meta
    except Exception:
        return 0
    n = 0
    db = wfdb()
    for p in db.list_projects():
        try:
            meta = repo_meta(p.path)
        except Exception:
            continue
        if not meta:
            continue
        branch = meta.get("branch") or ""
        at = meta.get("last_commit_at") or ""
        if branch == (p.branch or "") and at == (p.last_commit_at or ""):
            continue
        if hasattr(db, "update_project_git_meta"):
            db.update_project_git_meta(p.id, branch, at)
            n += 1
    return n


class ProjectsApi:
    def proj_scan(self) -> dict[str, Any]:
        try:
            from api.git_utils import scan_repos
        except Exception as exc:
            return {"ok": False, "error": "git 模块不可用: %r" % exc}
        root = _scan_root()
        found = scan_repos(root)
        for r in found:
            wfdb().upsert_project(Project(
                path=r["path"], name=r["name"], branch=r.get("branch", ""),
                source="scan", last_commit_at=r.get("last_commit_at", ""),
            ))
        return {"ok": True, "scanned": len(found), "root": root,
                "total": len(wfdb().list_projects())}

    def proj_list(self) -> list[dict[str, Any]]:
        refresh_projects_git_meta()
        out = []
        for p in wfdb().list_projects():
            out.append({
                "id": p.id, "name": p.name, "path": p.path, "branch": p.branch,
                "source": p.source, "enabled": bool(p.enabled), "note": p.note or "",
                "last_commit_at": p.last_commit_at or "",
                "biz_name": getattr(p, "biz_name", "") or "",
            })
        return out

    def proj_add(self, path: str) -> dict[str, Any]:
        path = (path or "").strip().strip('"')
        if not path:
            return {"ok": False, "error": "路径必填"}
        try:
            from api.git_utils import validate_repo
        except Exception as exc:
            return {"ok": False, "error": "git 模块不可用: %r" % exc}
        v = validate_repo(path)
        if not v.get("ok"):
            return v
        wfdb().upsert_project(Project(
            path=path, name=v["name"], branch=v.get("branch", ""),
            source="manual", last_commit_at=v.get("last_commit_at", ""),
        ))
        return {"ok": True, "name": v["name"]}

    def proj_set(self, project_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "note", "enabled", "biz_name"}
        clean = {k: v for k, v in (fields or {}).items() if k in allowed}
        wfdb().set_project_fields(int(project_id), clean)
        return {"ok": True}

    def proj_delete(self, project_id: int) -> dict[str, Any]:
        wfdb().delete_project(int(project_id))
        return {"ok": True}

    def proj_toggle(self, project_id: int, enabled: bool) -> dict[str, Any]:
        wfdb().set_project_fields(int(project_id), {"enabled": 1 if enabled else 0})
        return {"ok": True}

    def proj_dedup(self) -> dict[str, Any]:
        seen: dict[str, int] = {}
        disabled = 0
        for p in wfdb().list_projects():
            key = (p.name or "").lower()
            if not key or not p.enabled:
                continue
            if key in seen:
                wfdb().set_project_fields(p.id, {"enabled": 0, "note": "duplicate"})
                disabled += 1
            else:
                seen[key] = p.id
        return {"ok": True, "disabled": disabled}
