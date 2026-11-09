[README_route_mapper.md](https://github.com/user-attachments/files/34596921/README_route_mapper.md)
# Route Mapper

A tool for laying out parking occupancy count routes and generating inventory IDs,
distances and walk-time estimates before the field work starts.

```
python3 route_mapper.py
```

On Windows you can double-click **Start Route Mapper (Windows).bat** instead (it runs
`py route_mapper.py`). Each person runs their own copy on their own computer; the
step-by-step setup for Windows and Mac, including installing Python, is in
`docs/Route_Mapper_Workflow_v1.docx`. The map opens on all of Oakland.

That's the whole install. Standard library only — no pip, no virtualenv. It starts a
local server, opens your browser, and saves projects to `route_projects/` next to the
script, wherever you launch it from. Nothing leaves your machine except basemap tile
requests and place searches, which go to OpenStreetMap's Nominatim. Type a place in
the search box and press `Enter`. It doesn't search as you type, because Nominatim's
usage policy doesn't allow that.

If port 8765 is busy: `ROUTE_MAPPER_PORT=8790 python3 route_mapper.py`

To keep projects somewhere else: `ROUTE_MAPPER_DIR=~/surveys python3 route_mapper.py`

---

## The problem this solves

Your current process breaks when you insert a block face, because every ID after it
has to be renumbered by hand. Here, IDs are never stored — they're derived from route
order every time anything changes. Insert a block at position 2 and everything
downstream shifts by one automatically, including parking lots. Delete one and it
closes up. Reorder two legs and both renumber.

The numbering rule is in one place, `IdSequence`, and it reproduces the convention
your data already uses: `2.1` through `2.9`, then `2.11`, with every multiple of ten
skipped. This was verified against both study areas — generating 54 IDs for Fruitvale
zone 2 and 34 for Grand Lake zone 1 reproduces the real ID sets exactly. Turn it off
in the timing panel if a future study area drops the convention.

---

## Drawing a route

Click **Count block** and click along the street. Each click closes the leg behind it
and starts the next, which mirrors how you'd actually walk it. Switch to **Walk only**
when you're covering ground you don't count — those legs render dashed and never
receive an ID. New points snap to nearby corners and lines so legs join up; untick
the snap option in the Draw panel if it gets in the way.

**Parking lot** takes clicks as the lot's corners. Close the shape by clicking the
first corner again, pressing `Enter`, or double-clicking. You can draw a lot while
routing or add it afterwards.

Each lot is tied to the route by a dotted line from its ID to a blue pin on the
route. The pin marks where the lot is counted in the walk. The line only shows while
the lot is selected (click the lot, or its row in the list); click empty map to hide
it again. A new lot starts tied to the nearest point on the route, and its line is
showing, so if that's the wrong spot just drag the pin along the route. As you drag:

- Dropping the pin on the first half of a block counts the lot just before that block.
  Dropping it on the second half counts it just after.
- IDs renumber to match as soon as you let go.
- ↑/↓ on a lot's row moves its pin one block earlier or later.
- If the block a lot is tied to is deleted or merged, the lot re-ties itself to the
  nearest point on what's left.

The line only marks a place in the sequence. It isn't walking distance. Lots from
older projects, or drawn before there was any route, aren't tied yet and keep their
place in the list. Use **Attach to the route** on the lot to give them a line.
**Redraw the outline** changes a lot's shape without touching its line or ID.

**Flag** turns a click into a red circle around an ID — the follow-up marking you
currently do in the annotation pass, available while the route is still being built.

**Start point** and **End point** place the S and E markers. Placing one away from the
route drops a walking leg to join it up, so S and E always sit on the route.

To change a route after drawing it:

- Drag an ID to move that block face; the neighbouring legs follow. Hold `Alt`/`Option`
  while dragging to break it away instead. Drag a corner handle or an endpoint to
  adjust the line. Dragging a corner moves every leg that meets there.
- **Separate and rejoin one block's end.** Click a block and its two ends show as blue
  tabs (white for its start, filled for its end). Drag a tab to move just that block's
  end. It comes away from its neighbours and keeps its number while it's separated.
  Drop it on another block's end (a start onto an end, or an end onto a start) and
  they snap together:
  - If the two are already next to each other in the walk, the numbers stay as they
    are, and any walking leg that only bridged the old gap is removed. Use this, for
    example, to start the next block from the end of a missed block you just added.
  - If they weren't next to each other, the block moves beside the one you joined it
    to, and the IDs after it renumber.
- Click a corner handle and press `Delete` to remove that joint; the two legs it
  separated merge into one.
- **Insert a block** (for a missed block, or to split one). Click **＋ Insert a block**
  at the top of the list, press `I`, or click **＋** on a block's row. Every line on the
  route gets a white handle near its middle, as when editing a line in ArcGIS Online.
  Drag a handle, or just click it, to add a point there:
  - The block splits in two at that point. The second half becomes the next block,
    and the IDs after it renumber.
  - Drag the new point wherever the walk should go. Drop it on another corner to join
    up there. Keep adding points the same way to shape the route.
  - Hold `Alt`/`Option` while dragging to bend the line at the new point without
    adding a block.

  The route stays one continuous line, so its arrows keep agreeing. Press `Esc` or
  click the button again when you're done. ↑/↓ still move a single block (or a lot's
  pin) one place, which is handy for fixing two blocks in the wrong order.
- **Flip direction** (on an open block's row, or `F` with the block selected) reverses
  that one block so its arrows point the other way. It keeps its ID. Its two ends swap,
  so drag its blue end tabs if it should now join up differently.
- **Make this the END** / **Make this the START** lets a block face or lot stand in as
  the route's end or start. It keeps its own ID instead of getting a separate marker.
  Handy when the walk finishes in a parking lot.
- **Reverse direction** in the Routes panel flips the walking order and renumbers
  every ID to match.
- Right-click a route tab to hide or show that route.
- **Rotate the map** by right-clicking and dragging on it, as in ArcGIS Online. Shift +
  scroll also rotates. The compass button under the zoom buttons turns it back to north
  up. Exports are always north up.

Keys: `C` count · `W` walk · `L` lot · `A` direction arrows · `I` insert a block ·
`F` flip the selected block · `P` corner handles · `Esc` select (also ends Insert a
block) · `⌘Z` / `Ctrl+Z` undo · `⇧⌘Z` / `Ctrl+Shift+Z` redo · `Delete` removes the
selected block.

---

## Reference layers

Load other data to see what needs mapping, such as the City's parking meters, a
study-area outline drawn in ArcGIS, or zone boundaries. Click **Add a layer…** in the
left panel, or drop files onto the map. Route Mapper reads:

- **CSV** with latitude and longitude columns (or a WKT geometry column such as
  `the_geom`). The City of Oakland parking meters file loads as-is.
- **GeoJSON**, and ArcGIS's own JSON.
- **KML / KMZ**.
- **Zipped shapefiles** in WGS 1984 or Web Mercator.

From ArcGIS Online, open the layer's item page and choose **Export Data**. Pick
GeoJSON, CSV, KML or Shapefile, then load what it downloads. Files in another
projection, such as State Plane, are refused with a note to re-export in WGS 1984.

Layers draw under the routes. Click a feature (with **Select / pan** on) to see its
details. Each layer has a show/hide box, a colour, **Colour by** for fields with up to
20 values (for example meter Zone or Area) with a small legend, a zoom button and a
remove button. While you're drawing, clicking on a layer just draws the route there.

Layers aren't part of the project and aren't saved in its file. They stay in your
browser until you remove them, so on another computer, load them again there.

---

## Editing IDs

Two things you asked for, and they interact:

- **Individually** — click a block, type an ID in the first field. It's pinned, shows
  blue, and everything after it continues from that number. Pin `2.14` and the next
  auto ID is `2.15`. Pin `1.9` and the next is `1.11`, because the skip rule still
  applies. "Back to auto ID" releases it.
- **All at once** — change the zone number in the Routes panel and the whole route
  renumbers. Pinned IDs stay pinned; clear them individually to release.

If two blocks end up sharing an ID, both rows outline in red and a banner names the
conflict.

---

## Timing

The default follows your rule: a block face of about 10 spaces takes 2 minutes. Both
halves are editable, so if the real pace turns out to be 3 minutes per 12 spaces you
change it once and every route re-estimates. **Walk-only** legs count their walking
time, which is distance ÷ walking pace (default 260 ft/min, roughly 3 mph), but no
counting time, and they get no ID. The route panel shows the two separately, as
**Counting** and **Walking only**.

Space counts are never guessed. Until the inventory survey gives you a real count, a
block face is costed as one block of time (2 minutes by default). Its row says "not
surveyed", and the CSV marks it the same way. Typing a count replaces it.

**Parking lots are not included in the distance or time.** How long a lot takes
depends on its aisle layout, how many levels it has, and whether it's counted from the
edge or walked aisle by aisle. The map can't tell any of that. The block-face rule
doesn't carry over either, because it includes walking along the curb. So lots aren't
given a made-up figure. They're listed separately ("Parking lots: 2 · not timed"), and
the app and the shareable viewer both say so wherever totals appear. Walking into and
around a lot isn't counted either, so plan your own allowance for each lot.

Per-route you get total distance, total time, the counting/connecting split, block
and lot counts, total spaces (block faces and lots together), and the next ID in
sequence. Every block face carries its own length and estimated minutes.

---

## Exports

- **Inventory IDs (CSV)** — one row per block face and lot with ID, route, street,
  length, spaces, estimated minutes, flag state, note and label coordinates. This is
  the one that seeds Survey123. Lots have a blank minutes cell, since they aren't timed.
- **SVG** — a vector plan sheet with each route on its own named layer, solid and
  dashed legs, ID labels, red flag circles, start/end markers and lot polygons. Opens
  in Illustrator or Figma, so your annotation pass starts from real geometry instead
  of a hand trace.
- **PDF** — the same plan sheet as a vector PDF, ready to print. It never has a map
  behind it.
- **PNG** — the SVG plan sheet turned into an image at 2× size, for email or slides.
- **GIS layers for ArcGIS (.zip)** — ready for ArcGIS Pro's JSON To Features, which
  needs one geometry type per file:
  - block faces (lines)
  - parking lots (polygons)
  - the whole route including walking legs (lines)
  - start and end points

  The zip also holds the CSV with a `schema.ini` that makes ArcGIS read `INV_ID` as
  Text, and a short README with the import steps. Block faces and lots come in with a
  Text `INV_ID`, ready for the one-to-many join in the occupancy workflow, so you don't
  have to draw them again in ArcGIS. Field names match the CSV (`INV_ID`, `ROUTE`,
  `ZONE_PREFIX`, `WALK_ORDER`, `STREET`, `SPACES`…).
- **GeoJSON, one file** — everything in a single file, for QGIS and other tools that
  accept mixed geometry.
- **Shareable viewer (HTML)** — one self-contained, read-only file with the routes
  built in. Anyone can open it by double-clicking. No Python or install is needed, only
  internet for the map tiles.
- **Project file (JSON)** — the raw project, for backup or to hand to someone else.

The **map behind** option and the basemap picker next to the Export menu control
whether SVG and PNG exports have map tiles drawn behind the routes.

Projects save as JSON into `route_projects/` and reopen from the **Open…** menu.
**Import file…** opens a project JSON from anywhere on disk. **Merge file…** adds
another project's routes alongside the current ones. It bumps zone numbers where
they clash, so IDs stay unique.

---

## Using the engine without the UI

The Python side is importable if you'd rather script something:

```python
import route_mapper as rm

p = rm.new_project("Fruitvale 2027")
p["routes"][0]["prefix"] = 2
p["routes"][0]["segments"].append(
    rm.new_segment("count", [[37.7749, -122.2241], [37.7749, -122.2226]], spaces=12))

c = rm.compute(p)                    # fills in IDs, lengths, minutes, totals
print(c["routes"][0]["totals"])
open("routes.svg", "w").write(rm.export_svg(p))
```

`compute()` is pure — it takes a project and returns a new one with derived fields
filled in, so it's safe to call on every keystroke. That's exactly what the browser
does.

In Jupyter or Colab, where the normal blocking server would hang the cell, start it
in the background instead:

```python
rm.serve(open_browser=True)   # returns at once; tries the next port if 8765 is busy
rm.stop()                     # shut it down again
```

---

## Worth knowing

Distances are great-circle along the vertices you click, so they measure the path you
drew rather than the true curb line. At block scale the difference is small, but a
curving street needs a few more clicks to measure honestly.
