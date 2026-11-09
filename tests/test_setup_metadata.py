"""Running package metadata must preserve already-built distribution artifacts."""
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock


def test_setup_metadata_preserves_existing_dist(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "setup.py"
    candidate = tmp_path / "setup.py"
    (tmp_path / "README.md").write_text("Synthetic package description", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    artifact = dist / "synthetic.whl"
    artifact.write_bytes(b"previous completed build")
    # Replace packaging only: run the complete setup script against real files.
    # No SDK installation or actual package build is needed to test this side effect.
    setuptools = ModuleType("setuptools")
    setuptools.setup = Mock()
    setuptools.find_packages = Mock(return_value=["lumibot"])
    command = ModuleType("setuptools.command")
    build_py = ModuleType("setuptools.command.build_py")
    build_py.build_py = object
    monkeypatch.setitem(sys.modules, "setuptools", setuptools)
    monkeypatch.setitem(sys.modules, "setuptools.command", command)
    monkeypatch.setitem(sys.modules, "setuptools.command.build_py", build_py)
    monkeypatch.setattr(sys, "argv", [str(candidate), "--version"])
    monkeypatch.chdir(tmp_path)
    # Evaluate the actual source in memory; the synthetic project needs only data files.
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"),
         {"__name__": "__main__", "__file__": str(candidate)})
    assert artifact.read_bytes() == b"previous completed build"
    assert setuptools.setup.call_args.kwargs["name"] == "lumibot"
