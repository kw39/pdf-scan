"""Publication checks use synthetic repositories, text and archives only."""
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image, PngImagePlugin

spec=importlib.util.spec_from_file_location("publication",Path(__file__).parents[1]/"scripts/prepare_release.py")
publication=importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


@pytest.fixture
def repository(tmp_path,monkeypatch):
    subprocess.run(["git","init","-q",str(tmp_path)],check=True)
    for key,value in [("user.name","Example"),("user.email","example@example.invalid"),("commit.gpgsign","false")]:
        subprocess.run(["git","-C",str(tmp_path),"config",key,value],check=True)
    manifest=tmp_path/"public-files.txt"
    manifest.write_text("README.md\npublic-files.txt\n",encoding="utf-8")
    (tmp_path/"README.md").write_text("Public guide\n",encoding="utf-8")
    monkeypatch.setattr(publication,"ROOT",tmp_path)
    monkeypatch.setattr(publication,"PUBLIC_MANIFEST",manifest)
    subprocess.run(["git","-C",str(tmp_path),"add","public-files.txt"],check=True)
    return tmp_path


def run_git(root,*args):
    return subprocess.check_output(["git","-C",str(root),*args]).decode().strip()


def test_unreviewed_staged_notes_are_blocked(repository):
    (repository/"notes.md").write_text("Internal planning",encoding="utf-8")
    run_git(repository,"add","README.md","notes.md")
    with publication.PUBLIC_MANIFEST.open("a",encoding="utf-8") as manifest:
        manifest.write("notes.md\n")  # Unstaged edits must not approve staged files.
    with pytest.raises(SystemExit,match="not an approved public file"):
        publication.audit_git([])


def test_staged_content_is_checked_even_if_working_copy_is_clean(repository):
    note="PrivateExampleIntegration"
    (repository/"README.md").write_text(note,encoding="utf-8")
    run_git(repository,"add","README.md")
    (repository/"README.md").write_text("Public guide",encoding="utf-8")
    with pytest.raises(SystemExit,match="denied term"):
        publication.audit_git([note])


def test_clean_current_files_do_not_hide_private_history(repository):
    note="PrivateExampleIntegration"
    (repository/"README.md").write_text(note,encoding="utf-8")
    run_git(repository,"add","README.md")
    run_git(repository,"commit","-qm","First")
    (repository/"README.md").write_text("Public guide",encoding="utf-8")
    run_git(repository,"add","README.md")
    run_git(repository,"commit","-qm","Clean current files")
    publication.audit_git([note])
    with pytest.raises(SystemExit,match="denied term"):
        publication.audit_history(run_git(repository,"rev-parse","HEAD"),[note])


def test_approved_path_cannot_be_a_git_symlink(repository):
    oid=run_git(repository,"hash-object","-w","README.md")
    run_git(repository,"update-index","--add","--cacheinfo","120000,"+oid+",README.md")
    with pytest.raises(SystemExit,match="not an approved public file"):
        publication.audit_git([])


@pytest.mark.parametrize("entry",["AGENTS.md","docs/detection-feature-map.md","../private.md",".runtime/report.md","claude.MD","releases/report.txt",".env.local"])
def test_private_and_escaping_manifest_entries_are_rejected(repository,entry):
    publication.PUBLIC_MANIFEST.write_text(entry+"\n",encoding="utf-8")
    with pytest.raises(SystemExit,match="Invalid public manifest"):
        publication.approved_names()


def test_unlisted_files_are_not_in_source_zip_selection(repository):
    (repository/"local-notes.py").write_text("private planning",encoding="utf-8")
    assert [p.name for p in publication.public_files()]==["README.md","public-files.txt"]


def test_missing_approved_source_is_a_failure(repository):
    (repository/"README.md").unlink()
    with pytest.raises(SystemExit,match="Missing or unsafe"):
        publication.public_files()


def test_unlisted_files_inside_source_package_are_blocked(repository):
    archive=repository/"sample.tar.gz"
    with tarfile.open(archive,"w:gz") as output:
        data=b"Internal planning"
        member=tarfile.TarInfo("sample/pdfscan/local-notes.py")
        member.size=len(data)
        output.addfile(member,io.BytesIO(data))
    with pytest.raises(SystemExit,match="unapproved member"):
        publication.audit_dist(repository,[])


def test_wheel_metadata_does_not_bypass_private_content_checks(repository):
    with ZipFile(repository/"sample.whl","w") as output:
        output.writestr("sample.dist-info/METADATA","PrivateExampleIntegration")
    with pytest.raises(SystemExit,match="denied term"):
        publication.audit_dist(repository,["PrivateExampleIntegration"])


def test_screenshot_metadata_is_blocked():
    image=Image.new("RGB",(2,2),"white")
    metadata=PngImagePlugin.PngInfo()
    metadata.add_text("Comment","Private sample report")
    data=io.BytesIO()
    image.save(data,format="PNG",pnginfo=metadata)
    with pytest.raises(SystemExit,match="metadata must be removed"):
        publication.audit_blob("review.png",data.getvalue(),[])


def test_personal_path_is_blocked_without_printing_content():
    content=("C:"+"/"+"Users"+"/"+"Example/private.txt").encode()
    with pytest.raises(SystemExit,match="possible secret or personal machine path") as error:
        publication.audit_blob("README.md",content,[])
    assert "Example" not in str(error.value)


def test_public_manifest_cannot_be_removed_from_later_history(repository):
    run_git(repository,"add","README.md")
    run_git(repository,"commit","-qm","Public files")
    run_git(repository,"rm","public-files.txt")
    run_git(repository,"commit","-qm","Remove manifest")
    with pytest.raises(SystemExit,match="manifest removed"):
        publication.audit_history(run_git(repository,"rev-parse","HEAD"),[])
