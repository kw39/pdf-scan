"""Download pinned, checksummed OCR models; never upload any document.

Installs two readers:
- PP-OCR (default): PaddleOCR detection and recognition networks for Chinese
  (Traditional and Simplified) and English, taken from the pinned OnnxOCR wheel
  on PyPI (Apache-2.0). Only the three model files are kept.
- Tesseract (fallback): official tessdata_fast English and Traditional Chinese.
"""
import argparse
import hashlib
import io
import zipfile
from pathlib import Path
from urllib.request import urlopen

COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
TESSERACT = {
    "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "chi_tra": "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b",
}
PPOCR_WHEEL = ("https://files.pythonhosted.org/packages/92/fd/be4ce649c5fb4a59c028e4f5365bcb679e16abbc4da41baa4588e70a0ccd/"
               "onnxocr-4.0.0-py3-none-any.whl")
PPOCR_WHEEL_SHA256 = "71c570a347ec688cd47dbc400c77e9abcbd3f48e497519234e0f5ce575c5845b"
PPOCR_FILES = {  # target name: (path inside the wheel, sha256)
    "det.onnx": ("onnxocr/models/ppocrv6/small/det/det.onnx",
                 "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e"),
    "rec.onnx": ("onnxocr/models/ppocrv6/small/rec/rec.onnx",
                 "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634"),
    "dict.txt": ("onnxocr/models/ppocrv6/ppocrv6_dict.txt",
                 "769e7fa79bb297b5f18d8dbd149e364a45bc61f2b3f574e5ea836f0b261c23a6"),
}

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def write(target, data):
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)

def install_tesseract(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for language, checksum in TESSERACT.items():
        target = directory / (language + ".traineddata")
        if target.is_file() and sha256(target.read_bytes()) == checksum:
            print("Tesseract " + language + ": verified")
            continue
        url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{COMMIT}/{language}.traineddata"
        with urlopen(url, timeout=90) as response:
            data = response.read(20 * 1024 * 1024)
        if sha256(data) != checksum:
            raise SystemExit("OCR model checksum mismatch: " + language)
        write(target, data)
        print("Tesseract " + language + ": installed and verified")

def install_ppocr(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if all((directory / name).is_file() and sha256((directory / name).read_bytes()) == checksum
           for name, (_, checksum) in PPOCR_FILES.items()):
        print("PP-OCR models: verified")
        return
    print("PP-OCR models: downloading (about 75 MB, once)…", flush=True)
    with urlopen(PPOCR_WHEEL, timeout=300) as response:
        wheel = response.read(120 * 1024 * 1024)
    if sha256(wheel) != PPOCR_WHEEL_SHA256:
        raise SystemExit("PP-OCR package checksum mismatch.")
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for name, (inner, checksum) in PPOCR_FILES.items():
            data = archive.read(inner)
            if sha256(data) != checksum:
                raise SystemExit("PP-OCR model checksum mismatch: " + name)
            write(directory / name, data)
    print("PP-OCR models: installed and verified")

def main():
    parser = argparse.ArgumentParser(description="Install the pinned OCR models.")
    parser.add_argument("--directory", type=Path, default=Path(".runtime/tessdata"), help="Tesseract model folder")
    parser.add_argument("--models", type=Path, default=Path(".runtime/models/ppocr"), help="PP-OCR model folder")
    parser.add_argument("--skip-ppocr", action="store_true", help="Install only the Tesseract fallback")
    args = parser.parse_args()
    install_tesseract(args.directory)
    if not args.skip_ppocr:
        install_ppocr(args.models)

if __name__ == "__main__":
    main()
