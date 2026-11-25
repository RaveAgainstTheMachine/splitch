"""Tests for Splitch installer scripts, environment doctor, and git self-updater."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
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
    perform_rollback,
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


def test_git_check_and_perform_update_end_to_end(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "data_root"))
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


def test_updater_auto_rollback_on_sync_failure_and_manual_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    from backend.shared import updater as upd_mod

    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "data_root"))
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
    git("commit", "-m", "chore: initial v1", cwd=work)
    sha1_full = git("rev-parse", "HEAD", cwd=work)
    sha1_short = git("rev-parse", "--short", "HEAD", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)

    git("clone", "--branch", "main", str(origin), str(clone), cwd=tmp_path)

    # Push broken v2 commit
    (work / "BROKEN.txt").write_text("bad release\n", encoding="utf-8")
    git("add", "BROKEN.txt", cwd=work)
    git("commit", "-m", "feat: broken v2", cwd=work)
    sha2_full = git("rev-parse", "HEAD", cwd=work)
    git("push", "origin", "main", cwd=work)

    # Simulate uv sync failing after git pull -> must auto-rollback to sha1
    orig_run = upd_mod._run
    orig_which = upd_mod.shutil.which
    uv_calls = {"count": 0}

    def failing_uv_run(cmd: list[str], **kwargs):
        if len(cmd) >= 2 and Path(cmd[0]).name == "uv" and cmd[1] == "sync":
            uv_calls["count"] += 1
            if uv_calls["count"] == 1:
                return 1, "", "simulated dependency resolution failure"
            return 0, "", ""
        return orig_run(cmd, **kwargs)

    monkeypatch.setattr(upd_mod, "_run", failing_uv_run)
    monkeypatch.setattr(
        upd_mod.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else orig_which(name)
    )

    failed = perform_update(
        app_dir=clone,
        branch="main",
        agent_only=True,
        restart_services=False,
        sync_deps=True,
    )
    assert failed.ok is False
    assert failed.rolled_back is True
    assert failed.updated is False
    assert failed.current_sha == sha1_short
    assert git("rev-parse", "HEAD", cwd=clone) == sha1_full
    assert not (clone / "BROKEN.txt").exists()

    # Restore normal _run, apply clean update, then test manual `perform_rollback()`
    monkeypatch.setattr(upd_mod, "_run", orig_run)
    ok_res = perform_update(
        app_dir=clone,
        branch="main",
        agent_only=True,
        restart_services=False,
        sync_deps=False,
    )
    assert ok_res.ok is True and ok_res.updated is True
    assert git("rev-parse", "HEAD", cwd=clone) == sha2_full
    assert (clone / "BROKEN.txt").exists()

    rb = perform_rollback(
        app_dir=clone,
        agent_only=True,
        restart_services=False,
        sync_deps=False,
    )
    assert rb.ok is True
    assert rb.rolled_back is True
    assert rb.current_sha == sha1_short
    assert git("rev-parse", "HEAD", cwd=clone) == sha1_full
    assert not (clone / "BROKEN.txt").exists()


def test_perform_update_requires_signed_commit_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "data_root"))
    monkeypatch.setenv("SPLITCH_REQUIRE_SIGNED_UPDATES", "1")

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    clone = tmp_path / "clone"

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Splitch Test",
        "GIT_AUTHOR_EMAIL": "test@splitch.local",
        "GIT_COMMITTER_NAME": "Splitch Test",
        "GIT_COMMITTER_EMAIL": "test@splitch.local",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
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
    git("-c", "commit.gpgsign=false", "commit", "-m", "chore: initial commit", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)

    git("clone", "--branch", "main", str(origin), str(clone), cwd=tmp_path)
    old_full_sha = git("rev-parse", "HEAD", cwd=clone)
    old_short_sha = git("rev-parse", "--short", "HEAD", cwd=clone)

    # Push an unsigned update commit to the bare remote
    (work / "UNSIGNED.txt").write_text("unsigned update\n", encoding="utf-8")
    git("add", "UNSIGNED.txt", cwd=work)
    git("-c", "commit.gpgsign=false", "commit", "-m", "feat: unsigned update", cwd=work)
    git("push", "origin", "main", cwd=work)

    res = perform_update(
        app_dir=clone,
        branch="main",
        agent_only=True,
        restart_services=False,
        sync_deps=False,
    )
    assert res.ok is False
    assert "Signed update verification failed" in (res.error or "")
    assert res.updated is False
    assert res.previous_sha == old_short_sha
    assert res.current_sha == old_short_sha
    assert git("rev-parse", "HEAD", cwd=clone) == old_full_sha
    assert not (clone / "UNSIGNED.txt").exists()


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
    ps1_text = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    sh_text = install_sh.read_text(encoding="utf-8")
    assert DEFAULT_REPO_URL in ps1_text
    assert "verify_sha256" in sh_text and "pinned_uv_sha256" in sh_text and "pinned_node_sha256" in sh_text
    assert "f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f" in sh_text
    assert "69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec" in sh_text
    assert "astral.sh/uv/install.sh | sh" not in sh_text
    assert "Assert-Sha256Match" in ps1_text and "$PinnedUvSha256" in ps1_text and "$PinnedNodeSha256" in ps1_text
    assert "a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a" in ps1_text
    assert "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217" in ps1_text
    assert "astral.sh/uv/install.ps1 | iex" not in ps1_text
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


def test_tray_action_check_updates_and_snapshot_indicator(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from backend.desktop import tray as tray_mod
    from backend.shared.engines import EngineStatus
    from backend.shared.updater import UpdateStatus

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tray_mod,
        "send_desktop_notification",
        lambda title, msg: notifications.append((title, msg)),
    )
    monkeypatch.setattr(
        tray_mod,
        "get_engine_status",
        lambda check_remote=True: EngineStatus(
            platform="linux",
            arch="x86_64",
            recommended_backend="cpu",
            latest_llama_tag="b9000",
            update_available=False,
        ),
    )

    ctrl = tray_mod.TrayController("http://127.0.0.1:8080")

    # 1) Update available path: uses behind_count (never commits_behind)
    monkeypatch.setattr(
        tray_mod,
        "check_update",
        lambda fetch_remote=True: UpdateStatus(
            version="0.10.3",
            app_dir="/tmp/splitch",
            local_sha="1111111",
            remote_sha="2222222",
            behind_count=4,
            update_available=True,
        ),
    )
    ctrl.action_check_updates()
    for _ in range(50):
        if "4 behind" in ctrl.update_note:
            break
        time.sleep(0.02)
    assert "4 behind" in ctrl.update_note
    assert any("4 behind" in msg for _, msg in notifications)

    # 2) Up-to-date path: uses local_sha (never current_sha)
    monkeypatch.setattr(
        tray_mod,
        "check_update",
        lambda fetch_remote=True: UpdateStatus(
            version="0.10.3",
            app_dir="/tmp/splitch",
            local_sha="abcdef1",
            remote_sha="abcdef1",
            behind_count=0,
            update_available=False,
        ),
    )
    ctrl.action_check_updates()
    for _ in range(50):
        if "abcdef1" in ctrl.update_note:
            break
        time.sleep(0.02)
    assert "abcdef1" in ctrl.update_note

    # 3) Live /ws/status snapshot update_summary indicator
    ctrl.apply_snapshot(
        {
            "version": "0.10.3",
            "nodes": [],
            "control": {"gaming_mode": False, "force_node": "auto"},
            "update_summary": {
                "update_available": True,
                "behind_count": 2,
                "local_sha": "1111111",
                "remote_sha": "3333333",
                "checked_at": time.time(),
            },
        },
        sync_desktop=False,
    )
    assert ctrl.update_note == "Update available (2 commits)"
    _, body = ctrl.tooltip_summary()
    assert "Update available (2 commits)" in body

    ctrl.apply_snapshot(
        {
            "version": "0.10.3",
            "nodes": [],
            "control": {"gaming_mode": False, "force_node": "auto"},
            "update_summary": {
                "update_available": False,
                "behind_count": 0,
                "local_sha": "3333333",
                "remote_sha": "3333333",
                "checked_at": time.time(),
            },
        },
        sync_desktop=False,
    )
    assert ctrl.update_note == ""


def test_coordinated_cluster_update_success_and_partial_failure_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from backend.router import app as router_mod
    from backend.shared.models import (
        ClusterUpdateJob,
        ClusterUpdateRequest,
        NodeStatus,
    )
    from backend.shared.updater import UpdateResult

    old_nodes = dict(router_mod.cluster.nodes)
    try:
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes["worker-a"] = NodeStatus(
            node_id="worker-a",
            display_name="Worker A",
            role="gpu",
            agent_url="http://worker-a.example:8090",
            engine_url="http://worker-a.example:8081",
            online=True,
        )
        router_mod.cluster.nodes["worker-b"] = NodeStatus(
            node_id="worker-b",
            display_name="Worker B",
            role="gpu",
            agent_url="http://worker-b.example:8090",
            engine_url="http://worker-b.example:8081",
            online=True,
        )

        # Case A: Both workers succeed -> router update runs and marks restarting
        calls: list[tuple[str, str, dict]] = []
        router_calls = 0

        async def _fake_call_ok(_client, node, endpoint, payload):
            calls.append((node.node_id, endpoint, payload))
            return {
                "ok": True,
                "updated": True,
                "rolled_back": False,
                "previous_sha": "aaa1111",
                "current_sha": "bbb2222",
                "steps": ["Pulled 1 commit"],
                "error": None,
            }

        def _fake_router_update(**_kwargs):
            nonlocal router_calls
            router_calls += 1
            return UpdateResult(
                ok=True,
                updated=True,
                previous_sha="aaa1111",
                current_sha="bbb2222",
                steps=["Pulled 1 commit"],
            )

        monkeypatch.setattr(router_mod, "_call_worker_agent_system", _fake_call_ok)
        monkeypatch.setattr(router_mod, "perform_update", _fake_router_update)

        job_ok = ClusterUpdateJob(id="job-ok", action="update", status="running")
        asyncio.run(
            router_mod._run_cluster_update_job(
                job_ok, ClusterUpdateRequest(with_engine=False, rollback_on_failure=True)
            )
        )
        assert job_ok.status == "restarting"
        assert job_ok.router_restarting is True
        assert router_calls == 1
        assert {c[0] for c in calls} == {"worker-a", "worker-b"}

        # Case B: worker-a succeeds, worker-b fails -> router aborted, worker-a rolled back
        calls.clear()
        router_calls = 0

        async def _fake_call_partial(_client, node, endpoint, payload):
            calls.append((node.node_id, endpoint, payload))
            if endpoint == "/system/rollback":
                return {
                    "ok": True,
                    "updated": True,
                    "rolled_back": True,
                    "previous_sha": "bbb2222",
                    "current_sha": "aaa1111",
                    "steps": ["Rolled back to aaa1111"],
                    "error": None,
                }
            if node.node_id == "worker-a":
                return {
                    "ok": True,
                    "updated": True,
                    "rolled_back": False,
                    "previous_sha": "aaa1111",
                    "current_sha": "bbb2222",
                    "steps": ["Pulled 1 commit"],
                    "error": None,
                }
            return {
                "ok": False,
                "updated": False,
                "rolled_back": True,
                "previous_sha": "aaa1111",
                "current_sha": "aaa1111",
                "steps": ["uv sync failed"],
                "error": "uv sync failed",
            }

        monkeypatch.setattr(router_mod, "_call_worker_agent_system", _fake_call_partial)

        job_fail = ClusterUpdateJob(id="job-fail", action="update", status="running")
        asyncio.run(
            router_mod._run_cluster_update_job(
                job_fail, ClusterUpdateRequest(with_engine=False, rollback_on_failure=True)
            )
        )
        assert job_fail.status == "aborted"
        assert job_fail.router_restarting is False
        assert job_fail.rolled_back is True
        assert router_calls == 0
        rb_calls = [c for c in calls if c[1] == "/system/rollback"]
        assert len(rb_calls) == 1
        assert rb_calls[0][0] == "worker-a"
        assert rb_calls[0][2]["to_sha"] == "aaa1111"
    finally:
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes.update(old_nodes)


def test_mlx_engine_install_platform_gate_and_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.shared import engines as eng_mod

    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))

    # Non-macOS host is cleanly rejected
    monkeypatch.setattr(eng_mod, "_norm_os", lambda *_a: "linux")
    monkeypatch.setattr(eng_mod, "_norm_arch", lambda *_a: "x64")
    res_linux = eng_mod.install_or_update_engine("mlx-lm", dry_run=True)
    assert res_linux.ok is False
    assert "Apple Silicon" in (res_linux.error or "")

    # macOS arm64 dry-run resolves PyPI version and plans isolated venv + shim
    monkeypatch.setattr(eng_mod, "_norm_os", lambda *_a: "macos")
    monkeypatch.setattr(eng_mod, "_norm_arch", lambda *_a: "arm64")
    monkeypatch.setattr(eng_mod, "fetch_latest_mlx_version", lambda: "0.24.1")
    res_mac = eng_mod.install_or_update_engine("mlx-lm", dry_run=True, bin_dir=tmp_path / "bin")
    assert res_mac.ok is True
    assert res_mac.action == "dry-run"
    assert res_mac.backend == "metal"
    assert res_mac.current_tag == "0.24.1"
    assert any("splitch-mlx-server" in s for s in res_mac.steps)


def test_mlx_agent_discovery_local_scan_and_router_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    import httpx

    from backend.agent import main as agent_mod
    from backend.shared.mlx_server import discover_mlx_models

    # 1) Local MLX model directory scan & discovery
    mlx_dir = tmp_path / "mlx" / "mlx-community--Qwen3-4B-4bit"
    mlx_dir.mkdir(parents=True)
    (mlx_dir / "config.json").write_text('{"model_type":"qwen3"}', encoding="utf-8")
    (mlx_dir / "model.safetensors").write_bytes(b"\x00" * 1024)

    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))
    scanned = agent_mod._scan_local_mlx_models()
    assert len(scanned) == 1
    assert scanned[0].filename == "mlx-community--Qwen3-4B-4bit"
    assert scanned[0].format == "mlx"

    discovered = discover_mlx_models(tmp_path / "mlx")
    assert "mlx-community/Qwen3-4B-4bit" in discovered

    # 2) Agent _collect_engine_models detects mlx-lm via /health + /v1/models
    def _mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "engine": "mlx-lm"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "mlx-community/Qwen3-4B-4bit",
                            "owned_by": "mlx-lm",
                            "status": {"value": "loaded"},
                        }
                    ],
                },
            )
        return httpx.Response(404)

    async def _probe():
        transport = httpx.MockTransport(_mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await agent_mod._collect_engine_models(client)

    monkeypatch.setattr(agent_mod, "ENGINE_URL", "http://mlx.mock:8081")
    healthy, kind, models, _ = asyncio.run(_probe())
    assert healthy is True
    assert kind == "mlx-lm"
    assert len(models) == 1
    assert models[0].id == "mlx-community/Qwen3-4B-4bit"
    assert models[0].status == "loaded"

    # 3) Router GET /api/models/search?format=mlx forwards filter=mlx to HF
    seen_params: list[str] = []

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list:
            return []

    class _FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a) -> None:
            return None

        async def get(self, _url: str, params=None):
            seen_params.extend([f"{k}={v}" for k, v in (params or [])])
            return _FakeResp()

    from backend.router import models_api as models_api_mod

    monkeypatch.setattr(models_api_mod.httpx, "AsyncClient", _FakeAsyncClient)
    tc = TestClient(router_app)
    r = tc.get("/api/models/search?q=qwen&format=mlx")
    assert r.status_code == 200
    assert "filter=mlx" in seen_params


def test_strata_process_allowlist_and_engine_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx

    from backend.agent import main as agent_mod
    from backend.shared.gpu_probe import parse_nvidia_pmon

    # 1) strata-server VRAM is classified as LLM (is_llm == True)
    procs = parse_nvidia_pmon(
        "# gpu pid type fb mem enc dec jpg ofa command\n"
        "0 4242 C 14336 - 0 0 0 0 strata-server\n"
    )
    assert len(procs) == 1
    assert procs[0].is_llm is True

    # 2) Agent _collect_engine_models detects engine=="strata" via /health and /v1/models
    def _mock_health_strata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "engine": "strata"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "Qwen3.8-Flash-Next"}]},
            )
        return httpx.Response(404)

    def _mock_models_strata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "strata", "owned_by": "strata"}],
                },
            )
        return httpx.Response(404)

    async def _probe(handler) -> tuple:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await agent_mod._collect_engine_models(client)

    monkeypatch.setattr(agent_mod, "ENGINE_URL", "http://strata.mock:8081")
    h1, k1, m1, _ = asyncio.run(_probe(_mock_health_strata))
    assert h1 is True and k1 == "strata" and m1[0].id == "Qwen3.8-Flash-Next"
    assert m1[0].status == "loaded"

    h2, k2, m2, _ = asyncio.run(_probe(_mock_models_strata))
    assert h2 is True and k2 == "strata" and m2[0].id == "strata"
