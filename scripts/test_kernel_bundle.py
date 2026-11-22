#!/usr/bin/env python3
"""Check bundle_kernel_release against synthetic kernel bundles.

The kernel release must come from the bundle (kernel-ota-design 4.4 build
gate): from kernel.release when the bundle lists it, else from the single
modules tarball name; unsafe or ambiguous bundles are rejected."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build

DTB = "sky1-y4.dtb"
RELEASE = "6.6.89-cix-build-generic"


def make_bundle(tmp, name, files, extra_file=None):
    """Create a bundle directory with the listed artifacts and a matching
    SHA256SUMS; extra_file is written but left out of SHA256SUMS."""
    bundle = tmp / name
    bundle.mkdir()
    for artifact in files:
        if artifact == "kernel.release":
            (bundle / artifact).write_text(RELEASE + "\n")
        else:
            (bundle / artifact).write_bytes(b"payload:" + artifact.encode())
    (bundle / "SHA256SUMS").write_text("".join(
        build.sha(bundle / n) + "  " + n + "\n" for n in files))
    if extra_file is not None:
        (bundle / extra_file).write_text("stray\n")
    return bundle


def parse(bundle):
    sums = {}
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(None, 1)
        sums[name.strip()] = digest
    return sums


def expect_ok(tmp, name, files, want, want_source, **kwargs):
    bundle = make_bundle(tmp, name, files, **kwargs)
    version, source = build.bundle_kernel_release(bundle, parse(bundle))
    assert version == want, (name, version)
    assert source == want_source, (name, source)


def expect_fail(tmp, name, files, **kwargs):
    bundle = make_bundle(tmp, name, files, **kwargs)
    try:
        build.bundle_kernel_release(bundle, parse(bundle))
    except ValueError:
        return
    raise AssertionError("bundle was accepted: " + name)


def main():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        artifacts = ["Image", DTB,
                     "modules-" + RELEASE + ".tar.zst",
                     "System.map-" + RELEASE,
                     "config-" + RELEASE]
        # A current bundle names the release in kernel.release and SHA256SUMS.
        expect_ok(tmp, "new-style", artifacts + ["kernel.release"], RELEASE, "kernel.release")
        # A bundle produced before kernel.release existed still parses.
        expect_ok(tmp, "legacy", artifacts, RELEASE, "SHA256SUMS")
        # An unlisted kernel.release file is unverified content: refuse.
        expect_fail(tmp, "unlisted-release", artifacts, extra_file="kernel.release")
        # Ambiguous module tarballs are refused.
        expect_fail(tmp, "two-modules",
                    ["Image", DTB, "modules-1.0.tar.zst", "modules-2.0.tar.zst",
                     "System.map-x", "config-x"])
        # An unsafe release string is refused.
        escape = tmp / "escape"
        escape.mkdir()
        (escape / "kernel.release").write_text("../evil\n")
        (escape / "SHA256SUMS").write_text("0" * 64 + "  kernel.release\n")
        try:
            build.bundle_kernel_release(escape, parse(escape))
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe release accepted")
        print("test_kernel_bundle: all checks passed")


if __name__ == "__main__":
    main()
