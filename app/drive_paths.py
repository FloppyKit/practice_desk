"""Resolve a client to an existing Proton Drive folder. Do not invent names."""
from __future__ import annotations

import re
import time
from typing import Any, Callable

# Archive = old {Initial} Last tree. Living = Last, First (Matt, 2026-08-13).
ARCHIVE_ROOT = "/Client files"
LIVING_ROOT = "/Clients"
CLIENT_FILES_ROOT = ARCHIVE_ROOT
CHART_CHILD = "Chart"
BILLING_CHILD = "Billing"
VISIT_NOTES_FOLDER = "Visit notes"  # Matt: existing Chart child
_TTL = 300.0
_cache: dict[str, Any] = {"at": 0.0, "items": []}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dob_year(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 4 and s[:4].isdigit() and 1900 <= int(s[:4]) <= 2100:
        return s[:4]
    return ""


def person_folder_name(client: dict[str, Any], *, with_year: bool = False) -> str:
    """Directory label: `Last, First`. Optional birth year only for collisions."""
    family = (client.get("family_name") or "").strip()
    given = (client.get("given_name") or "").strip()
    if not family or not given:
        return ""
    if "/" in family or "/" in given or "\\" in family or "\\" in given:
        return ""
    if "\x00" in family or "\x00" in given:
        return ""
    name = f"{family}, {given}"
    if with_year:
        year = dob_year(str(client.get("dob") or ""))
        if not year:
            return ""
        name = f"{name} {year}"
    if len(name) > 180:
        return ""
    return name


def _initials(client: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("given_name", "preferred_name", "name"):
        toks = _norm(client.get(key) or "").split()
        if toks and toks[0]:
            ini = toks[0][0]
            if ini not in out:
                out.append(ini)
    return out


def match_person_folder(client: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Existing `/Client files` child. Prefer `Last, First`, then `{Initial} Last`."""
    names = []
    for it in items:
        if (it.get("type") or "") != "folder":
            continue
        name = (it.get("name") or "").strip()
        if name:
            names.append(name)
    if not names:
        return ""

    wanted = [person_folder_name(client), person_folder_name(client, with_year=True)]
    for target in wanted:
        if target and target in names:
            return target
        nt = _norm(target) if target else ""
        if nt:
            hits = [n for n in names if _norm(n) == nt]
            if len(hits) == 1:
                return hits[0]

    family = _norm(client.get("family_name") or "")
    initials = _initials(client)
    if family and initials:
        hits = []
        for name in names:
            toks = _norm(name).split()
            if len(toks) >= 2 and len(toks[0]) == 1 and toks[-1] == family and toks[0] in initials:
                hits.append(name)
        if len(hits) == 1:
            return hits[0]
    return ""


def client_file_folders(list_fn: Callable[[str], dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    if _cache["items"] and now - float(_cache["at"]) < _TTL:
        return list(_cache["items"])
    data = list_fn(CLIENT_FILES_ROOT) or {}
    items = data.get("items") or []
    if items:
        _cache["at"] = now
        _cache["items"] = items
    return list(items)


def living_paths_from_name(client: dict[str, Any]) -> dict[str, str]:
    """Known living tree: /Clients/Last, First. Does not create folders."""
    label = person_folder_name(client)
    if not label:
        return {"folder": "", "chart": "", "billing": ""}
    folder = LIVING_ROOT + "/" + label
    return {
        "folder": folder,
        "chart": folder + "/" + CHART_CHILD,
        "billing": folder + "/" + BILLING_CHILD,
    }


def paths_from_row(client: dict[str, Any]) -> dict[str, str]:
    """Use stored pointers when present. Else the living Last, First path from the phone book."""
    chart = (client.get("drive_folder_path") or client.get("chart_path") or "").strip()
    billing = (client.get("billing_folder_path") or client.get("billing_path") or "").strip()
    living = chart.startswith(LIVING_ROOT + "/") or chart.startswith(ARCHIVE_ROOT + "/")
    if living and "/../" not in chart:
        folder = chart.rsplit("/", 1)[0] if chart.endswith("/" + CHART_CHILD) else chart
        return {
            "folder": folder,
            "chart": chart if chart.endswith("/" + CHART_CHILD) else folder + "/" + CHART_CHILD,
            "billing": billing
            if billing.startswith(LIVING_ROOT + "/") or billing.startswith(ARCHIVE_ROOT + "/")
            else folder + "/" + BILLING_CHILD,
        }
    return living_paths_from_name(client)


def candidate_folders(client: dict[str, Any]) -> list[str]:
    """Possible existing folders. Does not create. Living first, then archive names."""
    out: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        p = (p or "").strip().rstrip("/")
        if not p or p in seen or p == "/" or "/../" in p:
            return
        if not (p.startswith(LIVING_ROOT + "/") or p.startswith(ARCHIVE_ROOT + "/")):
            return
        seen.add(p)
        out.append(p)

    stored = paths_from_row(client)
    add(stored.get("folder") or "")
    label = person_folder_name(client)
    family = (client.get("family_name") or "").strip()
    if label:
        add(LIVING_ROOT + "/" + label)
        add(ARCHIVE_ROOT + "/" + label)
    if family and "/" not in family and "\\" not in family:
        for ini in _initials(client):
            add(ARCHIVE_ROOT + "/" + ini.upper() + " " + family)
    return out


def existing_paths(
    client: dict[str, Any],
    list_fn: Callable[[str], dict[str, Any]],
) -> dict[str, str]:
    """First Proton folder that actually lists. Never invent a missing name."""
    empty = {"folder": "", "chart": "", "billing": ""}
    for folder in candidate_folders(client):
        data = list_fn(folder) or {}
        if not data.get("ok"):
            continue
        names = {(it.get("name") or "").strip() for it in (data.get("items") or [])}
        chart = folder + "/" + CHART_CHILD if CHART_CHILD in names else folder
        billing = folder + "/" + BILLING_CHILD if BILLING_CHILD in names else folder
        return {"folder": folder, "chart": chart, "billing": billing}
    return empty


def resolve_client_paths(
    client: dict[str, Any],
    list_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Local pointers only unless a list_fn is passed to confirm Proton."""
    if list_fn is not None:
        return existing_paths(client, list_fn)
    return paths_from_row(client)
