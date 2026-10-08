# Changelog

## Unreleased

## 0.2.0 — 2026-10-08

### Added
- PP-OCR reader for scans (local, ONNX Runtime): reads text inside ruled tables, Traditional and Simplified Chinese and English, and pages scanned sideways or upside down. On the held-out benchmark, fully covered details rose from 47% to 99% and misses fell from 155 to 0. Tesseract remains as a fallback.
- Close matches for small OCR reading errors, shown as "close match, read as …" for review, and Traditional/Simplified variant matching.
- `benchmarks/ocr/`: a reproducible synthetic scan benchmark with ground truth.
- New interface: page rail with counts, Original / Compare / Result views with a draggable divider, dashed outlines where content was removed, and a check of which result pages you have looked at.
- Detection of payment card numbers (Luhn-checked) and ID numbers (HKID, Taiwan, mainland China and US SSN, checksum-validated).
- Export options: black or white boxes, smaller JPEG pages, and searchable text with a leftover check that warns if your words are still readable.
- Progress with Cancel for Find and Build preview.
- Keep all / Remove all per group, undo and redo, and Leave this page out.
- Drawing areas by two clicks or by keyboard, as well as by dragging.
- Several PDFs at once: open or add up to 50 files, use one set of Find settings for all of them, review each file with its own choices, and export everything as one zip (`Redacted PDFs (n files).zip`). The file menu shows each file's status; **Next page with findings** and the `[` `]` keys move across files. New `POST /api/bundle` checks every file against its current preview before zipping.
- Editing found boxes and drawn areas: drag to move, drag a grip to resize, type an exact position and size in millimetres, grow or shrink every side, or nudge with the arrow keys. Changes apply to the selected box or, with **All n “word”**, to every match of the same word on every page, scaled to each box's size. Exports use the edited boxes (`adjusted` in review decisions).
- Resume after reloading the page; English and Traditional Chinese interface; dark theme; keyboard shortcuts.
- `pdfscan batch` for a folder of PDFs, and `--fill`, `--compact` and `--searchable` on `apply`.
- Confirmation before deleting a document.

### Changed
- Licence: PDF Scan's source code is now MIT-licensed (previously AGPL-3.0-only). PyMuPDF, a dependency, remains AGPL-3.0; see the README's Licence section.
- A Remove area drawn inside a Keep area is now removed; a Keep area inside a Remove area still stays (the innermost area wins).
- Files left by an earlier run, including uploaded originals, are deleted when the app starts.
- Words and codes are matched inside hyphenated or punctuated tokens and inside Chinese text without spaces: "ACME" is found in "INV-ACME-2026", "ACME2026" and "2026年ACME報告". Letters inside a word still never match ("cat" is not found in "category"). Previously "ACME" in "ACME-INC" was skipped on purpose.

### Fixed
- A change made while a preview was being built could be left out of the export without marking the preview out of date. Edit controls are now disabled during work, and a preview that no longer matches your choices is marked out of date.
- Exporting a zip asks first when some result pages haven't been looked at, and a zip is checked file by file before any file counts as reviewed.
- Large downloads and zips are streamed instead of loaded into memory.
- Removing or deleting a document also deletes any zip that contains it.
- Bad numbers in requests return a plain error instead of Python's message.
- On Windows, starting a second copy on a port that is already in use now fails with a clear message instead of sharing the port.
- Tesseract console spam ("Image too small to scale!!", "Line cannot be recognized!!") is silenced, and tiny image slivers are no longer sent to OCR.
- Phone numbers and web addresses written straight after Chinese text (for example `電話+852 2345 6789`) were not found.
- Phone numbers written with spaces (for example `+852 2345 6789`) were never found.
- After about 30 page reloads the app refused to open any PDF until restarted; old sessions are now freed automatically.
- Downloads were always called `cleaned.pdf`; they now use the original name with "(redacted)".
- The favicon was blocked by the page's security policy, logging an error on every load.
- Uploading a non-PDF file gave a generic error.

## 0.1.0

- First release: text and OCR matching, repeated images, logos, URLs, QR codes, drawn areas, page removal and image-only export.
