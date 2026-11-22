"""Deterministic Y4 GPU userspace installation checks (no driver replacement)."""
import hashlib
import json
import os
import stat
from pathlib import Path

GPU_LIB = Path("opt/cixgpu-pro/lib")
LD_CONF = Path("etc/ld.so.conf.d/00-cixgpu-pro.conf")
EGL_JSON = Path("usr/share/glvnd/egl_vendor.d/40_cix.json")
VULKAN_JSON = Path("usr/share/vulkan/icd.d/mali_icd.aarch64.json")
LEGACY_PROFILE = Path("etc/profile.d/rk1828.sh")


def tree_digest(path):
    """Hash names, types, modes and bytes, without following symlinks."""
    path = Path(path)
    digest = hashlib.sha256()
    if not path.exists() and not path.is_symlink():
        return None
    for entry in [path, *sorted(path.rglob("*"))]:
        info = entry.lstat()
        kind = stat.S_IFMT(info.st_mode)
        name = str(entry.relative_to(path))
        digest.update(json.dumps([name, kind, stat.S_IMODE(info.st_mode)]).encode() + b"\0")
        if entry.is_symlink():
            digest.update(os.readlink(entry).encode())
        elif entry.is_file():
            with entry.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
        elif not entry.is_dir():
            raise ValueError("Unsupported build input: " + str(entry))
        digest.update(b"\0")
    return digest.hexdigest()


def configure_graphics(root):
    """Normalize only the installed CIX library tree and owned configuration.

    The caller installs files from the exported source snapshot first, then
    runs the target architecture's ldconfig after this function.
    """
    root = Path(root).resolve()
    library = root / GPU_LIB
    if library.is_symlink() or not library.is_dir():
        raise ValueError("CIX GPU library directory missing or symlinked")
    entries = [library, *sorted(library.rglob("*"))]
    for entry in entries:
        if entry.is_symlink():
            if not entry.resolve(strict=True).is_relative_to(library):
                raise ValueError("CIX library symlink escapes its directory: " + str(entry))
        elif not entry.is_file() and not entry.is_dir():
            raise ValueError("Unexpected GPU installation entry: " + str(entry))
    for name in ("libmali.so.0", "libEGL_cix.so.1", "libgbm.so.1"):
        if not (library / name).is_file():
            raise ValueError("Required CIX library missing: " + name)
    vendor = root / EGL_JSON
    spec = json.loads(vendor.read_text())
    if spec.get("ICD", {}).get("library_path") != "/opt/cixgpu-pro/lib/libEGL_cix.so.1":
        raise ValueError("Unexpected CIX EGL vendor library path")
    profile = root / LEGACY_PROFILE
    removed_profile = profile.exists()
    if profile.exists():
        active = [line.strip() for line in profile.read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
        expected = ['export LD_LIBRARY_PATH="/lib:${LD_LIBRARY_PATH}"',
                    'export PATH="/bin:${PATH}"']
        if active != expected or "RK1828" not in profile.read_text():
            raise ValueError("Unknown rk1828.sh content; review before replacing")
    conf = root / LD_CONF
    if conf.is_symlink() or vendor.is_symlink():
        raise ValueError("GPU configuration must be a regular file")
    for parent in (root / "opt/cixgpu-pro", library):
        parent.chmod(0o755)
    for entry in entries:
        if not entry.is_symlink():
            entry.chmod(0o755 if entry.is_dir() else 0o644)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("/opt/cixgpu-pro/lib\n")
    conf.chmod(0o644)
    vendor.chmod(0o644)
    if profile.exists():
        profile.unlink()
    verify_graphics(root)
    return {"library_path": "/opt/cixgpu-pro/lib", "legacy_profile_removed": removed_profile}


def verify_graphics(root):
    root = Path(root).resolve()
    library = root / GPU_LIB
    if library.is_symlink() or not library.is_dir():
        raise ValueError("CIX GPU library directory missing or symlinked")
    for name in ("libmali.so.0", "libEGL_cix.so.1", "libgbm.so.1"):
        if not (library / name).is_file():
            raise ValueError("Required CIX library missing: " + name)
    manifest = root / VULKAN_JSON
    if manifest.resolve() != manifest or not manifest.is_file():
        raise ValueError("CIX Vulkan ICD manifest missing or symlinked")
    spec = json.loads(manifest.read_text())
    # libmali.so.0.54.1 reports 1.4.305 via its own vkEnumerateInstanceVersion.
    if (spec.get("file_format_version") != "1.0.0" or spec.get("ICD") !=
            {"library_path": "/opt/cixgpu-pro/lib/libmali.so.0", "api_version": "1.4.305"}):
        raise ValueError("Unexpected CIX Vulkan ICD library or API version")
    spec = json.loads((root / EGL_JSON).read_text())
    if spec.get("ICD", {}).get("library_path") != "/opt/cixgpu-pro/lib/libEGL_cix.so.1":
        raise ValueError("Unexpected CIX EGL vendor library path")
    if (root / LD_CONF).read_text().splitlines() != ["/opt/cixgpu-pro/lib"]:
        raise ValueError("CIX library search path does not match installation")
    for entry in [root / "opt/cixgpu-pro", library, *library.rglob("*"),
                  root / LD_CONF, root / EGL_JSON, manifest.parent.parent, manifest.parent, manifest]:
        if entry.is_symlink():
            if not entry.resolve(strict=True).is_relative_to(library):
                raise ValueError("CIX library link escapes installation")
            continue
        mode = stat.S_IMODE(entry.stat().st_mode)
        want = 0o755 if entry.is_dir() else 0o644
        if mode != want:
            raise ValueError("Unexpected graphics permissions: " + str(entry))
    if (root / LEGACY_PROFILE).exists():
        raise ValueError("Legacy RK1828 loader environment remains")
