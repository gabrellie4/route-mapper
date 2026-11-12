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

**Curved streets.** Hold `Shift` and click once or twice along the curve, then click
normally where the block ends. The block becomes one smooth curve through the points
you clicked, with one ID, and its length (and so its time) follows the curve. One
Shift-click in the middle of a bend is usually enough. Add a second on a long or
S-shaped curve. While you draw, the curve previews as it will be laid: solid up to
the last bend, dashed from there to the pointer.

The curve is fitted as circular arcs through your clicks, because street curves are
mostly arcs. Clicked with ordinary accuracy, it typically stays within 2–5 ft of the
real curve, where straight lines between the same clicks were 10–25 ft off. A click
within 15 ft of another is treated as a wobble, not a bend. A stretch that would
swing round more than 120° is drawn straight.

`Alt`/`Option`-click bends the block at a sharp corner instead, for an angled block
face. A block is either smooth or straight throughout. If you used both, it's
smooth, and the button below switches it.

While you're bending, a bar under the tools counts the bends:

- `Backspace` or `⌘Z` takes back the last bend.
- `Enter` or **End here** ends the block at the last bend.
- Switching tools ends it there too, so bends are never thrown away.

Clicking twice on the same spot no longer lays a block a few feet long with its own
ID.

**Make it a smooth curve** / **Make it straight** on an open block's row switches a
block of three or more points between a curve and straight lines. Use it on blocks
drawn with Shift-click before curves were smooth (Nov 9–10, 2026), which are still
straight lines between their clicks.

**A curve already drawn as several blocks?** Open the first of them and click
**Merge with next** (or select it and press `M`). The next block joins onto it with
every corner kept, so the shape doesn't change; it keeps the first block's ID and
details, and the IDs after it move up by one. Then click **Make it a smooth curve**. Space counts add together; if only one
half had a count, the note says to check it. Repeat until the curve is one block.
Merging needs the two to meet end to start and be the same kind (both counted or both
walk-only). Removing a joint with `Delete` also merges two blocks, but it straightens
the line across the missing corner.

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
  On a curved block the handles sit on the curve, and a split keeps both halves
  curved. Drag a handle, or just click it, to add a point there:
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

Keys: `C` count · `W` walk · `L` lot · `Shift`-click curve the block you're drawing ·
`Alt`/`Option`-click sharp bend ·
`A` direction arrows · `I` insert a block · `F` flip the selected block · `M` merge
the selected block with the next · `P` corner handles · `Esc` select (also ends Insert
a block) · `⌘Z` / `Ctrl+Z` undo · `⇧⌘Z` / `Ctrl+Shift+Z` redo · `Delete` removes the
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

The default follows your rule: a block face of about 10 spaces takes 2 minutes. That
is 0.2 minutes a space, so a block's time follows how many spaces it has, and block
faces of different lengths take different times. All of it is editable in the
**Timing model** panel, and every route re-estimates when you change it:

| Setting | Default | What it does |
|---|---|---|
| Minutes per block | 2 | Counting time for one "block" of spaces. |
| Spaces that counts as a block | 10 | How many spaces that is. |
| Feet of curb per space | 16 | Turns a block's length into spaces until it's surveyed. 10 spaces is about 160 ft of curb. |
| Lot sq ft per space | 325 | Turns a lot's area into spaces until it's surveyed. Surface lots run about 300–350 sq ft a space, aisles included. |
| Walking pace, ft/min | 260 | About 3 mph. Times walk-only legs, and is the least time any block can take. |

- **A block with a survey count** takes spaces ÷ 10 × 2 minutes.
- **A block with no count yet** is timed by its length: one space per 16 ft of curb,
  rounded down. A 160 ft block is about 10 spaces, so 2 minutes; a 1,300 ft block is
  about 81 spaces, so about 16 minutes; a 30 ft stub is about 0.2 minutes. Before this,
  every unsurveyed block cost a flat 2 minutes whatever its length. The row shows the
  estimate with a ≈ (for example "≈31 sp est."). It is never stored or exported as a
  count: the CSV's SPACES cell stays blank and SPACES_SOURCE says "not surveyed".
  Typing the survey count replaces the estimate.
- **No block takes less time than walking it** at the walking pace. This only matters
  for long blocks with few spaces, such as a 1,600 ft frontage with 2 spaces.
- **Walk-only** legs count their walking time (distance ÷ pace) but no counting time,
  and they get no ID.

**Parking lots get a suggested time.** It's the lot's spaces at the same 0.2 minutes a
space: the surveyed count once there is one, or until then its drawn area ÷ 325 sq ft.
A 10,000 sq ft lot with no count is about 30 spaces, so 6 minutes. The map can't see
levels, aisle layout or whether the lot is counted from the edge, so the suggestion is
a starting point. Open the lot and type your own figure in **Minutes to allow** (a
three-level garage, say), and that replaces it. Clear the box to go back to the
suggestion. Lot time is included in the route's time and shown on its own line
("Parking lots: 2 · 14 min"). Lots still add no distance, and walking into them isn't
measured.

Per route you get the route time (counting, walking and lots together), total
distance, the **Counting** / **Walking only** split, block and lot counts, spaces
entered so far plus an estimate for the rest ("Spaces 120 + ≈410 est."), and the next
ID in sequence. Every block face carries its own length and estimated minutes.

---

## Exports

**To keep working later, use Project file (JSON).** It's the only export Route Mapper
can open again, and it's listed first in the Export menu for that reason. Everything
else is one-way: for Survey123, GIS, printing or sharing.

- **Project file (JSON)** — the whole project as it is on screen: every route, block,
  lot, pinned ID, space count, note and setting. Open it again with **Import file…**
  on any computer running Route Mapper and carry on. Use it to move a project to
  another computer, hand it to a colleague, or keep a dated backup. It's built in the
  browser, so it still works if Route Mapper's black window or Terminal has been
  closed. (On your own computer, autosave already keeps the project in
  `route_projects/`; see below.)
Curved blocks export as their smooth line (densely spaced points) in the GIS layers,
GeoJSON, SVG, PDF and viewer.

- **Inventory IDs (CSV)** — one row per block face and lot with ID, route, street,
  length, spaces, estimated minutes, flag state, note and label coordinates. This is
  the one that seeds Survey123. A lot's minutes are its suggested (or typed)
  allowance. The columns haven't changed.
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
  Text, and a short README with the import steps. The lots layer now has
  `EST_MINUTES` too. Block faces and lots come in with a
  Text `INV_ID`, ready for the one-to-many join in the occupancy workflow, so you don't
  have to draw them again in ArcGIS. Field names match the CSV (`INV_ID`, `ROUTE`,
  `ZONE_PREFIX`, `WALK_ORDER`, `STREET`, `SPACES`…).
- **GeoJSON, one file** — everything in a single file, for QGIS and other tools that
  accept mixed geometry.
- **Shareable viewer (HTML)** — one self-contained, read-only file with the routes
  built in. Anyone can open it by double-clicking. No Python or install is needed, only
  internet for the map tiles.
The **map behind** option and the basemap picker next to the Export menu control
whether SVG and PNG exports have map tiles drawn behind the routes.

---

## Saving, autosave and closing the tab

Projects save as JSON into `route_projects/` and reopen from the **Open…** menu.

**Autosave** is on by default (the tick box beside **Save**). A moment after each
change, the project is written to `route_projects/<study area name>.json`, the same
file **Save** writes. The bottom-right corner says where things stand: "Saving…",
"Autosaved to Fruitvale 2027.json at 2:14 PM", or what went wrong. Untick it to save
only when you click **Save**; the choice is remembered in that browser.

Autosave never writes over a different project. If you rename a study area, or open
a file with **Import file…**, and a saved project already has that name, autosave
stops and the corner says so in red. Rename the study area, or click **Save**, which
asks before replacing the other one.

**Closing or reloading the tab with changes that aren't saved yet** brings up the
browser's "Leave site?" box. Browsers don't let a page change that box's wording.
Choose **Cancel** to stay. If you leave anyway with autosave on, Route Mapper makes one
last save on the way out. **New**, **Open…** and **Import file…** also save first
(autosave on) or ask before discarding unsaved changes (autosave off).

The project also lives in Route Mapper's memory while the black window or Terminal
stays open, so a closed tab reopens where you left off at
`http://127.0.0.1:8765/`. Closing that window stops Route Mapper; anything not saved
by then is lost.

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
curving street needs its curve: Shift-click once or twice along it, so the block keeps
one ID and its length follows the curve. On a 90° bend of a 150 ft radius, one
Shift-click gets the length within about 1%; straight lines between the same three
clicks come out about 2% short.
