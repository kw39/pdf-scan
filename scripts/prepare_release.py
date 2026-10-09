"""Create a source-only ZIP from an explicit allowlist."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from pdfscan import __version__  # noqa: E402
ROOT_FILES={"README.md","LICENSE","SECURITY.md","THIRD_PARTY_NOTICES.md","CONTRIBUTING.md","CHANGELOG.md",
            "DESIGN.md","CODE_OF_CONDUCT.md","pyproject.toml","requirements.txt","MANIFEST.in",".gitignore",
            ".dockerignore",".gitattributes","Setup.ps1","Start.ps1","start.cmd"}
PUBLIC_DIRS={"pdfscan","tests","examples","scripts",".github","docs/screenshots","benchmarks","LICENSES"}
TEXT={".py",".html",".css",".js",".json",".yml",".yaml",".ps1",".md",".svg",".txt"}
BINARY={".woff2",".png"}
EXTENSIONS=TEXT|BINARY
BLOCKED_PARTS={"__pycache__",".runtime",".venv",".git",".pytest_cache","build","dist"}
PATTERNS=[
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b[A-Z]:[\\/](?:Users|project|gpt)[\\/]",re.I),
    re.compile(r"(?<![\w.])/(?:home|Users)/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|qq|163|126|proton(?:mail)?)\.(?:com|me|net|hk|cn)\b",re.I),
]
def public_files():
    result=[ROOT/name for name in ROOT_FILES if (ROOT/name).is_file()]
    for directory in PUBLIC_DIRS:
        folder=ROOT/directory
        if not folder.exists(): continue
        result.extend(p for p in folder.rglob("*") if p.is_file() and p.suffix in EXTENSIONS
                      and not set(p.relative_to(ROOT).parts)&BLOCKED_PARTS)
    return sorted(set(result),key=lambda p:p.relative_to(ROOT).as_posix())

def audit(files,denied):
    failures=[]
    for path in files:
        if path.suffix in BINARY:
            continue
        text=path.read_text(encoding="utf-8-sig")
        if any(pattern.search(text) for pattern in PATTERNS):
            failures.append(path.relative_to(ROOT).as_posix()+": possible secret or personal machine path")
        for term in denied:
            if term.casefold() in text.casefold():
                failures.append(path.relative_to(ROOT).as_posix()+": denied term found")
    if failures:
        raise SystemExit("Release audit failed:\n"+"\n".join(failures))

def main():
    parser=argparse.ArgumentParser(description="Audit and create a clean, allowlisted source archive.")
    parser.add_argument("--deny-term",action="append",default=[])
    parser.add_argument("--audit-only",action="store_true")
    args=parser.parse_args()
    files=public_files()
    audit(files,args.deny_term)
    if args.audit_only:
        print(f"Audit passed for {len(files)} public source files.")
        return
    release=ROOT/"releases"
    release.mkdir(exist_ok=True)
    destination=release/f"pdfscan-local-{__version__}-source.zip"
    manifest=[]
    with ZipFile(destination,"w",ZIP_DEFLATED) as archive:
        for path in files:
            name=path.relative_to(ROOT).as_posix()
            data=path.read_bytes()
            archive.writestr("pdfscan-local/"+name,data)
            manifest.append({"path":name,"sha256":hashlib.sha256(data).hexdigest()})
    checksum=hashlib.sha256(destination.read_bytes()).hexdigest()
    (release/"source-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    destination.with_suffix(".zip.sha256").write_text(checksum+"  "+destination.name+"\n",encoding="ascii")
    with ZipFile(destination) as archive:
        assert len(archive.namelist())==len(files)
        assert not any(set(Path(name).parts)&BLOCKED_PARTS for name in archive.namelist())
    print(f"Created {destination.name}: {len(files)} public source files.")
    print("SHA-256: "+checksum)

if __name__=="__main__":
    main()
