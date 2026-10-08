"""Command-line entry point."""
import argparse
import json
import sys
from pathlib import Path
from . import __version__
from .engine import detect, apply
from .server import serve
from .demo import create_demo

def output_options(args):
    return {"fill": args.fill, "quality": "compact" if args.compact else "lossless", "searchable": args.searchable}

def add_output_flags(parser):
    parser.add_argument("--fill", choices=("white", "black"), default="white",
                        help="Colour used for removed areas (default: white)")
    parser.add_argument("--compact", action="store_true", help="Smaller JPEG page images instead of lossless PNG")
    parser.add_argument("--searchable", action="store_true",
                        help="OCR the cleaned pages and add an invisible text layer, then check for remaining targets")

def main():
    parser = argparse.ArgumentParser(prog="pdfscan", description="Local PDF detection, review and image-only redaction.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="Open the local review app")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open-browser", action="store_true", help="Open the review page in your default browser")
    web.add_argument("--runtime", type=Path, default=Path.cwd() / ".runtime")
    scan = sub.add_parser("detect", help="Create a detection plan without changing the source")
    scan.add_argument("source", type=Path)
    scan.add_argument("--rules", type=Path, required=True)
    scan.add_argument("--plan", type=Path, required=True)
    scan.add_argument("--tessdata", type=Path, default=Path.cwd() / ".runtime" / "tessdata")
    scan.add_argument("--models", type=Path, default=Path.cwd() / ".runtime" / "models" / "ppocr", help="PP-OCR model folder (falls back to Tesseract when missing)")
    scan.add_argument("--cache", type=Path)
    scan.add_argument("--force-ocr", action="store_true")
    scan.add_argument("--logo", type=Path, help="Reference logo PNG, JPEG or WebP")
    export = sub.add_parser("apply", help="Apply a reviewed plan to a new PDF")
    export.add_argument("source", type=Path)
    export.add_argument("--plan", type=Path, required=True)
    export.add_argument("--decisions", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--tessdata", type=Path, default=Path.cwd() / ".runtime" / "tessdata")
    export.add_argument("--models", type=Path, default=Path.cwd() / ".runtime" / "models" / "ppocr", help="PP-OCR model folder (falls back to Tesseract when missing)")
    export.add_argument("--reviewed", action="store_true", help="Acknowledge review of targets and decisions")
    add_output_flags(export)
    batch = sub.add_parser("batch", help="Detect and apply the same rules to every PDF in a folder")
    batch.add_argument("folder", type=Path)
    batch.add_argument("--rules", type=Path, required=True)
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--tessdata", type=Path, default=Path.cwd() / ".runtime" / "tessdata")
    batch.add_argument("--models", type=Path, default=Path.cwd() / ".runtime" / "models" / "ppocr", help="PP-OCR model folder (falls back to Tesseract when missing)")
    batch.add_argument("--logo", type=Path, help="Reference logo PNG, JPEG or WebP")
    batch.add_argument("--accept-proposals", action="store_true",
                       help="Required: apply every proposal without visual review; inspect each output afterwards")
    add_output_flags(batch)
    demo = sub.add_parser("demo", help="Generate a fictional sample PDF")
    demo.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            if not 1024 <= args.port <= 65535:
                parser.error("Port must be 1024-65535.")
            serve(args.port, args.runtime, open_browser=args.open_browser)
            return
        if args.command == "demo":
            create_demo(args.output)
            print("Synthetic demo created.")
            return
        if args.command == "detect":
            plan = detect(args.source, json.loads(args.rules.read_text("utf-8-sig")), cache_dir=args.cache,
                          tessdata=args.tessdata, force_ocr=args.force_ocr, logo_path=args.logo, models=args.models)
            args.plan.parent.mkdir(parents=True, exist_ok=True)
            args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Detected {sum(len(p['detections']) for p in plan['pages'])} regions. Review the private plan before applying.")
        elif args.command == "apply":
            if not args.reviewed:
                parser.error("Inspect the detection plan, then use --reviewed.")
            decisions = json.loads(args.decisions.read_text("utf-8-sig"))
            decisions.setdefault("output", output_options(args))
            result = apply(args.source, json.loads(args.plan.read_text("utf-8-sig")), decisions, args.output,
                           tessdata=args.tessdata, models=args.models)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if result["residual"]:
                print("Warning: configured targets are still readable in the output. Inspect it.", file=sys.stderr)
                sys.exit(2)
        elif args.command == "batch":
            run_batch(parser, args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Error: {exc}\n")

def run_batch(parser, args):
    if not args.accept_proposals:
        parser.error("Batch mode applies proposals without visual review. Add --accept-proposals to continue.")
    rules = json.loads(args.rules.read_text("utf-8-sig"))
    sources = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if not sources:
        parser.error("No PDF files found in " + str(args.folder))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary, failures = [], 0
    for index, source in enumerate(sources, 1):
        target = args.output_dir / (source.stem + " (redacted).pdf")
        print(f"[{index}/{len(sources)}] {source.name}", flush=True)
        try:
            plan = detect(source, rules, tessdata=args.tessdata, logo_path=args.logo, models=args.models)
            result = apply(source, plan, {"output": output_options(args)}, target, tessdata=args.tessdata,
                           models=args.models)
            regions = sum(len(p["detections"]) for p in plan["pages"])
            summary.append({"source": source.name, "output": target.name, "regions": regions,
                            "outputPages": result["outputPages"], "residual": result["residual"]})
            note = "  ⚠ targets still readable" if result["residual"] else ""
            print(f"    {regions} regions removed → {target.name}{note}", flush=True)
        except (ValueError, OSError) as exc:
            failures += 1
            summary.append({"source": source.name, "error": str(exc)})
            print(f"    skipped: {exc}", flush=True)
    (args.output_dir / "batch-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                        encoding="utf-8")
    print(f"Done: {len(sources) - failures} cleaned, {failures} skipped. Inspect every output before sharing.")
    if failures:
        sys.exit(1)

if __name__ == "__main__":
    main()
