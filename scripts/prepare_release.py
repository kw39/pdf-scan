"""Create a source-only ZIP from an explicit allowlist."""
import argparse
import hashlib
import json
import re
import sys
import io
import subprocess
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from pdfscan import __version__  # noqa: E402
PUBLIC_MANIFEST=ROOT/"scripts/public-files.txt"
PRIVATE_NAMES={"AGENTS.md","CLAUDE.md","pdfscan-repository-handoff.md",
               "detection-feature-map.md","detection-changes.md"}
BLOCKED_PARTS={"__pycache__",".runtime",".venv",".git",".pytest_cache","build","dist","releases","venv",
               ".agents",".codex",".claude",".vscode",".idea"}
PATTERNS=[
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b[A-Z]:[\\/](?:Users|project|gpt)[\\/]",re.I),
    re.compile(r"(?<![\w.])/(?:home|Users)/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|qq|163|126|proton(?:mail)?)\.(?:com|me|net|hk|cn)\b",re.I),
]
def approved_names(data=None):
    text=PUBLIC_MANIFEST.read_text(encoding="utf-8") if data is None else data.decode("utf-8")
    names={line.strip() for line in text.splitlines()
           if line.strip() and not line.lstrip().startswith("#")}
    for name in names:
        parts=Path(name).parts
        if ("\\" in name or Path(name).is_absolute() or ".." in parts
                or {part.casefold() for part in parts}&{part.casefold() for part in BLOCKED_PARTS|PRIVATE_NAMES}
                or any(part.casefold().startswith(".env") or part.casefold().endswith(".egg-info") for part in parts)
                or ":" in name):
            raise SystemExit("Invalid public manifest entry: "+name)
    return names


def public_files():
    result=[]
    for name in sorted(approved_names()):
        path=ROOT/name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise SystemExit("Missing or unsafe public source file: "+name)
        result.append(path)
    return result


def audit_blob(name,data,denied):
    if Path(name).suffix == ".woff2":
        return
    if Path(name).suffix == ".png":
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            if img.info:
                raise SystemExit(name+": image metadata must be removed before publication")
        return
    try:
        text=data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SystemExit(name+": unexpected binary data") from None
    if any(pattern.search(text) for pattern in PATTERNS):
        raise SystemExit(name+": possible secret or personal machine path")
    if any(term.casefold() in text.casefold() for term in denied):
        raise SystemExit(name+": denied term found")


def audit(files,denied):
    for path in files:
        audit_blob(path.relative_to(ROOT).as_posix(),path.read_bytes(),denied)


def git(*args):
    return subprocess.check_output(["git","-C",str(ROOT),*args])


def audit_git(denied,ref=None):
    command=("ls-tree","-rz",ref) if ref else ("ls-files","--stage","-z")
    entries=git(*command).split(b"\0")
    manifest_name=PUBLIC_MANIFEST.relative_to(ROOT).as_posix()
    manifests=[entry for entry in entries if entry.partition(b"\t")[2].decode("utf-8")==manifest_name]
    if manifests:
        metadata=manifests[0].split(b"\t",1)[0].decode().split()
        names=approved_names(git("cat-file","blob",metadata[2] if ref else metadata[1]))
    elif ref:
        names=approved_names()  # Early clean commits predate the public manifest.
    else:
        raise SystemExit("Public file manifest must be staged with the publication checks")
    for entry in entries:
        if not entry:
            continue
        metadata,raw_name=entry.split(b"\t",1)
        name=raw_name.decode("utf-8")
        mode,second,third=metadata.decode().split()
        if name not in names or mode not in {"100644","100755"}:
            raise SystemExit(name+": not an approved public file")
        if not ref and third != "0":
            raise SystemExit(name+": unresolved merge")
        oid=third if ref else second
        audit_blob(name,git("cat-file","blob",oid),denied)


def audit_history(ref,denied):
    if not re.fullmatch(r"[0-9a-f]{40}",ref):
        raise SystemExit("History audit requires a full commit ID")
    commits=git("rev-list","--reverse",ref).decode().splitlines()
    manifest_name=PUBLIC_MANIFEST.relative_to(ROOT).as_posix()
    seen_manifest=False
    for commit in commits:
        has_manifest=manifest_name in git("ls-tree","-r","--name-only",commit).decode().splitlines()
        if seen_manifest and not has_manifest:
            raise SystemExit("Public file manifest removed from publication history")
        seen_manifest=seen_manifest or has_manifest
        audit_git(denied,commit)
        audit_blob("commit metadata",git("show","-s","--format=%B%n%an%n%ae%n%cn%n%ce",commit),denied)
    print(f"History audit passed for {len(commits)} commits.")


def archive_name_allowed(name,wheel,names):
    parts=name.split("/")
    if any(part in {"..",""} for part in parts) or name.startswith("/") or "\\" in name:
        return False
    if wheel and parts[0].endswith(".dist-info"):
        rest="/".join(parts[1:])
        if rest.startswith("licenses/"):
            return rest[len("licenses/"):] in names
        return rest in {"METADATA","WHEEL","entry_points.txt","top_level.txt","RECORD"}
    if name in names:
        return True
    if not wheel:
        if name in {"PKG-INFO","setup.cfg"}:
            return True
        if parts[0] == "pdfscan_local.egg-info" and len(parts) == 2:
            return parts[1] in {"PKG-INFO","SOURCES.txt","dependency_links.txt", "entry_points.txt","requires.txt","top_level.txt"}
    return False


def audit_dist(folder,denied):
    names=approved_names()
    archives=sorted(Path(folder).glob("*.whl"))+sorted(Path(folder).glob("*.tar.gz"))
    if not archives:
        raise SystemExit("No wheel/source archives found to audit")
    for path in archives:
        if path.suffix == ".whl":
            with ZipFile(path) as archive:
                for member in archive.infolist():
                    if not archive_name_allowed(member.filename,True,names):
                        raise SystemExit(path.name+": unapproved member "+member.filename)
                    audit_blob(member.filename,archive.read(member),denied)
        else:
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if member.isdir():
                        continue
                    name=member.name.partition("/")[2]
                    if not member.isfile() or not archive_name_allowed(name,False,names):
                        raise SystemExit(path.name+": unapproved member "+member.name)
                    audit_blob(name,archive.extractfile(member).read(),denied)
    print(f"Package audit passed for {len(archives)} archives.")


def install_hooks():
    hook_dir=ROOT/".runtime/publication-hooks"
    existing=subprocess.run(["git","-C",str(ROOT),"config","--local","--get","core.hooksPath"],
                            capture_output=True,text=True).stdout.strip()
    if existing and existing != ".runtime/publication-hooks":
        raise SystemExit("Existing Git hooks path needs manual integration; not replaced")
    hook_dir.mkdir(parents=True,exist_ok=True)
    common='''#!/bin/sh
set -eu
if [ -x .venv/Scripts/python.exe ]; then
    python_cmd=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then
    python_cmd=.venv/bin/python
else
    python_cmd=python
fi
'''
    (hook_dir/"pre-commit").write_text(common+'"$python_cmd" scripts/prepare_release.py --audit-only --audit-git\n',encoding="utf-8",newline="\n")
    (hook_dir/"pre-push").write_text(common+'''while read -r local_ref local_oid remote_ref remote_oid; do
    case "$local_oid" in
        0000000000000000000000000000000000000000) continue ;;
    esac
    "$python_cmd" scripts/prepare_release.py --audit-only --audit-history "$local_oid"
done
''',encoding="utf-8",newline="\n")
    for path in hook_dir.iterdir():
        path.chmod(0o755)
    git("config","--local","core.hooksPath",".runtime/publication-hooks")
    print("Installed local commit/push publication checks.")


def main():
    parser=argparse.ArgumentParser(description="Audit and create a clean, allowlisted source archive.")
    parser.add_argument("--deny-term",action="append",default=[])
    parser.add_argument("--audit-only",action="store_true")
    parser.add_argument("--audit-git",action="store_true",help="Check the exact staged files and content")
    parser.add_argument("--audit-history",metavar="COMMIT",help="Check every reachable commit before pushing")
    parser.add_argument("--dist-dir",type=Path,help="Audit built source and wheel archives")
    parser.add_argument("--install-hooks",action="store_true",help="Install local commit/push checks")
    args=parser.parse_args()
    denied=list(args.deny_term)
    private_terms=ROOT/".runtime/publication-deny-terms.txt"
    if private_terms.is_file():
        denied.extend(line.strip() for line in private_terms.read_text(encoding="utf-8-sig").splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))
    files=public_files()
    audit(files,denied)
    if args.audit_git:
        audit_git(denied)
    if args.audit_history:
        audit_history(args.audit_history,denied)
    if args.dist_dir:
        audit_dist(args.dist_dir,denied)
    if args.install_hooks:
        install_hooks()
        return
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
