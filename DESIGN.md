# Design

PDF Scan prepares documents for sharing. Its visual language comes from preparing paper by hand: pages lie on a **green cutting mat**, proposals are **highlighter marks**, and removals are **ink bars or correction tape**. Keep new screens inside this vocabulary.

The opt-in **Watermarks** setting reuses the existing Find chips and review groups.
Object-removal proposals retain the normal highlighter/Keep/preview flow. The hint
explains that editing such a box switches to rectangle fill; do not present an object
watermark's large bounds as a filled rectangle in the result itself.

## Users and main task

Anyone who shares documents outside their circle: individuals sending CVs, agreements and forms; HR, legal and admin staff; companies and freelancers sharing drafts and reports; researchers and journalists de-identifying records; schools reusing papers. Copy and examples stay general (names, companies, invoice codes); school-specific help lives only in the optional "Exam or test paper" document type. Each one opens a PDF, finds private details, checks every proposal, and exports a clean copy. Accuracy and confidence matter more than speed, so the review surface (page, marks, list) gets most of the screen.

## Colour

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--chrome` | `#f6f7f4` | `#141816` | Panels, bar, landing |
| `--ink` | `#1a1f1c` | `#e8ece7` | Text, primary buttons |
| `--ink-2` | `#48514c` | `#b0b9b3` | Secondary text, hints (7.6:1) |
| `--ink-3` | `#5c6660` | `#87918b` | Tertiary text (≥ 5:1 on chrome) |
| `--mat` | `#2f4a40` | `#1e302a` | Page background (the cutting mat) |
| `--marker` | `#ffd83d` | same | Proposals, counts, the download card |
| `--keep` | `#1683a3` | same | Keep areas (protective film) |
| `--danger` | `#b8392a` | `#ff8a78` | Delete, errors, removed pages |

- Each found group gets a highlighter colour from a fixed set (yellow, pink, green, orange, blue, violet, teal, peach). The same colour appears on the page, in the list and as a dashed outline on the result. Colour is never the only signal: every mark also carries a letter, and every group has a text label.
- Highlights use `mix-blend-mode: multiply`, so text stays readable under them, like a real highlighter. Don't put a `clip-path` or `filter` on an ancestor of the marks: it isolates the blend and washes the text out.
- No gradients, no glass, no purple or blue accents. Shadows appear only on paper (the page, the drop sheet) and on floating notices.

## Type

- One family: **Bricolage Grotesque** (variable weight 200–800, width 75–100%), bundled in `pdfscan/web/fonts/` under the OFL. No CDN.
- Display: weight 700–760 at width 76–84% (`font-stretch`), tight leading (0.92), slightly negative tracking. Used for the landing headline, panel titles and the drop sheet.
- UI text: 14px, weight 400–650, width 100%. Hints are 12.5px. Numbers use tabular figures.
- Chinese falls back to PingFang TC, Noto Sans TC or Microsoft JhengHei. Chinese headlines use `word-break: keep-all` so words don't split.
- Sentence case everywhere. No all-caps labels, no letter-spaced eyebrows.

## Layout

```
┌ bar: mark · document · Find ─ Review ─ Export · ? 中文 ◐ delete ┐
├────────┬─────────────────────────────────────┬────────────────┤
│ pages  │ tools          views                │ tabs           │
│ rail   │ ░░░░ mat ░░░░░░░░░░░░░░░░░░░░░░░░░░ │ Find / Review /│
│ 112px  │ ░░░  ┌ page ┐  ░░░░░░░░░░░░░░░░░░░ │ Export panel   │
│        │ ░░░  └──────┘  ░░░░░░░░░░░░░░░░░░░ │ (384px)        │
│        │ undo · page n/N · next found · zoom │ sticky action  │
└────────┴─────────────────────────────────────┴────────────────┘
```

- Landing: one viewport. Headline and a live specimen on the left; the drop sheet lies on the mat on the right; three plain facts below. No cards.
- Under 900px everything stacks: the page rail becomes a horizontal strip, then the mat (72% of the viewport height), then the panel with a sticky action button.
- Radii follow hierarchy: paper 2–3px, inputs and buttons 10px, chips and small actions are pills, the segmented controls are 10/7px.
- Spacing steps: 4, 8, 12, 14, 18, 22, 24, 32, 48.

## Interaction and motion

- One orchestrated moment on the landing page: highlighter sweeps across the private details in the specimen, then ink bars cover them.
- One reveal after a preview is built: the Compare divider crosses the page once, then rests at the middle.
- Otherwise motion only answers an action: marks "stamp" in after Find, the drop sheet straightens and lifts on hover, the tab underline slides, buttons press in by 2%.
- `prefers-reduced-motion` turns all of this off; end states are shown directly.
- Selecting a box (click it, or its name in the list) lays an ink frame with white grips over the mark; small boxes keep their middle free by moving the corner grips just outside. While dragging, a dark pill under the box reads the size in millimetres and how many boxes follow. Edits never replay the stamp animation. Scope is a segmented control in the editor: "This box" or "All n in the group".
- Several PDFs form one set. The file name in the bar opens a file menu (a popover under it) with each file's status dot, name, pages and status text, a remove button and **Add PDFs**. Status dots: ring = Find needed, marker yellow = preview to build or pages still to look at, green = every result page looked at, red = problem; the text always says the same. With more than one file, buttons say what they do to the set ("Find in all 5 files", "Build all previews", "Export zip") and Export lists the files above the current file's check.
- Every control is reachable by keyboard; shortcuts are listed behind `?`. Focus rings are 2px `--focus`. With coarse pointers every control is at least 44px.

## States to cover in new UI

Hover, focus, active, disabled, loading (progress card with Cancel), empty (dashed note with the next step), error (red notice that says what to do), stale (veil over the result with "Build preview").

## Copy

Plain verbs that name the outcome: "Find on every page", "Build preview", "Export clean PDF". The same action keeps the same name across the button, the notice and the help. Errors say what happened and how to fix it. English and Traditional Chinese strings live in `pdfscan/web/i18n.js`; page text keeps its English in `index.html` with a `data-i18n` key.
