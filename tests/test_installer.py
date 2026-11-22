"""Tests for Splitch installer scripts, environment doctor, and git self-updater."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from backend.router.app import app as router_app
from backend.shared.engines import (
    get_engine_status,
    install_llama_from_archives,
    parse_llama_build_tag,
    select_llama_assets,
)
from backend.shared.updater import (
    DEFAULT_REPO_URL,
    check_update,
    perform_update,
    run_doctor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_doctor_and_system_api_endpoints() -> None:
    report = run_doctor(app_dir=REPO_ROOT)
    assert report.ok is True
    names = {c.name for c in report.checks}
    assert {
        "python",
        "uv",
        "dashboard_dist",
        "accelerator",
        "engine_binary",
        "data_dir",
        "security",
    }.issubset(names)

    tc = TestClient(router_app)
    r_doc = tc.get("/api/system/doctor")
    assert r_doc.status_code == 200
    assert r_doc.json()["version"] == report.version
    assert r_doc.headers.get("X-Content-Type-Options") == "nosniff"
    assert r_doc.headers.get("X-Frame-Options") == "DENY"

    r_auth = tc.get("/api/auth/status")
    assert r_auth.status_code == 200 and r_auth.json()["authenticated"] is True

    r_eng = tc.get("/api/system/engine?check_remote=false")
    assert r_eng.status_code == 200
    assert r_eng.json()["recommended_backend"] in {"cuda", "rocm", "vulkan", "metal", "cpu"}

    r_upd = tc.get("/api/system/update-status?fetch=false")
    assert r_upd.status_code == 200
    assert r_upd.json()["version"] == report.version


def test_engine_asset_selection_and_tag_parsing() -> None:
    assert parse_llama_build_tag("version: 1.0.0-dev (build 12359, commit b1b770d90d)") == "b12359"
    assert parse_llama_build_tag("version: 5432 (abc1234)") == "b5432"

    # Real ggml-org/llama.cpp asset naming catalog
    sample_assets = [
        {"name": n, "browser_download_url": f"https://example.invalid/{n}"}
        for n in [
            "cudart-llama-b12627-bin-ubuntu-cuda-12.8-x64.tar.gz",
            "cudart-llama-b12627-bin-ubuntu-cuda-13.5-x64.tar.gz",
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
            "cudart-llama-bin-win-cuda-13.5-x64.zip",
            "llama-b12627-bin-macos-arm64.tar.gz",
            "llama-b12627-bin-macos-x64.tar.gz",
            "llama-b12627-bin-ubuntu-cuda-12.8-x64.tar.gz",
            "llama-b12627-bin-ubuntu-cuda-13.5-x64.tar.gz",
            "llama-b12627-bin-ubuntu-rocm-10.0-x64.tar.gz",
            "llama-b12627-bin-ubuntu-vulkan-x64.tar.gz",
            "llama-b12627-bin-ubuntu-x64.tar.gz",
            "llama-b12627-bin-win-cpu-x64.zip",
            "llama-b12627-bin-win-cuda-12.4-x64.zip",
            "llama-b12627-bin-win-cuda-13.5-x64.zip",
            "llama-b12627-bin-win-vulkan-x64.zip",
        ]
    ]

    m, rt = select_llama_assets(
        sample_assets, os_name="linux", arch="x86_64", backend="cuda", cuda_version="13.5"
    )
    assert m and m["name"] == "llama-b12627-bin-ubuntu-cuda-13.5-x64.tar.gz"
    assert rt and rt["name"] == "cudart-llama-b12627-bin-ubuntu-cuda-13.5-x64.tar.gz"

    # Older driver (CUDA 12.9) must pick cuda-12.8 instead of cuda-13.5
    m2, rt2 = select_llama_assets(
        sample_assets, os_name="linux", arch="x86_64", backend="cuda", cuda_version="12.9"
    )
    assert m2 and m2["name"] == "llama-b12627-bin-ubuntu-cuda-12.8-x64.tar.gz"
    assert rt2 and rt2["name"] == "cudart-llama-b12627-bin-ubuntu-cuda-12.8-x64.tar.gz"

    m_mac, rt_mac = select_llama_assets(
        sample_assets, os_name="Darwin", arch="arm64", backend="metal"
    )
    assert m_mac and m_mac["name"] == "llama-b12627-bin-macos-arm64.tar.gz" and rt_mac is None

    m_rocm, _ = select_llama_assets(sample_assets, os_name="linux", arch="x64", backend="rocm")
    assert m_rocm and "rocm" in m_rocm["name"]

    m_cpu, _ = select_llama_assets(sample_assets, os_name="linux", arch="x64", backend="cpu")
    assert m_cpu and m_cpu["name"] == "llama-b12627-bin-ubuntu-x64.tar.gz"

    m_win, rt_win = select_llama_assets(
        sample_assets, os_name="Windows", arch="AMD64", backend="cuda", cuda_version="12.6"
    )
    assert m_win and m_win["name"] == "llama-b12627-bin-win-cuda-12.4-x64.zip"
    assert rt_win and rt_win["name"] == "cudart-llama-bin-win-cuda-12.4-x64.zip"


def test_managed_llama_archive_install_shim_and_tarslip_guard(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "data_root"
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(data_root))

    # Build a realistic tarball containing nested bin/llama-server + cudart lib
    src = tmp_path / "pkg" / "build" / "bin"
    src.mkdir(parents=True)
    fake_bin = src / "llama-server"
    fake_bin.write_text("#!/usr/bin/env sh\necho 'version: 1.0.0-dev (build 12627, commit abc)'\n")
    fake_bin.chmod(0o755)
    main_tar = tmp_path / "llama-b12627-bin-ubuntu-cuda-13.5-x64.tar.gz"
    with tarfile.open(main_tar, "w:gz") as tf:
        tf.add(fake_bin, arcname="build/bin/llama-server")

    lib_src = tmp_path / "rt" / "libcudart.so.13"
    lib_src.parent.mkdir(parents=True)
    lib_src.write_bytes(b"ELF")
    rt_tar = tmp_path / "cudart-llama-b12627-bin-ubuntu-cuda-13.5-x64.tar.gz"
    with tarfile.open(rt_tar, "w:gz") as tf:
        tf.add(lib_src, arcname="libcudart.so.13")

    res = install_llama_from_archives(
        tag="b12627",
        main_archive=main_tar,
        cudart_archive=rt_tar,
        backend="cuda",
        bin_dir=bin_dir,
    )
    assert res.ok is True and res.action == "install" and res.current_tag == "b12627"
    assert (Path(res.binary_path).parent / "libcudart.so.13").exists()
    shim = Path(res.shim_path)
    assert shim.exists() and os.access(shim, os.X_OK)

    out = subprocess.run([str(shim), "--version"], capture_output=True, text=True, check=True)
    assert "build 12627" in out.stdout

    st = get_engine_status(check_remote=False)
    assert any(e.managed and e.build_tag == "b12627" for e in st.installed)

    # TarSlip traversal archive must be rejected cleanly without corrupting existing install
    evil_tar = tmp_path / "evil.tar.gz"
    evil_file = tmp_path / "pwn.txt"
    evil_file.write_text("bad")
    with tarfile.open(evil_tar, "w:gz") as tf:
        tf.add(evil_file, arcname="../../escape.txt")
    bad = install_llama_from_archives(tag="b99999", main_archive=evil_tar, bin_dir=bin_dir)
    assert bad.ok is False and "unsafe" in (bad.error or "").lower()
    assert Path(res.binary_path).exists()


def test_git_check_and_perform_update_end_to_end(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    clone = tmp_path / "clone"

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Splitch Test",
        "GIT_AUTHOR_EMAIL": "test@splitch.local",
        "GIT_COMMITTER_NAME": "Splitch Test",
        "GIT_COMMITTER_EMAIL": "test@splitch.local",
    }

    def git(*args: str, cwd: Path) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    origin.mkdir()
    git("init", "--bare", "--initial-branch=main", cwd=origin)
    work.mkdir()
    git("init", "--initial-branch=main", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    (work / "pyproject.toml").write_text('[project]\nname = "splitch"\n', encoding="utf-8")
    git("add", "pyproject.toml", cwd=work)
    git("commit", "-m", "chore: initial commit", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)

    git("clone", "--branch", "main", str(origin), str(clone), cwd=tmp_path)

    # Already up to date initially
    st0 = check_update(app_dir=clone, branch="main", fetch_remote=True)
    assert st0.is_git_repo is True
    assert st0.update_available is False
    assert st0.behind_count == 0

    # Push a new commit to origin
    (work / "CHANGELOG.md").write_text("# v0.3.1\n- Added updater\n", encoding="utf-8")
    git("add", "CHANGELOG.md", cwd=work)
    git("commit", "-m", "feat: add changelog", cwd=work)
    git("push", "origin", "main", cwd=work)

    # Dirty working tree blocks update without force
    (clone / "local_untracked.txt").write_text("dirty", encoding="utf-8")
    blocked = perform_update(
        app_dir=clone,
        branch="main",
        agent_only=True,
        restart_services=False,
        sync_deps=False,
    )
    assert blocked.ok is False
    assert "uncommitted" in (blocked.error or "").lower()
    (clone / "local_untracked.txt").unlink()

    # Check-only dry run reports 1 commit behind without mutating files
    dry = perform_update(
        app_dir=clone,
        branch="main",
        agent_only=True,
        restart_services=False,
        sync_deps=False,
        check_only=True,
    )
    assert dry.ok is True and dry.updated is False and dry.commits_pulled == 1
    assert not (clone / "CHANGELOG.md").exists()

    # Real fast-forward update pulls the commit cleanly
    res = perform_update(
        app_dir=clone,
        branch="main",
        agent_only=True,
        restart_services=False,
        sync_deps=False,
    )
    assert res.ok is True
    assert res.updated is True
    assert res.commits_pulled == 1
    assert res.previous_sha != res.current_sha
    assert (clone / "CHANGELOG.md").exists()


def test_install_sh_syntax_and_local_install_uninstall(tmp_path: Path) -> None:
    install_sh = REPO_ROOT / "install.sh"
    assert install_sh.exists()

    # Verify strict POSIX sh syntax
    chk = subprocess.run(["sh", "-n", str(install_sh)], capture_output=True, text=True, check=False)
    assert chk.returncode == 0, chk.stderr

    # Run real --local install into isolated temp home, data & bin dirs (zero impact on real $HOME or systemd)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    bin_dir = tmp_path / "bin"
    data_root = tmp_path / "data_root"
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "XDG_CONFIG_HOME": str(fake_home / ".config"),
        "XDG_DATA_HOME": str(fake_home / ".local" / "share"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "DBUS_SESSION_BUS_ADDRESS": "",
        "SPLITCH_DATA_ROOT": str(data_root),
    }

    inst = subprocess.run(
        [
            "sh",
            str(install_sh),
            "--local",
            "--no-ui-build",
            "--no-desktop",
            "--engine",
            "none",
            "--bin-dir",
            str(bin_dir),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert inst.returncode == 0, inst.stdout + "\n" + inst.stderr

    shim = bin_dir / "splitch"
    assert shim.exists() and os.access(shim, os.X_OK)
    meta_path = data_root / "install_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["mode"] == "local"
    assert meta["repo_url"] == DEFAULT_REPO_URL
    assert DEFAULT_REPO_URL == "https://github.com/RaveAgainstTheMachine/splitch.git"
    assert DEFAULT_REPO_URL in (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert Path(meta["install_dir"]).resolve() == REPO_ROOT.resolve()

    # Verify installed global shim actually executes `splitch version`
    ver = subprocess.run([str(shim), "version"], capture_output=True, text=True, timeout=15, check=False)
    assert ver.returncode == 0 and "splitch " in ver.stdout

    # Verify --uninstall cleans up shim & metadata cleanly
    uninst = subprocess.run(
        ["sh", str(install_sh), "--uninstall", "--bin-dir", str(bin_dir)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert uninst.returncode == 0, uninst.stderr
    assert not shim.exists()
    assert not meta_path.exists()
    assert "[1/7]" in inst.stdout and "[7/7]" in inst.stdout


def test_terminal_progress_bar_spinner_and_osc_sequences() -> None:
    import io

    from backend.shared.progress import TerminalProgress

    # TTY mode: emits Unicode bar, OSC 9;4 progress, and clears OSC on exit
    buf = io.StringIO()
    prog = TerminalProgress(stream=buf, force_tty=True, enable_osc=True)
    with prog.download("llama.tar.gz", 1024 * 1024) as bar:
        bar.advance(512 * 1024)
        bar.advance(512 * 1024)
    out = buf.getvalue()
    assert "━" in out
    assert "100%" in out
    assert "\x1b]9;4;1;" in out
    assert "\x1b]9;4;0;0\x1b\\" in out

    # StepSpinner TTY mode: emits indeterminate OSC 9;4;3 and clears on completion
    sbuf = io.StringIO()
    sprog = TerminalProgress(stream=sbuf, force_tty=True, enable_osc=True)
    with sprog.step("Extracting archive", done_label="Extracted archive"):
        pass
    sout = sbuf.getvalue()
    assert "Extracted archive" in sout
    assert "\x1b]9;4;3;0\x1b\\" in sout
    assert "\x1b]9;4;0;0\x1b\\" in sout

    # Non-TTY / CI mode: clean single-line output with zero OSC or CR escape noise
    nbuf = io.StringIO()
    nprog = TerminalProgress(stream=nbuf, force_tty=False, enable_osc=True)
    with nprog.download("sd.zip", 2048) as bar:
        bar.advance(2048)
    with nprog.step("Syncing"):
        pass
    nout = nbuf.getvalue()
    assert "\x1b]" not in nout
    assert "\r" not in nout
    assert "✓ sd.zip" in nout
    assert "✓ Syncing" in nout
