#!/usr/bin/env python3
"""
Route Mapper
============

A local tool for laying out parking occupancy count routes and generating
inventory IDs, distances and walk-time estimates.

Run it:

    python3 route_mapper.py

It starts a local server and opens your browser. Nothing leaves your machine
except basemap tile requests and place searches, which go to OpenStreetMap's
Nominatim.

Python owns the model: distances, the ID numbering rule, time estimates and
every export. The browser is only the drawing surface. That means the numbering
convention lives in exactly one place (see IdSequence) and you can call the same
engine from a script if you'd rather batch something.

Standard library only.
"""

import html
import io
import json
import math
import os
import re
import socketserver
import sys
import urllib.parse
import urllib.request
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("ROUTE_MAPPER_PORT", "8765"))
# Saved projects live next to this script, not next to whatever folder you
# happened to launch from. Otherwise `cd ~ && python3 "Route Mapper/route_mapper.py"`
# writes to ~/route_projects and the Open menu looks empty next session.
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:          # pasted straight into a notebook cell
    _HERE = os.path.abspath(os.getcwd())
SAVE_DIR = os.path.abspath(os.environ.get("ROUTE_MAPPER_DIR",
                                          os.path.join(_HERE, "route_projects")))

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

EARTH_RADIUS_FT = 20902231.0  # feet


def haversine_ft(a, b):
    """Great-circle distance in feet between two [lat, lng] points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_FT * math.asin(min(1.0, math.sqrt(h)))


def path_length_ft(pts):
    return sum(haversine_ft(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def midpoint_of_path(pts):
    """Point at half the path's length - keeps ID labels off the corners."""
    if not pts:
        return None
    if len(pts) == 1:
        return list(pts[0])
    half = path_length_ft(pts) / 2.0
    run = 0.0
    for i in range(len(pts) - 1):
        seg = haversine_ft(pts[i], pts[i + 1])
        if run + seg >= half and seg > 0:
            t = (half - run) / seg
            return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                    pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t]
        run += seg
    return list(pts[-1])


def polygon_centroid(pts):
    if not pts:
        return None
    if len(pts) < 3:
        return [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
    a = cx = cy = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i][1], pts[i][0]
        x2, y2 = pts[(i + 1) % len(pts)][1], pts[(i + 1) % len(pts)][0]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        return [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
    a *= 0.5
    return [cy / (6 * a), cx / (6 * a)]


def polygon_area_sqft(pts):
    """Planar area via local equirectangular projection. Fine at block scale."""
    if len(pts) < 3:
        return 0.0
    lat0 = math.radians(sum(p[0] for p in pts) / len(pts))
    ft_per_deg_lat = 364000.0
    ft_per_deg_lng = 364000.0 * math.cos(lat0)
    xy = [(p[1] * ft_per_deg_lng, p[0] * ft_per_deg_lat) for p in pts]
    a = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


# ---------------------------------------------------------------------------
# The inventory ID numbering rule
# ---------------------------------------------------------------------------

class IdSequence:
    """Produces inventory IDs in the convention the Fruitvale and Grand Lake
    datasets already use.

    Both corridors number block faces as <zone>.<n> and skip every multiple of
    ten: 2.1 through 2.9, then 2.11, and no 2.10, 2.20, 2.30 anywhere. That is
    not an accident of the data - all four zones across both study areas do it -
    so any tool that generates IDs has to reproduce it or the output will not
    match Survey123.

    Set skip_multiples_of_ten=False if a future study area drops the convention.
    """

    def __init__(self, prefix, skip_multiples_of_ten=True, start=1):
        self.prefix = int(prefix)
        self.skip = bool(skip_multiples_of_ten)
        self.n = int(start)

    def _advance_past_skips(self):
        while self.skip and self.n % 10 == 0:
            self.n += 1

    def peek(self):
        self._advance_past_skips()
        return "%d.%d" % (self.prefix, self.n)

    def take(self):
        value = self.peek()
        self.n += 1
        return value

    def resync_after(self, manual_id):
        """After a manually pinned ID, continue counting from just past it."""
        m = re.match(r"^\s*(\d+)\s*\.\s*(\d+)\s*$", str(manual_id))
        if not m:
            return
        if int(m.group(1)) != self.prefix:
            return
        self.n = max(self.n, int(m.group(2)) + 1)


def walk_order_key(inv_id):
    """Sort IDs the way the route is walked: 2.9 before 2.11, not after 2.1."""
    m = re.match(r"^\s*(\d+)\s*\.\s*(\d+)\s*$", str(inv_id))
    if not m:
        return (10 ** 6, 10 ** 6, str(inv_id))
    return (int(m.group(1)), int(m.group(2)), "")


# ---------------------------------------------------------------------------
# Project model + derived values
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "minutes_per_block": 2.0,       # your rule: a block of ~10 spaces takes 2 min
    "spaces_per_block": 10.0,       # what "a block" means for that rule
    "walk_speed_ft_per_min": 260.0, # ~3 mph, for connector legs you don't count
    "skip_multiples_of_ten": True,
}

ROUTE_COLORS = [
    ("#2E7D4F", "Green"),
    ("#C0392B", "Red"),
    ("#7B4FA8", "Purple"),
    ("#1F6FB2", "Blue"),
    ("#C77C1E", "Orange"),
    ("#2A8C8C", "Teal"),
]


def new_project(name="Untitled study area"):
    return {
        "name": name,
        "settings": dict(DEFAULT_SETTINGS),
        "center": [37.7749, -122.2241],   # Fruitvale-ish
        "zoom": 15,
        "basemap": "muted",
        "routes": [new_route("Route 1", 1, ROUTE_COLORS[0][0])],
    }


def new_route(name, prefix, color):
    return {
        "uid": uuid.uuid4().hex[:10],
        "name": name,
        "prefix": int(prefix),
        "color": color,
        "start": None,
        "end": None,
        "visible": True,
        "segments": [],
    }


def new_segment(kind, pts, spaces=None):
    return {
        "uid": uuid.uuid4().hex[:10],
        "kind": kind,                 # "count", "walk" or "lot"
        "pts": [list(p) for p in pts],
        "spaces": spaces,             # None until the inventory survey gives a count
        "manual_id": None,
        "flagged": False,
        "note": "",
        "street": "",
    }


# Space counts are never guessed. They come from the inventory survey, and until
# one is entered a block face is simply costed as one block of walking time.


def migrate(project):
    """Older projects kept parking lots in a separate top-level list, which meant
    they were always numbered after every block face no matter where they sat on
    the route. Lots are now ordinary segments with kind "lot", so they take an ID
    in walking order and can be moved, flagged and renumbered like anything else.
    """
    lots = project.pop("lots", None)
    if lots:
        by_uid = {r["uid"]: r for r in project.get("routes", [])}
        first = project["routes"][0] if project.get("routes") else None
        for lot in lots:
            route = by_uid.get(lot.get("route_uid")) or first
            if route is None:
                continue
            route.setdefault("segments", []).append({
                "uid": lot.get("uid") or uuid.uuid4().hex[:10],
                "kind": "lot",
                "pts": [list(pt) for pt in lot.get("pts", [])],
                "spaces": lot.get("spaces") or None,
                "manual_id": lot.get("manual_id"),
                "flagged": bool(lot.get("flagged")),
                "note": lot.get("note", ""),
                "street": lot.get("name", ""),
            })
    return project


def _project_on_path(pts, at):
    """Where on the polyline `pts` the point nearest `at` falls: how far along it
    is (0 at the start, 1 at the end) and that point as [lat, lng]. Needs two or
    more points. Uses a local flat frame, which is fine at block scale."""
    k = math.cos(math.radians(at[0]))
    px, py = at[1] * k, at[0]
    best, run = None, 0.0
    for a, b in zip(pts, pts[1:]):
        ax, ay, dx, dy = a[1] * k, a[0], (b[1] - a[1]) * k, b[0] - a[0]
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2)) if l2 else 0.0
        d = (ax + t * dx - px) ** 2 + (ay + t * dy - py) ** 2
        leg_ft = haversine_ft(a, b)
        if best is None or d < best[0]:
            best = (d, run + t * leg_ft, [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])])
        run += leg_ft
    return (best[1] / run if run else 1.0), best[2]


def _walk_order(segments):
    """The order a route is walked in.

    Legs keep the order they were drawn in. A lot tied to a leg by its line
    (`anchor_leg` and the point `anchor`) is counted next to that leg: just
    before it if the line meets the leg's first half, just after it if the
    second half. Dragging the line along the route therefore moves the lot
    through the sequence, and Reverse direction flips it with everything else.
    Lots on one leg go in the order their lines meet it. A lot with no line, or
    whose leg is gone, keeps its place in the list. Sets each lot's `anchor_at`,
    the point its line meets the route, for the UI to draw.
    """
    legs = {sg["uid"]: sg for sg in segments
            if sg.get("kind") != "lot" and len(sg.get("pts") or []) >= 2}
    before, after, order = {}, {}, []
    for sg in segments:
        is_lot = sg.get("kind") == "lot"
        leg = legs.get(sg.get("anchor_leg")) if is_lot and sg.get("anchor") else None
        if leg is None:
            if is_lot:
                sg["anchor_at"] = None
            order.append(sg)
            continue
        t, sg["anchor_at"] = _project_on_path(leg["pts"], sg["anchor"])
        (before if t < 0.5 else after).setdefault(leg["uid"], []).append((t, sg))
    out = []
    for sg in order:
        out += [s for _, s in sorted(before.get(sg.get("uid"), []), key=lambda x: x[0])]
        out.append(sg)
        out += [s for _, s in sorted(after.get(sg.get("uid"), []), key=lambda x: x[0])]
    return out


def compute(project):
    """Return the project with all derived fields filled in.

    This is the single source of truth. The browser never calculates an ID, a
    distance or a duration; it posts geometry and renders what comes back.
    """
    s = {**DEFAULT_SETTINGS, **project.get("settings", {})}
    mins_per_block = float(s["minutes_per_block"])
    spaces_per_block = max(1.0, float(s["spaces_per_block"]))
    walk_speed = max(1.0, float(s["walk_speed_ft_per_min"]))

    out = migrate(json.loads(json.dumps(project)))
    out["settings"] = s
    seen_ids = set()
    duplicate_ids = set()

    for route in out["routes"]:
        # The returned segments are in walking order, so the UI's list and the
        # next save follow it too.
        route["segments"] = _walk_order(route.get("segments", []))
        seq = IdSequence(route.get("prefix", 1), s["skip_multiples_of_ten"])
        r_count_ft = r_walk_ft = r_count_min = r_walk_min = 0.0
        n_blocks = n_lots = 0
        r_spaces = r_lot_spaces = 0

        # The start marker occupies the first slot in the sequence rather than
        # echoing the first block face's number, which produced two 2.1s. Every
        # block face therefore begins at .2, and the end marker takes whatever
        # number is left over once the route is walked.
        has_line = any(sg.get("pts") for sg in route.get("segments", []))
        # A block face or a parking lot can stand in as the route's own start or
        # end. When it does it keeps its own ID and no extra number is spent, so
        # a lot at the end of the walk reads as END rather than getting a
        # separate marker beside it. Walk legs carry no ID, so a flag left on one
        # is ignored; honouring it would leave the route with no end number.
        stands_in = [sg for sg in route["segments"] if sg.get("kind", "count") != "walk"]
        own_start = next((sg for sg in stands_in if sg.get("is_route_start")), None)
        own_end = next((sg for sg in stands_in if sg.get("is_route_end")), None)
        route["start_inv_id"] = seq.take() if (has_line and not own_start) else None

        for seg in route["segments"]:
            pts = seg.get("pts") or []
            kind = seg.get("kind", "count")

            if kind == "lot":
                # A lot adds no distance. Its line to the route only marks where
                # it is counted, and its perimeter is not a path anyone walks.
                seg["area_sqft"] = round(polygon_area_sqft(pts), 0)
                seg["perimeter_ft"] = round(
                    path_length_ft(pts + pts[:1]) if len(pts) > 2 else 0.0, 1)
                seg["length_ft"] = 0.0
                seg["label_at"] = polygon_centroid(pts)
                length = 0.0
            else:
                length = path_length_ft(pts)
                seg["length_ft"] = round(length, 1)
                seg["label_at"] = midpoint_of_path(pts)
                seg["area_sqft"] = 0.0

            if seg.get("label_pos"):
                seg["label_at"] = list(seg["label_pos"])
                seg["label_custom"] = True
            else:
                seg["label_custom"] = False

            if kind in ("count", "lot"):
                if seg.get("spaces") in (None, ""):
                    seg["spaces_effective"] = None
                    seg["spaces_known"] = False
                else:
                    seg["spaces_effective"] = int(seg["spaces"])
                    seg["spaces_known"] = True

                manual = (seg.get("manual_id") or "").strip()
                if manual:
                    seg["inv_id"] = manual
                    seg["id_is_manual"] = True
                    seq.resync_after(manual)
                else:
                    seg["inv_id"] = seq.take()
                    seg["id_is_manual"] = False

                if kind == "lot":
                    # How long a lot takes depends on its aisles, how many levels
                    # it has and whether it is counted from the edge or walked,
                    # none of which the map knows, and the block-face rate bakes
                    # in a walk along the curb that a lot does not have. Rather
                    # than invent a figure, lots stay out of the time and
                    # distance totals and every output that shows them says so.
                    seg["minutes"] = None
                    n_lots += 1
                    r_lot_spaces += seg["spaces_effective"] or 0
                else:
                    minutes = ((seg["spaces_effective"] / spaces_per_block) * mins_per_block
                               if seg["spaces_known"] else mins_per_block)
                    seg["minutes"] = round(minutes, 2)
                    r_count_ft += length
                    r_count_min += minutes
                    n_blocks += 1
                r_spaces += seg["spaces_effective"] or 0

                if seg["inv_id"] in seen_ids:
                    duplicate_ids.add(seg["inv_id"])
                seen_ids.add(seg["inv_id"])
            else:
                seg["inv_id"] = None
                seg["id_is_manual"] = False
                seg["spaces_effective"] = 0
                seg["spaces_known"] = False
                minutes = length / walk_speed
                seg["minutes"] = round(minutes, 2)
                r_walk_ft += length
                r_walk_min += minutes

        route["end_inv_id"] = seq.take() if (has_line and not own_end) else None
        if own_start:
            route["start_inv_id"] = own_start.get("inv_id")
            route["start_owner"] = own_start["uid"]
        if own_end:
            route["end_inv_id"] = own_end.get("inv_id")
            route["end_owner"] = own_end["uid"]
        for key, owner in (("start_inv_id", own_start), ("end_inv_id", own_end)):
            # An endpoint that a block or lot is standing in for shares that
            # segment's ID on purpose, so it is not a clash.
            if route.get(key) and owner is None:
                if route[key] in seen_ids:
                    duplicate_ids.add(route[key])
                seen_ids.add(route[key])
        # Kept so older saved projects and any external scripts still resolve.
        route["first_id"] = route["start_inv_id"]
        route["last_id"] = route["end_inv_id"]

        line_pts = [pt for seg in route["segments"]
                    if seg.get("kind") != "lot" for pt in seg.get("pts", [])]
        route["start_at"] = ((own_start.get("label_at") if own_start else None)
                             or route.get("start") or (line_pts[0] if line_pts else None))
        route["end_at"] = ((own_end.get("label_at") if own_end else None)
                           or route.get("end") or (line_pts[-1] if line_pts else None))

        route["totals"] = {
            "count_ft": round(r_count_ft, 1),
            "walk_ft": round(r_walk_ft, 1),
            "total_ft": round(r_count_ft + r_walk_ft, 1),
            "total_mi": round((r_count_ft + r_walk_ft) / 5280.0, 2),
            "count_min": round(r_count_min, 1),
            "walk_min": round(r_walk_min, 1),
            "total_min": round(r_count_min + r_walk_min, 1),
            "blocks": n_blocks,             # block faces only
            "lots": n_lots,                 # not in any time or distance figure
            "spaces": r_spaces,             # block faces and lots together
            "lot_spaces": r_lot_spaces,
            "unsurveyed": sum(1 for sg in route["segments"]
                              if sg.get("kind") in ("count", "lot") and not sg.get("spaces_known")),
            "next_id": route.get("end_inv_id") or seq.peek(),
        }

    out["duplicate_ids"] = sorted(duplicate_ids, key=walk_order_key)
    out["grand_totals"] = {
        "routes": len(out["routes"]),
        "blocks": sum(r["totals"]["blocks"] for r in out["routes"]),
        "lots": sum(r["totals"]["lots"] for r in out["routes"]),
        "spaces": sum(r["totals"]["spaces"] for r in out["routes"]),
        "lot_spaces": sum(r["totals"]["lot_spaces"] for r in out["routes"]),
        "total_mi": round(sum(r["totals"]["total_ft"] for r in out["routes"]) / 5280.0, 2),
        "total_min": round(sum(r["totals"]["total_min"] for r in out["routes"]), 1),
    }
    return out


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def export_csv(project):
    p = compute(project)
    rows = [("INV_ID", "ROUTE", "ZONE_PREFIX", "TYPE", "STREET", "LENGTH_FT",
             "SPACES", "SPACES_SOURCE", "EST_MINUTES", "FLAGGED", "NOTE",
             "LABEL_LAT", "LABEL_LNG")]
    for route in p["routes"]:
        if route.get("start_inv_id") and route.get("start_at") and not route.get("start_owner"):
            rows.append((route["start_inv_id"], route["name"], route["prefix"],
                         "start point", "", "", 0, "n/a", "0.00", "", "",
                         "%.6f" % route["start_at"][0], "%.6f" % route["start_at"][1]))
        for seg in route["segments"]:
            if seg["kind"] not in ("count", "lot"):
                continue
            at = seg.get("label_at") or [None, None]
            rows.append((
                seg["inv_id"], route["name"], route["prefix"],
                "parking lot" if seg["kind"] == "lot" else "block face",
                seg.get("street", ""),
                "" if seg["kind"] == "lot" else "%.1f" % seg["length_ft"],
                seg["spaces_effective"] if seg["spaces_known"] else "",
                "entered" if seg["spaces_known"] else "not surveyed",
                # Blank, not 0, for lots: they are not timed (see compute).
                "" if seg["minutes"] is None else "%.2f" % seg["minutes"],
                "yes" if seg["flagged"] else "",
                seg.get("note", ""),
                "" if at[0] is None else "%.6f" % at[0],
                "" if at[1] is None else "%.6f" % at[1],
            ))
        if route.get("end_inv_id") and route.get("end_at") and not route.get("end_owner"):
            rows.append((route["end_inv_id"], route["name"], route["prefix"],
                         "end point", "", "", 0, "n/a", "0.00", "", "",
                         "%.6f" % route["end_at"][0], "%.6f" % route["end_at"][1]))

    def esc(v):
        v = "" if v is None else str(v)
        return '"%s"' % v.replace('"', '""') if any(c in v for c in ',"\n') else v

    return "\n".join(",".join(esc(c) for c in r) for r in rows) + "\n"


# GIS exports. Field names match the CSV and the Text INV_ID that the ArcGIS
# occupancy workflow joins on. Checked against ArcGIS Pro's JSON To Features:
# a feature class holds one geometry type, the first feature in a file sets the
# field types, and ORDER is a reserved word. So the ArcGIS zip has one file per
# geometry type, each file starts with its most complete feature, every feature
# in a file carries the same fields, and the sequence field is WALK_ORDER.

TYPE_NAMES = {"count": "block face", "walk": "walking leg", "lot": "parking lot"}
BLOCK_FIELDS = ("INV_ID", "ROUTE", "ZONE_PREFIX", "WALK_ORDER", "STREET", "LENGTH_FT",
                "SPACES", "SPACES_SOURCE", "EST_MINUTES", "FLAGGED", "NOTE")
LOT_FIELDS = ("INV_ID", "ROUTE", "ZONE_PREFIX", "WALK_ORDER", "STREET", "AREA_SQFT",
              "SPACES", "SPACES_SOURCE", "FLAGGED", "NOTE")
LINE_FIELDS = ("INV_ID", "ROUTE", "ZONE_PREFIX", "WALK_ORDER", "TYPE", "STREET",
               "LENGTH_FT", "EST_MINUTES", "COLOR")
ALL_FIELDS = ("INV_ID", "ROUTE", "ZONE_PREFIX", "WALK_ORDER", "TYPE", "STREET", "LENGTH_FT",
              "SPACES", "SPACES_SOURCE", "EST_MINUTES", "AREA_SQFT", "FLAGGED", "NOTE", "COLOR")


def export_basename(project):
    return re.sub(r"[^A-Za-z0-9._-]", "_", project.get("name") or "routes")


def _lnglat(pt):
    return [round(pt[1], 7), round(pt[0], 7)]       # GeoJSON is [lng, lat]; 7 dp is ~1 cm


def _gis_geometry(seg):
    """GeoJSON geometry for a segment, or None when it has too few points to be
    valid, which ArcGIS would reject."""
    if seg["kind"] == "lot":
        ring = [_lnglat(pt) for pt in seg.get("pts") or []]
        if len({tuple(c) for c in ring}) < 3:
            return None
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        # RFC 7946 wants exterior rings counter-clockwise.
        if sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:])) < 0:
            ring.reverse()
        return {"type": "Polygon", "coordinates": [ring]}
    coords = [_lnglat(pt) for pt in seg.get("pts") or []]
    return {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None


def _gis_props(route, seg, order):
    lot, walk = seg["kind"] == "lot", seg["kind"] == "walk"
    return {
        "INV_ID": seg.get("inv_id"),
        "ROUTE": route["name"],
        "ZONE_PREFIX": route["prefix"],
        "WALK_ORDER": order,
        "TYPE": TYPE_NAMES.get(seg["kind"], seg["kind"]),
        "STREET": seg.get("street", ""),
        "LENGTH_FT": None if lot else seg["length_ft"],
        "SPACES": seg["spaces_effective"] if seg["spaces_known"] else None,
        "SPACES_SOURCE": "n/a" if walk else ("entered" if seg["spaces_known"] else "not surveyed"),
        "EST_MINUTES": seg["minutes"],              # None for lots: they are not timed
        "AREA_SQFT": seg.get("area_sqft") if lot else None,
        "FLAGGED": "yes" if seg["flagged"] else "no",
        "NOTE": seg.get("note", ""),
        "COLOR": route["color"],
    }


def _gis_features(p):
    """Every feature, sorted into the layers ArcGIS needs."""
    layers = {"block_faces": [], "lots": [], "route_lines": [], "start_end": [], "all": []}

    def feature(geom, props, fields):
        return {"type": "Feature", "geometry": geom, "properties": {f: props.get(f) for f in fields}}

    for route in p["routes"]:
        for i, seg in enumerate(route["segments"]):
            geom = _gis_geometry(seg)
            if geom is None:
                continue
            props = _gis_props(route, seg, i + 1)
            layers["all"].append(feature(geom, props, ALL_FIELDS))
            if seg["kind"] == "lot":
                layers["lots"].append(feature(geom, props, LOT_FIELDS))
                continue
            layers["route_lines"].append(feature(geom, props, LINE_FIELDS))
            if seg["kind"] == "count":
                layers["block_faces"].append(feature(geom, props, BLOCK_FIELDS))
        for key, idkey, label in (("start_at", "start_inv_id", "start point"),
                                  ("end_at", "end_inv_id", "end point")):
            if route.get(key):
                geom = {"type": "Point", "coordinates": _lnglat(route[key])}
                props = {"INV_ID": route.get(idkey), "ROUTE": route["name"],
                         "ZONE_PREFIX": route["prefix"], "TYPE": label, "COLOR": route["color"]}
                layers["start_end"].append(feature(geom, props, ("INV_ID", "ROUTE", "ZONE_PREFIX", "TYPE", "COLOR")))
                layers["all"].append(feature(geom, props, ALL_FIELDS))
    return layers


def _feature_collection(feats):
    """A FeatureCollection with its most complete feature first. ArcGIS Pro sets
    the field types from the first feature, so one that opens with blanks could
    leave a numeric field untyped."""
    if feats:
        k = min(range(len(feats)), key=lambda i: sum(v is None for v in feats[i]["properties"].values()))
        feats = [feats[k]] + feats[:k] + feats[k + 1:]
    return json.dumps({"type": "FeatureCollection", "features": feats}, indent=1)


def export_geojson(project):
    """Everything in one file, for QGIS and other tools that take mixed
    geometry. For ArcGIS use export_gis_zip, which splits it by geometry type."""
    return _feature_collection(_gis_features(compute(project))["all"])


CSV_SCHEMA = (("INV_ID", "Text"), ("ROUTE", "Text"), ("ZONE_PREFIX", "Long"), ("TYPE", "Text"),
              ("STREET", "Text"), ("LENGTH_FT", "Double"), ("SPACES", "Long"),
              ("SPACES_SOURCE", "Text"), ("EST_MINUTES", "Double"), ("FLAGGED", "Text"),
              ("NOTE", "Text"), ("LABEL_LAT", "Double"), ("LABEL_LNG", "Double"))


def _schema_ini(csv_name):
    """ArcGIS reads a CSV's field types from a schema.ini beside it. Without one
    it guesses INV_ID is a number, and a Number INV_ID will not join to the Text
    one in the occupancy table ("No join was possible")."""
    lines = ["[%s]" % csv_name, "Format=CSVDelimited", "ColNameHeader=True", "CharacterSet=65001"]
    lines += ["Col%d=%s %s" % (i, name, kind) for i, (name, kind) in enumerate(CSV_SCHEMA, 1)]
    return "\r\n".join(lines) + "\r\n"


def _gis_readme(project, base, layers):
    files = [
        ("block_faces", "Lines. One per counted block face, with INV_ID (Text)."),
        ("lots", "Polygons. One per parking lot, with INV_ID (Text)."),
        ("route_lines", "Lines. The whole walk, walking legs included (TYPE says which)."),
        ("start_end", "Points. Each route's start and end."),
    ]
    out = ["Route Mapper GIS export: %s" % (project.get("name") or "routes"),
           "Exported %s" % time.strftime("%Y-%m-%d %H:%M"), "", "FILES"]
    out += ["  %s_%s.geojson  %s" % (base, key, text) for key, text in files if layers.get(key)]
    out += ["  %s_inventory_ids.csv  The Survey123 seed table, one row per INV_ID." % base,
            "  schema.ini  Makes ArcGIS read INV_ID in the CSV as Text. Keep it next to the CSV.",
            "", "ARCGIS PRO",
            "  1. Unzip this folder inside a folder connected in the Catalog pane.",
            "  2. Analysis > Tools > JSON To Features. Input: the block_faces file. Output: your",
            "     project geodatabase (for example LOCATION_INV_ID). Run it, then again for the",
            "     lots file (for example LOCATION_INV_ID_LOTS).",
            "  3. INV_ID comes in as a Text field, ready for the one-to-many join on INV_ID.",
            "  4. GeoJSON is always WGS 1984. ArcGIS projects it on the fly; run Project if you",
            "     want WGS 1984 Web Mercator (auxiliary sphere) like a hand-drawn feature class.",
            "", "ARCGIS ONLINE",
            "  Content > New item > upload one .geojson at a time and create a hosted feature layer.",
            "", "NOTES",
            "  Parking lots have no EST_MINUTES: lots are not included in route time or distance.",
            "  WALK_ORDER is each piece's position along its route, walking legs included."]
    return "\r\n".join(out) + "\r\n"


def export_gis_zip(project):
    """The ArcGIS package: one GeoJSON per geometry type, plus the CSV with a
    schema.ini and a README, in one zip."""
    p = compute(project)
    base = export_basename(project)
    layers = _gis_features(p)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for key in ("block_faces", "lots", "route_lines", "start_end"):
            if layers[key]:
                z.writestr("%s_%s.geojson" % (base, key), _feature_collection(layers[key]))
        csv_name = base + "_inventory_ids.csv"
        z.writestr(csv_name, export_csv(project))
        z.writestr("schema.ini", _schema_ini(csv_name))
        z.writestr("README.txt", _gis_readme(project, base, layers))
    return buf.getvalue()


def _mercator(lat, lng):
    x = math.radians(lng)
    y = math.log(math.tan(math.pi / 4 + math.radians(max(-85.0, min(85.0, lat))) / 2))
    return x, y


def _plan_frame(p, width, pad, fallback=None):
    """Bounds and scale of a plan sheet in Web Mercator, for an already computed
    project. The SVG, the PDF and the basemap metadata all use this one frame, so
    the tiles the browser drops behind an SVG always line up with the drawing."""
    pts = [pt for route in p["routes"] for seg in route["segments"] for pt in seg["pts"]]
    pts += [route[k] for route in p["routes"] for k in ("start_at", "end_at") if route.get(k)]
    if not pts:
        if not fallback:
            return None
        pts = fallback
    xs, ys = zip(*[_mercator(pt[0], pt[1]) for pt in pts])
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    scale = (width - 2 * pad) / spanx
    return {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy,
            "scale": scale, "pad": pad, "width": width,
            "height": spany * scale + 2 * pad}


def svg_geometry(project, width=2000, pad=70):
    """The exact frame export_svg uses, handed out so the browser can drop
    basemap tiles behind the drawing in the same coordinate space."""
    f = _plan_frame(compute(project), width, pad)
    if f:
        f["height"] = int(f["height"])
    return f


def export_svg(project, width=2000, pad=70):
    """Vector plan sheet. Opens in Illustrator or Figma with every route,
    label and flag on its own layer, so the annotation pass you already do
    starts from real geometry instead of a hand trace."""
    p = compute(project)
    f = _plan_frame(p, width, pad)
    if not f:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">' \
               '<text x="20" y="60" font-family="sans-serif" font-size="15">' \
               'Nothing drawn yet.</text></svg>'
    minx, maxy, scale = f["minx"], f["maxy"], f["scale"]
    height = int(f["height"])

    def P(pt):
        x, y = _mercator(pt[0], pt[1])
        return ((x - minx) * scale + pad, (maxy - y) * scale + pad)

    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
         'viewBox="0 0 %d %d">' % (width, height, width, height),
         '<rect width="%d" height="%d" fill="#ffffff"/>' % (width, height),
         '<!--BASEMAP-->']

    for route in p["routes"]:
        o.append('<g id="%s">' % re.sub(r"[^A-Za-z0-9_-]", "_", route["name"]))
        for seg in route["segments"]:
            if len(seg["pts"]) < 2:
                continue
            if seg["kind"] == "lot":
                d = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", *P(pt))
                             for i, pt in enumerate(seg["pts"])) + " Z"
                o.append('<path d="%s" fill="%s" fill-opacity="0.16" stroke="%s" '
                         'stroke-width="3" stroke-linejoin="round"/>'
                         % (d, route["color"], route["color"]))
                continue
            d = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", *P(pt))
                         for i, pt in enumerate(seg["pts"]))
            dash = '' if seg["kind"] == "count" else ' stroke-dasharray="9 7"'
            w = 5 if seg["kind"] == "count" else 2.6
            o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.1f" '
                     'stroke-linecap="round" stroke-linejoin="round"%s/>'
                     % (d, route["color"], w, dash))
            # Chevrons pointing the way the route is walked.
            for j in range(len(seg["pts"]) - 1):
                ax, ay = P(seg["pts"][j])
                bx, by = P(seg["pts"][j + 1])
                seglen = math.hypot(bx - ax, by - ay)
                if seglen < 26:
                    continue
                ang = math.degrees(math.atan2(by - ay, bx - ax))
                step = 62.0
                dist = min(step, seglen / 2)
                while dist < seglen - 6:
                    t = dist / seglen
                    cx, cy = ax + (bx - ax) * t, ay + (by - ay) * t
                    o.append('<path d="M-4.5,-3.6 L4.5,0 L-4.5,3.6 Z" fill="#ffffff" '
                             'stroke="%s" stroke-width="1" stroke-linejoin="round" '
                             'transform="translate(%.1f,%.1f) rotate(%.1f)"/>'
                             % (route["color"], cx, cy, ang))
                    dist += step
        for seg in route["segments"]:
            if seg["kind"] not in ("count", "lot") or not seg.get("label_at"):
                continue
            x, y = P(seg["label_at"])
            if seg["flagged"]:
                o.append('<circle cx="%.1f" cy="%.1f" r="17" fill="none" '
                         'stroke="#D42A2A" stroke-width="3"/>' % (x, y))
            o.append('<circle cx="%.1f" cy="%.1f" r="12.5" fill="#ffffff" '
                     'stroke="%s" stroke-width="2"/>' % (x, y, route["color"]))
            o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica,Arial" '
                     'font-size="10.5" font-weight="700" fill="#1B2430">%s</text>'
                     % (x, y + 3.6, html.escape(seg["inv_id"])))
        for key, idkey, cap in (("start_at", "start_inv_id", "START"),
                                ("end_at", "end_inv_id", "END")):
            if route.get(key) and route.get(idkey):
                x, y = P(route[key])
                o.append('<circle cx="%.1f" cy="%.1f" r="16" fill="%s" stroke="#ffffff" '
                         'stroke-width="3"/>' % (x, y, route["color"]))
                o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica,Arial" '
                         'font-size="11" font-weight="700" fill="#ffffff">%s</text>'
                         % (x, y + 4, html.escape(route[idkey])))
                o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica,Arial" '
                         'font-size="8" font-weight="700" fill="%s">%s</text>'
                         % (x, y + 27, route["color"], cap))
        o.append('</g>')
    o.append('</svg>')
    return "\n".join(o)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

VIEWER = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — Route Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--ink:#1B2533;--ink2:#233041;--ink3:#2C3A4D;--line:#3A4B60;--paper:#EDF1F4;--muted:#9BADC1;--flag:#E5484D}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:13.5px;color:var(--paper);
 background:var(--ink);display:grid;grid-template-columns:1fr 330px;grid-template-rows:50px 1fr;
 grid-template-areas:"head head" "map side";overflow:hidden}
.num{font-variant-numeric:tabular-nums}
header{grid-area:head;display:flex;align-items:center;gap:12px;padding:0 16px;
 background:var(--ink2);border-bottom:1px solid var(--line)}
header h1{font-size:14.5px;font-weight:600;margin:0}
header span{color:var(--muted);font-size:12.5px}
#map{grid-area:map;background:#263546}
#side{grid-area:side;background:var(--ink2);border-left:1px solid var(--line);overflow-y:auto;padding:14px}
h2{font-size:12.5px;font-weight:600;margin:0 0 8px;color:var(--muted)}
section{margin-bottom:18px;padding-bottom:16px;border-bottom:1px solid var(--line)}
section:last-child{border-bottom:0}
.rt{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-weight:500}
.dot{width:10px;height:10px;border-radius:50%}
dl{display:grid;grid-template-columns:1fr auto;gap:2px 10px;font-size:12.5px;margin:0 0 4px}
dt{color:var(--muted)}dd{margin:0;text-align:right}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td{padding:4px 0;border-bottom:1px solid var(--line)}
td:last-child{text-align:right;color:var(--muted)}
.fl{color:var(--flag)}
.lotnote{margin:0;font-size:12px;line-height:1.5;padding:8px 10px;border:1px solid var(--line);
 border-left:3px solid #5AA9F2;border-radius:6px}
.idlabel{background:#fff;color:#1B2430;border-radius:50%;width:26px;height:26px;display:flex;
 align-items:center;justify-content:center;font-size:10.5px;font-weight:600;border:2px solid #666;
 font-variant-numeric:tabular-nums}
.idlabel.flagged{box-shadow:0 0 0 3px var(--flag)}
.endpt{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;
 color:#fff;font-weight:700;font-size:11px;border:3px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.55)}
.cap{background:#1B2430;color:#fff;font-size:8.5px;font-weight:700;padding:1px 5px;border-radius:3px;
 margin-top:2px;letter-spacing:.04em}
.mutedtiles{filter:grayscale(1) brightness(1.07) contrast(.9)}
.leaflet-container{background:#263546}
@media(max-width:820px){body{grid-template-columns:1fr;grid-template-rows:50px 55vh 1fr;
 grid-template-areas:"head" "map" "side"}#side{border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header><h1>__TITLE__</h1><span id="sub"></span></header>
<div id="map"></div>
<div id="side"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = __DATA__;
const map = L.map('map',{zoomControl:true}).setView(DATA.center||[37.8,-122.25], DATA.zoom||15);
const bases = {
 'Muted': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
   {maxZoom:20,maxNativeZoom:19,className:'mutedtiles',attribution:'&copy; OpenStreetMap contributors'}),
 'Streets': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
   {maxZoom:20,maxNativeZoom:19,attribution:'&copy; OpenStreetMap contributors'}),
 'Aerial': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
   {maxZoom:20,maxNativeZoom:19,attribution:'Imagery &copy; Esri'})};
bases['Muted'].addTo(map);
L.control.layers(bases,null,{position:'topright'}).addTo(map);

// Names, notes and manual IDs are free text; escape them before they become HTML.
const esc = t => String(t == null ? '' : t).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const LOT_NOTE = 'Parking lots, and walking into and around them, are not included in the '
  + 'distance or time. How long a lot takes depends on its layout and how it is counted, '
  + 'which the map cannot tell.';
const all = [];
DATA.routes.forEach(rt => {
  const col = esc(rt.color);
  rt.segments.forEach(seg => {
    if (!seg.pts || seg.pts.length < 2) return;
    all.push(...seg.pts);
    const tip = (seg.inv_id ? '<b>'+esc(seg.inv_id)+'</b><br>' : '')
      + (seg.street ? esc(seg.street)+'<br>' : '')
      + (seg.kind === 'lot' ? 'Parking lot, not timed'
         : seg.kind === 'walk' ? 'Walking leg, not counted'
         : Math.round(seg.length_ft)+' ft')
      + (seg.kind !== 'walk' && seg.spaces_effective ? ' · '+seg.spaces_effective+' spaces' : '')
      + (seg.note ? '<br><i>'+esc(seg.note)+'</i>' : '')
      + (seg.flagged ? '<br><b style="color:#E5484D">Flagged for follow-up</b>' : '');
    const shape = seg.kind === 'lot'
      ? L.polygon(seg.pts,{color:rt.color,weight:3,fillColor:rt.color,fillOpacity:.18})
      : L.polyline(seg.pts,{color:rt.color,weight:seg.kind==='count'?6:3,
          dashArray:seg.kind==='count'?null:'9 8'});
    shape.bindPopup(tip).addTo(map);
    if (seg.inv_id && seg.label_at)
      L.marker(seg.label_at,{zIndexOffset:500,icon:L.divIcon({className:'',iconSize:[26,26],
        html:'<div class="idlabel'+(seg.flagged?' flagged':'')+'" style="border-color:'+col+'">'
             +esc(seg.inv_id)+'</div>'})}).bindPopup(tip).addTo(map);
  });
  [['start_at','start_inv_id','START'],['end_at','end_inv_id','END']].forEach(([k,ik,cap]) => {
    if (!rt[k]) return;
    all.push(rt[k]);
    L.marker(rt[k],{zIndexOffset:1000,icon:L.divIcon({className:'',iconSize:[32,44],
      html:'<div class="endpt" style="background:'+col+'">'+esc(rt[ik]||cap[0])+'</div>'
           +'<div class="cap">'+cap+'</div>'})})
      .bindPopup('<b>'+esc(rt[ik]||cap)+'</b><br>'+cap.toLowerCase()+' of '+esc(rt.name)).addTo(map);
  });
});
if (all.length) map.fitBounds(L.latLngBounds(all).pad(0.12));

const g = DATA.grand_totals;
document.getElementById('sub').textContent =
  g.routes+' route'+(g.routes>1?'s':'')+' · '+g.blocks+' block faces'
  + (g.lots ? ' · '+g.lots+' lot'+(g.lots>1?'s':'') : '')
  + ' · '+g.total_mi+' mi · '+fmt(g.total_min) + (g.lots ? ' · lots not timed' : '');
function fmt(m){m=Math.round(m);return m<60?m+' min':Math.floor(m/60)+'h '+String(m%60).padStart(2,'0');}

let html = g.lots ? '<section><p class="lotnote">'+LOT_NOTE+'</p></section>' : '';
DATA.routes.forEach(rt => {
  const t = rt.totals;
  html += '<section><div class="rt"><span class="dot" style="background:'+esc(rt.color)+'"></span>'+esc(rt.name)+'</div>'
    + '<dl class="num"><dt>Walking time</dt><dd>'+fmt(t.total_min)+'</dd>'
    + '<dt>Distance</dt><dd>'+t.total_mi+' mi</dd>'
    + '<dt>Block faces</dt><dd>'+t.blocks+'</dd>'
    + (t.lots ? '<dt>Parking lots</dt><dd>'+t.lots+' · not timed</dd>' : '')
    + '<dt>Spaces</dt><dd>'+(t.spaces||'—')+'</dd></dl><table>';
  if (rt.start_inv_id) html += '<tr><td><b>'+esc(rt.start_inv_id)+'</b></td><td>start</td></tr>';
  rt.segments.forEach(seg => {
    if (!seg.inv_id) return;
    html += '<tr><td><b>'+esc(seg.inv_id)+'</b>'+(seg.flagged?' <span class="fl">&#9873;</span>':'')+'</td>'
      + '<td>'+(seg.kind==='lot'?'lot':Math.round(seg.length_ft)+' ft')+'</td></tr>';
  });
  if (rt.end_inv_id && !rt.end_owner) html += '<tr><td><b>'+esc(rt.end_inv_id)+'</b></td><td>end</td></tr>';
  html += '</table></section>';
});
html += '<section><p style="color:var(--muted);font-size:12px;line-height:1.5;margin:0">'
  + 'Solid lines are counted block faces. Dashed lines are walking legs that are not counted. '
  + 'Shaded areas are parking lots. A red ring means flagged for follow-up. Click anything for detail.</p></section>';
document.getElementById('side').innerHTML = html;
</script></body></html>
'''


def export_viewer(project):
    """A single self-contained HTML file: the routes, baked in, read-only.

    Opens by double-clicking in any browser on any machine. No Python, no
    install, no account, nothing to run. Needs internet only for the map tiles.
    """
    data = compute(project)
    title = project.get("name") or "Route map"
    safe = json.dumps(data).replace("</", "<\\/")
    return (VIEWER.replace("__DATA__", safe)
                  .replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;")))


def export_pdf(project, width=1000, pad=44):
    """Vector PDF built by hand - no reportlab, no cairo, nothing to install.
    A PDF content stream is just drawing operators, so the same geometry that
    feeds the SVG can be written straight out."""
    p = compute(project)
    # An empty project still gets a blank page, framed on Oakland.
    f = _plan_frame(p, width, pad, fallback=[[37.8, -122.25], [37.81, -122.24]])
    minx, miny, scale, height = f["minx"], f["miny"], f["scale"], f["height"]

    def P(pt):                      # PDF origin is bottom-left
        x, y = _mercator(pt[0], pt[1])
        return ((x - minx) * scale + pad, (y - miny) * scale + pad)

    def rgb(hexcolor):
        h = hexcolor.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    K = 0.5523                      # circle-from-beziers constant
    out = ["1 1 1 rg", "0 0 %.1f %.1f re f" % (width, height)]

    def circle(x, y, r, fill=None, stroke=None, w=1.0):
        if fill:
            out.append("%.3f %.3f %.3f rg" % fill)
        if stroke:
            out.append("%.3f %.3f %.3f RG" % stroke)
            out.append("%.2f w" % w)
        out.append("%.2f %.2f m" % (x + r, y))
        for (cx1, cy1, cx2, cy2, ex, ey) in (
            (x + r, y + r * K, x + r * K, y + r, x, y + r),
            (x - r * K, y + r, x - r, y + r * K, x - r, y),
            (x - r, y - r * K, x - r * K, y - r, x, y - r),
            (x + r * K, y - r, x + r, y - r * K, x + r, y)):
            out.append("%.2f %.2f %.2f %.2f %.2f %.2f c" % (cx1, cy1, cx2, cy2, ex, ey))
        out.append("b" if (fill and stroke) else ("f" if fill else "S"))

    def label(x, y, text, size, color=(0, 0, 0)):
        w = 0.55 * size * len(text)          # Helvetica-Bold, close enough to centre
        out.append("BT /F1 %.1f Tf %.3f %.3f %.3f rg %.2f %.2f Td (%s) Tj ET"
                   % (size, color[0], color[1], color[2], x - w / 2, y - size * 0.35,
                      text.replace("\\", "").replace("(", "").replace(")", "")))

    for route in p["routes"]:
        col = rgb(route["color"])
        out.append("%.3f %.3f %.3f RG" % col)
        out.append("1 J 1 j")
        for seg in route["segments"]:
            if len(seg["pts"]) < 2:
                continue
            path = ["%.2f %.2f m" % P(seg["pts"][0])]
            path += ["%.2f %.2f l" % P(q) for q in seg["pts"][1:]]
            if seg["kind"] == "lot":
                out.append("%.3f %.3f %.3f rg" % col)
                out.append("[] 0 d 2.5 w")
                out.append("/GSlot gs")          # translucent fill, solid outline
                out += path + ["h", "B"]
                out.append("/GSfull gs")
            else:
                out.append("[6 5] 0 d" if seg["kind"] == "walk" else "[] 0 d")
                out.append("%.2f w" % (4.0 if seg["kind"] == "count" else 2.0))
                out += path + ["S"]
        out.append("[] 0 d")
        for seg in route["segments"]:
            if not seg.get("inv_id") or not seg.get("label_at"):
                continue
            x, y = P(seg["label_at"])
            if seg["flagged"]:
                circle(x, y, 13.5, stroke=(0.83, 0.16, 0.16), w=2.4)
            circle(x, y, 10, fill=(1, 1, 1), stroke=col, w=1.6)
            label(x, y, seg["inv_id"], 7.5)
        for key, cap in (("start_at", "start_inv_id"), ("end_at", "end_inv_id")):
            if route.get(key) and route.get(cap):
                x, y = P(route[key])
                circle(x, y, 13, fill=col, stroke=(1, 1, 1), w=2.5)
                label(x, y, route[cap], 7.5, (1, 1, 1))
    stream = "\n".join(out).encode("latin-1", "replace")

    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        ("<</Type/Page/Parent 2 0 R/MediaBox[0 0 %.1f %.1f]/Resources<</Font<</F1 5 0 R>>"
         "/ExtGState<</GSlot<</ca 0.18>>/GSfull<</ca 1>>>>>>"
         "/Contents 4 0 R>>" % (width, height)).encode(),
        b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica-Bold/Encoding/WinAnsiEncoding>>",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(buf)
    buf += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        buf += ("%010d 00000 n \n" % off).encode()
    buf += (b"trailer\n<</Size " + str(len(objs) + 1).encode() + b"/Root 1 0 R>>\nstartxref\n"
            + str(xref).encode() + b"\n%%EOF\n")
    return bytes(buf)


def project_path(name):
    safe = re.sub(r"[^A-Za-z0-9 ._-]", "_", name).strip() or "project"
    return os.path.join(SAVE_DIR, safe + ".json")


def list_projects():
    if not os.path.isdir(SAVE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SAVE_DIR) if f.endswith(".json"))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STATE = {"project": new_project()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200, filename=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        # A charset only means something on text; the PDF and the zip are binary.
        self.send_header("Content-Type", ctype if ctype in ("application/pdf", "application/zip")
                         else ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(HTML, "text/html")
        if u.path == "/api/state":
            return self._send(json.dumps(compute(STATE["project"])))
        if u.path == "/api/export/meta":
            return self._send(json.dumps(svg_geometry(STATE["project"]) or {}))
        if u.path == "/api/geocode":
            term = (q.get("q") or [""])[0].strip()
            if not term:
                return self._send(json.dumps({"results": []}))
            try:
                url = ("https://nominatim.openstreetmap.org/search?format=json"
                       "&limit=6&q=" + urllib.parse.quote(term))
                req = urllib.request.Request(url, headers={
                    # Nominatim's usage policy wants a real identifying agent.
                    "User-Agent": "RouteMapper/1.0 (parking survey planning tool)"})
                with urllib.request.urlopen(req, timeout=12) as fh:
                    raw = json.loads(fh.read().decode("utf-8"))
                out = [{"label": r.get("display_name", ""),
                        "lat": float(r["lat"]), "lng": float(r["lon"]),
                        "bbox": [float(v) for v in r.get("boundingbox", [])] or None}
                       for r in raw]
                return self._send(json.dumps({"results": out}))
            except Exception as e:
                return self._send(json.dumps(
                    {"error": "Place search unavailable (%s). Check your connection." % e}),
                    code=502)
        if u.path == "/api/projects":
            return self._send(json.dumps({"projects": list_projects(),
                                          "dir": SAVE_DIR}))
        if u.path == "/api/load":
            name = (q.get("name") or [""])[0]
            path = project_path(name)
            if not os.path.exists(path):
                return self._send(json.dumps({"error": "No project named %r." % name}), code=404)
            try:
                with open(path) as fh:
                    STATE["project"] = migrate(json.load(fh))
            except Exception as e:
                return self._send(json.dumps(
                    {"error": "Could not read %s: %s" % (path, e)}), code=500)
            return self._send(json.dumps(compute(STATE["project"])))
        if u.path.startswith("/api/export/"):
            kind = u.path.rsplit("/", 1)[-1]
            base = export_basename(STATE["project"])
            if kind == "csv":
                return self._send(export_csv(STATE["project"]), "text/csv",
                                  filename=base + "_inventory_ids.csv")
            if kind == "gis":
                return self._send(export_gis_zip(STATE["project"]), "application/zip",
                                  filename=base + "_arcgis.zip")
            if kind == "geojson":
                return self._send(export_geojson(STATE["project"]), "application/geo+json",
                                  filename=base + ".geojson")
            if kind == "svg":
                return self._send(export_svg(STATE["project"]), "image/svg+xml",
                                  filename=base + ".svg")
            if kind == "pdf":
                return self._send(export_pdf(STATE["project"]), "application/pdf",
                                  filename=base + ".pdf")
            if kind == "viewer":
                return self._send(export_viewer(STATE["project"]), "text/html",
                                  filename=base + "_viewer.html")
            if kind == "json":
                return self._send(json.dumps(STATE["project"], indent=1),
                                  "application/json", filename=base + ".json")
        self._send(json.dumps({"error": "Not found"}), code=404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(json.dumps({"error": "Bad request body: %s" % e}), code=400)

        if u.path == "/api/state":
            STATE["project"] = payload
            return self._send(json.dumps(compute(STATE["project"])))
        if u.path == "/api/save":
            STATE["project"] = payload.get("project", STATE["project"])
            os.makedirs(SAVE_DIR, exist_ok=True)
            path = project_path(STATE["project"].get("name", "project"))
            with open(path, "w") as fh:
                json.dump(STATE["project"], fh, indent=1)
            return self._send(json.dumps({"saved": path}))
        if u.path == "/api/new":
            STATE["project"] = new_project(payload.get("name") or "Untitled study area")
            return self._send(json.dumps(compute(STATE["project"])))
        self._send(json.dumps({"error": "Not found"}), code=404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Route Mapper</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#1B2533; --ink2:#233041; --ink3:#2C3A4D; --line:#3A4B60;
  --paper:#EDF1F4; --muted:#9BADC1; --accent:#5AA9F2; --flag:#E5484D;
  --rail:352px; --list:376px;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:13.5px;
  color:var(--paper);background:var(--ink);overflow:hidden;
  display:grid;grid-template-columns:var(--rail) 1fr var(--list);grid-template-rows:52px 1fr 34px;
  grid-template-areas:"head head head" "rail map list" "foot foot foot";
}
.num{font-variant-numeric:tabular-nums}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}

header{grid-area:head;display:flex;align-items:center;gap:14px;padding:0 16px;
  background:var(--ink2);border-bottom:1px solid var(--line)}
header h1{font-size:14.5px;font-weight:600;margin:0;letter-spacing:.01em}
header h1 span{color:var(--muted);font-weight:400}
#pname{background:var(--ink3);border:1px solid var(--line);border-radius:5px;
  padding:5px 9px;width:190px}
#findwrap{position:relative}
#find{background:var(--ink3);border:1px solid var(--line);border-radius:5px;
  padding:5px 9px;width:300px}
#find:focus{outline:none;border-color:var(--accent)}
#findlist{position:absolute;top:34px;left:0;width:430px;background:var(--ink2);
  border:1px solid var(--line);border-radius:7px;box-shadow:0 10px 26px rgba(0,0,0,.5);
  z-index:1200;display:none;overflow:hidden}
#findlist button{display:block;width:100%;text-align:left;background:transparent;
  border:0;border-bottom:1px solid var(--line);padding:8px 11px;font-size:12.5px;line-height:1.35}
#findlist button:last-child{border-bottom:0}
#findlist button:hover{background:var(--ink3)}
.arw{width:0;height:0;border-left:9px solid #fff;
  border-top:5.5px solid transparent;border-bottom:5.5px solid transparent;
  filter:drop-shadow(0 0 1.5px rgba(0,0,0,.85))}
.mutedtiles{filter:grayscale(1) brightness(1.07) contrast(.9)}
.vtx{width:13px;height:13px;border-radius:50%;border:2px solid #fff;
  box-shadow:0 1px 3px rgba(0,0,0,.5);cursor:move}
.vtx.vsel{box-shadow:0 0 0 4px var(--accent);width:15px;height:15px}
.endtab{width:18px;height:18px;border-radius:5px;background:#fff;border:3px solid var(--accent);
  box-shadow:0 1px 4px rgba(0,0,0,.55);cursor:grab}
.endtab.fin{background:var(--accent);border-color:#fff}
.lotpin{width:16px;height:16px;border-radius:50%;background:var(--accent);border:3px solid #fff;
  box-shadow:0 1px 4px rgba(0,0,0,.6);cursor:grab}
.spacer{flex:1}
.hbtn{background:var(--ink3);border:1px solid var(--line);border-radius:5px;padding:6px 11px}
.hbtn:hover{border-color:var(--muted)}
.hbtn.go{background:var(--accent);border-color:var(--accent);color:#1B2430;font-weight:600}
.opt{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:12px;white-space:nowrap}

#rail{grid-area:rail;background:var(--ink2);border-right:1px solid var(--line);
  overflow-y:auto;padding:14px}
#list{grid-area:list;background:var(--ink2);border-left:1px solid var(--line);
  overflow-y:auto;padding:14px}
#map{grid-area:map;background:#263546}
footer{grid-area:foot;background:var(--ink2);border-top:1px solid var(--line);
  display:flex;align-items:center;gap:16px;padding:0 16px;color:var(--muted);font-size:12.5px}
#hint{color:var(--paper)}

h2{font-size:12.5px;font-weight:600;margin:0 0 8px;color:var(--muted)}
section{margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid var(--line)}
section:last-child{border-bottom:0}

.tools{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.tool{background:var(--ink3);border:1px solid var(--line);border-radius:6px;
  padding:9px 8px;text-align:left;line-height:1.25}
.tool b{display:block;font-weight:500}
.tool i{font-style:normal;color:var(--muted);font-size:11.5px}
.tool.on{background:var(--accent);border-color:var(--accent);color:#1B2430}
.tool.on i{color:#16324F}
.tool.wide{grid-column:1/-1}

.rowline{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.rowline label{color:var(--muted);flex:1;font-size:12.5px}
.rowline input[type=number],.rowline input[type=text],.rowline select{
  background:var(--ink3);border:1px solid var(--line);border-radius:5px;padding:5px 7px;width:96px}
.rowline input[type=color]{width:40px;height:28px;padding:2px;background:var(--ink3);
  border:1px solid var(--line);border-radius:5px}

.tabs{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.tab{background:var(--ink3);border:1px solid var(--line);border-radius:999px;
  padding:5px 11px;display:flex;align-items:center;gap:6px}
.tab .dot{width:9px;height:9px;border-radius:50%}
.tab.on{border-color:var(--paper)}

.stats{display:grid;grid-template-columns:1fr auto;gap:3px 10px;font-size:12.5px}
.stats dt{color:var(--muted)}
.stats dd{margin:0;text-align:right}
.stats .big{font-size:19px;font-weight:600;padding-top:2px}

.blk{border:1px solid var(--line);border-radius:7px;margin-bottom:6px;background:var(--ink3);
  border-left-width:4px}
.blk.walkrow{background:transparent;border-style:dashed}
.blk.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.blk.dup{border-color:var(--flag)}
.blkhead{display:flex;align-items:center;gap:8px;padding:7px 9px}
.blkhead .idtag{font-weight:600;min-width:52px}
.blkhead .idtag.man{color:var(--accent)}
.blkhead .meta{color:var(--muted);font-size:12px;flex:1;text-align:right}
.mini{background:transparent;border:1px solid var(--line);border-radius:4px;
  width:24px;height:23px;line-height:1;padding:0;color:var(--muted)}
.mini:hover{color:var(--paper);border-color:var(--muted)}
.mini.flagon{color:var(--flag);border-color:var(--flag)}
.blkbody{padding:0 9px 9px;display:none;gap:6px;flex-direction:column}
.blk.sel .blkbody{display:flex}
.blkbody input{background:var(--ink2);border:1px solid var(--line);border-radius:5px;
  padding:5px 7px;width:100%}
.f2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.tiny{color:var(--muted);font-size:11.5px;line-height:1.4}
.warn{background:#3A2126;border:1px solid var(--flag);border-radius:6px;padding:8px 10px;
  margin-bottom:10px;font-size:12.5px}
.empty{color:var(--muted);font-size:12.5px;line-height:1.55}
.lotnote{display:none;margin:10px 0 0;padding:7px 9px;border-left:3px solid var(--accent);
  background:var(--ink3);border-radius:4px}
.idlabel{background:#fff;color:#1B2430;border-radius:50%;width:26px;height:26px;
  display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:600;
  border:2px solid #666;font-variant-numeric:tabular-nums}
.idlabel.flagged{box-shadow:0 0 0 3px var(--flag)}
.endpt{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;color:#fff;font-weight:700;font-size:12px;
  border:3px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.55);
  font-variant-numeric:tabular-nums;margin-left:-1px}
.endcap{text-align:center;font-size:8.5px;font-weight:700;letter-spacing:.09em;
  color:#fff;background:rgba(20,28,36,.82);border-radius:3px;margin-top:2px;
  padding:1px 3px;width:38px;margin-left:-3px}
.endpt.isend{box-shadow:0 1px 6px rgba(0,0,0,.55), inset 0 0 0 2px rgba(255,255,255,.85)}
.idlabel{cursor:grab}
#lotbar{display:none;align-items:center;gap:9px;margin-top:9px;padding:8px 10px;
  background:var(--ink3);border:1px solid var(--accent);border-radius:7px}
#lotbar span{flex:1;color:var(--muted);font-size:12.5px}
#lotfinish{background:var(--accent);border:1px solid var(--accent);border-radius:5px;
  color:#1B2430;font-weight:600;padding:5px 10px}
#lotfinish:disabled{opacity:.45;cursor:default}
#lotcancel{background:transparent;border:1px solid var(--line);border-radius:5px;padding:5px 10px}
#insbar{display:none;flex-wrap:wrap;align-items:center;gap:8px;margin-top:9px;padding:8px 10px;
  background:var(--ink3);border:1px solid var(--accent);border-radius:7px}
#insbar span{flex:1 1 100%;font-size:12.5px;line-height:1.4}
#insbar label{margin-right:auto}
#insdone{background:var(--accent);border:1px solid var(--accent);border-radius:5px;
  color:#1B2430;font-weight:600;padding:5px 12px}
#inscancel{background:transparent;border:1px solid var(--line);border-radius:5px;padding:5px 10px}
.addgap{display:block;width:100%;background:transparent;border:1px dashed var(--line);border-radius:7px;
  margin-bottom:6px;padding:6px 10px;color:var(--muted);font-size:12px;text-align:left}
.addgap:hover,.mini.addafter:hover{color:var(--accent);border-color:var(--accent)}
.leaflet-container{background:#263546}
kbd{background:var(--ink3);border:1px solid var(--line);border-radius:3px;padding:1px 5px;
  font-family:inherit;font-size:11.5px}
</style>
</head>
<body>

<header>
  <h1>Route Mapper</h1>
  <input id="pname" placeholder="Study area name">
  <div id="findwrap">
    <input id="find" placeholder="Find a place, then Enter…  e.g. Grand Ave &amp; Lakeshore, Oakland" autocomplete="off">
    <div id="findlist"></div>
  </div>
  <div class="spacer"></div>
  <button class="hbtn" id="bNew">New</button>
  <select id="bLoad" class="hbtn"><option value="">Open…</option></select>
  <button class="hbtn" id="bImport">Import file…</button>
  <button class="hbtn" id="bMerge" title="Add another project's routes alongside these">Merge file…</button>
  <input type="file" id="fileIn" accept=".json,application/json" style="display:none">
  <button class="hbtn go" id="bSave">Save</button>
  <select id="bExport" class="hbtn">
    <option value="">Export…</option>
    <option value="csv">Inventory IDs (CSV)</option>
    <option value="pdf">Plan sheet (PDF)</option>
    <option value="png">Image (PNG)</option>
    <option value="svg">Vector (SVG)</option>
    <option value="gis">GIS layers for ArcGIS (.zip)</option>
    <option value="geojson">GeoJSON, one file (QGIS)</option>
    <option value="viewer">Shareable viewer (HTML)</option>
    <option value="json">Project file (JSON)</option>
  </select>
  <label class="opt" title="Draw the map behind SVG and PNG exports. PDF stays vector-only.">
    <input type="checkbox" id="optMapBg" checked> map behind
  </label>
  <select id="optBase" class="hbtn" title="Which basemap to bake into the export">
    <option value="muted">Muted</option>
    <option value="streets">Streets</option>
    <option value="aerial">Aerial</option>
  </select>
</header>

<div id="rail">
  <section>
    <h2>Draw</h2>
    <div class="tools">
      <button class="tool" data-tool="count"><b>Count block</b><i>solid · gets an ID</i></button>
      <button class="tool" data-tool="walk"><b>Walk only</b><i>dashed · no ID</i></button>
      <button class="tool" data-tool="lot"><b>Parking lot</b><i>draw its outline</i></button>
      <button class="tool" data-tool="flag"><b>Flag</b><i>circle in red</i></button>
      <button class="tool" data-tool="start"><b>Start point</b><i>place S</i></button>
      <button class="tool" data-tool="end"><b>End point</b><i>place E</i></button>
      <button class="tool wide" data-tool="none"><b>Select / pan</b><i>click a block to edit it</i></button>
    </div>
    <div class="rowline" style="margin-top:9px">
      <label for="optSnap">Snap to nearby lines and corners</label>
      <input id="optSnap" type="checkbox" checked style="width:auto">
    </div>
    <div class="rowline">
      <label for="optJoints">Dragging keeps the route joined</label>
      <input id="optJoints" type="checkbox" checked style="width:auto">
    </div>
    <div id="lotbar">
      <span id="lotcount"></span>
      <button id="lotcancel">Discard</button>
      <button id="lotfinish">Finish shape</button>
    </div>
    <div id="insbar">
      <span id="instext"></span>
      <label class="opt" title="Add the walking legs needed to reach the new blocks and get back to the route, so that walk is counted">
        <input type="checkbox" id="insJoin" checked> join up</label>
      <button id="inscancel">Discard</button>
      <button id="insdone">Done</button>
    </div>
    <p class="tiny" style="margin:9px 0 0">Click along the street to lay the route down.
    Each click closes the leg behind it, so switching between counting and walking
    mid-route just means clicking the other tool and carrying on. A parking lot is tied
    to the route by a line where it is counted; click the lot to see the line, and drag
    its end along the route to move the lot in the walking order. Missed blocks? Click
    <b>＋</b> in the list, then draw them starting on the route where they branch off and
    finish back on it; they're stitched into the walk there and the IDs renumber.
    <kbd>C</kbd> count · <kbd>W</kbd> walk · <kbd>L</kbd> lot · <kbd>A</kbd> direction arrows · <kbd>I</kbd> add blocks at the selected joint · <kbd>P</kbd> corner handles · <kbd>Esc</kbd> select · <kbd>⌘Z</kbd> undo · <kbd>⇧⌘Z</kbd> redo · <kbd>Delete</kbd> removes the selected block. Drag an ID to move that block face, or drag a corner handle or endpoint to adjust the line. A selected block shows its own two ends as blue tabs: drag one to pull just that end away (it keeps its number), and drop it on another block's end to join them.</p>
  </section>

  <section>
    <h2>Routes</h2>
    <div class="tabs" id="tabs"></div>
    <div class="rowline"><label for="rname">Name</label><input id="rname" type="text"></div>
    <div class="rowline"><label for="rprefix">Zone number</label><input id="rprefix" type="number" min="1" step="1"></div>
    <div class="rowline"><label for="rcolor">Colour</label><input id="rcolor" type="color"></div>
    <div class="rowline" style="margin-top:10px">
      <button class="hbtn" id="bReverse" style="flex:1">Reverse direction</button>
    </div>
    <p class="tiny" style="margin:0 0 10px">Flips which end is the start and renumbers
    every ID to match the new walking order.</p>
    <div class="rowline">
      <button class="hbtn" id="bClearStart">Reset S</button>
      <button class="hbtn" id="bClearEnd">Reset E</button>
    </div>
    <p class="tiny" style="margin:0 0 10px">S and E sit on the first and last point of the
    route unless you place them yourself. Resetting snaps them back onto the line.</p>
    <div class="rowline">
      <button class="hbtn" id="bAddRoute">Add route</button>
      <button class="hbtn" id="bDelRoute">Delete route</button>
    </div>
  </section>

  <section>
    <h2>Timing model</h2>
    <div class="rowline"><label for="sMin">Minutes per block</label><input id="sMin" type="number" step="0.25" min="0"></div>
    <div class="rowline"><label for="sSp">Spaces that counts as a block</label><input id="sSp" type="number" step="1" min="1"></div>
    <div class="rowline"><label for="sWalk">Walking pace, ft/min</label><input id="sWalk" type="number" step="10" min="1"></div>
    <div class="rowline"><label for="sSkip">Skip 10, 20, 30…</label><input id="sSkip" type="checkbox" style="width:auto"></div>
    <p class="tiny">A block face takes <span id="fx" class="num"></span>. Connector legs are
    distance ÷ pace. Fruitvale and Grand Lake both skip every multiple of ten in their
    IDs, so that stays on unless a new study area drops the convention.</p>
  </section>

  <section>
    <h2>This route</h2>
    <dl class="stats num" id="rstats"></dl>
  </section>

  <section>
    <h2>All routes</h2>
    <dl class="stats num" id="gstats"></dl>
    <p class="tiny lotnote" id="lotnote">Parking lots, and walking into and around them,
    are not included in the distance or time. How long a lot takes depends on its layout
    and how it is counted, which the map cannot tell.</p>
  </section>
</div>

<div id="map"></div>

<div id="list">
  <h2>Blocks in walking order</h2>
  <div id="dupwarn"></div>
  <div id="blocks"></div>
</div>

<footer>
  <span id="hint">Pick a drawing tool to begin.</span>
  <div class="spacer"></div>
  <span id="saved"></span>
</footer>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const $ = s => document.querySelector(s);
// Names, notes and manual IDs are free text, and a project can be imported from
// someone else's file, so anything typed goes through this before innerHTML.
const esc = t => String(t == null ? '' : t).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
// Silent failures were the hardest part of the last round to diagnose, so any
// error now says so in the footer instead of vanishing into the console.
window.addEventListener('error', e => note('Error: ' + e.message, true));
window.addEventListener('unhandledrejection', e =>
  note('Error: ' + ((e.reason && e.reason.message) || e.reason), true));
let P = null, tool = 'none', activeRoute = 0, selected = null, lotPts = [],
    lotFor = null, toolBeforeLot = 'none',
    dragging = false, showPts = true, jointsOn = true, snapOn = true, mergeMode = false,
    breakAway = false, selVertex = null;
let undoStack = [], redoStack = [], snapshot = null;

const map = L.map('map', {zoomControl:true, doubleClickZoom:false}).setView([37.7749,-122.2241], 15);
// All three tile providers below are free and need no API key.
const bases = {
  streets: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    {maxZoom:20, maxNativeZoom:19, crossOrigin:true, attribution:'&copy; OpenStreetMap contributors'}),
  aerial: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {maxZoom:20, maxNativeZoom:19, crossOrigin:true, attribution:'Imagery &copy; Esri'}),
  // Esri's light-grey canvas only has real tiles to z16, so it turned to mush the
  // moment you zoomed in on a block face. This is the standard OSM raster -
  // sharp to z19 - desaturated in CSS instead. Same muted look, still no API key.
  quiet: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    {maxZoom:20, maxNativeZoom:19, crossOrigin:true, className:'mutedtiles',
     attribution:'&copy; OpenStreetMap contributors'})
};
bases.quiet.addTo(map);   // muted is the default: routes read clearly against it
L.control.layers({'Muted':bases.quiet,'Streets':bases.streets,'Aerial':bases.aerial},
                 null, {position:'topright', collapsed:false}).addTo(map);
const layer = L.layerGroup().addTo(map);
const arrows = L.layerGroup().addTo(map);
const draft = L.layerGroup().addTo(map);
// A dashed preview from the pen to the pointer, so you can see where the next
// click will lay a leg. Most useful when adding a missed block mid-route.
const rubber = L.polyline([], {color:'#5AA9F2', weight:2, dashArray:'4 6', interactive:false});

async function api(path, opts){
  const r = await fetch(path, opts);
  const j = await r.json();
  if (j && j.error){ note(j.error, true); throw new Error(j.error); }
  return j;
}
function note(msg, bad){
  const el = $('#saved');
  el.textContent = msg; el.style.color = bad ? 'var(--flag)' : 'var(--muted)';
  clearTimeout(note._t); note._t = setTimeout(() => { el.textContent = ''; }, 7000);
}
// One request at a time, newest wins. The server is threaded, so two quick edits
// (holding ⌘Z, say) could otherwise arrive out of order and leave it holding an
// older project than the one on screen, which is what Save and Export then use.
// A reply that is already out of date is dropped rather than drawn, so an edit
// made while it was in flight is never overwritten.
let sending = null, queued = null;
function push(record){
  if (record !== false && snapshot){
    undoStack.push(snapshot);
    if (undoStack.length > 80) undoStack.shift();
    redoStack = [];
  }
  reattachLots();
  queued = JSON.stringify(strip(P));
  if (!sending) sending = (async () => {
    try {
      while (queued !== null){
        const body = queued; queued = null;
        const fresh = await api('/api/state', {method:'POST',
          headers:{'Content-Type':'application/json'}, body: body});
        snapshot = body;
        if (queued === null){ P = fresh; render(); }
      }
    } catch (err) {
      queued = null;
      note('Could not apply that change: ' + err.message, true);
    } finally { sending = null; }
  })();
  return sending;
}
async function restore(json){
  P = JSON.parse(json);
  snapshot = json;
  await push(false);
}
// Opening, importing or starting a project begins a fresh history. Without this
// the first edit after opening could not be undone, and ⌘Z after opening a
// second project brought back the first one.
function freshHistory(){
  undoStack = []; redoStack = [];
  snapshot = JSON.stringify(strip(P));
}
async function undo(){
  if (!undoStack.length) return note('Nothing left to undo');
  redoStack.push(snapshot);
  await restore(undoStack.pop());
  note('Undone');
}
async function redo(){
  if (!redoStack.length) return note('Nothing to redo');
  undoStack.push(snapshot);
  await restore(redoStack.pop());
  note('Redone');
}
function strip(p){
  const c = JSON.parse(JSON.stringify(p));
  c.center = [map.getCenter().lat, map.getCenter().lng]; c.zoom = map.getZoom();
  delete c.duplicate_ids; delete c.grand_totals;
  c.routes.forEach(r => { delete r.totals; delete r.start_at; delete r.end_at;
    delete r.start_inv_id; delete r.end_inv_id; delete r.start_owner; delete r.end_owner; delete r.first_id; delete r.last_id;
    r.segments.forEach(s => {['length_ft','label_at','label_custom','inv_id','minutes',
      'spaces_effective','spaces_known','id_is_manual','area_sqft','perimeter_ft','anchor_at']
      .forEach(k => delete s[k]);}); });
  delete c.lots;
  return c;
}
const R = () => P.routes[activeRoute];

// ---- drawing -------------------------------------------------------------
// Dragging one corner has to move it on every leg that shares it, or the route
// tears apart at the joint.
function moveVertex(route, from, to){
  route.segments.forEach(sg => sg.pts.forEach((pt,i) => {
    if (samePt(pt, from)) sg.pts[i] = to.slice();
  }));
  if (samePt(route.start, from)) route.start = to.slice();
  if (samePt(route.end, from)) route.end = to.slice();
}
// Moving a whole block face: shift its own points, then pull the neighbouring
// legs' shared ends along so the line stays continuous.
function translateSeg(route, seg, dLat, dLng){
  const before = seg.pts.map(pt => pt.slice());
  seg.pts = seg.pts.map(pt => [pt[0]+dLat, pt[1]+dLng]);
  if (!jointsOn || breakAway) return;   // Alt/Option breaks a block away on purpose
  before.forEach((old, i) => {
    route.segments.forEach(other => {
      if (other === seg) return;
      other.pts.forEach((pt, j) => { if (samePt(pt, old)) other.pts[j] = seg.pts[i].slice(); });
    });
    if (samePt(route.start, old)) route.start = seg.pts[i].slice();
    if (samePt(route.end, old)) route.end = seg.pts[i].slice();
  });
}
// Snap a dragged point onto a nearby corner or line so pieces actually join up.
// Vertices win over lines, because landing exactly on a shared corner is what
// makes the route continuous.
const SNAP_PX = 16;
let showArrows = true;

// ---- adding missed blocks --------------------------------------------------
// You draw the missed blocks as a path; nothing changes until you finish, then
// the path is stitched into the walk where it touches the route, so it is one
// step to undo. Where it goes:
//  - Start (or finish) on the route and the new blocks branch off at exactly that
//    point. A block that the point falls in the middle of is cut in two.
//  - Come back to the point you left from and they form a loop there.
//  - Come back further along and they rejoin there. If the stretch they skip has
//    no counted blocks it is replaced. If it does, it is kept: the new blocks are
//    walked back from its far end and a walking leg follows the street on to
//    where the walk carries on, so nothing is dropped and no leg runs against
//    the walk.
//  - Touch the route at neither end and they go after the block whose ＋ you
//    clicked (or the nearest point on the route), with walking legs out and back.
// The first version added each leg straight after the ＋ block as you clicked,
// which numbered detours from the middle of a block in the wrong place and drew
// connecting legs back over the route against the walk.
const ATTACH_PX = 16;
let ins = null;   // {route, anchor (uid | '^' | null), path: [[lat, lng]...], kinds: [...]}
// Close enough to be the same spot on the street: within a fingertip on screen
// or 15 m on the ground. Coming back to a corner and clicking a little to one
// side of it would otherwise cut a sliver off the block there.
const sameSpot = (a, b) => samePt(a, b) || near(a, b) || map.distance(a, b) < 15;
function startInsert(ri, anchor, pen){
  if (ins) finishInsert();
  activeRoute = ri; selected = null;
  ins = {route: P.routes[ri].uid, anchor: anchor || null, path: pen ? [pen.slice()] : [], kinds: []};
  if (tool !== 'count' && tool !== 'walk') setTool('count');
  render();
}
function insLabel(uid){
  if (uid === '^') return 'the start';
  const seg = P.routes.flatMap(r => r.segments).find(sg => sg.uid === uid);
  return seg ? (seg.inv_id || 'the walking leg') : 'the gap';
}
function updateInsBar(){
  const bar = $('#insbar');
  if (!ins){ bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  const n = ins.kinds.length;
  $('#instext').textContent = !ins.path.length
    ? 'Adding missed blocks' + (ins.anchor ? ' (after ' + insLabel(ins.anchor) + ' unless you start on the route)' : '')
      + '. Click where they start; start on the route to branch off at that point.'
    : 'Adding missed blocks · ' + n + (n === 1 ? ' leg' : ' legs') + '. Click the end of each block face'
      + ' (C / W switch count and walk). Click back on the route to finish there, or press Done.'
      + ' Backspace removes the last point.';
}
// Where on a route's walk a point lies: the leg (`idx` in walking order, `seg`
// and `u` within it), how far along that leg (`t`, 0..1) and the point itself,
// or null when it is more than `tol` px away (no limit if tol is null). Lands
// exactly on a corner when close to one, so joints stay shared.
function routePos(rt, pt, tol){
  const c = map.latLngToContainerPoint(pt);
  let best = null;
  rt.segments.forEach((sg, idx) => {
    if (sg.kind === 'lot' || sg.pts.length < 2) return;
    const xy = sg.pts.map(q => map.latLngToContainerPoint(q));
    const lens = xy.slice(1).map((b, i) => Math.hypot(b.x - xy[i].x, b.y - xy[i].y));
    const total = lens.reduce((a, b) => a + b, 0) || 1;
    let run = 0;
    lens.forEach((len, i) => {
      const a = xy[i], b = xy[i+1];
      const u = len ? Math.max(0, Math.min(1, ((c.x-a.x)*(b.x-a.x) + (c.y-a.y)*(b.y-a.y)) / (len*len))) : 0;
      const x = a.x + u*(b.x-a.x), y = a.y + u*(b.y-a.y), d = Math.hypot(c.x-x, c.y-y);
      if (!best || d < best.d - 1e-9) best = {d, idx, uid: sg.uid, seg: i, u, t: (run + u*len) / total, x, y};
      run += len;
    });
  });
  if (!best || (tol != null && best.d > tol)) return null;
  const sg = rt.segments[best.idx];
  const k = sg.pts.findIndex(q => { const p = map.latLngToContainerPoint(q);
    return Math.hypot(p.x - best.x, p.y - best.y) < 6; });
  if (k >= 0){
    best.pt = sg.pts[k].slice();
    if (k === 0){ best.seg = 0; best.u = 0; best.t = 0; }
    else if (k === sg.pts.length - 1){ best.seg = k - 1; best.u = 1; best.t = 1; }
  } else {
    const ll = map.containerPointToLatLng(L.point(best.x, best.y));
    best.pt = [ll.lat, ll.lng];
  }
  return best;
}
// Cut the route at a point on it. Returns the uid of the leg that now ends
// there, or '^' when the point is the very start of the walk. A cut block keeps
// its ID and details on the first half; the second half is a new block on the
// same street (so it gets the next ID). Lots tied to it are re-tied to the half
// they sit by.
function cutAt(rt, pos){
  const sg = rt.segments.find(s => s.uid === pos.uid);
  const pts = sg.pts;
  if (samePt(pos.pt, pts[pts.length-1])) return sg.uid;
  const at = rt.segments.indexOf(sg);
  if (samePt(pos.pt, pts[0])){
    const prev = rt.segments.slice(0, at).reverse().find(s => s.kind !== 'lot' && s.pts.length >= 2);
    return prev ? prev.uid : '^';
  }
  const k = pts.findIndex(q => samePt(q, pos.pt));
  const head = k > 0 ? pts.slice(0, k + 1) : pts.slice(0, pos.seg + 1).concat([pos.pt]);
  const tail = k > 0 ? pts.slice(k) : [pos.pt].concat(pts.slice(pos.seg + 1));
  const rest = mkSeg(sg.kind, tail.map(q => q.slice()));
  rest.street = sg.street;
  if (sg.is_route_end){ rest.is_route_end = true; sg.is_route_end = false; }
  sg.pts = head.map(q => q.slice());
  rt.segments.splice(at + 1, 0, rest);
  rt.segments.forEach(l => {
    if (l.kind === 'lot' && l.anchor_leg === sg.uid && l.anchor) attachLot(rt, l, l.anchor);
  });
  return sg.uid;
}
function insertClick(pt){
  const rt = P.routes.find(r => r.uid === ins.route);
  if (!rt) return finishInsert();
  const last = ins.path[ins.path.length - 1];
  if (last && near(pt, last)) return;               // the second click of a double-click
  const on = routePos(rt, pt, ATTACH_PX);
  let hit = on ? on.pt : pt;
  // Back where you left the route: close the loop exactly.
  if (on && ins.path.length && routePos(rt, ins.path[0], 1) && sameSpot(hit, ins.path[0])) hit = ins.path[0].slice();
  ins.path.push(hit);
  if (ins.path.length > 1) ins.kinds.push(tool);
  if (on && ins.path.length > 1) return finishInsert();   // back on the route: done
  drawDraft(); updateInsBar();
}
function undoInsertPoint(){
  ins.path.pop();
  ins.kinds.length = Math.max(0, ins.path.length - 1);
  drawDraft(); updateInsBar();
}
// Stitch the drawn path into the walk (see the top of this section).
function finishInsert(discard){
  const s = ins; ins = null;
  rubber.remove(); drawDraft(); updateInsBar();
  if (discard || !s || s.path.length < 2) return render();
  const rt = P.routes.find(r => r.uid === s.route);
  if (!rt) return render();
  const join = $('#insJoin').checked;
  let path = s.path.map(q => q.slice()), kinds = s.kinds.slice();
  const flip = () => { path.reverse(); kinds.reverse(); };
  const first = () => path[0], lastPt = () => path[path.length - 1];
  const legs = () => path.slice(1).map((q, i) => mkSeg(kinds[i], [path[i].slice(), q.slice()]));
  const put = (afterUid, segs) => {
    const i = afterUid === '^' ? 0 : rt.segments.findIndex(x => x.uid === afterUid) + 1;
    rt.segments.splice(i, 0, ...segs);
    return segs[segs.length - 1].uid;
  };
  const later = (p, q) => p.idx !== q.idx ? p.idx > q.idx : p.t > q.t;
  let A = routePos(rt, first(), 1), B = routePos(rt, lastPt(), 1), how, added;

  if (A && B && sameSpot(A.pt, B.pt)){
    path[path.length - 1] = A.pt.slice();
    how = 'as a loop from that point';
    put(cutAt(rt, A), added = legs());
  } else if (A && B){
    if (later(A, B)){ flip(); A = routePos(rt, first(), 1); }
    const xAfter = cutAt(rt, A);
    const yAfter = cutAt(rt, routePos(rt, lastPt(), 1));   // after the first cut, so it finds the right half
    const i0 = xAfter === '^' ? 0 : rt.segments.findIndex(x => x.uid === xAfter) + 1;
    const i1 = rt.segments.findIndex(x => x.uid === yAfter) + 1;
    const skipped = rt.segments.slice(i0, i1).filter(x => x.kind !== 'lot');
    if (!skipped.some(x => x.kind === 'count')){
      how = skipped.length ? 'in place of the walking leg they bypass' : 'between the blocks either side';
      rt.segments = rt.segments.filter(x => !skipped.includes(x));
      put(xAfter, added = legs());
    } else {
      // Keep the skipped blocks, walk the new ones back from their far end, then
      // follow the street on to where the walk carries on.
      flip();
      const tail = put(yAfter, added = legs());
      if (join) put(tail, [mkSeg('walk', skipped.flatMap((x, k) => k ? x.pts.slice(1) : x.pts).map(q => q.slice()))]);
      how = () => 'after ' + insLabel(yAfter) + ', walked back round from there' + (join ? ' with a walking leg on' : '');
    }
  } else {
    // One end on the route (a spur), or neither: go out from one point and back to it.
    let at = A || B;
    if (B && !A) flip();
    if (!at){
      const end = rt.segments.find(x => x.uid === s.anchor);
      const firstLeg = rt.segments.find(x => x.kind !== 'lot' && x.pts.length >= 2);
      const anchorPt = s.anchor === '^' ? firstLeg && firstLeg.pts[0]
        : end && end.kind !== 'lot' ? end.pts[end.pts.length - 1] : null;
      at = anchorPt ? routePos(rt, anchorPt, 1) : routePos(rt, first(), null);
      if (at && !anchorPt){
        const other = routePos(rt, lastPt(), null);
        if (other && other.d < at.d) at = other;
      }
    }
    if (!at){ rt.segments.push(...(added = legs())); how = 'at the end'; }
    else {
      const d = q => map.distance(at.pt, q);
      if (d(lastPt()) < d(first())) flip();          // start from the end nearer the route
      const segs = added = legs();
      if (join && !samePt(at.pt, first())) segs.unshift(mkSeg('walk', [at.pt.slice(), first().slice()]));
      if (join && !samePt(at.pt, lastPt())) segs.push(mkSeg('walk', [lastPt().slice(), at.pt.slice()]));
      const xAfter = cutAt(rt, at);
      put(xAfter, segs);
      const outBack = join && segs.length > added.length ? ', with walking legs out and back' : '';
      how = () => (A || B ? 'from that point' : 'after ' + insLabel(xAfter)) + outBack;
    }
  }
  const uids = added.map(x => x.uid);
  push().then(() => {
    const r = P.routes.find(x => x.uid === s.route);
    const ids = r ? r.segments.filter(x => uids.includes(x.uid) && x.inv_id).map(x => x.inv_id) : [];
    note((ids.length ? 'Added ' + (ids.length > 1 ? ids[0] + '–' + ids[ids.length-1] : ids[0])
                     : 'Added the walking legs') + ' ' + (typeof how === 'function' ? how() : how)
         + '. Everything after renumbered.');
  });
}
// ---- end of adding missed blocks ------------------------------------------
function snapTarget(pt, ignoreSeg){
  if (!snapOn) return null;
  const c = map.latLngToContainerPoint(pt);
  let best = null, bestD = SNAP_PX;
  const consider = (cand, d) => { if (d < bestD){ bestD = d; best = cand.slice(); } };
  const rt = R();
  rt.segments.forEach(sg => {
    if (sg === ignoreSeg) return;
    sg.pts.forEach(q => {
      const cq = map.latLngToContainerPoint(q);
      consider(q, Math.hypot(c.x-cq.x, c.y-cq.y));
    });
  });
  [rt.start, rt.end].forEach(q => { if (q){
    const cq = map.latLngToContainerPoint(q);
    consider(q, Math.hypot(c.x-cq.x, c.y-cq.y));
  }});
  if (best) return best;
  // nothing at a corner - try landing on a line
  rt.segments.forEach(sg => {
    if (sg === ignoreSeg || sg.pts.length < 2) return;
    const n = sg.kind === 'lot' ? sg.pts.length : sg.pts.length - 1;
    for (let i = 0; i < n; i++){
      const a = map.latLngToContainerPoint(sg.pts[i]);
      const b = map.latLngToContainerPoint(sg.pts[(i+1) % sg.pts.length]);
      const vx = b.x-a.x, vy = b.y-a.y, L2 = vx*vx + vy*vy;
      if (!L2) continue;
      let t = ((c.x-a.x)*vx + (c.y-a.y)*vy) / L2;
      t = Math.max(0, Math.min(1, t));
      const px = a.x + t*vx, py = a.y + t*vy;
      const d = Math.hypot(c.x-px, c.y-py);
      if (d < bestD){
        const ll = map.containerPointToLatLng(L.point(px, py));
        bestD = d; best = [ll.lat, ll.lng];
      }
    }
  });
  return best;
}
// Snap a dragged leg's two ends onto the route. Lots are reached by their own
// walking leg, so their corners never snap.
function snapSeg(route, seg){
  let moved = false;
  [0, seg.pts.length-1].forEach(i => {
    const hit = snapTarget(seg.pts[i], seg);
    if (hit && !samePt(hit, seg.pts[i])){ seg.pts[i] = hit; moved = true; }
  });
  return moved;
}
function samePt(a, b){
  return a && b && Math.abs(a[0]-b[0]) < 1e-9 && Math.abs(a[1]-b[1]) < 1e-9;
}
// I on a selected joint: start adding missed blocks from that joint, pen down.
function continueFromVertex(pt){
  startInsert(activeRoute, null, pt);
}
// Removing a joint means the two legs that met there become one leg.
function dissolveVertex(pt){
  const rt = R();
  // Lot corners are checked first: a corner that belongs only to a lot used to
  // fall through the "no legs here" exit below and could never be deleted.
  const lot = rt.segments.find(sg => sg.kind === 'lot' && sg.pts.some(q => samePt(q, pt)));
  if (lot){
    selVertex = null;
    if (lot.pts.length > 3){ lot.pts = lot.pts.filter(q => !samePt(q, pt)); return push(); }
    note('A lot needs at least three corners. Delete the lot instead.', true);
    return render();
  }
  const touching = rt.segments.filter(sg => sg.kind !== 'lot'
    && sg.pts.some(q => samePt(q, pt)));
  if (!touching.length){ selVertex = null; return render(); }
  if (touching.length === 1){
    const sg = touching[0];
    if (sg.pts.length > 2){ sg.pts = sg.pts.filter(q => !samePt(q, pt)); }
    else { rt.segments = rt.segments.filter(x => x !== sg); }
    selVertex = null; return push();
  }
  const [a, b] = touching;
  const other = q => !samePt(q, pt);
  const keepA = a.pts.filter(other), keepB = b.pts.filter(other);
  a.pts = a.pts[a.pts.length-1] && samePt(a.pts[a.pts.length-1], pt)
        ? keepA.concat(keepB) : keepB.concat(keepA);
  rt.segments = rt.segments.filter(x => x !== b);
  selVertex = null;
  push();
  note('Joint removed; the two legs were merged.');
}
function mkSeg(kind, pts){
  return {uid: Math.random().toString(36).slice(2,12), kind: kind, pts: pts,
    spaces: null, manual_id: null, flagged: false, note: '', street: '', label_pos: null};
}
function lastVertex(){
  const segs = R().segments;
  for (let i = segs.length-1; i >= 0; i--)
    if (segs[i].kind !== 'lot' && segs[i].pts.length) return segs[i].pts[segs[i].pts.length-1];
  return R().start || null;
}
function firstVertex(){
  const segs = R().segments;
  for (let i = 0; i < segs.length; i++)
    if (segs[i].kind !== 'lot' && segs[i].pts.length) return segs[i].pts[0];
  return null;
}
map.on('click', e => {
  const pt = [e.latlng.lat, e.latlng.lng];
  // Start and end stay on their own tool until you pick another one. They used
  // to flip you straight into Count block, which turned the next stray click
  // into an unwanted block face.
  // Placing an endpoint away from the route used to leave a floating marker.
  // It now drops a walking leg to join it up, so S and E are always on the route.
  if (tool === 'start'){
    const first = firstVertex();
    R().start = pt;
    if (first && !near(pt, first)) R().segments.unshift(mkSeg('walk', [pt, first]));
    return push();
  }
  if (tool === 'end'){
    const last = lastVertex();
    R().end = pt;
    if (last && !near(pt, last)) R().segments.push(mkSeg('walk', [last, pt]));
    return push();
  }
  if (tool === 'lot') return addLotCorner(pt);
  // Clicking empty map lets go of the selection, which also hides a lot's line.
  if (tool === 'none' && selected){ selected = null; return render(); }
  if (tool === 'count' || tool === 'walk'){
    if (ins) return insertClick(pt);
    const rt = R();
    const prev = lastVertex();
    if (!prev){ rt.start = pt; return push(); }
    rt.segments.push(mkSeg(tool, [prev, pt]));
    return push();
  }
});
map.on('dblclick', () => { if (tool === 'lot') finishLot(); else if (ins) finishInsert(); });
map.on('mousemove', e => {
  const from = P && (tool === 'count' || tool === 'walk')
    && (ins ? ins.path[ins.path.length - 1] : lastVertex());
  if (!from) return rubber.remove();
  rubber.setLatLngs([from, e.latlng]);
  if (!map.hasLayer(rubber)) rubber.addTo(map);
});
map.on('mouseout', () => rubber.remove());

function near(a, b){
  const pa = map.latLngToContainerPoint(a), pb = map.latLngToContainerPoint(b);
  return Math.hypot(pa.x-pb.x, pa.y-pb.y) < 14;   // ~a fingertip on screen
}
// ---- parking lots --------------------------------------------------------
// Draw a lot's outline with the Parking lot tool. A line then ties the lot to
// the route at the point where it is counted. The line only shows while the lot
// is selected; drag its end along the route to move the lot through the walking
// order, and compute() turns that point into the lot's place and ID. An earlier
// version made lots out of a routed leg instead, but then the route had to walk
// into the shape and back out of it, which drew odd shapes.
function lotTarget(){              // the lot being redrawn, if any
  for (const rt of P.routes){
    const seg = rt.segments.find(sg => sg.uid === lotFor);
    if (seg) return seg;
  }
  return null;
}
function startRedraw(seg, ri){
  activeRoute = ri;
  lotFor = seg.uid; lotPts = []; selected = seg.uid;
  setTool('lot');
  render();
}
function addLotCorner(pt){
  if (lotPts.length >= 3 && near(pt, lotPts[0])) return finishLot();
  lotPts.push(pt); drawDraft();
}
// Committing the shape, not discarding it, is the whole point here. Switching
// tools mid-draw used to silently throw the polygon away.
function finishLot(){
  const pts = [];
  lotPts.forEach(pt => { if (!pts.length || !near(pt, pts[pts.length-1])) pts.push(pt); });
  let seg = lotFor && lotTarget();
  lotPts = []; lotFor = null; draft.clearLayers();
  if (pts.length < 3){ updateLotBar(); return false; }
  if (seg) seg.pts = pts;          // redrawn: same lot, same line, same ID
  else {
    const rt = R();
    seg = mkSeg('lot', pts);
    rt.segments.push(seg);
    // Start it at the nearest point on the route. The new lot is selected, so
    // its line is showing and a wrong guess is one drag to fix.
    if (attachLot(rt, seg)) note('Lot added. Drag the end of its line along the route to change where it is counted.');
    else note('Lot added at the end. Draw the route, then use Attach to the route.', true);
  }
  selected = seg.uid;
  push();
  setTool(toolBeforeLot);          // back to whatever you were doing
  return true;
}
// Nearest point on any of the route's legs to any of the given points, measured
// on screen so it matches what you see.
function nearestOnRoute(rt, pts){
  let best = null;
  const cs = pts.map(p => map.latLngToContainerPoint(p));
  rt.segments.forEach(sg => {
    if (sg.kind === 'lot' || sg.pts.length < 2) return;
    for (let i = 0; i < sg.pts.length - 1; i++){
      const a = map.latLngToContainerPoint(sg.pts[i]), b = map.latLngToContainerPoint(sg.pts[i+1]);
      const vx = b.x-a.x, vy = b.y-a.y, L2 = vx*vx + vy*vy;
      cs.forEach(c => {
        const t = L2 ? Math.max(0, Math.min(1, ((c.x-a.x)*vx + (c.y-a.y)*vy) / L2)) : 0;
        const x = a.x + t*vx, y = a.y + t*vy, d = Math.hypot(c.x-x, c.y-y);
        if (!best || d < best.d) best = {d, uid: sg.uid, x, y};
      });
    }
  });
  if (!best) return null;
  const ll = map.containerPointToLatLng(L.point(best.x, best.y));
  return {uid: best.uid, pt: [ll.lat, ll.lng]};
}
// Ties a lot to the route at the point nearest `from`, or nearest the lot itself.
function attachLot(rt, lot, from){
  const hit = nearestOnRoute(rt, from ? [from] : lot.pts);
  if (!hit) return false;
  lot.anchor = hit.pt; lot.anchor_leg = hit.uid;
  return true;
}
// Legs get merged, split and deleted. A lot whose leg is gone is tied again to
// the nearest point on what is left, so it never drops out of the walking order.
function reattachLots(){
  P.routes.forEach(rt => {
    const legs = new Set(rt.segments.filter(sg => sg.kind !== 'lot').map(sg => sg.uid));
    rt.segments.forEach(sg => {
      if (sg.kind !== 'lot' || !sg.anchor_leg || legs.has(sg.anchor_leg)) return;
      if (!attachLot(rt, sg, sg.anchor)){ sg.anchor = null; sg.anchor_leg = null; }
    });
  });
}
// The line from a lot's ID to the point on the route where it is counted. It is
// drawn only for the selected lot, so finished routes stay uncluttered.
function drawLotLine(rt, seg){
  if (!seg.label_at || !seg.anchor_at) return;
  const from = seg.label_at;
  const line = L.polyline([from, seg.anchor_at], {color:'#5AA9F2', weight:3,
    dashArray:'1 7', lineCap:'round', interactive:false}).addTo(layer);
  const h = L.marker(seg.anchor_at, {draggable: true, zIndexOffset: 900,
    icon: L.divIcon({className:'', iconSize:[16,16], html:'<div class="lotpin"></div>'})})
    .bindTooltip('Drag along the route to change where this lot is counted').addTo(layer);
  h.on('dragstart', () => { dragging = true; });
  h.on('drag', ev => line.setLatLngs([from, ev.target.getLatLng()]));
  h.on('dragend', ev => {
    dragging = false;
    const ll = ev.target.getLatLng();
    if (attachLot(rt, seg, [ll.lat, ll.lng])) push();
  });
}
// ---- separating and rejoining a block's ends -------------------------------
// A selected block shows its own two ends as tabs over the shared corners.
// Dragging a corner moves every leg that meets there; dragging a tab moves just
// this block's end, so it comes away from its neighbours. It keeps its number
// while it is separated. Dropped on another block's end the right way round (a
// start onto an end, or an end onto a start) it joins that block:
//  - if the two are already next to each other in the walk, numbers stay put and
//    any walking legs that only bridged the old gap between them are removed;
//  - otherwise this block moves to sit beside that one, and the IDs after it
//    renumber.
function drawEndTabs(rt, seg, line){
  [0, seg.pts.length - 1].forEach(k => {
    const start = k === 0;
    const h = L.marker(seg.pts[k], {draggable: true, zIndexOffset: 950,
      icon: L.divIcon({className:'', iconSize:[18,18], html:'<div class="endtab' + (start ? '' : ' fin') + '"></div>'})})
      .bindTooltip((start ? 'Start' : 'End') + ' of ' + esc(seg.inv_id || 'this walking leg')
        + ': drag to move just this end; drop it on another block\'s end to join them').addTo(layer);
    h.on('dragstart', () => { dragging = true; });
    h.on('drag', ev => {
      const ll = ev.target.getLatLng(), pts = seg.pts.map(q => q.slice());
      pts[k] = [ll.lat, ll.lng]; line.setLatLngs(pts);
    });
    h.on('dragend', ev => {
      dragging = false;
      const ll = ev.target.getLatLng();
      rejoinEnd(rt, seg, k, [ll.lat, ll.lng]);
    });
  });
}
function rejoinEnd(rt, seg, k, dropped){
  const start = k === 0;
  const hit = snapTarget(dropped, seg) || dropped;
  seg.pts[k] = hit.slice();
  const others = rt.segments.filter(x => x !== seg && x.kind !== 'lot' && x.pts.length >= 2);
  const mate = others.find(x => samePt(start ? x.pts[x.pts.length - 1] : x.pts[0], hit));
  let what, bridged = 0;
  if (!mate) what = others.some(x => x.pts.some(q => samePt(q, hit))) ? 'joined' : 'separated';
  else {
    const segs = rt.segments, i = segs.indexOf(seg), j = segs.indexOf(mate);
    const between = segs.slice(Math.min(i, j) + 1, Math.max(i, j)).filter(x => x.kind !== 'lot');
    if ((start ? j < i : i < j) && between.every(x => x.kind === 'walk')){
      rt.segments = segs.filter(x => !between.includes(x));
      bridged = between.length;
      what = bridged ? 'tidied' : 'next';
    } else {
      segs.splice(i, 1);
      segs.splice(segs.indexOf(mate) + (start ? 1 : 0), 0, seg);
      what = 'moved';
    }
  }
  const uid = seg.uid, mateUid = mate && mate.uid;
  push().then(() => {
    const r = P.routes.find(x => x.uid === rt.uid) || rt;
    const id = u => { const s = r.segments.find(x => x.uid === u); return s ? (s.inv_id || 'the walking leg') : 'it'; };
    const me = id(uid), them = mateUid ? id(mateUid) : '', side = start ? 'end of ' : 'start of ';
    const cap = t => t.charAt(0).toUpperCase() + t.slice(1);
    note(cap({
      separated: me + ' is separated there and keeps its number until you join it to another block.',
      joined: me + ' is joined there; its place in the walk is unchanged.',
      next: 'Joined ' + me + ' to the ' + side + them + '.',
      tidied: 'Joined ' + me + ' to the ' + side + them + ' and removed the walking leg'
        + (bridged > 1 ? 's' : '') + ' that only bridged the gap.',
      moved: me + ' now ' + (start ? 'follows ' : 'leads into ') + them + '; the IDs after it renumbered.',
    }[what]));
  });
}
function joinToRoute(seg){
  const rt = R();
  let best = null, bestD = Infinity, from = null;
  seg.pts.forEach(a => {
    rt.segments.forEach(other => {
      if (other === seg) return;
      other.pts.forEach(b => {
        const d = map.distance(a, b);
        if (d < bestD && d > 0.2){ bestD = d; best = b.slice(); from = a.slice(); }
      });
    });
    [rt.start, rt.end].forEach(b => {
      if (!b) return;
      const d = map.distance(a, b);
      if (d < bestD && d > 0.2){ bestD = d; best = b.slice(); from = a.slice(); }
    });
  });
  if (!best) return note('Nothing on this route to join to yet.', true);
  const i = rt.segments.indexOf(seg);
  rt.segments.splice(i, 0, mkSeg('walk', [best, from]));
  push();
  note('Joined with a walking leg of ' + Math.round(bestD * 3.28084) + ' ft.');
}
function deleteSelected(){
  if (!selected) return;
  const rt = R();
  const i = rt.segments.findIndex(sg => sg.uid === selected);
  if (i < 0) return;
  const gone = rt.segments[i].inv_id;
  rt.segments.splice(i, 1);
  selected = null;
  push();
  note(gone ? 'Deleted ' + gone + '. Everything after it renumbered.' : 'Deleted the walking leg.');
}
function drawDraft(){
  draft.clearLayers();
  // While redrawing, keep the old outline lit up so it is clear which lot changes.
  const target = lotFor && lotTarget();
  if (target) L.polygon(target.pts, {color:'#5AA9F2', weight:3, opacity:.55, fill:false,
    interactive:false}).addTo(draft);
  if (lotPts.length){
    L.polygon(lotPts, {color:'#5AA9F2', weight:2, dashArray:'5 5', fillOpacity:.15,
      interactive:false}).addTo(draft);
    lotPts.forEach((pt,i) => L.circleMarker(pt, {radius: i === 0 ? 7 : 4,
      color:'#fff', weight:2, fillColor:'#5AA9F2', fillOpacity:1})
      .bindTooltip(i === 0 ? 'Click here again to close the shape' : '').addTo(draft));
  }
  // Missed blocks being drawn: solid for counted, dashed for walking, as they
  // will look once added. Nothing is in the route until you finish.
  if (ins && ins.path.length){
    ins.kinds.forEach((k, i) => L.polyline([ins.path[i], ins.path[i+1]], {color:'#5AA9F2',
      weight: k === 'count' ? 6 : 3, dashArray: k === 'count' ? null : '9 8', interactive:false}).addTo(draft));
    ins.path.forEach((pt, i) => L.circleMarker(pt, {radius: i === 0 ? 6 : 4, color:'#fff', weight:2,
      fillColor:'#5AA9F2', fillOpacity:1, interactive:false}).addTo(draft));
  }
  updateLotBar();
}
function updateLotBar(){
  const bar = $('#lotbar');
  const target = lotFor && lotTarget();
  if (tool !== 'lot' || (!lotPts.length && !target)){ bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  $('#lotcount').textContent = (target ? 'Redrawing ' + (target.inv_id || 'the lot') + ' · ' : '')
    + lotPts.length + (lotPts.length === 1 ? ' corner' : ' corners');
  $('#lotfinish').disabled = lotPts.length < 3;
}
function setTool(t){
  if (tool === 'lot' && t !== 'lot' && lotPts.length >= 3) finishLot();
  if (t === 'lot' && tool !== 'lot') toolBeforeLot = tool;
  // Count and Walk keep an insert going (you switch between them mid-insert);
  // any other tool ends it.
  if (ins && t !== 'count' && t !== 'walk') finishInsert();
  if (t !== 'count' && t !== 'walk') rubber.remove();
  tool = t;
  document.querySelectorAll('.tool').forEach(b => b.classList.toggle('on', b.dataset.tool === t));
  const hints = {
    count:'Click along the street. Each click ends a block face and starts the next.',
    walk:'Click along the connector. These legs are dashed and never get an ID.',
    lot: (lotFor ? 'Click the new corners for this lot.' : 'Click the corners of the lot.')
      + ' Close it by clicking the first corner again, pressing Enter, or double-clicking.',
    flag:'Click a block or lot to circle it in red for follow-up.',
    start:'Click where this route begins. Pick another tool when you are done.',
    end:'Click where this route ends. Pick another tool when you are done.',
    none:'Click a block on the map or in the list to edit it.'};
  $('#hint').textContent = hints[t];
  if (t !== 'lot'){ lotPts = []; lotFor = null; }
  drawDraft(); updateInsBar();
}
document.querySelectorAll('.tool').forEach(b => b.onclick = () => setTool(b.dataset.tool));
$('#optSnap').onchange = e => { snapOn = e.target.checked; };
$('#optJoints').onchange = e => { jointsOn = e.target.checked; };
$('#lotfinish').onclick = () => finishLot();
$('#insdone').onclick = () => finishInsert();
$('#inscancel').onclick = () => finishInsert(true);
$('#lotcancel').onclick = () => { lotPts = []; lotFor = null; setTool('lot'); };
document.addEventListener('keyup', e => { if (!e.altKey) breakAway = false; });
document.addEventListener('keydown', e => {
  if (e.altKey) breakAway = true;
  if (ins && (e.key === 'Backspace' || e.key === 'Delete')
      && !['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)){
    e.preventDefault();
    return undoInsertPoint();          // while adding missed blocks, take back the last point
  }
  // Click a corner handle, press Delete, and the joint goes - the two legs it
  // separated are merged into one.
  if ((e.key === 'Delete' || e.key === 'Backspace') && selVertex
      && !['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)){
    e.preventDefault();
    return dissolveVertex(selVertex);
  }
  if (['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)) return;
  if (e.key === 'Enter' && tool === 'lot'){ e.preventDefault(); return finishLot(); }
  if (e.key === 'p'){ showPts = !showPts; return render(); }
  if (e.key === 'a'){ showArrows = !showArrows; drawArrows();
    return note(showArrows ? 'Direction arrows on' : 'Direction arrows off'); }
  if (e.key === 'i' && selVertex){ return continueFromVertex(selVertex); }
  if (e.key === 'c') setTool('count');
  else if (e.key === 'w') setTool('walk');
  else if (e.key === 'l') setTool('lot');
  else if (e.key === 'Escape') setTool('none');      // also finishes an insert
  else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z'){
    e.preventDefault();
    (e.shiftKey ? redo() : undo());
  }
  else if (e.key === 'Delete' || e.key === 'Backspace'){
    if (!selected) return;
    e.preventDefault();
    deleteSelected();
  }
});

// ---- render --------------------------------------------------------------
function render(){
  layer.clearLayers();
  $('#pname').value = P.name;
  P.routes.forEach((rt, ri) => {
    if (!rt.visible) return;
    const pick = (seg, ev) => {
      L.DomEvent.stop(ev);
      // With the lot tool every click is a corner, even one that lands on a line.
      if (tool === 'lot') addLotCorner([ev.latlng.lat, ev.latlng.lng]);
      else if (tool === 'flag'){ seg.flagged = !seg.flagged; push(); }
      else { activeRoute = ri; selected = seg.uid; render(); scrollToSel(); }
    };
    let selSeg = null, selLine = null;
    rt.segments.forEach(seg => {
      if (seg.pts.length < 2) return;
      const dim = ri === activeRoute ? 1 : .45;
      // Until you start drawing, light up the block whose ＋ you clicked.
      if (ins && !ins.path.length && seg.uid === ins.anchor && seg.kind !== 'lot')
        L.polyline(seg.pts, {color:'#5AA9F2', weight:15, opacity:.4, interactive:false}).addTo(layer);
      if (seg.kind === 'lot'){
        // Solid outline, matching the route colour, so lots read as counted area.
        L.polygon(seg.pts, {color: rt.color, weight: 3, opacity: dim,
          fillColor: rt.color, fillOpacity: .18})
          .on('click', ev => pick(seg, ev)).addTo(layer);
      } else {
        const line = L.polyline(seg.pts, {color: rt.color, weight: seg.kind === 'count' ? 6 : 3,
          opacity: dim, dashArray: seg.kind === 'count' ? null : '9 8'})
          .on('click', ev => pick(seg, ev)).addTo(layer);
        if (seg.uid === selected){ selSeg = seg; selLine = line; }
      }
      if (seg.inv_id && seg.label_at){
        // Dragging an ID drags the block face with it - the line follows.
        const anchor = seg.label_at.slice();
        const m = L.marker(anchor, {draggable: true, zIndexOffset: 500,
          icon: L.divIcon({className:'', iconSize:[26,26],
            html:'<div class="idlabel'+(seg.flagged?' flagged':'')
                 +'" style="border-color:'+rt.color+'">'+esc(seg.inv_id)+'</div>'})}).addTo(layer);
        m.on('dragstart', () => { dragging = true; });
        m.on('dragend', ev => {
          dragging = false;
          const ll = ev.target.getLatLng();
          activeRoute = ri;
          translateSeg(rt, seg, ll.lat - anchor[0], ll.lng - anchor[1]);
          // A lot keeps its slot in the walk wherever it is dragged; only legs snap.
          if (seg.kind !== 'lot' && snapSeg(rt, seg)) note('Snapped into the route');
          push();
        });
        m.on('click', ev => { if (!dragging) pick(seg, ev); });
      }
      if (seg.kind === 'lot' && seg.uid === selected) drawLotLine(rt, seg);
    });
    // Corner handles on the route you are working on, for finer adjustment.
    if (ri === activeRoute && showPts){
      // Shared joints are exact copies of one point, so a string key finds them
      // without comparing every corner against every other one.
      const seen = new Set();
      rt.segments.forEach(seg => seg.pts.forEach(pt => {
        const key = pt[0] + ',' + pt[1];
        if (seen.has(key)) return;
        seen.add(key);
        const from = pt.slice();
        const h = L.marker(pt, {draggable: true, zIndexOffset: 400,
          icon: L.divIcon({className:'', iconSize:[13,13],
            html:'<div class="vtx'+(samePt(selVertex, pt)?' vsel':'')
                 +'" style="background:'+rt.color+'"></div>'})}).addTo(layer);
        h.on('click', ev => { L.DomEvent.stop(ev);
          if (tool === 'lot') return addLotCorner(from.slice());
          selVertex = samePt(selVertex, from) ? null : from.slice();
          render();
          if (selVertex) note('Joint selected. Press Delete to remove it.');
        });
        h.on('dragstart', () => { dragging = true; });
        h.on('dragend', ev => {
          dragging = false;
          const ll = ev.target.getLatLng();
          const hit = snapTarget([ll.lat, ll.lng], null) || [ll.lat, ll.lng];
          moveVertex(rt, from, hit);
          push();
        });
      }));
    }
    if (selSeg && tool !== 'lot' && !ins) drawEndTabs(rt, selSeg, selLine);
    // Endpoints take the route's own colour so you can tell three routes apart.
    // S is a solid disc, E carries an inner ring.
    const endpoint = (pt, glyph, label) => {
      const from = pt.slice();
      const m = L.marker(pt, {draggable: true, zIndexOffset: 1000,
        icon: L.divIcon({className:'', iconSize:[34,48], iconAnchor:[17,17],
          html:'<div class="endpt'+(glyph === 'E' ? ' isend' : '')
               +'" style="background:'+rt.color+'">'
               +esc(glyph === 'S' ? (rt.start_inv_id || 'S') : (rt.end_inv_id || 'E'))
               +'</div><div class="endcap">'+(glyph === 'S' ? 'START' : 'END')+'</div>'})})
        .bindTooltip(esc(label) + ' — drag to move').addTo(layer);
      m.on('dragstart', () => { dragging = true; });
      m.on('dragend', ev => {
        dragging = false;
        const ll = ev.target.getLatLng(), to = [ll.lat, ll.lng];
        activeRoute = ri;
        // If it was sitting on the route, carry that corner along so the line
        // stays joined; otherwise just reposition the pin.
        const onLine = rt.segments.some(sg => sg.pts.some(q => samePt(q, from)));
        if (onLine) moveVertex(rt, from, to);
        if (glyph === 'S') rt.start = to; else rt.end = to;
        push();
      });
      return m;
    };
    if (rt.start_at) endpoint(rt.start_at, 'S', 'Start · '+rt.name);
    if (rt.end_at) endpoint(rt.end_at, 'E', 'End · '+rt.name);
  });

  // route tabs + fields
  $('#tabs').innerHTML = '';
  P.routes.forEach((rt, i) => {
    const b = document.createElement('button');
    b.className = 'tab' + (i === activeRoute ? ' on' : '');
    b.innerHTML = '<span class="dot" style="background:'+esc(rt.color)+'"></span>'+esc(rt.name);
    b.onclick = () => { if (ins) finishInsert(); activeRoute = i; selected = null; render(); };
    b.oncontextmenu = ev => { ev.preventDefault(); rt.visible = !rt.visible; push(); };
    b.title = rt.visible ? 'Right-click to hide this route' : 'Hidden — right-click to show';
    if (!rt.visible) b.style.opacity = .45;
    $('#tabs').appendChild(b);
  });
  const rt = R();
  $('#rname').value = rt.name; $('#rprefix').value = rt.prefix; $('#rcolor').value = rt.color;

  const s = P.settings;
  $('#sMin').value = s.minutes_per_block; $('#sSp').value = s.spaces_per_block;
  $('#sWalk').value = s.walk_speed_ft_per_min; $('#sSkip').checked = s.skip_multiples_of_ten;
  $('#fx').textContent = s.minutes_per_block + ' min per ' + s.spaces_per_block + ' spaces';

  const t = rt.totals;
  $('#rstats').innerHTML =
    row('Walking time', fmtMin(t.total_min), true) +
    row('Total distance', t.total_mi + ' mi', true) +
    row('Block faces', t.blocks) + lotsRow(t) + row('Spaces', t.spaces) +
    row('Counting', fmtMin(t.count_min) + ' · ' + fmtFt(t.count_ft)) +
    row('Connecting', fmtMin(t.walk_min) + ' · ' + fmtFt(t.walk_ft)) +
    row('Next ID', esc(t.next_id));
  const g = P.grand_totals;
  $('#gstats').innerHTML = row('Routes', g.routes) + row('Block faces', g.blocks) +
    lotsRow(g) + row('Spaces', g.spaces) + row('Distance', g.total_mi + ' mi') +
    row('Time', fmtMin(g.total_min));
  $('#lotnote').style.display = g.lots ? 'block' : 'none';

  updateInsBar();
  drawArrows();
  renderList();
}
function row(k, v, big){
  return '<dt>'+k+'</dt><dd'+(big?' class="big"':'')+'>'+v+'</dd>';
}
function lotsRow(t){
  return t.lots ? row('Parking lots', t.lots + ' · not timed') : '';
}
function fmtMin(m){
  m = Math.round(m); return m < 60 ? m+' min' : Math.floor(m/60)+'h '+String(m%60).padStart(2,'0');
}
function fmtFt(f){ return f >= 1000 ? (f/5280).toFixed(2)+' mi' : Math.round(f)+' ft'; }

function renderList(){
  const dup = P.duplicate_ids || [];
  $('#dupwarn').innerHTML = dup.length
    ? '<div class="warn">Two blocks share '+esc(dup.join(', '))+'. Clear one of the manual IDs to let the numbering flow again.</div>' : '';
  const host = $('#blocks'); host.innerHTML = '';
  const rt = R();
  if (!rt.segments.length){
    host.innerHTML = '<p class="empty">Nothing drawn yet. Pick <b>Count block</b> and click along the first street. '
      + 'IDs appear as you go and renumber themselves whenever you insert, delete or reorder a leg. '
      + 'Parking lots take an ID in the same sequence, at the point where their line meets the route.</p>';
    return;
  }
  const top = document.createElement('button'); top.className = 'addgap';
  top.textContent = '＋ Add missed blocks';
  top.title = 'Draw them anywhere; start or finish on the route to place them exactly';
  top.onclick = () => startInsert(P.routes.indexOf(rt), null);
  host.appendChild(top);
  rt.segments.forEach((seg, i) => host.appendChild(blockRow(seg, i, rt)));
}

function blockRow(seg, i, rt){
  const d = document.createElement('div');
  const isDup = seg.inv_id && (P.duplicate_ids||[]).includes(seg.inv_id);
  const isLot = seg.kind === 'lot';
  d.className = 'blk' + (seg.kind === 'walk' ? ' walkrow' : '')
              + (selected === seg.uid ? ' sel' : '') + (isDup ? ' dup' : '');
  d.style.borderLeftColor = seg.kind === 'walk' ? 'transparent' : rt.color;
  d.dataset.uid = seg.uid;

  const head = document.createElement('div'); head.className = 'blkhead';
  const sp = seg.spaces_known ? seg.spaces_effective + ' sp' : 'not surveyed';
  const meta = isLot
    ? 'lot · ' + Math.round(seg.area_sqft).toLocaleString() + ' sq ft · ' + sp + ' · not timed'
    : Math.round(seg.length_ft) + ' ft'
      + (seg.kind === 'count' ? ' · ' + sp + ' · ' + seg.minutes.toFixed(1) + ' min'
                              : ' · walk ' + seg.minutes.toFixed(1) + ' min');
  head.innerHTML =
    '<span class="idtag num'+(seg.id_is_manual?' man':'')+'">'
      + esc(seg.inv_id || '—') + '</span>'
    + '<span class="meta num">' + meta + '</span>';

  const mk = (label, title, cls, fn) => {
    const b = document.createElement('button');
    b.className = 'mini' + (cls||''); b.textContent = label; b.title = title;
    b.onclick = ev => { ev.stopPropagation(); fn(); };
    return b;
  };
  if (seg.kind !== 'walk')
    head.appendChild(mk('⚑', 'Flag for follow-up', seg.flagged ? ' flagon' : '',
      () => { seg.flagged = !seg.flagged; push(); }));
  head.appendChild(mk('◎', 'Zoom to it on the map', '', () => {
    const b = L.latLngBounds(seg.pts); map.fitBounds(b.pad(0.6)); }));
  if (!isLot)
    head.appendChild(mk('＋', 'Add a missed block after this one', ' addafter',
      () => startInsert(P.routes.indexOf(rt), seg.uid)));
  head.appendChild(mk('↑', 'Move earlier in the route', '', () => step(-1)));
  head.appendChild(mk('↓', 'Move later in the route', '', () => step(1)));
  // A tied lot's place comes from its line, so the arrows move the line: to the
  // start of the leg before it, or the end of the leg after it. A leg steps past
  // the next leg and takes its tied lots along, since they follow their leg.
  function step(dir){
    const a = rt.segments, tied = sg => sg.kind === 'lot' && sg.anchor_leg;
    if (tied(seg)){
      const leg = (dir < 0 ? a.slice(0, i).reverse() : a.slice(i + 1))
        .find(sg => sg.kind !== 'lot' && sg.pts.length >= 2);
      if (!leg) return;
      seg.anchor_leg = leg.uid;
      seg.anchor = (dir < 0 ? leg.pts[0] : leg.pts[leg.pts.length-1]).slice();
      return push();
    }
    let j = i + dir;
    while (j >= 0 && j < a.length && tied(a[j])) j += dir;
    if (j < 0 || j >= a.length) return;
    [a[i], a[j]] = [a[j], a[i]];
    push();
  }
  head.appendChild(mk('×', 'Delete this leg', '', () => {
    rt.segments.splice(i,1); selected = null; push(); }));
  head.onclick = () => { selected = selected === seg.uid ? null : seg.uid; render(); };
  d.appendChild(head);

  const body = document.createElement('div'); body.className = 'blkbody';
  const f2 = document.createElement('div'); f2.className = 'f2';

  const idIn = inp(seg.manual_id || '',
    seg.kind === 'walk' ? 'No ID on walk legs' : 'Auto: ' + seg.inv_id);
  idIn.disabled = seg.kind === 'walk';
  idIn.onchange = () => { seg.manual_id = idIn.value.trim() || null; push(); };

  const spIn = inp(seg.spaces == null ? '' : seg.spaces,
    seg.kind === 'walk' ? '—' : 'Spaces from survey');
  spIn.type = 'number'; spIn.min = 0; spIn.disabled = seg.kind === 'walk';
  spIn.onchange = () => { seg.spaces = spIn.value === '' ? null : parseInt(spIn.value,10); push(); };

  f2.appendChild(idIn); f2.appendChild(spIn); body.appendChild(f2);

  const stIn = inp(seg.street || '', isLot ? 'Lot name' : 'Street or cross streets');
  stIn.onchange = () => { seg.street = stIn.value; push(); };
  body.appendChild(stIn);

  const noteIn = inp(seg.note || '', 'Note for the field team');
  noteIn.onchange = () => { seg.note = noteIn.value; push(); };
  body.appendChild(noteIn);

  const acts = document.createElement('div'); acts.className = 'f2';
  if (!isLot){
    const swap = document.createElement('button'); swap.className = 'hbtn';
    swap.textContent = seg.kind === 'count' ? 'Make it walk-only' : 'Make it a counted block';
    swap.onclick = () => {
      seg.kind = seg.kind === 'count' ? 'walk' : 'count';
      if (seg.kind === 'walk'){ seg.is_route_start = false; seg.is_route_end = false; }
      push();
    };
    acts.appendChild(swap);
  }
  const join = document.createElement('button'); join.className = 'hbtn';
  if (isLot){
    join.textContent = 'Attach to the route';
    join.title = 'Tie this lot to the nearest point on the route. Then drag its line to adjust.';
    join.onclick = () => {
      if (!attachLot(rt, seg)) return note('Draw some of the route first.', true);
      selected = seg.uid; push();
    };
  } else {
    join.textContent = 'Join to the route';
    join.title = 'Drop a walking leg from here to the nearest point on the route';
    join.onclick = () => joinToRoute(seg);
  }
  acts.appendChild(join);
  const clr = document.createElement('button'); clr.className = 'hbtn';
  clr.textContent = 'Back to auto ID';
  clr.onclick = () => { seg.manual_id = null; push(); };
  acts.appendChild(clr);
  if (isLot){
    const redraw = document.createElement('button'); redraw.className = 'hbtn';
    redraw.textContent = 'Redraw the outline';
    redraw.title = 'Click new corners for this lot. It keeps its line, place and ID.';
    redraw.onclick = () => startRedraw(seg, P.routes.indexOf(rt));
    acts.appendChild(redraw);
  }
  body.appendChild(acts);

  // Same as the ＋ in the row's header: blocks drawn next land right after this
  // one and everything downstream renumbers. A lot is not on the line, so new
  // blocks go after the block before or after it instead.
  const acts2 = document.createElement('div'); acts2.className = 'f2';
  if (!isLot){
    const cont = document.createElement('button'); cont.className = 'hbtn';
    cont.textContent = 'Add a missed block after this';
    cont.title = 'Blocks you draw next go in right after this one; the IDs after them renumber';
    cont.onclick = () => startInsert(P.routes.indexOf(rt), seg.uid);
    acts2.appendChild(cont);
  }

  // Marking a block or lot as the route's own start or end. Useful when the walk
  // finishes in a parking lot: the lot becomes END and keeps its own ID rather
  // than a separate marker landing on top of it. Walk legs have no ID to lend,
  // so they don't get these buttons.
  const rt2 = R();
  const isEnd = !!seg.is_route_end, isStart = !!seg.is_route_start;
  const cap = document.createElement('button'); cap.className = 'hbtn';
  cap.textContent = isEnd ? 'Stop being the END' : 'Make this the END';
  cap.onclick = () => {
    rt2.segments.forEach(x => { x.is_route_end = false; });
    seg.is_route_end = !isEnd;
    if (seg.is_route_end){ seg.is_route_start = false; rt2.end = null; }
    push();
  };
  if (seg.kind !== 'walk') acts2.appendChild(cap);
  body.appendChild(acts2);

  const cap2 = document.createElement('button'); cap2.className = 'hbtn';
  cap2.textContent = isStart ? 'Stop being the START' : 'Make this the START';
  cap2.onclick = () => {
    rt2.segments.forEach(x => { x.is_route_start = false; });
    seg.is_route_start = !isStart;
    if (seg.is_route_start){ seg.is_route_end = false; rt2.start = null; }
    push();
  };
  if (seg.kind !== 'walk') body.appendChild(cap2);

  if (seg.kind !== 'walk' && (isLot || !seg.spaces_known)){
    const p = document.createElement('p'); p.className = 'tiny';
    p.textContent = !isLot
      ? 'No space count yet. Until the inventory survey comes back this block is '
        + 'costed at one block of walking time.'
      : (seg.anchor_at
          ? 'Counted where its line meets the route (shown on the map while this lot is '
            + 'selected). Drag the end of the line along the route to move it. '
          : 'Not tied to the route yet, so it keeps its place in this list. '
            + 'Attach to the route to place it with a line. ')
        + 'Lots are not included in the time or distance.';
    body.appendChild(p);
  }
  d.appendChild(body);
  return d;
}

function inp(val, ph){
  const e = document.createElement('input'); e.value = val; e.placeholder = ph || '';
  e.onclick = ev => ev.stopPropagation(); return e;
}
// Chevrons stepped along every leg, pointing the way the route is walked. They
// are recomputed in screen space so the spacing stays even at any zoom.
function drawArrows(){
  arrows.clearLayers();
  if (!P || !showArrows) return;
  const STEP = 78;
  P.routes.forEach((rt, ri) => {
    if (!rt.visible) return;
    const op = ri === activeRoute ? 0.95 : 0.4;
    rt.segments.forEach(seg => {
      if (seg.kind === 'lot' || seg.pts.length < 2) return;
      for (let i = 0; i < seg.pts.length - 1; i++){
        const a = map.latLngToContainerPoint(seg.pts[i]);
        const b = map.latLngToContainerPoint(seg.pts[i+1]);
        const len = Math.hypot(b.x-a.x, b.y-a.y);
        if (len < 34) continue;
        const deg = Math.atan2(b.y-a.y, b.x-a.x) * 180 / Math.PI;
        for (let d = Math.min(STEP, len/2); d < len - 8; d += STEP){
          const t = d / len;
          const ll = map.containerPointToLatLng(
            L.point(a.x + (b.x-a.x)*t, a.y + (b.y-a.y)*t));
          L.marker(ll, {interactive: false, keyboard: false, zIndexOffset: 300,
            icon: L.divIcon({className:'', iconSize:[16,16], iconAnchor:[8,8],
              html:'<div class="arw" style="transform:rotate('+deg+'deg);opacity:'+op+'"></div>'})
          }).addTo(arrows);
        }
      }
    });
  });
}
map.on('moveend', drawArrows);   // also fires after every zoom, so one listener is enough

function scrollToSel(){
  const el = document.querySelector('.blk.sel');
  if (el) el.scrollIntoView({block:'nearest', behavior:'smooth'});
}

// ---- panel wiring --------------------------------------------------------
$('#rname').onchange = e => { R().name = e.target.value; push(); };
$('#rprefix').onchange = e => { R().prefix = parseInt(e.target.value||'1',10); push(); };
// The colour picker fires on every pixel of a drag. Repaint locally while it
// moves and send the change once when it settles; colour changes no IDs.
$('#rcolor').oninput = e => { R().color = e.target.value; render(); };
$('#rcolor').onchange = e => { R().color = e.target.value; push(); };
$('#pname').onchange = e => { P.name = e.target.value; push(); };
['sMin','sSp','sWalk'].forEach(id => $('#'+id).onchange = () => {
  P.settings.minutes_per_block = parseFloat($('#sMin').value);
  P.settings.spaces_per_block = parseFloat($('#sSp').value);
  P.settings.walk_speed_ft_per_min = parseFloat($('#sWalk').value);
  push();
});
$('#sSkip').onchange = e => { P.settings.skip_multiples_of_ten = e.target.checked; push(); };

$('#bReverse').onclick = () => {
  const r = R();
  r.segments.reverse();
  r.segments.forEach(sg => { if (sg.kind !== 'lot') sg.pts.reverse(); });
  const tmp = r.start; r.start = r.end; r.end = tmp;
  push();
};
$('#bClearStart').onclick = () => { R().start = null; push(); };
$('#bClearEnd').onclick = () => { R().end = null; push(); };
$('#bAddRoute').onclick = () => {
  const palette = ['#2E7D4F','#C0392B','#7B4FA8','#1F6FB2','#C77C1E','#2A8C8C'];
  const n = P.routes.length;
  P.routes.push({uid: Math.random().toString(36).slice(2,12), name: 'Route '+(n+1),
    prefix: n+1, color: palette[n % palette.length], start:null, end:null,
    visible:true, segments:[]});
  activeRoute = P.routes.length-1; push();
};
$('#bDelRoute').onclick = () => {
  if (P.routes.length < 2) return alert('Keep at least one route.');
  if (!confirm('Delete "'+R().name+'" and everything drawn on it?')) return;
  P.routes.splice(activeRoute,1); activeRoute = 0; selected = null; push();
};
$('#bSave').onclick = async () => {
  try {
    const r = await api('/api/save', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({project: strip(P)})});
    note('Saved to ' + r.saved);
    loadProjectList();
  } catch (err) {}
};
$('#bNew').onclick = async () => {
  const name = prompt('Name the study area', 'New study area');
  if (!name) return;
  P = await api('/api/new', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name})});
  activeRoute = 0; selected = null; render(); freshHistory();
};
$('#bLoad').onchange = async e => {
  const name = e.target.value;
  e.target.selectedIndex = 0;          // so the same project can be reopened
  if (!name) return;
  try {
    P = await api('/api/load?name=' + encodeURIComponent(name));
  } catch (err) { return; }            // api() has already shown the reason
  activeRoute = 0; selected = null; setTool('none');
  if (P.center) map.setView(P.center, P.zoom || 15);
  render(); freshHistory();
  note('Opened "' + name + '"');
};
// ---- find a place -------------------------------------------------------
// Searches when you press Enter, not as you type. Nominatim's usage policy
// forbids search-as-you-type against its public server, and one request per
// search is all a planning tool needs.
async function findPlace(){
  const q = $('#find').value.trim();
  const list = $('#findlist');
  if (q.length < 3){ list.style.display = 'none'; return; }
  let r;
  try { r = await api('/api/geocode?q=' + encodeURIComponent(q)); }
  catch (err) { return; }
  if (!r.results.length){
    list.innerHTML = '<button disabled>Nothing found for that</button>';
    list.style.display = 'block'; return;
  }
  list.innerHTML = '';
  r.results.forEach(hit => {
    const b = document.createElement('button');
    b.textContent = hit.label;
    b.onclick = () => {
      if (hit.bbox) map.fitBounds([[hit.bbox[0], hit.bbox[2]], [hit.bbox[1], hit.bbox[3]]]);
      else map.setView([hit.lat, hit.lng], 17);
      list.style.display = 'none'; $('#find').value = '';
    };
    list.appendChild(b);
  });
  list.style.display = 'block';
}
$('#find').addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); findPlace(); }
  if (e.key === 'Escape'){ $('#findlist').style.display = 'none'; $('#find').blur(); }
});
document.addEventListener('click', e => {
  if (!e.target.closest('#findwrap')) $('#findlist').style.display = 'none';
});

// ---- open a project file from anywhere on disk ---------------------------
$('#bImport').onclick = () => { mergeMode = false; $('#fileIn').click(); };
$('#bMerge').onclick = () => { mergeMode = true; $('#fileIn').click(); };
$('#fileIn').onchange = async e => {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  try {
    const obj = JSON.parse(await f.text());
    if (!obj.routes) throw new Error('That file is not a Route Mapper project.');
    if (mergeMode){
      // Bring the other file's routes in alongside these ones. Each keeps its
      // own zone number, so IDs stay separate; rename or renumber after.
      const base = strip(P);
      const used = new Set(base.routes.map(r => r.prefix));
      obj.routes.forEach(r => {
        const copy = JSON.parse(JSON.stringify(r));
        copy.uid = Math.random().toString(36).slice(2,12);
        copy.name = r.name + ' · ' + f.name.replace(/\.json$/i, '');
        while (used.has(copy.prefix)) copy.prefix += 1;
        used.add(copy.prefix);
        base.routes.push(copy);
      });
      P = base; selected = null;
      await push();                    // through push, so a merge can be undone
      note('Merged ' + obj.routes.length + ' route(s) from ' + f.name +
           '. Zone numbers were bumped to keep IDs unique.');
    } else {
      P = await api('/api/state', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(obj)});
      activeRoute = 0; selected = null; setTool('none');
      if (P.center) map.setView(P.center, P.zoom || 15);
      render(); freshHistory();
      note('Opened ' + f.name + '. Save to keep a copy in the projects folder.');
    }
  } catch (err) { note('Could not open that file: ' + err.message, true); }
};

// ---- basemap tiles, fetched and re-projected into the export's own frame ----
function tileUrl(z, x, y){
  const which = $('#optBase').value;
  if (which === 'aerial')
    return 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/'+z+'/'+y+'/'+x;
  return 'https://tile.openstreetmap.org/'+z+'/'+x+'/'+y+'.png';
}
async function asDataUrl(url){
  const r = await fetch(url, {mode:'cors'});
  if (!r.ok) throw new Error('tile ' + r.status);   // don't bake an error page into the sheet
  const b = await r.blob();
  return await new Promise(res => { const fr = new FileReader();
    fr.onload = () => res(fr.result); fr.readAsDataURL(b); });
}
// Python's projection is plain Web Mercator, so tiles land in the same frame.
async function basemapTiles(meta){
  const nx = m => (m / Math.PI + 1) / 2;          // mercator x -> 0..1
  const ny = m => (1 - m / Math.PI) / 2;          // mercator y -> 0..1 (down)
  const spanN = nx(meta.maxx) - nx(meta.minx);
  let z = Math.round(Math.log2(meta.width / (spanN * 256)));
  z = Math.max(1, Math.min(19, z));
  const n = Math.pow(2, z);
  const x0 = Math.floor(nx(meta.minx) * n), x1 = Math.floor(nx(meta.maxx) * n);
  const y0 = Math.floor(ny(meta.maxy) * n), y1 = Math.floor(ny(meta.miny) * n);
  const count = (x1-x0+1) * (y1-y0+1);
  if (count > 400) throw new Error('That area needs ' + count + ' map tiles. Zoom the study area in a bit.');
  const sx = v => ((v / n) * 2 - 1) * Math.PI;                  // tile idx -> mercator x
  const sy = v => (1 - (v / n) * 2) * Math.PI;                  // tile idx -> mercator y
  const px = m => (m - meta.minx) * meta.scale + meta.pad;
  const py = m => (meta.maxy - m) * meta.scale + meta.pad;
  const jobs = [];
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) jobs.push([x, y]);
  // Four at a time, about what the live map does, rather than strictly one after
  // another: a sheet needing 60 tiles used to wait for 60 round trips in a row.
  const out = new Array(jobs.length);
  let next = 0;
  await Promise.all(Array.from({length: Math.min(4, jobs.length)}, async () => {
    while (next < jobs.length){
      const j = next++, [x, y] = jobs[j];
      let href;
      try { href = await asDataUrl(tileUrl(z, x, y)); } catch (err) { continue; }
      const left = px(sx(x)), right = px(sx(x+1)), top = py(sy(y)), bot = py(sy(y+1));
      out[j] = {href, x:left, y:top, w:right-left, h:bot-top};
    }
  }));
  return out.filter(Boolean);
}
async function svgWithBasemap(){
  const svg = await (await fetch('/api/export/svg')).text();
  if (!$('#optMapBg').checked) return svg;
  const meta = await api('/api/export/meta');
  if (!meta.width) return svg;
  const tiles = await basemapTiles(meta);
  // Muted uses the same sharp OSM tiles as the live map, desaturated here with an
  // SVG filter so the printed sheet matches what is on screen.
  const muted = $('#optBase').value === 'muted';
  const def = muted
    ? '<defs><filter id="mute"><feColorMatrix type="saturate" values="0"/>'
      + '<feComponentTransfer><feFuncR type="linear" slope="0.9" intercept="0.09"/>'
      + '<feFuncG type="linear" slope="0.9" intercept="0.09"/>'
      + '<feFuncB type="linear" slope="0.9" intercept="0.09"/></feComponentTransfer></filter></defs>'
    : '';
  const g = def + '<g id="basemap"' + (muted ? ' filter="url(#mute)"' : '') + '>' + tiles.map(t =>
    '<image href="'+t.href+'" x="'+t.x.toFixed(1)+'" y="'+t.y.toFixed(1)+
    '" width="'+(t.w+0.6).toFixed(1)+'" height="'+(t.h+0.6).toFixed(1)+
    '" preserveAspectRatio="none"/>').join('') + '</g>';
  return svg.replace('<!--BASEMAP-->', g);
}
function download(blob, name){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);   // free the copy once it's saved
}
const safeName = () => (P.name || 'routes').replace(/[^\w.-]+/g, '_');

$('#bExport').onchange = async e => {
  const kind = e.target.value;
  e.target.selectedIndex = 0;
  if (!kind) return;
  if (kind === 'svg'){
    try {
      note('Building SVG…');
      download(new Blob([await svgWithBasemap()], {type:'image/svg+xml'}), safeName() + '.svg');
      note('SVG saved to your downloads');
    } catch (err) { note('SVG export failed: ' + err.message, true); }
    return;
  }
  if (kind !== 'png') return void (location.href = '/api/export/' + kind);
  // PNG is rasterised here from the SVG, which keeps the Python side dependency-free.
  try {
    note('Rendering PNG…');
    const svg = await svgWithBasemap();
    const url = URL.createObjectURL(new Blob([svg], {type:'image/svg+xml;charset=utf-8'}));
    const img = new Image();
    img.onload = () => {
      const k = 2;                       // 2x so it holds up when printed
      const cv = document.createElement('canvas');
      cv.width = (img.width || 1600) * k; cv.height = (img.height || 900) * k;
      const ctx = cv.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, 0, 0, cv.width, cv.height);
      cv.toBlob(b => {
        download(b, safeName() + '.png');
        URL.revokeObjectURL(url);
        note('PNG saved to your downloads');
      }, 'image/png');
    };
    img.onerror = () => note('Could not turn the SVG into a PNG. Use SVG or PDF instead.', true);
    img.src = url;
  } catch (err) { note('PNG export failed: ' + err.message, true); }
};

async function loadProjectList(){
  const r = await api('/api/projects');
  $('#bLoad').innerHTML = '<option value="">' +
    (r.projects.length ? 'Open\u2026 (' + r.projects.length + ')' : 'Open\u2026 (none saved)') +
    '</option>' +
    r.projects.map(n => '<option value="'+esc(n)+'">'+esc(n)+'</option>').join('');
  $('#bLoad').title = 'Saved projects live in ' + r.dir;
}

(async () => {
  P = await api('/api/state');
  if (P.center) map.setView(P.center, P.zoom || 15);
  setTool('none'); render(); freshHistory(); loadProjectList();
})();
</script>
</body>
</html>
"""


_RUNNING = []


def stop():
    """Shut down any server this module started."""
    while _RUNNING:
        srv = _RUNNING.pop()
        try:
            srv.shutdown()
            srv.server_close()      # shutdown() alone leaves the socket bound
        except Exception:
            pass


def serve(port=None, open_browser=False, tries=20):
    """Start the server on a background thread and return it.

    For Jupyter and Colab, where a blocking serve_forever() would hang the cell.
    Re-running is safe: any server this module already started is closed first,
    and if the port is still held by something else it steps to the next one.
    """
    global PORT
    stop()
    start = int(port) if port else PORT
    os.makedirs(SAVE_DIR, exist_ok=True)
    last = None
    for n in range(tries):
        try:
            srv = Server(("127.0.0.1", start + n), Handler)
        except OSError as e:
            last = e
            continue
        PORT = start + n
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _RUNNING.append(srv)
        if open_browser:
            webbrowser.open("http://127.0.0.1:%d/" % PORT)
        if n:
            print("Port %d was busy." % start, end=" ")
        print("Route Mapper serving on port %d." % PORT)
        print("Projects save to %s" % SAVE_DIR)
        print("In Colab, now run:  "
              "from google.colab.output import serve_kernel_port_as_window; "
              "serve_kernel_port_as_window(%d)" % PORT)
        return srv
    raise OSError("No free port in %d-%d (%s)" % (start, start + tries - 1, last))


def main():
    os.makedirs(SAVE_DIR, exist_ok=True)
    url = "http://127.0.0.1:%d/" % PORT
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print("Could not bind port %d (%s)." % (PORT, e))
        print("Another copy may already be running, or set a different port:")
        print("    ROUTE_MAPPER_PORT=8790 python3 route_mapper.py")
        sys.exit(1)
    print("Route Mapper is running at %s" % url)
    print("Projects are saved in %s" % SAVE_DIR)
    print("Press Ctrl+C to stop.")
    threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)),
                     daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
