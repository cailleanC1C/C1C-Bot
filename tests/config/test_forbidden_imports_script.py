from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "check_forbidden_imports.sh"


def _run_without_ripgrep(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    tools = tmp_path / "tools"
    tools.mkdir()
    grep = shutil.which("grep")
    assert grep is not None
    (tools / "grep").symlink_to(grep)

    (tmp_path / "example.py").write_text(source, encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(tools)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_forbidden_import_check_falls_back_and_allows_shared_ports(tmp_path: Path) -> None:
    result = _run_without_ripgrep(
        tmp_path, "from shared.ports import get_port\nPORT = get_port()\n"
    )

    assert result.returncode == 0
    assert "using grep fallback" in result.stdout
    assert "No forbidden imports found" in result.stdout


def test_forbidden_import_check_fallback_rejects_deprecated_paths(tmp_path: Path) -> None:
    result = _run_without_ripgrep(
        tmp_path,
        "from config.runtime import get_port\n"
        "import shared.config\n"
        "PORT = shared.config.get_port()\n",
    )

    assert result.returncode == 1
    assert "Forbidden import path detected" in result.stdout
    assert "example.py:1" in result.stdout
    assert "example.py:3" in result.stdout
