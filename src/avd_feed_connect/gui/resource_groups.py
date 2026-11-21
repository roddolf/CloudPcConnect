"""Sort feed resources into sections and match them against the filter bar.

The feed has no region or category field, so both are read from the names
administrators give workspaces and resources ("Productivity Apps - West US",
"devcenter-… / Project", "Europe Desktop").
"""
import re

PC = "pc"
DESKTOP = "desktop"
APP = "app"
KINDS = (PC, DESKTOP, APP)
KIND_LABELS = {PC: "Cloud PCs", DESKTOP: "Virtual desktops", APP: "Apps"}

ALL = ""

# Longest names first so "West US 2" wins over "West US" and "US".
_REGIONS = sorted((
    "West US 3", "West US 2", "West US", "East US 2", "East US", "Central US",
    "North Central US", "South Central US", "West Central US", "Canada Central",
    "Canada East", "Canada", "Brazil South", "Brazil", "Mexico Central",
    "North Europe", "West Europe", "Europe", "UK South", "UK West", "UK",
    "France Central", "France", "Germany West Central", "Germany",
    "Switzerland North", "Switzerland", "Norway East", "Norway",
    "Sweden Central", "Sweden", "Poland Central", "Poland", "Italy North", "Italy",
    "Spain Central", "Spain", "Central India", "South India", "West India", "India",
    "Southeast Asia", "East Asia", "Asia", "Japan East", "Japan West", "Japan",
    "Korea Central", "Korea", "Australia East", "Australia Southeast",
    "Australia Central", "Australia", "New Zealand North", "New Zealand",
    "UAE North", "UAE", "Qatar Central", "Israel Central", "South Africa North",
    "South Africa",
), key=len, reverse=True)
# Region names overlap ("Asia" and "Southeast Asia"), so the filter offers
# the geography each one belongs to instead.
_GEOGRAPHY_PREFIXES = (
    ("United States", ("US",)),
    ("Canada", ("Canada",)),
    ("Latin America", ("Brazil", "Mexico")),
    ("Europe", ("Europe", "UK", "France", "Germany", "Switzerland", "Norway", "Sweden",
                "Poland", "Italy", "Spain")),
    ("India", ("India",)),
    ("Asia Pacific", ("Asia", "Korea")),
    ("Japan", ("Japan",)),
    ("Australia", ("Australia", "New Zealand")),
    ("Middle East", ("UAE", "Qatar", "Israel")),
    ("Africa", ("South Africa",)),
)
GEOGRAPHIES = tuple(name for name, _ in _GEOGRAPHY_PREFIXES)


def _geography_of(name):
    words = name.split()
    for geography, keys in _GEOGRAPHY_PREFIXES:
        for key in keys:
            key_words = key.split()
            if any(words[i:i + len(key_words)] == key_words for i in range(len(words))):
                return geography
    return ""


_GEOGRAPHY = {r: _geography_of(r) for r in _REGIONS}
_REGION_RE = re.compile(r"\b(" + "|".join(re.escape(r) for r in _REGIONS) + r")\b", re.IGNORECASE)
_REGION_CASE = {r.casefold(): r for r in _REGIONS}


def kind(res):
    """PC for personal Cloud PCs and Dev Boxes, DESKTOP for pooled desktops."""
    if res.get("type") != "Desktop":
        return APP
    workspace = (res.get("tenant") or "").casefold()
    if (workspace.startswith("devcenter-") or "windows 365" in workspace
            or "cloud pc" in workspace or "dev box" in workspace):
        return PC
    return DESKTOP


def region(res):
    """Region named in the resource title or its workspace, or "" if none."""
    for text in (res.get("title") or "", res.get("tenant") or ""):
        match = _REGION_RE.search(text)
        if match:
            return _REGION_CASE[match.group(1).casefold()]
    return ""


def geography(res):
    """Broad area of the resource's region (Europe, India, …), or ""."""
    return _GEOGRAPHY.get(region(res), "")


def workspace(res):
    return res.get("tenant") or ""


def workspace_label(name):
    """Dev Box workspaces read "devcenter-<id>-dc / Project"; show the project."""
    if name.casefold().startswith("devcenter-") and " / " in name:
        return name.split(" / ", 1)[1]
    return name


def app_name(res):
    return (res.get("title") or "") if kind(res) == APP else ""


FACETS = ("kind", "region", "workspace", "app")
_FACET_VALUE = {"kind": kind, "region": geography, "workspace": workspace, "app": app_name}


def options(resources, selected=None):
    """Distinct values for each drop-down; empty values are left out.

    With ``selected`` ({facet: value}), each facet only offers values that
    still match the other facets, so no combination comes up empty.
    """
    selected = selected or {}
    values = {}
    for facet in FACETS:
        others = {f"{name}_filter": selected.get(name, ALL) for name in FACETS if name != facet}
        pool = [r for r in resources if matches(r, **others)]
        found = {_FACET_VALUE[facet](r) for r in pool} - {""}
        if facet == "kind":
            values[facet] = [k for k in KINDS if k in found]
        elif facet == "region":
            values[facet] = [g for g in GEOGRAPHIES if g in found]
        else:
            values[facet] = sorted(found, key=lambda v: workspace_label(v).casefold())
    return values


def matches(res, text="", kind_filter=ALL, region_filter=ALL, workspace_filter=ALL, app_filter=ALL):
    if kind_filter and kind(res) != kind_filter:
        return False
    if region_filter and geography(res) != region_filter:
        return False
    if workspace_filter and workspace(res) != workspace_filter:
        return False
    if app_filter and app_name(res) != app_filter:
        return False
    query = text.strip().casefold()
    if query:
        haystack = " ".join((res.get("title") or "", workspace(res), region(res),
                             geography(res))).casefold()
        return all(word in haystack for word in query.split())
    return True


def sections(resources):
    """[(kind, [(workspace heading or "", [resources])])] with Cloud PCs first.

    Cloud PCs and desktops are one flat list each; apps repeat per workspace
    (the same Word in four regions), so they keep a heading per workspace.
    """
    result = []
    for section_kind in KINDS:
        items = [r for r in resources if kind(r) == section_kind]
        if not items:
            continue
        if section_kind != APP:
            result.append((section_kind, [("", items)]))
            continue
        groups = {}
        for res in items:
            groups.setdefault(workspace(res), []).append(res)
        result.append((section_kind, list(groups.items())))
    return result
