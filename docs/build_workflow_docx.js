// Regenerates docs/Route_Mapper_Workflow_v1.docx. Not part of the tool (which stays
// standard-library Python); only needed when the workflow guide changes:
//   npm install docx && node docs/build_workflow_docx.js docs/Route_Mapper_Workflow_v1.docx
// Builds the Route Mapper workflow guide in the style of the Occupancy Rate
// ArcGIS workflow (v5): Calibri, navy headings, navy-header tables with banded
// rows, per-step numbered lists, NOTE/TIP callouts and "Back to overview" links.
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType,
  ShadingType, BorderStyle, AlignmentType, HeadingLevel, LevelFormat, TableLayoutType,
  Bookmark, InternalHyperlink, ExternalHyperlink,
} = require("docx");

const OUT = process.argv[2];
const FONT = "Calibri", NAVY = "0F4761", BAND = "F3F6FA", LINK = "0563C1", RULE = "BFBFBF";

// Inline markup: **bold**, *italic* (used for on-screen names, as the v5 guide
// does) and `code` (commands and file names).
function runs(text, base = {}) {
  return text.split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/).filter(Boolean).map(t => {
    if (t.startsWith("**")) return new TextRun({ text: t.slice(2, -2), font: FONT, bold: true, ...base });
    if (t.startsWith("*")) return new TextRun({ text: t.slice(1, -1), font: FONT, italics: true, ...base });
    if (t.startsWith("`")) return new TextRun({ text: t.slice(1, -1), font: "Consolas", size: 21, ...base });
    return new TextRun({ text: t, font: FONT, ...base });
  });
}
const para = (text, o = {}) => new Paragraph({ children: runs(text, o.run), ...o.p });
const label = (text, id) => new Paragraph({
  spacing: { before: 240, after: 120 }, keepNext: true,
  children: id ? [new Bookmark({ id, children: [new TextRun({ text, font: FONT, bold: true })] })]
               : [new TextRun({ text, font: FONT, bold: true })],
});
const note = (kind, text) => new Paragraph({ spacing: { after: 160 },
  children: [new TextRun({ text: kind + ": ", font: FONT, bold: true }), ...runs(text)] });
const back = (anchor = "overview", text = "↑ Back to overview") => new Paragraph({ spacing: { after: 120 },
  children: [new InternalHyperlink({ anchor, children: [new TextRun({ text, font: FONT, color: LINK, underline: {} })] })] });
const learn = (text, url) => new Paragraph({ spacing: { after: 160 }, children: [
  new TextRun({ text: "Learn more: ", font: FONT }),
  new ExternalHyperlink({ link: url, children: [new TextRun({ text, font: FONT, color: LINK, underline: {} })] })] });
const h1 = (text, id) => new Paragraph({ heading: HeadingLevel.HEADING_1,
  children: [new Bookmark({ id, children: [new TextRun({ text, font: FONT })] })] });
const h2 = text => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun({ text, font: FONT })] });
const h3 = (text, id) => new Paragraph({ heading: HeadingLevel.HEADING_3,
  children: [new Bookmark({ id, children: [new TextRun({ text, font: FONT })] })] });
let listNo = 0;
// One numbered list per step; each restarts at 1 like the v5 guide's steps.
function steps(items) {
  const instance = ++listNo;
  const out = [];
  for (const it of items) {
    if (Array.isArray(it)) it.forEach(sub => out.push(new Paragraph({ numbering: { reference: "steps", level: 1, instance },
      spacing: { after: 80 }, children: runs(sub) })));
    else out.push(new Paragraph({ numbering: { reference: "steps", level: 0, instance }, spacing: { after: 120 }, children: runs(it) }));
  }
  return out;
}
const bullets = items => items.map(t => new Paragraph({ numbering: { reference: "bullets", level: 0 },
  spacing: { after: 60 }, children: runs(t) }));

const border = { style: BorderStyle.SINGLE, size: 4, color: RULE };
function table(widths, header, rows, links = {}) {
  const cell = (content, w, fill, head) => new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: fill ? { type: ShadingType.CLEAR, color: "auto", fill } : undefined,
    children: [new Paragraph({ spacing: { after: 0 }, children:
      typeof content === "string"
        ? runs(content, head ? { bold: true, color: "FFFFFF", size: 20 } : { size: 20 })
        : content })],
  });
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA }, columnWidths: widths,
    layout: TableLayoutType.FIXED, margins: { top: 60, bottom: 60, left: 100, right: 100 },
    borders: { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border },
    rows: [
      new TableRow({ tableHeader: true, children: header.map((h, i) => cell(h, widths[i], NAVY, true)) }),
      ...rows.map((r, ri) => new TableRow({ cantSplit: true, children: r.map((c, i) => {
        const anchor = i === 0 && links[ri];
        const content = anchor ? [new InternalHyperlink({ anchor, children:
          [new TextRun({ text: c, font: FONT, size: 20, color: LINK, underline: {} })] })] : c;
        return cell(content, widths[i], ri % 2 ? BAND : null);
      }) })),
    ],
  });
}
const gap = () => new Paragraph({ spacing: { after: 0 }, children: [] });

// ---------------------------------------------------------------- content
const body = [
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 240 },
    children: [new TextRun({ text: "Parking Count Route Planning with Route Mapper", font: FONT, bold: true, size: 36 })] }),
  para("**Time needed:** ~ 1–2 hours for a study area of three or four routes; less once you know the tool."),
  para("**Tools & Materials:**", { p: { spacing: { after: 60 } } }),
  ...bullets([
    "Route Mapper: `route_mapper.py`, and on Windows `Start Route Mapper (Windows).bat` (from the team's SharePoint folder)",
    "Python 3.10 or newer, free from python.org (Step 1 shows how to install it; nothing else is needed)",
    "A web browser (Chrome, Edge, Firefox or Safari)",
    "The study area and zone boundaries, and the parking inventory if you have it",
    "Optional: the parking meters CSV or any ArcGIS layer, to show under the routes (Step 2)",
    "ArcGIS Pro (only for bringing the routes into ArcGIS in Step 7)",
  ]),
  label("What Route Mapper does"),
  para("Route Mapper lays out parking occupancy count routes on a map before field work. You click along the streets in the order the counter will walk them, and it numbers every block face and parking lot with its INV_ID, measures each leg, and estimates the walking time. The routes then export to Survey123 (CSV), ArcGIS (GIS layers), printable plan sheets and a shareable map, so the IDs on the route map, in Survey123 and in ArcGIS all come from the same place."),
  label("Route Mapper at a glance", "overview"),
  table([2300, 4160, 2900], ["Step", "What you do", "What you end up with"], [
    ["1  Install and Start Route Mapper", "Install Python once, then double-click the launcher (Windows) or run one command (Mac)", "Route Mapper open in your browser"],
    ["2  Set Up the Study Area", "Name it, add one route per zone, check the timing, and load meters or other layers to see what to map", "A project with its routes and zone numbers"],
    ["3  Draw the Routes", "Click along the streets in walking order, counting or walking", "Block faces with INV_IDs, distances and times"],
    ["4  Add Parking Lots", "Draw each lot and drag its pin to where it is counted", "Lots numbered in walking order"],
    ["5  Check and Fix the Route", "Enter spaces and streets, insert missed blocks, flip, pin or flag", "A checked, complete route"],
    ["6  Review the Totals", "Compare route time, distance and spaces", "Balanced, field-ready routes"],
    ["7  Export", "Download the CSV, GIS layers, plan sheets or viewer", "Files for Survey123, ArcGIS and the field team"],
    ["8  Save and Share", "Save the project and upload it with the exports", "A shared project anyone can reopen"],
    ["Troubleshooting", "Fix start-up, numbering, lot and ArcGIS problems", "A working route map"],
  ], { 0: "s1", 1: "s2", 2: "s3", 3: "s4", 4: "s5", 5: "s6", 6: "s7", 7: "s8", 8: "ts" }),
  gap(),
  para("**Two habits prevent most problems:** (1) Draw in walking order and let Route Mapper number the IDs. When an ID is wrong, fix the order (Step 5) rather than typing over it. (2) Leave **autosave** ticked (it is by default). Route Mapper then saves your project a moment after every change, and warns you before you close a tab with changes that aren't saved yet (Step 8).", { run: { color: LINK } }),
  label("Key terms"),
  table([2600, 6760], ["Term", "What it means"], [
    ["Route", "One counter's walk, drawn in the order it is walked. Each route has its own tab and colour."],
    ["Zone number", "The first part of every INV_ID on a route. Zone 2 gives 2.1, 2.2, 2.3 and so on."],
    ["Block face", "One side of one block, drawn with Count block. Each gets an INV_ID."],
    ["Walking leg", "Ground the counter covers without counting, drawn with Walk only. Dashed, no INV_ID."],
    ["Parking lot", "A lot drawn as an outline. It gets an INV_ID where its pin sits on the route."],
    ["INV_ID", "The inventory ID used in Survey123 and ArcGIS. IDs never end in 0: 2.9 is followed by 2.11."],
    ["Walking order", "The order of the legs in the Blocks in walking order list. INV_IDs follow it."],
    ["Pinned ID", "An ID you typed yourself. The blocks after it continue from it."],
    ["START / END", "The markers at each end of a route. The START takes the zone's first number (x.1)."],
    ["Not surveyed", "A block with no space count yet. It is timed by its length (one space per 16 ft of curb) until you enter the count."],
    ["Bend", "A point added with *Shift*-click so a block face follows a curved street. A bend doesn't start a new block or use an INV_ID."],
  ]),
  gap(),
  para("*TIP: Keep the window that starts Route Mapper (the black Command Prompt window on Windows, Terminal on a Mac) open while you work. Hover over any button to see what it does.*"),

  // ---- STEP 1
  h1("STEP 1: Install and Start Route Mapper", "s1"), back(),
  para("*Everyone who plans routes runs their own copy of Route Mapper on their own computer. There is no shared server: each copy keeps its projects in its own folder, and you share work by passing project files around (Step 8). Python is the only thing to install, and only once.*"),
  h2("Get the files"),
  ...steps([
    "From the team's SharePoint folder, download `route_mapper.py` (and on Windows, `Start Route Mapper (Windows).bat`) into one folder on your computer, for example Documents › Route Mapper.",
    "Keep that folder somewhere ordinary such as Documents, not inside a synced SharePoint or OneDrive folder. Route Mapper saves your projects in a `route_projects` folder it creates next to the script.",
  ]),
  h2("Windows: install Python and start Route Mapper"),
  ...steps([
    "Go to python.org/downloads and click *Download Python 3.x*. Any version from 3.10 up works.",
    "Run the installer. On its first screen tick **Add python.exe to PATH**, then click *Install Now*. If your computer asks for an administrator password you don't have, ask IT to install Python 3 for you.",
    "Check it worked: press the Windows key, type `cmd` and press *Enter*. In the black window, type `py --version` and press *Enter*. It should answer with Python 3 and a version number.",
    "Start Route Mapper: double-click *Start Route Mapper (Windows)* in your folder. A black window opens, then your browser opens Route Mapper.",
    "Leave the black window open while you work. Closing it stops Route Mapper.",
  ]),
  note("NOTE", "The first time, Windows may say *Windows protected your PC*. Click *More info* > *Run anyway*. The file only runs `py route_mapper.py`. Without the .bat file you can do the same by hand: in the black window type `py ` (with a space), drag `route_mapper.py` into the window and press *Enter*."),
  h2("Mac: install Python and start Route Mapper"),
  ...steps([
    "Go to python.org/downloads, click *Download Python 3.x* for macOS, open the downloaded .pkg file and follow the installer.",
    "When it finishes, open Applications › Python 3.x and double-click *Install Certificates.command*. This lets the place search reach the internet (Troubleshooting G).",
    "Check it worked: open *Terminal* (press *Cmd + Space*, type Terminal and press *Return*), type `python3 --version` and press *Return*. It should answer with Python 3 and a version number.",
    "Start Route Mapper: in Terminal, type `python3 ` (with a space), drag `route_mapper.py` from Finder into the Terminal window, and press *Return*. Your browser opens Route Mapper.",
    "Leave the Terminal window open while you work. Closing it stops Route Mapper.",
  ]),
  note("TIP", "Next time, just repeat the start step. In Terminal on a Mac you can press the up arrow to bring back the last command and press *Return*."),
  note("NOTE", "Route Mapper opens at http://127.0.0.1:8765 and only listens on your own computer. If the window says *Could not bind port 8765*, Route Mapper is probably already running; see Troubleshooting A."),
  note("NOTE", "Use a full-size browser window. In a narrow window the map column disappears (Troubleshooting C)."),

  // ---- STEP 2
  h1("STEP 2: Set Up the Study Area", "s2"), back(),
  ...steps([
    "Click *New* in the top bar and name the study area (for example “Fruitvale 2027”).",
    "The map opens on all of Oakland. Zoom in to the study area, or type a place in the *Find a place* box (for example “Fruitvale Ave & International Blvd, Oakland”), press *Enter* and click a result.",
    "In the *Routes* panel, give Route 1 a *Name*, set its *Zone number* and pick a *Colour*. The zone number becomes the first part of every INV_ID on that route.",
    "Click *Add route* for each other zone or counter. Each route gets a tab; click a tab to work on that route, or right-click it to hide or show it.",
    "Check the *Timing model*: *Minutes per block* and *Spaces that counts as a block* (default 2 minutes per 10 spaces), *Feet of curb per space* (default 16, so 10 spaces is about 160 ft of curb), *Lot sq ft per space* (default 325, for lot time) and *Walking pace* (default 260 ft/min, about 3 mph). Leave *Skip 10, 20, 30…* ticked.",
  ]),
  note("TIP", "INV_IDs skip numbers ending in 0 (there is no 2.10, 2.20 and so on) because a Number column in Excel drops the trailing zero and 2.10 would become 2.1. Route Mapper does this for you. Keep the box ticked unless a study area drops the convention."),
  h2("Showing meters and other ArcGIS data (optional)"),
  para("Reference layers show other data under the routes so you can see what needs mapping, such as the City's parking meters, a study-area outline drawn in ArcGIS, or zone boundaries."),
  ...steps([
    "Get the data as a file: for example the parking meters CSV, or from ArcGIS Online open the layer's item page and choose *Export Data*, then CSV, GeoJSON, KML or Shapefile, and download it.",
    "Click *Add a layer…* under *Reference layers* in the left panel, or drag the file onto the map. It loads and the map zooms to it.",
    "With *Select / pan* on, click a feature to see its details. Use *Colour by* to colour features by a field such as Zone or Area; a legend appears under the layer.",
    "Use the layer's checkbox to hide or show it, *◎* to zoom to it, and *×* to remove it.",
  ]),
  note("NOTE", "Reference layers are only for looking: they aren't part of the project, get no IDs and aren't exported. They stay in your browser on this computer until you remove them; on another computer, load them again."),
  note("NOTE", "Route Mapper reads CSV with latitude and longitude columns, GeoJSON, KML/KMZ and zipped shapefiles in WGS 1984 or Web Mercator. If a file is refused because of its coordinate system, export it again from ArcGIS in WGS 1984 or as GeoJSON (Troubleshooting K)."),

  // ---- STEP 3
  h1("STEP 3: Draw the Routes", "s3"), back(),
  para("*Draw each route in the order it will be walked. Every click ends one leg and starts the next, and INV_IDs are handed out in that order, so you never type them yourself.*"),
  ...steps([
    "Click the route's tab, then pick *Count block* (or press *C*).",
    "Click where the walk starts, then click at the end of each block face along the route. Each click ends a block face, and it gets the next INV_ID.",
    "**On a curved street**, hold *Shift* while you click to bend the block instead of ending it. Shift-click a few points around the curve, then click normally where the block face ends. It stays one block face with one INV_ID, and its length follows the curve. While you bend, *Backspace* takes back the last bend and *Enter* ends the block at the last bend.",
    "Where the counter walks without counting (crossing a street or cutting through to the next block), pick *Walk only* (*W*) and keep clicking. These legs are dashed, get no ID, and count only their walking time, not counting time. Press *C* to go back to counting.",
    "The start and end of the route are marked automatically. To put them somewhere else, use *Start point* or *End point*.",
    "Press *A* to show or hide the direction arrows. If a route runs the wrong way, click *Reverse direction* in the Routes panel; every ID renumbers to match.",
  ]),
  note("NOTE", "The START marker takes the zone's first number, so the first block face is normally x.2 (for example 2.2), and the END marker takes the number after the last block."),
  label("TIP: Drawing clean, easy-to-read routes"),
  ...bullets([
    "**Zoom in first.** Draw at street level so each click lands on the right side of the street.",
    "**One side, one leg.** Draw each block face along its own side of the street.",
    "**Click at corners, Shift-click bends.** A plain click ends a block face, so use it only where one ends. Where the street bends mid-block, *Shift*-click: a plain click there would split the block into two INV_IDs, and clicking only the two ends would cut across the curve and shorten it. A curve already split into several IDs can be joined back with *Merge with next* (Step 5.11).",
    "**Snapping** pulls new points onto nearby corners and lines so legs join up. Untick *Snap to nearby lines and corners* if it gets in the way.",
    "**Rotate the map** by right-clicking and dragging on it, as in ArcGIS Online. The compass button under the zoom buttons turns it back to north up. Exports are always north up.",
    "**Fix instead of redrawing.** Drag an ID badge to move that block face, or drag a corner handle (*P* shows or hides them); a corner moves every leg that meets there, while a selected block's blue end tabs move just that block's end (Step 5.6). *Ctrl + Z* (⌘Z on a Mac) undoes and *Ctrl + Shift + Z* redoes.",
  ]),

  // ---- STEP 4
  h1("STEP 4: Add Parking Lots", "s4"), back(),
  ...steps([
    "Pick *Parking lot* (*L*) and click the corners of the lot. Close the shape by clicking the first corner again, pressing *Enter*, or double-clicking. You can add lots while you route or afterwards.",
    "The new lot is tied to the nearest point on the route by a dotted line ending in a blue pin. The pin marks where the lot is counted, and the lot takes the INV_ID at that point.",
    "If that is the wrong place, drag the pin along the route. On the first half of a block face the lot is counted just before it; on the second half, just after. The IDs renumber when you let go.",
    "Click empty map to hide the line. Click the lot, or its row in the list, to show it again. *↑* and *↓* on the lot's row move the pin one block at a time.",
    "Open the lot's row to see its suggested time. Route Mapper counts the lot's spaces at the block-face rate: the surveyed count once you enter it, or until then the lot's area ÷ 325 sq ft. If you know the lot takes longer or shorter (a garage with several levels, say), type your own figure in *Minutes to allow*; clear the box to go back to the suggestion.",
  ]),
  note("NOTE", "Lot time is a suggestion, because the map can't see levels, aisle layout or how the lot is counted. It is included in the route's time and shown on its own line. Lots add no distance, and walking into and around them isn't measured."),
  note("NOTE", "Lots in older projects may not be tied to the route yet. Open the lot in the list and click *Attach to the route*."),

  // ---- STEP 5
  h1("STEP 5: Check and Fix the Route", "s5"), back(),
  para("*The Blocks in walking order list on the right shows every leg in the order it will be walked. Click a row to open it.*"),
  ...steps([
    "Enter the space count from the inventory in *Spaces from survey*. Until a block has a count, its row shows an estimate such as *≈31 sp est.* and it is timed by its length: one space per 16 ft of curb, at 2 minutes per 10 spaces. The estimate is never exported as a count.",
    "Type the street or cross streets, and any *Note for the field team*.",
    "**Missed a block?** Click *＋ Insert a block* at the top of the list (or press *I*, or click *＋* on a block's row). Every line on the route gets a white handle near its middle, as when editing a line in ArcGIS Online. Then:",
    [
      "Drag the white handle on the line where the missed block belongs to the corner the walk should go to. Or just click the handle to add a point where it is.",
      "The block splits in two at the new point: the second half becomes the next block and the IDs after it renumber. Drop the point on another corner to join up there.",
      "Add more points the same way to shape the route. Hold *Alt* (*Option* on a Mac) while dragging to bend a line without adding a block. Press *Esc* when you're done.",
    ],
    "To remove a leg, click *×* on its row (or select it and press *Delete*). Everything after it renumbers.",
    "To swap two blocks that are in the wrong order, use *↑* and *↓* on a row.",
    "To reconnect a block, click it: its two ends show as blue tabs (white for the start, filled for the end). Drag a tab to move just that end; the block keeps its number while it is separated. Drop it on another block's end and they snap together. If they were already next to each other in the walk, the numbers stay and any walking leg that only bridged the gap is removed; otherwise the block moves beside the one you joined it to and the IDs after it renumber.",
    "If a single block runs the wrong way, open its row and click *Flip direction* (or select it and press *F*). Its arrows reverse and it keeps its ID; drag its end tabs if it should now join up differently.",
    "To keep a specific ID, type it in the first field of the row; it turns blue. The blocks after it continue from it. *Back to auto ID* releases it.",
    "Flag anything to check in the field with *⚑* on the row (or the *Flag* tool). It gets a red ring on the map, the plan sheets and the viewer.",
    "If the walk ends in a parking lot or at a block face, open that row and click *Make this the END* (or *Make this the START*). It keeps its own ID instead of getting a separate marker.",
    "**A curve drawn as several blocks?** Open the first of them and click *Merge with next* (or select it and press *M*). The next block joins onto it with its shape kept, it keeps the first block's INV_ID, street and note, and the IDs after it move up by one. Repeat until the curve is one block. The two must meet end to start and both be counted blocks (or both walk-only).",
  ]),
  note("NOTE", "Inserting keeps the route one continuous line, so its arrows stay consistent. Each point added with a plain drag or click is a new block; that is how missed blocks get their IDs. *Ctrl + Z* (⌘Z on a Mac) takes back the last insert."),
  note("NOTE", "If a red banner says two blocks share an ID, two pinned IDs clash. Open one of them and click *Back to auto ID*."),

  // ---- STEP 6
  h1("STEP 6: Review the Totals", "s6"), back(),
  ...steps([
    "*This route* (left panel) shows the route time (counting, walking and lot time together), total distance, block faces, parking lots with their time, spaces (entered counts, plus an estimate such as *+ ≈410 est.* for blocks and lots not surveyed yet), the split between *Counting* time and *Walking only* time (walk-only legs count their walking time but no counting time), and the next ID.",
    "*All routes* shows the same for the whole study area, with a note on how lot time is suggested when there are lots.",
    "Compare the routes. If one counter's walk is much longer than the others, move the boundary between routes so the walks are similar.",
    "Scroll the list for rows that still say *not surveyed* and for *⚑* flags before exporting.",
  ]),

  // ---- STEP 7
  h1("STEP 7: Export", "s7"), back(),
  para("Choose an export from the *Export…* menu in the top bar. Files go to your browser's Downloads folder."),
  para("**To keep working on a project later, export the *Project file (JSON)*.** It is the only export Route Mapper can open again (with *Import file…*), so it is listed first in the menu under *To keep working later*. The other exports are one-way, for Survey123, GIS, printing and sharing.", { run: { color: LINK } }),
  table([2700, 2700, 3960], ["Export", "Use it for", "Good to know"], [
    ["Project file (JSON)", "Picking up where you left off (recommended), backups, handing over", "Everything on screen: routes, IDs, spaces, notes and settings. Reopen it with *Import file…* on any computer. Works even if Route Mapper's window was closed."],
    ["Inventory IDs (CSV)", "Seeding Survey123", "One row per INV_ID, plus each route's start and end. A lot's minutes are its suggested (or typed) time."],
    ["GIS layers for ArcGIS (.zip)", "ArcGIS Pro or ArcGIS Online", "Block faces (lines) and lots (polygons) with a Text INV_ID. See below."],
    ["Plan sheet (PDF)", "Printing route maps for the field", "Vector, ready to print. No basemap."],
    ["Vector (SVG)", "Clean-up in Illustrator or Figma", "One layer per route. Tick *map behind* to include the basemap."],
    ["Image (PNG)", "Email and slides", "Twice screen resolution. Tick *map behind* to include the basemap."],
    ["Shareable viewer (HTML)", "Sending routes to anyone", "Opens by double-clicking; needs internet for the map."],
    ["GeoJSON, one file (QGIS)", "QGIS and similar tools", "Everything in one file. Use the .zip for ArcGIS."],
  ]),
  h2("Bringing the routes into ArcGIS Pro"),
  para("The GIS layers can stand in for drawing the block faces by hand in Step 2 of the Occupancy Rate workflow."),
  ...steps([
    "Export *GIS layers for ArcGIS (.zip)* and unzip it into a folder connected in ArcGIS Pro (Occupancy Rate workflow, Step 2.2).",
    "Open *Analysis* > *Tools* and search for *JSON To Features*. For *Input JSON* choose the file ending `_block_faces.geojson`; for *Output Feature Class* use your project's .gdb and a clear name such as LOCATION_INV_ID. Click *Run*.",
    "If the study area has lots, run *JSON To Features* again on the file ending `_lots.geojson` (for example LOCATION_INV_ID_LOTS).",
    "Right-click each new layer > *Attribute Table*, then right-click a column heading > *Fields*, and confirm INV_ID is Text. It will be.",
    "Continue with Step 3 of the Occupancy Rate workflow (join the long-form data).",
  ]),
  note("NOTE", "Route Mapper's lines follow the walk, so neighbouring block faces meet at the corners. If you want the small gaps at corners that the Step 2 drawing tips recommend, shorten those ends with *Edit Vertices* in ArcGIS."),
  note("NOTE", "GeoJSON is always in WGS 1984, and ArcGIS draws it in the right place on any map. If you want the feature class in WGS 1984 Web Mercator (auxiliary sphere) like a hand-drawn one, run the *Project* tool."),
  note("NOTE", "If you add the CSV to ArcGIS, keep the `schema.ini` file from the zip in the same folder. It tells ArcGIS to read INV_ID as Text; without it ArcGIS reads INV_ID as a number and the join fails (Troubleshooting H)."),
  learn("JSON To Features (Esri help)", "https://pro.arcgis.com/en/pro-app/latest/tool-reference/conversion/json-to-features.htm"),

  // ---- STEP 8
  h1("STEP 8: Save and Share", "s8"), back(),
  ...steps([
    "Leave *autosave* (next to *Save*) ticked. A moment after each change the project is saved as `<study area name>.json` in the `route_projects` folder next to `route_mapper.py`, and the bottom-right corner says when (for example *Autosaved to Fruitvale 2027.json at 2:14 PM*). With autosave unticked, click *Save* yourself; the corner shows *Unsaved changes* until you do.",
    "If you close or reload the tab while changes aren't saved yet, the browser asks *Leave site?*. Choose *Cancel* to stay. If you leave anyway with autosave on, Route Mapper makes one last save on the way out.",
    "To reopen it later, start Route Mapper (Step 1) and pick the project from *Open…*. *Import file…* opens a project file saved anywhere else, and *Merge file…* adds another project's routes alongside the current ones.",
    "To share with colleagues, who each run their own copy, or to carry on from another computer: upload the project's .json (*Export…* > *Project file (JSON)*, or the file in the `route_projects` folder) and your exports to the neighbourhood's Maps folder on SharePoint. They download it and open it with *Import file…*; it then autosaves into their own *Open…* list.",
    "To combine routes drawn by different people, open your project and use *Merge file…* on theirs. Zone numbers that clash are bumped so the IDs stay unique.",
    "Anyone without Route Mapper can double-click the shareable viewer (*Export…* > *Shareable viewer (HTML)*).",
    "When you are done, close the browser tab, then close the black window (Windows) or press *Ctrl + C* in Terminal (Mac) to stop Route Mapper.",
  ]),
  note("NOTE", "Each copy saves projects only on its own computer. Autosave never writes over a different saved project: if one already has this study area's name, the corner turns red and says so (Troubleshooting M). If you close the black window or Terminal, Route Mapper stops, and anything not saved by then is lost."),
  note("NOTE", "Two people editing the same project at once are each editing their own copy. Agree who owns which routes, then bring them together with *Merge file…*."),

  // ---- TROUBLESHOOTING
  h1("TROUBLESHOOTING", "ts"), back(),
  para("Problems that come up most often, what causes them, and how to fix them. Click a problem to jump to it."),
  table([5800, 3560], ["Problem", "Where it shows up"], [
    ["A. The browser doesn't open, or “Could not bind port”", "Step 1"],
    ["B. “python3: command not found” or “not recognized”", "Step 1"],
    ["C. The map is missing; only the side panels show", "Any time"],
    ["D. IDs changed after an edit", "Steps 3–5"],
    ["E. A parking lot has the wrong INV_ID", "Step 4"],
    ["F. A red banner says two blocks share an ID", "Step 5"],
    ["G. Find a place shows no results", "Step 2"],
    ["H. ArcGIS says “No join was possible”", "Step 7"],
    ["I. My latest changes are gone after reopening", "Step 8"],
    ["J. An inserted block is in the wrong place or runs the wrong way", "Step 5"],
    ["K. A reference layer won't load, or lands in the wrong place", "Step 2"],
    ["L. A curved street has several INV_IDs", "Step 3"],
    ["M. The corner says “Not autosaved”", "Step 8"],
  ], { 0: "ta", 1: "tb", 2: "tc", 3: "td", 4: "te", 5: "tf", 6: "tg", 7: "th", 8: "ti", 9: "tj", 10: "tk", 11: "tl", 12: "tm" }),
];

const trouble = [
  ["ta", "A. The browser doesn't open, or “Could not bind port”",
    "The browser doesn't open, or the Terminal shows *Could not bind port 8765*.",
    "Route Mapper is already running in another Terminal window, or another program is using port 8765.",
    ["Look for an open Route Mapper tab or Terminal window and use that one.",
     "Or start a second copy on another port: `ROUTE_MAPPER_PORT=8790 python3 route_mapper.py` on a Mac; on Windows run `set ROUTE_MAPPER_PORT=8790` first, then `py route_mapper.py`.",
     "If the browser still doesn't open by itself, go to http://127.0.0.1:8765 (or the port you chose)."]],
  ["tb", "B. “python3: command not found” or “not recognized”",
    "The Terminal says *python3: command not found* (Mac) or *'python3' is not recognized* (Windows).",
    "Python 3 isn't installed, or Windows uses a different command.",
    ["On Windows, use `py route_mapper.py` (or the *Start Route Mapper (Windows)* launcher), not `python3`.",
     "If `py` isn't found either, run the Python installer again and tick *Add python.exe to PATH* (Step 1).",
     "On a Mac, install Python from python.org (Step 1). If instead a box offers to install the command line developer tools, accepting that also provides Python 3.",
     "Open a new Command Prompt or Terminal window and try again."]],
  ["tc", "C. The map is missing; only the side panels show",
    "The left and right panels fill the window and there is no map.",
    "The window is too narrow for the three columns.",
    ["Make the browser window full screen, or wider."]],
  ["td", "D. IDs changed after an edit",
    "Block INV_IDs changed after you added, deleted or moved something.",
    "This is expected. INV_IDs follow the walking order, so everything after an edit renumbers.",
    ["Check the order in the *Blocks in walking order* list.",
     "If one block must keep a specific ID, type it into the row's first field to pin it (Step 5.8).",
     "Remember there is no x.10 or x.20: a jump from 2.9 to 2.11 is correct."]],
  ["te", "E. A parking lot has the wrong INV_ID",
    "A lot's ID is not where you expect it in the sequence.",
    "Its pin is on the wrong block face, or on the wrong half of it.",
    ["Click the lot to show its line, then drag the pin: on the first half of a block face the lot comes before it, on the second half after it.",
     "Or use *↑* and *↓* on the lot's row to move the pin one block at a time."]],
  ["tf", "F. A red banner says two blocks share an ID",
    "A red banner says, for example, *Two blocks share 2.14*, and those rows are outlined in red.",
    "A pinned ID matches another block's ID, on this route or another one.",
    ["Open one of the rows and click *Back to auto ID*, or type a different ID."]],
  ["tg", "G. Find a place shows no results",
    "Typing a place in *Find a place* shows nothing, or the bottom of the window says *Place search unavailable*.",
    "The search only runs when you press Enter, and it needs an internet connection (it uses OpenStreetMap). On a Mac with Python from python.org, Python's security certificates may not be installed yet.",
    ["Press *Enter* after typing.", "Check your internet connection.",
     "On a Mac: open Applications › Python 3.x, double-click *Install Certificates.command*, then stop and restart Route Mapper.",
     "Try a simpler search, such as an intersection and the city."]],
  ["th", "H. ArcGIS says “No join was possible”",
    "After bringing Route Mapper files into ArcGIS, *Validate Join* ends with *No join was possible*.",
    "INV_ID is a number on one side of the join. The GeoJSON layers come in with a Text INV_ID, but a CSV added without its schema.ini comes in as a number.",
    ["Use the block face and lot layers made with *JSON To Features* from the zip (Step 7). Their INV_ID is Text.",
     "If you used the CSV, put `schema.ini` from the zip next to it, remove the table from the map and add it again.",
     "If the join still fails, follow Troubleshooting B in the Occupancy Rate workflow."]],
  ["ti", "I. My latest changes are gone after reopening",
    "A project opens without your latest changes, or the *Open…* list is empty.",
    "Autosave was unticked, or stopped with a red message in the corner, and the changes weren't saved before Route Mapper closed. Or `route_mapper.py` moved: projects are saved next to it, so it now looks in a new, empty `route_projects` folder.",
    ["Keep *autosave* ticked, and check the bottom-right corner says *Autosaved* before closing Route Mapper. If it is red, see Troubleshooting M.",
     "If you moved `route_mapper.py`, move the `route_projects` folder with it, or use *Import file…* to open the .json from its old place."]],
  ["tj", "J. An inserted block is in the wrong place or runs the wrong way",
    "A block you inserted has an unexpected ID, the line goes somewhere you didn't mean, or a block's arrows point against the walk.",
    "The handle you used was on a different block, the new point snapped onto a nearby corner, or the block was drawn the other way round.",
    ["Press *Ctrl + Z* (⌘Z on a Mac) to take back the last insert, then use the handle on the right line.",
     "Or drag the new point to where it should be. If snapping keeps pulling it to the wrong corner, untick *Snap to nearby lines and corners* while you place it.",
     "If one block runs the wrong way, open its row and click *Flip direction* (Step 5.7)."]],
  ["tk", "K. A reference layer won't load, or lands in the wrong place",
    "Loading a file says it could not be read, or its features appear far from Oakland.",
    "The CSV has no latitude and longitude columns, or the file uses a coordinate system Route Mapper can't read (for example State Plane), or it isn't one of the formats Route Mapper reads.",
    ["For a CSV, check it has columns named Latitude and Longitude (or Lat / Lon, or a WKT geometry column).",
     "From ArcGIS, export the layer again as GeoJSON, or as a shapefile or CSV in WGS 1984, and load that.",
     "Use CSV, GeoJSON, KML/KMZ or a zipped shapefile; other formats (such as a file geodatabase) won't load."]],
  ["tl", "L. A curved street has several INV_IDs",
    "One block face on a curving street shows up as several short blocks, each with its own INV_ID.",
    "Every plain click ends a block face, so clicking around a bend split the block at each click.",
    ["Open the first of those blocks in the list and click *Merge with next* (or select it and press *M*). Repeat until the curve is one block; it keeps the first INV_ID and its shape (Step 5.11).",
     "Next time, hold *Shift* while you click around a bend (Step 3.3)."]],
  ["tm", "M. The corner says “Not autosaved”",
    "The bottom-right corner is red and says *Not autosaved: another saved project is called …*.",
    "A different project in the `route_projects` folder already has this study area's name, usually after renaming a study area or opening a file with *Import file…*. Autosave won't write over it.",
    ["If the saved one is an older copy of this same project, click *Save* and choose *OK* to replace it. Autosave carries on from there.",
     "Otherwise rename this study area (the name box in the top bar). Autosave saves it under the new name."]],
];
for (const [id, title, see, why, fix] of trouble) {
  body.push(h3(title, id));
  body.push(note("What you see", see));
  body.push(note("Why it happens", why));
  body.push(new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: "How to fix it:", font: FONT, bold: true })] }));
  body.push(...steps(fix));
  body.push(back("ts", "↑ Back to troubleshooting list"));
}

const doc = new Document({
  creator: "Route Mapper",
  title: "Parking Count Route Planning with Route Mapper",
  styles: {
    default: {
      document: { run: { font: FONT, size: 24 }, paragraph: { spacing: { after: 160, line: 278 } } },
      heading1: { run: { font: FONT, size: 40, color: NAVY }, paragraph: { spacing: { before: 360, after: 80 }, keepNext: true } },
      heading2: { run: { font: FONT, size: 32, color: NAVY }, paragraph: { spacing: { before: 240, after: 80 }, keepNext: true } },
      heading3: { run: { font: FONT, size: 28, color: NAVY }, paragraph: { spacing: { before: 240, after: 80 }, keepNext: true } },
    },
  },
  numbering: { config: [
    { reference: "steps", levels: [
      { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.START,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      { level: 1, format: LevelFormat.LOWER_LETTER, text: "%2.", alignment: AlignmentType.START,
        style: { paragraph: { indent: { left: 1440, hanging: 360 } } } } ] },
    { reference: "bullets", levels: [
      { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.START,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } } } ] },
  ] },
  sections: [{ properties: { page: { size: { width: 12240, height: 15840 },
    margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } }, children: body }],
});
Packer.toBuffer(doc).then(buf => { fs.writeFileSync(OUT, buf); console.log("wrote", OUT, buf.length, "bytes"); });
