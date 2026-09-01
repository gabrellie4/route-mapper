#!/usr/bin/env python3
"""
Route Mapper
============

A local tool for laying out parking occupancy count routes and generating
inventory IDs, distances and walk-time estimates.

Run it:

    python3 route_mapper.py

It starts a local server and opens your browser. Nothing leaves your machine
except basemap tile requests.

Python owns the model: distances, the ID numbering rule, time estimates and
every export. The browser is only the drawing surface. That means the numbering
convention lives in exactly one place (see IdSequence) and you can call the same
engine from a script if you'd rather batch something.

Standard library only.
"""

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
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("ROUTE_MAPPER_PORT", "8765"))
# Saved projects live next to this script, not next to whatever folder you
# happened to launch from. Otherwise `cd ~ && python3 "Route Mapper/route_mapper.py"`
# writes to ~/route_projects and the Open menu looks empty next session.
_HERE = os.path.dirname(os.path.abspath(__file__))
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
        "spaces": spaces,             # None means "estimate from length"
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
    project.pop("lots", None)
    return project


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
    all_ids = {}
    duplicate_ids = set()

    for route in out["routes"]:
        seq = IdSequence(route.get("prefix", 1), s["skip_multiples_of_ten"])
        r_count_ft = r_walk_ft = r_count_min = r_walk_min = 0.0
        n_blocks = 0
        r_spaces = 0

        # The start marker occupies the first slot in the sequence rather than
        # echoing the first block face's number, which produced two 2.1s. Every
        # block face therefore begins at .2, and the end marker takes whatever
        # number is left over once the route is walked.
        has_line = any(sg.get("pts") for sg in route.get("segments", []))
        # A block face or a parking lot can stand in as the route's own start or
        # end. When it does it keeps its own ID and no extra number is spent, so
        # a lot at the end of the walk reads as END rather than getting a
        # separate marker beside it.
        own_start = next((sg for sg in route["segments"] if sg.get("is_route_start")), None)
        own_end = next((sg for sg in route["segments"] if sg.get("is_route_end")), None)
        route["start_inv_id"] = seq.take() if (has_line and not own_start) else None

        for seg in route["segments"]:
            pts = seg.get("pts") or []
            kind = seg.get("kind", "count")

            if kind == "lot":
                # A lot's perimeter is not route walking distance - the time to
                # cover it is already carried by its space count.
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

                minutes = ((seg["spaces_effective"] / spaces_per_block) * mins_per_block
                           if seg["spaces_known"] else mins_per_block)
                seg["minutes"] = round(minutes, 2)
                r_count_ft += length
                r_count_min += minutes
                r_spaces += seg["spaces_effective"] or 0
                n_blocks += 1

                key = seg["inv_id"]
                if key in all_ids:
                    duplicate_ids.add(key)
                all_ids.setdefault(key, []).append(seg["uid"])
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
                if route[key] in all_ids:
                    duplicate_ids.add(route[key])
                all_ids.setdefault(route[key], []).append(route["uid"] + ":" + key)
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
            "blocks": n_blocks,
            "spaces": r_spaces,
            "unsurveyed": sum(1 for sg in route["segments"]
                              if sg.get("kind") in ("count", "lot") and not sg.get("spaces_known")),
            "next_id": route.get("end_inv_id") or seq.peek(),
        }

    out["duplicate_ids"] = sorted(duplicate_ids, key=walk_order_key)
    out["grand_totals"] = {
        "routes": len(out["routes"]),
        "blocks": sum(r["totals"]["blocks"] for r in out["routes"]),
        "spaces": sum(r["totals"]["spaces"] for r in out["routes"]),
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
                "%.2f" % seg["minutes"], "yes" if seg["flagged"] else "",
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


def export_geojson(project):
    p = compute(project)
    feats = []
    for route in p["routes"]:
        for i, seg in enumerate(route["segments"]):
            if seg["kind"] == "lot":
                ring = [[pt[1], pt[0]] for pt in seg["pts"]]
                if ring and ring[0] != ring[-1]:
                    ring.append(ring[0])
                geom = {"type": "Polygon", "coordinates": [ring]}
            else:
                geom = {"type": "LineString",
                        "coordinates": [[pt[1], pt[0]] for pt in seg["pts"]]}
            feats.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "inv_id": seg.get("inv_id"), "route": route["name"],
                    "zone_prefix": route["prefix"], "order": i + 1,
                    "kind": seg["kind"], "street": seg.get("street", ""),
                    "length_ft": seg["length_ft"], "spaces": seg["spaces_effective"],
                    "minutes": seg["minutes"], "flagged": seg["flagged"],
                    "note": seg.get("note", ""), "color": route["color"],
                    "area_sqft": seg.get("area_sqft", 0),
                },
            })
        for key in ("start_at", "end_at"):
            if route.get(key):
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "Point",
                                 "coordinates": [route[key][1], route[key][0]]},
                    "properties": {"route": route["name"], "color": route["color"],
                                   "kind": key.replace("_at", ""),
                                   "inv_id": route.get(
                                       "start_inv_id" if key == "start_at" else "end_inv_id")},
                })
    return json.dumps({"type": "FeatureCollection", "features": feats}, indent=1)


def svg_geometry(project, width=2000, pad=70):
    """The exact projection export_svg uses, handed out so the browser can drop
    basemap tiles behind the drawing in the same coordinate space."""
    p = compute(project)
    pts = []
    for route in p["routes"]:
        for seg in route["segments"]:
            pts.extend(seg["pts"])
        for k in ("start_at", "end_at"):
            if route.get(k):
                pts.append(route[k])
    if not pts:
        return None
    xs, ys = zip(*[_mercator(pt[0], pt[1]) for pt in pts])
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    scale = (width - 2 * pad) / spanx
    return {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy,
            "scale": scale, "pad": pad, "width": width,
            "height": int(spany * scale + 2 * pad)}


def arrow_marks(proj_pts, spacing=78.0, closed=False, first=42.0):
    """Points and bearings for direction arrows along an already-projected
    polyline, spaced evenly in output units so density stays even regardless of
    how long the leg is."""
    marks = []
    seq = list(proj_pts) + ([proj_pts[0]] if closed and proj_pts else [])
    carry = first
    for i in range(len(seq) - 1):
        (x1, y1), (x2, y2) = seq[i], seq[i + 1]
        dx, dy = x2 - x1, y2 - y1
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-6:
            continue
        ux, uy = dx / seg_len, dy / seg_len
        t = carry
        while t <= seg_len:
            marks.append((x1 + ux * t, y1 + uy * t, math.atan2(uy, ux)))
            t += spacing
        carry = t - seg_len
    return marks


def _mercator(lat, lng):
    x = math.radians(lng)
    y = math.log(math.tan(math.pi / 4 + math.radians(max(-85.0, min(85.0, lat))) / 2))
    return x, y


def export_svg(project, width=2000, pad=70):
    """Vector plan sheet. Opens in Illustrator or Figma with every route,
    label and flag on its own layer, so the annotation pass you already do
    starts from real geometry instead of a hand trace."""
    p = compute(project)
    pts = []
    for route in p["routes"]:
        for seg in route["segments"]:
            pts.extend(seg["pts"])
        for k in ("start_at", "end_at"):
            if route.get(k):
                pts.append(route[k])
    if not pts:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">' \
               '<text x="20" y="60" font-family="sans-serif" font-size="15">' \
               'Nothing drawn yet.</text></svg>'

    xs, ys = zip(*[_mercator(pt[0], pt[1]) for pt in pts])
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    scale = (width - 2 * pad) / spanx
    height = int(spany * scale + 2 * pad)

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
                     % (x, y + 3.6, seg["inv_id"]))
        for key, idkey, cap in (("start_at", "start_inv_id", "START"),
                                ("end_at", "end_inv_id", "END")):
            if route.get(key) and route.get(idkey):
                x, y = P(route[key])
                o.append('<circle cx="%.1f" cy="%.1f" r="16" fill="%s" stroke="#ffffff" '
                         'stroke-width="3"/>' % (x, y, route["color"]))
                o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica,Arial" '
                         'font-size="11" font-weight="700" fill="#ffffff">%s</text>'
                         % (x, y + 4, route[idkey]))
                o.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica,Arial" '
                         'font-size="8" font-weight="700" fill="%s">%s</text>'
                         % (x, y + 27, route["color"], cap))
        o.append('</g>')
    o.append('</svg>')
    return "\n".join(o)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def export_pdf(project, width=1000, pad=44):
    """Vector PDF built by hand - no reportlab, no cairo, nothing to install.
    A PDF content stream is just drawing operators, so the same geometry that
    feeds the SVG can be written straight out."""
    p = compute(project)
    pts = []
    for route in p["routes"]:
        for seg in route["segments"]:
            pts.extend(seg["pts"])
        for k in ("start_at", "end_at"):
            if route.get(k):
                pts.append(route[k])
    if not pts:
        pts = [[37.8, -122.25], [37.81, -122.24]]

    xs, ys = zip(*[_mercator(pt[0], pt[1]) for pt in pts])
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    scale = (width - 2 * pad) / spanx
    height = spany * scale + 2 * pad

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
        self.send_header("Content-Type", ctype + "; charset=utf-8")
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
            base = re.sub(r"[^A-Za-z0-9._-]", "_", STATE["project"].get("name", "routes"))
            if kind == "csv":
                return self._send(export_csv(STATE["project"]), "text/csv",
                                  filename=base + "_inventory_ids.csv")
            if kind == "geojson":
                return self._send(export_geojson(STATE["project"]), "application/geo+json",
                                  filename=base + ".geojson")
            if kind == "svg":
                return self._send(export_svg(STATE["project"]), "image/svg+xml",
                                  filename=base + ".svg")
            if kind == "pdf":
                return self._send(export_pdf(STATE["project"]), "application/pdf",
                                  filename=base + ".pdf")
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
  --ink:#16202B; --ink2:#1E2B39; --ink3:#26333F; --line:#31424F;
  --paper:#EDF1F4; --muted:#8CA0B2; --amber:#E0A32E; --flag:#E5484D;
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
#find:focus{outline:none;border-color:var(--amber)}
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
.vtx.vsel{box-shadow:0 0 0 4px var(--amber);width:15px;height:15px}
.spacer{flex:1}
.hbtn{background:var(--ink3);border:1px solid var(--line);border-radius:5px;padding:6px 11px}
.hbtn:hover{border-color:var(--muted)}
.hbtn.go{background:var(--amber);border-color:var(--amber);color:#1B2430;font-weight:600}
.opt{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:12px;white-space:nowrap}

#rail{grid-area:rail;background:var(--ink2);border-right:1px solid var(--line);
  overflow-y:auto;padding:14px}
#list{grid-area:list;background:var(--ink2);border-left:1px solid var(--line);
  overflow-y:auto;padding:14px}
#map{grid-area:map;background:#20303c}
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
.tool.on{background:var(--amber);border-color:var(--amber);color:#1B2430}
.tool.on i{color:#4A3A12}
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
.blk.sel{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber)}
.blk.dup{border-color:var(--flag)}
.blkhead{display:flex;align-items:center;gap:8px;padding:7px 9px}
.blkhead .idtag{font-weight:600;min-width:52px}
.blkhead .idtag.man{color:var(--amber)}
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
  background:var(--ink3);border:1px solid var(--amber);border-radius:7px}
#lotbar span{flex:1;color:var(--muted);font-size:12.5px}
#lotfinish{background:var(--amber);border:1px solid var(--amber);border-radius:5px;
  color:#1B2430;font-weight:600;padding:5px 10px}
#lotfinish:disabled{opacity:.45;cursor:default}
#lotcancel{background:transparent;border:1px solid var(--line);border-radius:5px;padding:5px 10px}
.leaflet-container{background:#20303c}
kbd{background:var(--ink3);border:1px solid var(--line);border-radius:3px;padding:1px 5px;
  font-family:inherit;font-size:11.5px}
</style>
</head>
<body>

<header>
  <h1>Route Mapper</h1>
  <input id="pname" placeholder="Study area name">
  <div id="findwrap">
    <input id="find" placeholder="Find a place…  e.g. Grand Ave &amp; Lakeshore, Oakland" autocomplete="off">
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
    <option value="geojson">GIS (GeoJSON)</option>
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
      <button class="tool" data-tool="lot"><b>Parking lot</b><i>click a shape</i></button>
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
    <p class="tiny" style="margin:9px 0 0">Click along the street to lay the route down.
    Each click closes the leg behind it, so switching between counting and walking
    mid-route just means clicking the other tool and carrying on.
    <kbd>C</kbd> count · <kbd>W</kbd> walk · <kbd>L</kbd> lot · <kbd>A</kbd> direction arrows · <kbd>I</kbd> insert after selected joint · <kbd>P</kbd> corner handles · <kbd>Esc</kbd> select · <kbd>⌘Z</kbd> undo · <kbd>⇧⌘Z</kbd> redo · <kbd>Delete</kbd> removes the selected block. Drag an ID to move that block face, or drag a corner handle or endpoint to adjust the line.</p>
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
// Silent failures were the hardest part of the last round to diagnose, so any
// error now says so in the footer instead of vanishing into the console.
window.addEventListener('error', e => note('Error: ' + e.message, true));
window.addEventListener('unhandledrejection', e =>
  note('Error: ' + ((e.reason && e.reason.message) || e.reason), true));
let P = null, tool = 'none', activeRoute = 0, selected = null, lotPts = [],
    dragging = false, showPts = true, jointsOn = true, snapOn = true, mergeMode = false,
    breakAway = false, selVertex = null, layers = [];
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
async function push(record){
  try {
    if (record !== false && snapshot){
      undoStack.push(snapshot);
      if (undoStack.length > 80) undoStack.shift();
      redoStack = [];
    }
    const body = JSON.stringify(strip(P));
    P = await api('/api/state', {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: body});
    snapshot = body;
    render();
  } catch (err) {
    note('Could not apply that change: ' + err.message, true);
  }
}
async function restore(json){
  P = JSON.parse(json);
  snapshot = json;
  await push(false);
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
      'spaces_effective','spaces_known','id_is_manual','area_sqft','perimeter_ft']
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
let insertAt = null, penAnchor = null, showArrows = true;
function continueFrom(index, anchor){
  insertAt = index; penAnchor = anchor.slice();
  setTool('count');
  note('Next block inserts at position ' + (index + 1) + '. Esc returns to drawing at the end.');
  render();
}
function clearInsert(){ insertAt = null; penAnchor = null; }
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
function snapSeg(route, seg){
  let moved = false;
  const idx = seg.kind === 'lot' ? seg.pts.map((_,i) => i) : [0, seg.pts.length-1];
  idx.forEach(i => {
    const hit = snapTarget(seg.pts[i], seg);
    if (hit && !samePt(hit, seg.pts[i])){ seg.pts[i] = hit; moved = true; }
  });
  return moved;
}
function samePt(a, b){
  return a && b && Math.abs(a[0]-b[0]) < 1e-9 && Math.abs(a[1]-b[1]) < 1e-9;
}
function continueFromVertex(pt){
  const rt = R();
  let after = -1;
  rt.segments.forEach((sg, i) => {
    if (sg.kind === 'lot') return;
    if (sg.pts.some(q => samePt(q, pt))) after = Math.max(after, i);
  });
  continueFrom(after + 1, pt);
}
// Removing a joint means the two legs that met there become one leg.
function dissolveVertex(pt){
  const rt = R();
  const touching = rt.segments.filter(sg => sg.kind !== 'lot'
    && sg.pts.some(q => samePt(q, pt)));
  if (!touching.length){ selVertex = null; return render(); }

  const lot = rt.segments.find(sg => sg.kind === 'lot' && sg.pts.some(q => samePt(q, pt)));
  if (lot && lot.pts.length > 3){
    lot.pts = lot.pts.filter(q => !samePt(q, pt));
    selVertex = null; return push();
  }
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
  if (tool === 'lot'){
    if (lotPts.length >= 3 && near(pt, lotPts[0])) return finishLot();
    lotPts.push(pt); return drawDraft();
  }
  if (tool === 'count' || tool === 'walk'){
    const rt = R();
    // If you picked a block or joint and chose "continue from here", the next
    // leg splices in at that position instead of being tacked on the end, and
    // every ID after it shifts up.
    if (insertAt !== null && penAnchor){
      const hit = snapTarget(pt, null) || pt;
      rt.segments.splice(insertAt, 0, mkSeg(tool, [penAnchor.slice(), hit]));
      insertAt += 1; penAnchor = hit.slice();
      return push();
    }
    const prev = lastVertex();
    if (!prev){ rt.start = pt; return push(); }
    rt.segments.push(mkSeg(tool, [prev, pt]));
    return push();
  }
});
map.on('dblclick', () => { if (tool === 'lot') finishLot(); });

function near(a, b){
  const pa = map.latLngToContainerPoint(a), pb = map.latLngToContainerPoint(b);
  return Math.hypot(pa.x-pb.x, pa.y-pb.y) < 14;   // ~a fingertip on screen
}
// Committing the shape, not discarding it, is the whole point here. Switching
// tools mid-draw used to silently throw the polygon away.
function finishLot(){
  const pts = [];
  lotPts.forEach(pt => { if (!pts.length || !near(pt, pts[pts.length-1])) pts.push(pt); });
  lotPts = []; draft.clearLayers();
  if (pts.length < 3){ updateLotBar(); return false; }
  const rt = R();
  const seg = mkSeg('lot', pts);
  // Snap and attach first so any shared joints exist, then work out where the
  // lot sits in the walking order from those joints.
  rt.segments.push(seg);
  snapSeg(rt, seg);
  attachLot(rt, seg, true);
  reorderLot(rt, seg);
  selected = seg.uid;
  updateLotBar();
  push();
  setTool('count');              // straight back to laying route down
  return true;
}
// Connects a stranded lot or block face to the rest of the route with a short
// walking leg, so it is reachable rather than floating.
// Nearest point on the leg a-b to point p, in lat/lng.
function closestOnLeg(p, a, b){
  const A = map.latLngToLayerPoint(a), B = map.latLngToLayerPoint(b),
        P0 = map.latLngToLayerPoint(p);
  const dx = B.x-A.x, dy = B.y-A.y, L2 = dx*dx + dy*dy;
  if (!L2) return a.slice();
  let t = ((P0.x-A.x)*dx + (P0.y-A.y)*dy) / L2;
  t = Math.max(0, Math.min(1, t));
  const ll = map.layerPointToLatLng(L.point(A.x + t*dx, A.y + t*dy));
  return [ll.lat, ll.lng];
}
// Where in the walking order does this lot belong? Next to the leg it sits by,
// so its ID follows the route instead of landing at the end.
// Where a lot belongs in the walking order. A shared joint is definitive: if a
// lot corner sits on the end of one leg it goes after that leg, and if it sits
// on the start of a leg it goes before it. A lot bridging the joint between two
// legs therefore lands between them and takes the ID in between. Only when
// nothing is actually joined do we fall back to guessing from distance.
function lotInsertIndex(route, lot){
  let byJoint = null;
  route.segments.forEach((sg, i) => {
    if (sg === lot || sg.kind === 'lot' || sg.pts.length < 2) return;
    const first = sg.pts[0], last = sg.pts[sg.pts.length-1];
    lot.pts.forEach(q => {
      if (samePt(q, last)) byJoint = byJoint === null ? i + 1 : Math.min(byJoint, i + 1);
      else if (samePt(q, first)) byJoint = byJoint === null ? i : Math.min(byJoint, i);
    });
  });
  if (byJoint !== null) return byJoint;

  const c = lot.pts.reduce((a,q) => [a[0]+q[0]/lot.pts.length, a[1]+q[1]/lot.pts.length], [0,0]);
  let best = route.segments.length, bestD = Infinity;
  route.segments.forEach((sg, i) => {
    if (sg === lot || sg.kind === 'lot' || sg.pts.length < 2) return;
    for (let k = 0; k < sg.pts.length-1; k++){
      const d = map.distance(c, closestOnLeg(c, sg.pts[k], sg.pts[k+1]));
      if (d < bestD){ bestD = d; best = i + 1; }
    }
  });
  return best;
}
// Snapping happens after a lot is first dropped, so its position has to be
// recomputed once the joints exist. Called after any snap, join or drag.
function reorderLot(route, lot){
  const at = route.segments.indexOf(lot);
  if (at < 0) return false;
  route.segments.splice(at, 1);
  const to = lotInsertIndex(route, lot);
  route.segments.splice(to, 0, lot);
  return to !== at;
}
// Pull a lot onto the route: snap its nearest corner to the line if it is close,
// otherwise drop a short walking leg so it is never left floating.
function attachLot(route, lot, quiet){
  let best = null, bestD = Infinity, corner = null;
  lot.pts.forEach(a => {
    route.segments.forEach(sg => {
      if (sg === lot || sg.kind === 'lot' || sg.pts.length < 2) return;
      for (let k = 0; k < sg.pts.length-1; k++){
        const hit = closestOnLeg(a, sg.pts[k], sg.pts[k+1]);
        const d = map.distance(a, hit);
        if (d < bestD){ bestD = d; best = hit; corner = a; }
      }
    });
  });
  if (!best){ if (!quiet) note('No route leg nearby to attach the lot to.', true); return false; }
  const ftAway = bestD * 3.28084;
  if (ftAway <= 60){
    const i = lot.pts.findIndex(q => samePt(q, corner));
    lot.pts[i] = best.slice();
    if (!quiet) note('Lot snapped onto the route.');
  } else {
    const at = route.segments.indexOf(lot);
    route.segments.splice(Math.max(at, 0), 0, mkSeg('walk', [best, corner.slice()]));
    if (!quiet) note('Lot joined with a walking leg of ' + Math.round(ftAway) + ' ft.');
  }
  return true;
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
  if (lotPts.length){
    L.polygon(lotPts, {color:'#E0A32E', weight:2, dashArray:'5 5', fillOpacity:.15}).addTo(draft);
    lotPts.forEach((pt,i) => L.circleMarker(pt, {radius: i === 0 ? 7 : 4,
      color:'#fff', weight:2, fillColor:'#E0A32E', fillOpacity:1})
      .bindTooltip(i === 0 ? 'Click here again to close the shape' : '').addTo(draft));
  }
  updateLotBar();
}
function updateLotBar(){
  const bar = $('#lotbar');
  if (tool === 'lot' && lotPts.length){
    bar.style.display = 'flex';
    $('#lotcount').textContent = lotPts.length + (lotPts.length === 1 ? ' corner' : ' corners');
    $('#lotfinish').disabled = lotPts.length < 3;
  } else bar.style.display = 'none';
}
function setTool(t){
  if (tool === 'lot' && t !== 'lot' && lotPts.length >= 3) finishLot();
  tool = t;
  document.querySelectorAll('.tool').forEach(b => b.classList.toggle('on', b.dataset.tool === t));
  const hints = {
    count:'Click along the street. Each click ends a block face and starts the next.',
    walk:'Click along the connector. These legs are dashed and never get an ID.',
    lot:'Click the corners of the lot. Close it by clicking the first corner again, pressing Enter, or double-clicking.',
    flag:'Click a block or lot to circle it in red for follow-up.',
    start:'Click where this route begins. Pick another tool when you are done.',
    end:'Click where this route ends. Pick another tool when you are done.',
    none:'Click a block on the map or in the list to edit it.'};
  $('#hint').textContent = hints[t];
  if (t !== 'lot'){ lotPts = []; draft.clearLayers(); }
  updateLotBar();
}
document.querySelectorAll('.tool').forEach(b => b.onclick = () => setTool(b.dataset.tool));
$('#optSnap').onchange = e => { snapOn = e.target.checked; };
$('#optJoints').onchange = e => { jointsOn = e.target.checked; };
$('#lotfinish').onclick = () => finishLot();
$('#lotcancel').onclick = () => { lotPts = []; draft.clearLayers(); updateLotBar(); };
document.addEventListener('keyup', e => { if (!e.altKey) breakAway = false; });
document.addEventListener('keydown', e => {
  if (e.altKey) breakAway = true;
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
  else if (e.key === 'Escape'){ clearInsert(); setTool('none'); }
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
      if (tool === 'flag'){ seg.flagged = !seg.flagged; push(); }
      else { activeRoute = ri; selected = seg.uid; render(); scrollToSel(); }
    };
    rt.segments.forEach(seg => {
      if (seg.pts.length < 2) return;
      const dim = ri === activeRoute ? 1 : .45;
      if (seg.kind === 'lot'){
        // Solid outline, matching the route colour, so lots read as counted area.
        L.polygon(seg.pts, {color: rt.color, weight: 3, opacity: dim,
          fillColor: rt.color, fillOpacity: .18})
          .on('click', ev => pick(seg, ev)).addTo(layer);
      } else {
        L.polyline(seg.pts, {color: rt.color, weight: seg.kind === 'count' ? 6 : 3,
          opacity: dim, dashArray: seg.kind === 'count' ? null : '9 8'})
          .on('click', ev => pick(seg, ev)).addTo(layer);
      }
      if (seg.inv_id && seg.label_at){
        // Dragging an ID drags the block face with it - the line follows.
        const anchor = seg.label_at.slice();
        const m = L.marker(anchor, {draggable: true, zIndexOffset: 500,
          icon: L.divIcon({className:'', iconSize:[26,26],
            html:'<div class="idlabel'+(seg.flagged?' flagged':'')
                 +'" style="border-color:'+rt.color+'">'+seg.inv_id+'</div>'})}).addTo(layer);
        m.on('dragstart', () => { dragging = true; });
        m.on('dragend', ev => {
          dragging = false;
          const ll = ev.target.getLatLng();
          activeRoute = ri;
          translateSeg(rt, seg, ll.lat - anchor[0], ll.lng - anchor[1]);
          if (seg.kind === 'lot' && snapOn){
            snapSeg(rt, seg);
            attachLot(rt, seg, true);
            reorderLot(rt, seg);
            note('Lot re-attached and renumbered in route order.');
          } else if (snapSeg(rt, seg)) note('Snapped into the route');
          push();
        });
        m.on('click', ev => { if (!dragging) pick(seg, ev); });
      }
    });
    // Corner handles on the route you are working on, for finer adjustment.
    if (ri === activeRoute && showPts){
      const seen = [];
      rt.segments.forEach(seg => seg.pts.forEach(pt => {
        if (seen.some(q => samePt(q, pt))) return;
        seen.push(pt);
        const from = pt.slice();
        const h = L.marker(pt, {draggable: true, zIndexOffset: 400,
          icon: L.divIcon({className:'', iconSize:[13,13],
            html:'<div class="vtx'+(samePt(selVertex, pt)?' vsel':'')
                 +'" style="background:'+rt.color+'"></div>'})}).addTo(layer);
        h.on('click', ev => { L.DomEvent.stop(ev);
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
    // Endpoints take the route's own colour so you can tell three routes apart.
    // S is a solid disc, E carries an inner ring.
    const endpoint = (pt, glyph, label) => {
      const from = pt.slice();
      const m = L.marker(pt, {draggable: true, zIndexOffset: 1000,
        icon: L.divIcon({className:'', iconSize:[34,48], iconAnchor:[17,17],
          html:'<div class="endpt'+(glyph === 'E' ? ' isend' : '')
               +'" style="background:'+rt.color+'">'
               +(glyph === 'S' ? (rt.start_inv_id || 'S') : (rt.end_inv_id || 'E'))
               +'</div><div class="endcap">'+(glyph === 'S' ? 'START' : 'END')+'</div>'})})
        .bindTooltip(label + ' — drag to move').addTo(layer);
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
    b.innerHTML = '<span class="dot" style="background:'+rt.color+'"></span>'+rt.name;
    b.onclick = () => { activeRoute = i; selected = null; render(); };
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
    row('Block faces', t.blocks) + row('Spaces', t.spaces) +
    row('Counting', fmtMin(t.count_min) + ' · ' + fmtFt(t.count_ft)) +
    row('Connecting', fmtMin(t.walk_min) + ' · ' + fmtFt(t.walk_ft)) +
    row('Next ID', t.next_id);
  const g = P.grand_totals;
  $('#gstats').innerHTML = row('Routes', g.routes) + row('Block faces', g.blocks) +
    row('Spaces', g.spaces) + row('Distance', g.total_mi + ' mi') +
    row('Time', fmtMin(g.total_min));

  drawArrows();
  renderList();
}
function row(k, v, big){
  return '<dt>'+k+'</dt><dd'+(big?' class="big"':'')+'>'+v+'</dd>';
}
function fmtMin(m){
  m = Math.round(m); return m < 60 ? m+' min' : Math.floor(m/60)+'h '+String(m%60).padStart(2,'0');
}
function fmtFt(f){ return f >= 1000 ? (f/5280).toFixed(2)+' mi' : Math.round(f)+' ft'; }

function renderList(){
  const dup = P.duplicate_ids || [];
  $('#dupwarn').innerHTML = dup.length
    ? '<div class="warn">Two blocks share '+dup.join(', ')+'. Clear one of the manual IDs to let the numbering flow again.</div>' : '';
  const host = $('#blocks'); host.innerHTML = '';
  const rt = R();
  if (!rt.segments.length){
    host.innerHTML = '<p class="empty">Nothing drawn yet. Pick <b>Count block</b> and click along the first street. '
      + 'IDs appear as you go and renumber themselves whenever you insert, delete or reorder a leg. Parking lots take an ID in the same sequence.</p>';
    return;
  }
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
  const meta = isLot
    ? 'lot · ' + Math.round(seg.area_sqft).toLocaleString() + ' sq ft · '
      + seg.spaces_effective + ' sp · ' + seg.minutes.toFixed(1) + ' min'
    : Math.round(seg.length_ft) + ' ft'
      + (seg.kind === 'count' ? ' · ' + seg.spaces_effective + ' sp · ' + seg.minutes.toFixed(1) + ' min'
                              : ' · walk ' + seg.minutes.toFixed(1) + ' min');
  head.innerHTML =
    '<span class="idtag num'+(seg.id_is_manual?' man':'')+'">'
      + (seg.inv_id || '—') + '</span>'
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
  head.appendChild(mk('↑', 'Move earlier in the route', '', () => {
    if (i > 0){ const a = rt.segments; [a[i-1], a[i]] = [a[i], a[i-1]]; push(); } }));
  head.appendChild(mk('↓', 'Move later in the route', '', () => {
    const a = rt.segments; if (i < a.length-1){ [a[i+1], a[i]] = [a[i], a[i+1]]; push(); } }));
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
    seg.kind === 'walk' ? '—'
      : 'Est. ' + seg.spaces_effective + (isLot ? ' from area' : ' from length'));
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
    swap.onclick = () => { seg.kind = seg.kind === 'count' ? 'walk' : 'count'; push(); };
    acts.appendChild(swap);
  }
  const join = document.createElement('button'); join.className = 'hbtn';
  join.textContent = 'Join to the route';
  join.title = 'Drop a walking leg from here to the nearest point on the route';
  join.onclick = () => {
    if (seg.kind === 'lot'){ const rt = R(); attachLot(rt, seg); reorderLot(rt, seg); push(); }
    else joinToRoute(seg);
  };
  acts.appendChild(join);
  const clr = document.createElement('button'); clr.className = 'hbtn';
  clr.textContent = 'Back to auto ID';
  clr.onclick = () => { seg.manual_id = null; push(); };
  acts.appendChild(clr);
  body.appendChild(acts);

  // Continue drawing from this point: the next block lands immediately after
  // this one and everything downstream renumbers.
  const acts2 = document.createElement('div'); acts2.className = 'f2';
  const cont = document.createElement('button'); cont.className = 'hbtn';
  cont.textContent = 'Continue from here';
  cont.title = 'The next block you draw is inserted directly after this one';
  cont.onclick = () => {
    const anchor = seg.kind === 'lot'
      ? (seg.label_at || seg.pts[0]) : seg.pts[seg.pts.length-1];
    continueFrom(i + 1, anchor);
  };
  acts2.appendChild(cont);

  // Marking a block or lot as the route's own start or end. Useful when the walk
  // finishes in a parking lot: the lot becomes END and keeps its own ID rather
  // than a separate marker landing on top of it.
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
  acts2.appendChild(cap);
  body.appendChild(acts2);

  const cap2 = document.createElement('button'); cap2.className = 'hbtn';
  cap2.textContent = isStart ? 'Stop being the START' : 'Make this the START';
  cap2.onclick = () => {
    rt2.segments.forEach(x => { x.is_route_start = false; });
    seg.is_route_start = !isStart;
    if (seg.is_route_start){ seg.is_route_end = false; rt2.start = null; }
    push();
  };
  body.appendChild(cap2);



  if (!seg.spaces_known && seg.kind !== 'walk'){
    const p = document.createElement('p'); p.className = 'tiny';
    p.textContent = 'No space count yet. Until the inventory survey comes back this block is '
      + 'costed at one block of walking time.';
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
map.on('zoomend', drawArrows);
map.on('moveend', drawArrows);

function scrollToSel(){
  const el = document.querySelector('.blk.sel');
  if (el) el.scrollIntoView({block:'nearest', behavior:'smooth'});
}

// ---- panel wiring --------------------------------------------------------
$('#rname').onchange = e => { R().name = e.target.value; push(); };
$('#rprefix').onchange = e => { R().prefix = parseInt(e.target.value||'1',10); push(); };
$('#rcolor').oninput = e => { R().color = e.target.value; push(); };
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
  activeRoute = 0; selected = null; render();
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
  render();
  note('Opened "' + name + '"');
};
// ---- find a place -------------------------------------------------------
let findTimer = null;
$('#find').addEventListener('input', () => {
  clearTimeout(findTimer);
  const q = $('#find').value.trim();
  if (q.length < 3){ $('#findlist').style.display = 'none'; return; }
  findTimer = setTimeout(async () => {
    let r;
    try { r = await api('/api/geocode?q=' + encodeURIComponent(q)); }
    catch (err) { return; }
    const list = $('#findlist');
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
  }, 350);
});
$('#find').addEventListener('keydown', e => {
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
      P = await api('/api/state', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(base)});
      selected = null; render();
      note('Merged ' + obj.routes.length + ' route(s) from ' + f.name +
           '. Zone numbers were bumped to keep IDs unique.');
    } else {
      P = await api('/api/state', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(obj)});
      activeRoute = 0; selected = null; setTool('none');
      if (P.center) map.setView(P.center, P.zoom || 15);
      render();
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
  const b = await r.blob();
  return await new Promise(res => { const fr = new FileReader();
    fr.onload = () => res(fr.result); fr.readAsDataURL(b); });
}
// Python's projection is plain Web Mercator, so tiles land in the same frame.
async function basemapTiles(meta){
  const TAU = Math.PI * 2;
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
  const out = [];
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++){
    let href;
    try { href = await asDataUrl(tileUrl(z, x, y)); } catch (err) { continue; }
    const left = px(sx(x)), right = px(sx(x+1)), top = py(sy(y)), bot = py(sy(y+1));
    out.push({href, x:left, y:top, w:right-left, h:bot-top});
  }
  return out;
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
        const a = document.createElement('a');
        a.href = URL.createObjectURL(b);
        a.download = (P.name || 'routes').replace(/[^\w.-]+/g, '_') + '.png';
        a.click();
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
  const esc = t => t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
  $('#bLoad').innerHTML = '<option value="">' +
    (r.projects.length ? 'Open\u2026 (' + r.projects.length + ')' : 'Open\u2026 (none saved)') +
    '</option>' +
    r.projects.map(n => '<option value="'+esc(n)+'">'+esc(n)+'</option>').join('');
  $('#bLoad').title = 'Saved projects live in ' + r.dir;
}

(async () => {
  P = await api('/api/state');
  if (P.center) map.setView(P.center, P.zoom || 15);
  setTool('none'); render(); loadProjectList();
})();
</script>
</body>
</html>
"""


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
