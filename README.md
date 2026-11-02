# VVS Mängdning – drawing-adaptive automatic pipe takeoff for clean vector VVS PDFs

**Aktuell verifieringsstatus:** [Tester, kända bortfall och leveransens omfattning](docs/VERIFIERINGSSTATUS-2026-09-21.md). Systemet är inte verifierat för obevakad mängdning.

**Lokal leverans:** [Börja här på svenska](START_HAR.md) · [Ändringar, verifiering och kvarvarande begränsningar](docs/LEVERANS-2026-09-20.md).

One generic engine discovers, per drawing, how the PDF represents layers, text (searchable or open-stroke
CAD glyphs), VVS designations, DN, leaders, markers, pipes, topology and scale, then reconstructs
PhysicalPipes with a full evidence chain (RAW PDF OBJECT → family → glyph/text → designation → DN → actual
leader → endpoint → pipe attachment → topology → physical pipe → scale → measurement).

```
engine/          Python engine (vvs_engine) + CLI + tests
backend/         FastAPI application (auth, projects, drawings, background analysis jobs, exports)
frontend/        React + TypeScript (Vite) web UI, Swedish or English, with a PDF.js viewer
docker/          Dockerfiles + nginx config; docker-compose.yml at the root
results/         Local analysis output (excluded from GitHub)
data/            Supply your own drawings locally (excluded from GitHub)
```

**Hur allt fungerar, vilka AI-modeller som används och exakt hur, säkerhet, drift och validering: se [`docs/SYSTEMET.md`](docs/SYSTEMET.md).**

## Install and run locally

```bash
# engine
pip install -e engine          # pymupdf, shapely, numpy, scipy
vvs-takeoff analyze /path/to/drawing.pdf --out results/example --name example
vvs-takeoff why /path/to/drawing.pdf <physical_pipe_id>

# tests (engine + API)
cd engine && python -m pytest -q tests

# backend (SQLite by default; see .env.example for PostgreSQL)
cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000

# frontend
cd frontend && npm install && npm run dev      # http://localhost:5173 (proxies /api to :8000)
```

## Complete application with Docker

```bash
cp .env.example .env            # optional
docker compose up --build       # web UI on http://localhost:8080, API on http://localhost:8000, PostgreSQL inside
```

Workflow: skapa projekt → ladda upp VVS-PDF → analysera → följ förloppet → inspektera ritning och mängder →
Ej lösta → rätta, markera och mät själv (fliken Markera) → fråga agenten → ladda ner markerad PDF / Excel / CSV /
JSON / analysrapport. Ett helt projekt: Analysera projektet → handlingsförteckning, versioner, ändringar, mängder
per hus och projektagenten.

## Deploy as one container (Railway, any Docker host)

The root `Dockerfile` builds the frontend, installs the engine and the API and serves the built frontend from
FastAPI (`railway.json` points Railway at it). Environment: `PORT` (honoured), `VVS_SECRET_KEY`, optionally
`VVS_DATABASE_URL` (PostgreSQL) and a volume at `/data` for uploads and the SQLite database.

`VVS_SECRET_KEY` signs every sign-in token, and the default sits in the source. A service running on it signs
tokens with a string anyone who has read the repository can read too - and anyone can then write their own token
for any account, the admin account included. Nothing downstream would notice: sign-in, ownership and the admin
gate all look only at the signature. So the service **refuses to start** on a deployment platform while that
default stands, rather than warning into a log nobody reads. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## What a vector PDF actually contains

Worth being precise about, because it decides what is readable and what must be recognised. Drawing A's content
stream holds 74 976 `l`, 33 081 `m`, 18 459 `S` and 7 649 `c` operators - and 17 text-show operators, 88
characters in total: the grid bubbles (A2, A00, A100, A200, 00, 2, 70, 140, 210) and the revision table. Not one
pipe designation. W-50-1-A-0014 has four text operators, 27 characters.

The CAD export exploded every label into line geometry. `S1-P2-110` is not the string "S1-P2-110" anywhere in the
file; it is a few dozen `m`/`l` pairs. There is no character code to read, so each character has to be recognised
from the shape its strokes make - which is what the engine does, and where an unnamed '?' comes from.

The drawing does embed the typeface it drew with (ISOCPEUR), and those glyph shapes join the reference alphabet as
the drawing's own evidence. The embedded copy is subset to the characters its remaining real text uses, though -
16 of them on drawing A, none of which are the S, R, V or K a designation needs - so it helps less than it sounds.

## Vector PDFs only

The engine reads the drawing's own vector content: every path operator with its segments, layer, stroke width and
colour, plus the text objects and the stroke-font glyph outlines. Nothing is inferred from a rendered image, and
there is no OCR anywhere in the pipeline.

Every page is therefore classified first (`vvs_engine/pdf/classify.py`) from vector paths and text characters
against embedded images and their page coverage. A vector page is analysed; a scanned or image-only page is
skipped and reported, and a PDF with no vector page at all is rejected with `UnsupportedInputError` rather than
measured from pixels. In the web application that becomes a plain message: export the drawing as a vector PDF
from CAD instead of scanning it.

## Quantities: horizontal, hatched areas, risers

* Horizontal metres are measured from the pipe geometry outside hatched areas; pipe running through hatched areas
  (wall sections, adjacent sheet parts) is measured too and reported separately as "varav i skrafferat område".
  It stays out of the total unless the checkbox "Räkna med skrafferade ytor" is ticked (`?include_hatched=true` on
  Excel/CSV export). Measuring the reference markup of drawing A shows the takeoff excludes it: 0.25 m of 213.4 m.
* Two label forms mean two different things: a dimension inline ("S3-R8-75") names the horizontal run, a
  dimension on the row below ("S3-R8" over "75") names the vertical pipe at that point. A count prefix is the
  exception - "2xKV1-X31" over "16" bundles parallel pipes along the run, and the reference takeoff of drawing A
  gives that label no vertical metres. Both riser sources are reported per row and the operator picks which one
  the vertical quantity uses. Labels are the default: on W-50-1-A-0012 they give exactly the reference's 2 + 2
  where the drawn symbols give 1 + 0, and on drawing A the two are equally far off overall (deviation 20 against
  21 across the nine identities) while the labels match four identities exactly that the symbols get wrong.
* Risers are counted from the drawn riser marks per designation. The drawing carries no floor height, so vertical
  metres are computed only when the user enters a floor height (Mängder tab; `?floor_height=` on Excel/CSV export).
* DN changes are placed at drawn tick marks; labels pointing at a riser mark describe the riser, not the run.
* Dashed runs are chained across the line style's own gap, including where the run turns a corner inside a gap:
  the two free ends' outward rays must meet one gap away, which is the drawing's own evidence for the bend.
* A branch with no size label of its own, off a junction where every labelled arm carries the same identity, is
  that identity: a size change is always drawn with its own label. A junction with two competing identities leaves
  the branch AMBIGUOUS rather than guessing.
* Every designation gets its own colour, the same one in the viewer and in the exported marked PDF. No colour is
  dark enough to be mistaken for the drawing's own black line work.

## Scale

The scale is read from the drawing and never assumed. A printed ratio ("1:50") is exact; a drawn scale bar is
measured from the bar's own extent rather than from its label glyph centres, which sit a fraction of a character
off the graduations (that error was 0.95 % on these drawings). When a sheet states one ratio per print format
("SKALA A1 (A3)" over "1:50 (1:100)"), the k-th format belongs to the k-th ratio and the sheet's own size selects
the one that applies. Two ratios with nothing to separate them stay a CONFLICT and no scale is assumed.

Ratios tied to a format in the stamp ("1:50 (A1) 1:100 (A3)") are chosen by the sheet's own format, found from its
size in points (A0-A4, either orientation). A ratio stated for another format applies rescaled by the format step
(1:100 for A3 is 1:50 on an A1 sheet); format-tied ratios that disagree after rescaling are a CONFLICT, and bare
ratios elsewhere on such a sheet (a fall written 1:100) are not the stamp.

**Verified means measured.** A scale is `VERIFIED` only when an independent measurement on the sheet - a scale bar
read with its numbers, or dimension chains - confirms the stated ratio within 1 %. A bar whose numbers cannot be
read gives points, not metres, and neither chooses nor confirms a ratio: on the Årjäng plans a hatched EI30 wall was
taken for such a bar and "confirmed" 1:100 on A1 sheets drawn in 1:50, doubling every metre. Hatching - many
parallel strokes of one length, closely and evenly spaced, with no numbering - is never a scale-bar candidate, even
with figures standing beside it; `hatch_bands()` finds it on the sheet.

The same plans carry the same labelled bar (0-1-2-5 METER) as the schematic, and it was missed for two reasons in
the text reading, both fixed: a lone glyph was claimed by whichever text size was commonest on the sheet, so the
bowl of the 5 became a "character" of the 4.5 pt legend text and the 5 was read as two '?' (lone glyphs are now
taken after every size has had its turn, the one holding most strokes first); and a lone '0' was lent the 10 degree
direction of nearby text, so the bar's labels did not line up (a one-character label is no longer held to its lent
angle; the bar's axis is tried along each label beside it). A scale that is only
stated (`TEXT_ONLY`) or only measured one way (`BAR_ONLY`, `DIMENSIONS_ONLY`) is still used, but every metre measured
under it carries the row state `SCALE_UNVERIFIED` instead of `CONFIRMED`.

Local regression against real sheets (client material, never committed): list them in
`data/local_facit/scale.json` (or `VVS_LOCAL_SCALE_FACIT`) and `tests/test_the_scale_is_the_one_for_the_sheet_format.py`
reads each one and checks ratio and state.

## Review agents

Every analysis is checked afterwards by agents that do not trust it (`vvs_engine/review/`). They report findings
with a severity, a place to look and the numbers behind the verdict; none of them may change a measurement, since
a review that could edit the result would hide the disagreement it exists to surface.

* **scale** - is there a scale, does a bar confirm it, is the ratio a plausible one
* **coverage** - how much of the accepted pipe geometry actually ended up owned by a designation
* **plausibility** - sizes against the nominal series, lengths against the drawing's own extent, rows without DN
* **topology** - free pipe ends and runs left between two possible designations
* **designations** - labels with a dimension that never reached a pipe, unreadable characters
* **ocr_crosscheck** - the page is rendered and read with OCR as an independent second opinion. It never feeds the
  measurement: it only asks whether OCR sees a designation, in the drawing's own label pattern, where the vector
  reading has no text at all. On drawings A and W-50-1-A-0014 it finds none, which is the confirmation that the
  vector reading missed nothing. Needs the `review` extra (`pip install -e engine[review]`); without it the agent
  reports that the cross-check did not run. Disable with `VVS_REVIEW_OCR=false`, the whole layer with
  `VVS_RUN_REVIEW=false`.

The findings are written to `review-findings.json` and shown in the application's "Granskning" tab.

### A second reader, where the drawing itself leaves a choice

A language model (`VVS_SECOND_READER_MODEL`, default `gpt-6-astra`) may answer the cases the geometry has already
declared AMBIGUOUS - and only those. It follows the key: on where `OPENAI_API_KEY` is present, off where it is
not, since a service has exactly one use for that key. `VVS_SECOND_READER=true` forces it on for a machine behind
a proxy that attaches the credential and holds no key itself; `false` forces it off. `GET /api/version` says which
of those applies, in words, without logging in. What it may answer is bounded by the drawing: a
question carries the families this leader's own end landed on, and `verify()` refuses, character for character,
anything that is not one of them, so it cannot name a pipe, a leader, a DN, a coordinate or a metre the reading
did not already put forward. An answer naming two candidates stays ambiguous. Every settled case records that a
model chose it and from which candidates.

The takeoff does not depend on it: with no transport nothing is asked, the engine runs with no network, and the
result is the same. A job that did consult it says so - the result carries
`second_reader: {consulted, asked, settled, refused}` and reports determinism as
`NOT_APPLICABLE_A_SECOND_READER_WAS_CONSULTED`, because a reading that asked another machine is not the same kind
of answer as one that did not.

Measured on `S3_25.pdf`, the sheet in the corpus with real multi-family ambiguity: 2 cases asked, 2 answered
"OKLART", 0 settled, 42.399 m either way. That is the useful shape of the thing - it declined to invent a
discriminator that was not there.

`POST /api/jobs/{id}/vision` is the other model path: a look at the rendered page and at the overlay, asked for
rather than automatic, which reports what the vector reading seems to have missed. It cannot write: there is no
`apply()`, a finding carries no number, and no finding connects to a quantity.

It does not stop at the observation. Both pictures carry a named grid, and a finding must name one of those tiles;
a name that is not on the list is dropped. Every named tile is then read out of the vectors - which stroke family
the ink belongs to, what the reading made of it, which labels sit there and whether their leaders reached anything
- and the finding carries that account and a sentence naming the reason there are no metres there. The eye says
where to look; the vectors say why. On the reference drawing that turns "the pipes in the section detail have no
overlay" into "197 m on layer `K-------EDN`, and no designation on the sheet points at it", and it catches the
eye's own false alarms: one finding landed on a tile holding 45 m of measured pipe.

### Resolving what the vector reader could not name

Where a glyph's shape matches no reference letter the row keeps a '?', and everything built on it - the
designation, its dimension, the pipe it labels - is lost. An opt-in pass (`VVS_OCR_ASSIST`, on by default when the
`review` extra is installed) renders the page, reads it in overlapping tiles and fills in those positions, but only
where an OCR word lines up character for character with a vector-read word and agrees everywhere both readings are
sure. Every adopted character is written to `ocr-assisted-characters.json` with its confidence and position.

It resolves what OCR can actually see and no more: on drawing A it named 18 of 56 unreadable characters, on
drawings C and W-50-1-A-0014 none, because OCR finds no text at those positions either. The measured quantities on
all three are unchanged, since those characters sit in legend text and notes rather than in designations.

## The sheet's own designation list

Every one of these drawings carries a legend - a column of short codes, each with the words that say what it is,
under headings that group them. The engine reads it (`drawing-legend.json`) and uses it as the drawing's own
vocabulary.

It is found by shape, not by words: a stack of rows sharing a left edge, each a short code beside a description,
with the rows on that edge that are not codes taken as the headings above them. A code's role then comes from
how the drawing itself uses it - a code that opens a designation carrying a dimension is a system, a code that
stands alone as a whole label out on the drawing is a component tag, the rest are materials and insulation
classes. A legend that writes the varying part of a tag as placeholders matches the numbers the drawing writes.

What it decides: a label the legend defines as a component describes an object - a floor drain, a mixer - so it
never seeds pipe ownership and never votes for what pipe geometry looks like on the sheet. Read on four office
styles it names, for example, the systems KV01/KV02/VV01/VVC01/S01/SF01/VP01/VS21/VS31 of one and the components
GB1/TS2/TS3/VK2/VK3 of another, with the pipe materials of each kept apart from both.

## Validation against reference takeoffs

Four drawings have a reference takeoff (`results/validation/`). None is read by production code; the
contamination scan fails the build if the package ever imports from the validation directories.

| | reference | engine | deviation | per-designation error | reference risers | from labels | from symbols |
|---|---|---|---|---|---|---|---|
| W-50-1-A-0011 (full markup) | 213.70 m | 212.58 m | -0.5 % | 3.90 m | 55 | 38 | 58 |
| W-50-1-A-0012 (two systems) | 17.60 m | 17.38 m | -1.3 % | 0.23 m | 4 | 4 | 1 |
| W-50-1-A-0013 (eight systems) | 112.90 m | 112.44 m | -0.4 % | 9.52 m | 24 | 25 | 25 |
| W-50-1-A-0014 (run blind first) | 50.90 m | 50.16 m | -1.5 % | 2.60 m | 16 | 21 | 22 |

Sampling the reference polylines point by point shows the engine owns essentially all of the geometry they mark;
what remains sits inside hatching, or at run ends where the reference clicks a point or two past where the drawn
line stops. The per-designation error on W-50-1-A-0013 is almost entirely one size boundary on a single stack:
the reference puts the change at the smaller size's tick, and the engine carries the smaller size on to the next
junction, because that rule currently applies only where both labels sit on one chain.

W-50-1-A-0014 was run blind - the engine's numbers were written down before the reference was opened
(`results/validation/W501A0014/blind-run.md`).

## Drawing styles the engine has been read against

Beyond the reference set, three sheets in other drawing styles were used to find defects that only appear
outside one office's conventions (no reference takeoff exists for them, so they are read qualitatively):

| sheet | style | what the engine makes of it |
|---|---|---|
| V53.1-1944, Plan 10 Del 44 | no optional-content layers, real text, one dashed system | scale verified, 51 designations, 13 of 17 leaders attached, 52.68 m on one system, 1.23 m unowned |
| V53.1-1842, Plan 09 Del 42 | as above, dash-dot lines, dimension on the row below | scale verified, 44 designations, four sizes measured, 11.13 m confirmed and 15.14 m still unowned |
| W--50-1-0501112 | no layers, several systems in one plan, leaders ending in circles | scale from the printed ratio only, 49 designations, two systems measured; the pipe families are not yet separated from the sheet's other black line work |

These three drove the layer-less reading: colour as part of a line's family, a tag with a bare integer read as a
code rather than a level, an underline that may not be a border line, and every recurring gap of a line style
being bridged rather than the commonest one alone.

## Artifacts per drawing (results/<name>/)

drawing-profile.json, drawing-profile-report.md, raw-vector-inventory.json, cad-layer-map.json,
vector-designations.json, designation-overlay.pdf, leader-forensics.json, leader-family-report.json,
pipe-code-anchors.json, endpoint-pipe-attachment-overlay.pdf, pipe-representation-families.json,
pipe-geometry-inventory.json, declined-geometry.json, pipe-topology.json, physical-pipes.json, quantities.json, unresolved-issues.json,
evidence-graph.json, reconciliation.json, review-findings.json, ocr-assisted-characters.json, determinism.json, contamination-report.json, performance-report.json,
production-overlay.pdf (+ topology/ambiguous/unsupported-style overlays), analysis-report.md, freeze-manifest.json.

## Principles enforced by the engine

* Identity comes from the visible designation and its ACTUAL vector leader, never from nearest-text/pipe logic.
* DrawingProfile is derived from the PDF only, per analysis job; nothing persists between drawings.
* Ambiguity is a valid result (AMBIGUOUS_* states with machine-readable reasons); wrong certainty is not.
* Geometry conservation: raw pipe geometry = confirmed + ambiguous + unowned, no double counting (reconciliation.json).
* Every drawn line is accounted for: measured, weighed and set aside, or never pointed at by any label -
  the last two with the reason, so ink missing from the takeoff is explained rather than silent
  (declined-geometry.json, and the *Bortvald geometri* layer in the viewer).
* Determinism: original / reversed / two shuffled object orders give identical semantic results (determinism.json).
* Contamination firewall: the production package is scanned for drawing-specific literals and never imports validation data.

Fullständig källimport och kvarvarande regelskillnader: [källgranskning 20 september](docs/KALLGRANSKNING-2026-09-20.md).
