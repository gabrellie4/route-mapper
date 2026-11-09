# Route Mapper

A tool for laying out parking occupancy count routes and generating inventory IDs,
distances and walk-time estimates before the field work starts.

```
python3 route_mapper.py
```

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

Each lot is tied to the route by a dotted line from its ID to an amber pin on the
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
- **Missed blocks?** Click **＋** on a block's row (or **＋ Add missed blocks** at the
  top of the list), then draw the missed blocks:
  1. Click on the route where they branch off. A corner is best; a point partway along
     a block cuts that block in two.
  2. Click at the end of each missed block face. `C` and `W` switch between counting
     and walking, and `Backspace` takes back the last point.
  3. Click back on the route where the walk rejoins. That finishes it. You can also
     press **Done**.

  Nothing in the route changes while you draw. When you finish, the blocks are stitched
  into the walk where your path touched the route, numbered in walking order, and
  everything after renumbers. It's one step to undo.
  - **Back to where you started:** the blocks form a loop at that point. Clicking within
    about 15 m counts as the same spot.
  - **Rejoining further along:** what happens depends on the stretch you went around.
    If it was only walking, the new blocks replace it. If it had counted blocks (for
    example the frontage of a block you looped round), it's kept. The new blocks are
    then walked back from its far end, and a dashed walking leg follows the street on
    to where the walk continues. Nothing is dropped, and no leg points against the walk.
  - **Ending away from the route:** the new blocks are a spur. A walking leg takes the
    walk back to where it branched off.
  - **Touching the route at neither end:** the new blocks go after the block whose ＋
    you clicked, with walking legs out and back.

  Untick **join up** to leave out those walking legs. **Discard** throws the drawing
  away. You can also select a joint on the map and press `I` to start from exactly
  there. ↑/↓ still move a single block (or a lot's pin) one place, which is handy for
  fixing two blocks in the wrong order.
- **Make this the END** / **Make this the START** lets a block face or lot stand in as
  the route's end or start. It keeps its own ID instead of getting a separate marker.
  Handy when the walk finishes in a parking lot.
- **Reverse direction** in the Routes panel flips the walking order and renumbers
  every ID to match.
- Right-click a route tab to hide or show that route.

Keys: `C` count · `W` walk · `L` lot · `A` direction arrows · `I` add blocks at the
selected joint · `P` corner handles · `Esc` select (also finishes adding blocks) ·
`⌘Z` undo · `⇧⌘Z` redo · `Delete` removes the selected block.

---

## Editing IDs

Two things you asked for, and they interact:

- **Individually** — click a block, type an ID in the first field. It's pinned, shows
  amber, and everything after it continues from that number. Pin `2.14` and the next
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
change it once and every route re-estimates. Connector legs are distance ÷ walking
pace, default 260 ft/min (roughly 3 mph).

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
