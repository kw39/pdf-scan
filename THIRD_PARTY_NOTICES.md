# Third-party dependencies

PDF Scan's own source code is MIT-licensed (see `LICENSE`). The components below keep their own licences.

- **PyMuPDF / MuPDF** — GNU AGPL-3.0, or a commercial licence from Artifex. PDF Scan imports PyMuPDF but does not include it; `pip` installs it separately. Distributing PDF Scan bundled with PyMuPDF (an installer, frozen executable or container image) makes that combined distribution subject to the AGPL-3.0 unless you hold a commercial PyMuPDF licence. https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright
- **Pillow** — HPND-style licence. https://github.com/python-pillow/Pillow/blob/main/LICENSE
- **Tesseract OCR / tessdata_fast models** — Apache-2.0. Setup downloads pinned English and Traditional Chinese language models from the official repository. https://github.com/tesseract-ocr/tessdata_fast
- **PP-OCR models** (PaddleOCR detection and recognition networks, ONNX format) — Apache-2.0, © PaddlePaddle Authors. Downloaded by `scripts/setup_ocr.py` from the pinned OnnxOCR 4.0.0 package on PyPI (Apache-2.0, https://github.com/jingsongliujing/OnnxOCR); only the model files and character dictionary are kept. https://github.com/PaddlePaddle/PaddleOCR
- **ONNX Runtime** — MIT. https://github.com/microsoft/onnxruntime/blob/main/LICENSE
- **OpenCC character data** — Apache-2.0. `pdfscan/data/zh_fold.json` is derived from OpenCC's TSCharacters table (via opencc-python-reimplemented 0.1.7) to match Traditional and Simplified forms. Attribution and the full licence text: `LICENSES/Apache-2.0-OpenCC.txt`. https://github.com/BYVoid/OpenCC
- **OpenCV / opencv-python-headless** — OpenCV Apache-2.0; Python wheel packaging MIT and bundled third-party notices remain in the installed distribution. https://github.com/opencv/opencv/blob/4.x/LICENSE and https://github.com/opencv/opencv-python/blob/4.x/LICENSE.txt
- **NumPy** — BSD-3-Clause, with bundled component notices. https://github.com/numpy/numpy/blob/main/LICENSE.txt
- **Bricolage Grotesque** (interface font, bundled in `pdfscan/web/fonts/`) — SIL Open Font License 1.1, © 2022 The Bricolage Grotesque Project Authors. Full licence text: `pdfscan/web/fonts/OFL.txt`. https://github.com/ateliertriay/bricolage
- **pytest** (development only) — MIT. https://github.com/pytest-dev/pytest/blob/main/LICENSE
- **setuptools / build** (packaging and development) — MIT. https://github.com/pypa/setuptools and https://github.com/pypa/build

Dependency licences and notices remain with their distributions. OCR model binaries are not included in the source ZIP or the repository; setup downloads them separately and verifies their checksums. The browser page uses no third-party JavaScript and loads nothing from the internet; its only third-party asset is the bundled Bricolage Grotesque font.
