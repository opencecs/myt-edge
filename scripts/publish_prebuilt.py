#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Publish approved full-build inputs to the public MYT Edge main branch."""

import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from spi_bundle import BOARDS, MAX_BUNDLE_SIZE, read_spi_bundle

ROOT = Path(__file__).resolve().parents[1]
MAX_GIT_FILE_BYTES = 100 * 1024 * 1024


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_regular(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Invalid release input: " + path.name)
    return path.read_bytes()


def verify_mcu_inputs(components, board, trust_key):
    directory = components / "mcu"
    if directory.is_symlink():
        raise ValueError("Expected a regular MCU assets directory")
    manifest = read_regular(directory / "manifest.json", 65536)
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()

    spec = importlib.util.spec_from_file_location("public_mcu_assets", ROOT / "board/mcu_assets.py")
    assets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assets)
    files = assets.load_assets(directory, board, manifest_sha256)
    if files["mcu-public-key.bin"] != read_regular(trust_key, 32):
        raise ValueError("MCU trust key must match the production public key")
    metadata = json.loads(files["mcu-builtin.json"])
    sequence = metadata["sequence"]
    if (metadata.get("signed") is not True or metadata.get("board", "Y4") != board
            or type(sequence) is not int or not 1 <= sequence < 2**64
            or hashlib.sha256(files["mcu-package.bin"]).hexdigest() != metadata["package_sha256"]):
        raise ValueError("Invalid signed MCU package metadata for " + board)

    files["manifest.json"] = manifest
    return files, metadata, manifest_sha256


def stage_components(repository, components, board):
    """Stage allowlisted component bytes after full release verification."""
    if board not in BOARDS:
        raise ValueError("Unknown public component board")
    repository = Path(repository).resolve(strict=True)
    components = Path(components).resolve(strict=True)
    trust = repository / "board/trust/y4-mcu-production.pub"
    files, metadata, mcu_sha256 = verify_mcu_inputs(components, board, trust)
    spi = read_regular(components / "myt-spi.bin", MAX_BUNDLE_SIZE)
    read_spi_bundle(spi, board)
    panel_json = read_regular(components / "panel.json", 16384)
    panel = components / "myt_desktop"
    if panel.is_symlink() or not panel.is_file():
        raise ValueError("Expected a regular management panel binary")
    # build imports this publisher only after finishing the private build.
    from build import verify_panel_binary
    panel_record = verify_panel_binary(panel, json.loads(panel_json))

    public = repository / "prebuilt"
    target = public / board
    index = public / "manifest.json"
    if public.is_symlink() or target.is_symlink():
        raise ValueError("Invalid prebuilt components destination")
    data = json.loads(read_regular(index, 16384)) if index.exists() else {"schema": 1, "boards": {}}
    if data.get("schema") != 1 or not set(data["boards"]) <= set(BOARDS):
        raise ValueError("Unsupported prebuilt components manifest")
    if target.exists():
        old = json.loads(read_regular(target / "mcu/mcu-builtin.json", 16384))
        if metadata["sequence"] < old["sequence"] or (
                metadata["sequence"] == old["sequence"] and metadata["package_sha256"] != old["package_sha256"]):
            raise ValueError(f"{board} MCU sequence reuses or predates the published package")

    public.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prebuilt-", dir=public) as scratch:
        pending = Path(scratch)
        board_input = pending / board
        board_input.mkdir()
        (board_input / "myt-spi.bin").write_bytes(spi)
        for name, content in sorted(files.items()):
            path = board_input / "mcu" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755 if name.startswith("host/") else 0o644)

        panel_archive = pending / "myt_desktop.gz"
        copied_hash = hashlib.sha256()
        with panel.open("rb") as stream, panel_archive.open("wb") as destination:
            with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
                while block := stream.read(4 * 1024 * 1024):
                    copied_hash.update(block)
                    compressed.write(block)
        if copied_hash.hexdigest() != panel_record["sha256"]:
            raise ValueError("Panel binary changed during compression")
        if not 0 < panel_archive.stat().st_size <= MAX_GIT_FILE_BYTES:
            raise ValueError("Compressed panel exceeds the regular Git file size limit")

        (pending / "panel.json").write_bytes(panel_json)
        data["boards"][board] = {"spi_sha256": hashlib.sha256(spi).hexdigest(),
                                 "mcu_manifest_sha256": mcu_sha256}
        data["panel"] = {"manifest_sha256": hashlib.sha256(panel_json).hexdigest(),
                         "gzip_sha256": digest(panel_archive)}
        (pending / "manifest.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        if target.exists():
            shutil.rmtree(target)
        board_input.rename(target)
        os.replace(panel_archive, public / "myt_desktop.gz")
        os.replace(pending / "panel.json", public / "panel.json")
        os.replace(pending / "manifest.json", index)


def git(repository, operation, *args):
    result = subprocess.run(
        ["git", "-C", str(repository), operation, *map(str, args)],
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        # Do not include command arguments or remote errors in the build log.
        raise RuntimeError(f"Public component Git {operation} failed with exit {result.returncode}")
    return result.stdout.strip()


def publish(repo, components, board, version):
    """Push a verified full build's inputs to main and return the public commit."""
    if board not in BOARDS or not re.fullmatch(r"[0-9]{8}-r[0-9]+", version):
        raise ValueError("Expected a supported board and full build version")
    repo = Path(repo).resolve(strict=True)
    git_dir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    with (git_dir / "myt-prebuilt-publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remote = git(repo, "remote", "get-url", "--push", "origin")
        git(repo, "fetch", "--quiet", "--no-tags", remote, "refs/heads/main:refs/remotes/origin/main")
        with tempfile.TemporaryDirectory(prefix="myt-public-components-") as scratch:
            work = Path(scratch) / "tree"
            git(repo, "worktree", "add", "--quiet", "--detach", work, "refs/remotes/origin/main")
            try:
                index = json.loads(read_regular(work / "prebuilt/manifest.json", 16384))
                if set(index["boards"]) != set(BOARDS):
                    raise ValueError("Public main must already contain inputs for both supported boards")
                stage_components(work, components, board)
                paths = ("prebuilt/" + board, "prebuilt/manifest.json", "prebuilt/panel.json",
                         "prebuilt/myt_desktop.gz")
                git(work, "add", "-A", "--", *paths)
                if git(work, "diff", "--cached", "--name-only", "--", *paths):
                    git(work, "commit", "--quiet", "-m", f"Update {board} build inputs for {version}")
                    git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
                return git(work, "rev-parse", "HEAD")
            finally:
                git(repo, "worktree", "remove", "--force", work)
