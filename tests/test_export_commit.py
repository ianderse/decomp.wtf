"""An export commits its tree when it sits in a git repository (the owner's private decomp repo)."""
import subprocess
from pathlib import Path

from decompnet.rom.export import commit_tree


def test_commit_tree_commits_only_the_export_and_skips_without_changes(tmp_path):
    repo = tmp_path / "game"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    out = repo / "source"
    out.mkdir()
    (out / "a.c").write_text("int a;\n")
    (repo / "elsewhere.txt").write_text("not staged\n")
    said = []
    assert commit_tree(out, "Export: 1 of 2", said.append) is True
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "Export: 1 of 2" in log and len(log.splitlines()) == 1
    status = subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout
    assert status.strip() == "?? elsewhere.txt"
    assert commit_tree(out, "again", said.append) is False  # nothing changed


def test_commit_tree_without_a_repository_is_quiet(tmp_path):
    out = tmp_path / "source"
    out.mkdir()
    assert commit_tree(out, "x", lambda *_: None) is False
