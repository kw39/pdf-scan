## What changed and why

## How I checked it
- [ ] `python -m pytest -q --basetemp=.runtime/test-run` passes
- [ ] `python scripts/prepare_release.py --audit-only` passes
- [ ] Interface changes checked at about 375, 768 and 1280px, with the keyboard and in both themes
- [ ] New interface text added in English and Traditional Chinese (`pdfscan/web/i18n.js`)
- [ ] CHANGELOG.md updated

## Compatibility
- [ ] Existing detection/decision/preview/export contracts are preserved, or changes and compatibility are explained
- [ ] Existing UI tokens/components are reused; intentional design changes are recorded in DESIGN.md (or no UI change)

## Privacy
- [ ] No real documents, OCR caches, reports or screenshots of private pages are included
