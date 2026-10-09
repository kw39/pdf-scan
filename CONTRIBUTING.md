# Contributing

Thanks for helping. PDF Scan handles private documents, so a few rules matter more than usual.

## Never share real documents

Use the fictional sample (`python -m pdfscan demo sample.pdf`) or build a small synthetic PDF in a test. Don't attach real PDFs, OCR caches, private reports or screenshots of real documents to issues or pull requests. Report security problems privately: see [SECURITY.md](SECURITY.md).

## Set up

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .\Setup.ps1
python -m pip install -e '.[dev]'
python scripts/setup_ocr.py
python -m pdfscan serve --open-browser
```

## Before you open a pull request

```bash
python -m pytest -q --basetemp=.runtime/test-run
python scripts/prepare_release.py --audit-only --audit-git
```

- Add a test for every engine or server change. Tests generate their own fictional PDFs (see `tests/test_features.py`).
- OCR or matching changes: run `benchmarks/ocr/` before and after, and include both results in the pull request. Don't merge changes that lower full coverage on the held-out or stress set.
- New detectors must be **opt-in** and show their guesses for review. Prefer validators (checksums, formats) over loose patterns.
- Output stays image-only unless the user turns on searchable text, and the text layer may only come from the cleaned pixels.
- Interface changes follow [DESIGN.md](DESIGN.md): its tokens, one font family, sentence case, keyboard access, at least 44px touch targets, and reduced motion. Add English and Traditional Chinese text to `pdfscan/web/i18n.js`.
- Keep dependencies to what is in `pyproject.toml`. The browser page loads nothing from the internet.
- Add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).

## Public files and publication checks

Only files listed in `scripts/public-files.txt` belong in the public repository and
source ZIP. Review additions for their purpose, private data and metadata before
adding them to this list. Keep local assistant notes, integration planning, real
documents, caches and build output private. Use fictional examples and screenshots.

After setup, install the local commit and push checks:

```bash
python scripts/prepare_release.py --install-hooks
```

The commit check audits staged content; the push check audits the complete history
being pushed. CI checks tracked files and built package contents. After building,
run `python scripts/prepare_release.py --audit-only --dist-dir dist` before sharing
archives. These checks require review when an approved file changes; they do not
replace reading the diff. Additional private deny terms can be stored one per line
in ignored `.runtime/publication-deny-terms.txt`.

## Licence

By contributing you agree that your contribution is licensed under the MIT License, the project's licence. Don't add code or data copied from projects under incompatible or unclear licences, and list any new third-party dependency or asset, with its licence, in `THIRD_PARTY_NOTICES.md`.
