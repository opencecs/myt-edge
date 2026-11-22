#!/usr/bin/env python3
"""Create the build tree from pinned MYT Edge, kernel and rootfs repositories."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def export(repo, revision, destination):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Source revisions must be full Git commit IDs")
    if git(repo, "rev-parse", revision + "^{commit}") != revision:
        raise ValueError("Source revision is not a pinned commit")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(repo), "-c", "tar.umask=0022", "archive", revision],
                       stdout=archive, check=True)
        archive.seek(0)
        subprocess.run(["tar", "-xf", "-", "-C", str(destination)], stdin=archive, check=True)


def assemble(output, edge, kernel, rootfs, grub):
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Source assembly output must be new")
    repos = {"myt-edge": edge, "cix-kernel": kernel, "cix-rootfs": rootfs}
    if any(output.is_relative_to(repo.resolve()) for repo, _ in repos.values()):
        raise ValueError("Source assembly output must be outside the source repositories")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".myt-assembly-", dir=output.parent) as scratch:
        work = Path(scratch) / "sources"
        export(*edge, work)
        export(*kernel, work / "kernel/linux")
        export(*rootfs, work / "rootfs")
        if grub.is_symlink() or not grub.is_file():
            raise ValueError("GRUB input must be a regular ARM64 EFI executable")
        expected = (work / "boot/grub/BOOTAA64.EFI.sha256").read_text().split()[0]
        if hashlib.sha256(grub.read_bytes()).hexdigest() != expected:
            raise ValueError("GRUB executable differs from the committed SHA256")
        subprocess.run(["grub-file", "--is-arm64-efi", str(grub)], check=True)
        shutil.copyfile(grub, work / "boot/grub/BOOTAA64.EFI")
        for name in ("debian.config", "patches.json"):
            source = work / "kernel/linux" / name
            if source.is_file():
                shutil.copyfile(source, work / "kernel" / name)
        if not (work / "kernel/patches.json").exists():
            # The standalone CIX kernel already contains the board patches.
            (work / "kernel/patches.json").write_text('{"patches":[]}\n')
        sources = {name: revision for name, (_, revision) in repos.items()}
        (work / "SOURCES.json").write_text(json.dumps(sources, sort_keys=True, indent=2) + "\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        git(work, "add", "-f", ".")
        git(work, "-c", "user.name=MYT Source Assembly", "-c", "user.email=build@localhost",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Assemble pinned system components")
        work.rename(output)
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--edge-ref")
    for name in ("kernel", "rootfs"):
        parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--" + name + "-ref", required=True)
    parser.add_argument("--grub", required=True, type=Path)
    args = parser.parse_args()
    sources = assemble(args.output, (ROOT, args.edge_ref or git(ROOT, "rev-parse", "HEAD")),
                       (args.kernel, args.kernel_ref), (args.rootfs, args.rootfs_ref), args.grub)
    print(json.dumps(sources, indent=2))


if __name__ == "__main__":
    main()
