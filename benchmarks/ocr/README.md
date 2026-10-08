# OCR benchmark

Synthetic scanned documents with exact ground truth, used to choose and tune the OCR engine. Everything is fictional: names, schools, companies, phone, ID and card numbers.

Pages imitate what people share: exam papers (header, ruled candidate table, answer boxes, margin rule, answer lines) and staff forms or letters (letterhead, ruled table with name, HKID, phone and card number, signature), in English or Traditional Chinese. Each page is degraded like a scan: slight rotation, grey paper, uneven light, noise, specks, blur, 150–300 dpi, JPEG. The stress set adds faint ink, small print, heavy compression and pages turned 90°, 180° or 270°.

```bash
python benchmarks/ocr/generate.py .runtime/bench/test 2027 60
python benchmarks/ocr/generate.py .runtime/bench/stress 777 30 --stress
python benchmarks/ocr/evaluate.py .runtime/bench/test --engine ppocr
python benchmarks/ocr/evaluate.py .runtime/bench/test --engine tesseract
```

`covered@95` is the share of private details whose ground-truth box is at least 95% covered by removals; `missed(<50%)` counts details less than half covered. Tuning used a separate development set (seed 11, 40 pages); the seeds above are held out.

Results at release (same CPU):

| Set | Engine | covered@95 | missed | false alarms | s/page |
| --- | --- | --- | --- | --- | --- |
| test (60 pages) | PP-OCR | 0.988 | 0 | 2 | 2.75 |
| test | Tesseract | 0.474 | 155 | 1 | 3.42 |
| stress (30 pages) | PP-OCR | 0.956 | 1 | 2 | 3.30 |
| stress | Tesseract | 0.110 | 160 | 1 | 2.32 |

The two PP-OCR "false alarms" are student numbers whose digits pass the HKID check digit, so they are flagged as ID numbers. Fonts needed: Liberation, DejaVu, FreeFont and Noto CJK.
