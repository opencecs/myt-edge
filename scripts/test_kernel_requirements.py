#!/usr/bin/env python3
"""Real policy and Builder integration tests; no firmware/image construction."""
import hashlib
import json
import tempfile
import subprocess
import types
import unittest
from pathlib import Path

import build
from kernel_requirements import kernel_requirement, verify_kernel_compatibility

VERSION = "6.6.89-cix-build-generic"


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value)
    else:
        path.write_bytes(value)
    return path


class KernelRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.rootfs = self.source / "rootfs"
        self.image = put(self.base / "Image", b"validated kernel image")
        self.policy = {
            "schema": 1, "image_sha256": build.sha(self.image),
            "reason": "Validated MVX wakeup fix for default hardware decoding",
            "source": {"base_commit": "1" * 40, "patch_sha256": "2" * 64},
        }
        self.enable(self.rootfs / "overlay")
        self.manifest = self.rootfs / "fetch/inputs-kernel.json"
        self.write_policy()

    def enable(self, root):
        return put(root / "etc/chromium.d/99-y4",
                   'CHROMIUM_FLAGS="$CHROMIUM_FLAGS --enable-features=Foo,AcceleratedVideoDecoder"\n')

    def write_policy(self):
        put(self.manifest, json.dumps(self.policy))

    def builder(self):
        b = build.Builder.__new__(build.Builder)
        b.source = self.source
        b.out = self.base / "out"
        b.repackage_from = None
        b.report = {"inputs": {}, "components": {}}
        b.record = lambda: None
        b.dtb_name = "sky1-y4.dtb"
        b.board_label = "Y4"
        b.args = types.SimpleNamespace(component="all")
        return b

    def bundle(self, image=None):
        root = self.base / "bundle"
        root.mkdir()
        data = bytearray(64)
        data[56:60] = b"ARMd"
        if image is None:
            image = bytes(data)
        dtb = subprocess.check_output(
            ["dtc", "-I", "dts", "-O", "dtb"],
            input=b'/dts-v1/; / { compatible = "myt,y4"; };',
        )
        files = {"Image": image, "sky1-y4.dtb": dtb,
                 "System.map-" + VERSION: b"map", "config-" + VERSION: b"config",
                 "modules-" + VERSION + ".tar.zst": b"test-only archive",
                 "kernel.release": (VERSION + "\n").encode()}
        for name, value in files.items():
            put(root / name, value)
        put(root / "SHA256SUMS", "".join(build.sha(root / n) + "  " + n + "\n" for n in files))
        put(root / "RELEASE.md", "synthetic handoff")
        return root

    def test_y12_dtb_checks_switch_payload_and_port_map(self):
        ports = "8 7 1 2 4 5 11 10 9 6 0 3"
        cases = ((256, ports, None), (512, ports, "max-payload = 256"),
                 (256, ports[:-1] + "0", "twelve distinct downstream ports"),
                 (256, "", "twelve distinct downstream ports"))
        for payload, port_map, error in cases:
            with self.subTest(payload=payload, port_map=port_map):
                mapping = f"myt,slot-downstream-ports = <{port_map}>;" if port_map else ""
                dts = f""" /dts-v1/;
                / {{
                    compatible = "myt,y12";
                    soc {{
                        pcie@0a010000 {{
                            sky1,fixed-mps = <256>;
                            max-payload = <{payload}>;
                            {mapping}
                        }};
                    }};
                }}; """
                dtb = put(self.base / "sky1-y12.dtb", subprocess.check_output(
                    ["dtc", "-q", "-I", "dts", "-O", "dtb"], input=dts.encode()))
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        build.verify_dtb_board(dtb, "Y12")
                else:
                    build.verify_dtb_board(dtb, "Y12")

    def test_matching_image(self):
        r = verify_kernel_compatibility(self.rootfs, self.image)
        self.assertEqual(r["verified_image_sha256"], build.sha(self.image))

    def test_y12_requires_its_own_complete_payload_policy(self):
        with self.assertRaisesRegex(ValueError, "inputs-kernel-y12.json"):
            kernel_requirement(self.rootfs, board="Y12")

        path = self.rootfs / "fetch/inputs-kernel-y12.json"
        put(path, json.dumps(dict(self.policy, board="Y4")))
        with self.assertRaisesRegex(ValueError, "Y12 manifest pinning"):
            kernel_requirement(self.rootfs, board="Y12")

        put(path, json.dumps(dict(self.policy, board="Y12")))
        with self.assertRaisesRegex(ValueError, "Y12 manifest pinning"):
            kernel_requirement(self.rootfs, board="Y12")

        dtb = put(self.base / "y12.dtb", b"Y12 DTB fixture")
        modules = put(self.base / "modules.tar.zst", b"Y12 modules fixture")
        put(path, json.dumps(dict(self.policy, board="Y12",
                                  dtb_sha256=build.sha(dtb),
                                  modules_archive_sha256=build.sha(modules))))
        proof = verify_kernel_compatibility(self.rootfs, self.image, board="Y12",
                                            dtb=dtb, modules_archive=modules)
        self.assertEqual(proof["manifest"], str(path))
        self.assertEqual(proof["verified_dtb_sha256"], build.sha(dtb))
        with self.assertRaisesRegex(ValueError, "requires the modules archive"):
            verify_kernel_compatibility(self.rootfs, self.image, board="Y12", dtb=dtb)

    def test_new_image_needs_explicit_revalidation_and_pin_update(self):
        self.image.write_bytes(b"new rebuild with new timestamp")
        with self.assertRaisesRegex(ValueError, "not validated"):
            verify_kernel_compatibility(self.rootfs, self.image)
        self.policy["image_sha256"] = build.sha(self.image)
        self.write_policy()
        verify_kernel_compatibility(self.rootfs, self.image)

    def test_enabled_default_requires_manifest(self):
        self.manifest.unlink()
        with self.assertRaisesRegex(ValueError, "requires"):
            kernel_requirement(self.rootfs)

    def test_staged_enabled_default_cannot_hide_behind_historical_source(self):
        self.manifest.unlink()
        (self.rootfs / "overlay/etc/chromium.d/99-y4").unlink()
        stage = self.base / "filesystem"
        self.enable(stage)
        with self.assertRaisesRegex(ValueError, "requires"):
            verify_kernel_compatibility(self.rootfs, self.image, stage)

    def test_historical_disabled_or_commented_defaults_need_no_manifest(self):
        self.manifest.unlink()
        put(self.rootfs / "overlay/etc/chromium.d/99-y4",
            '# --enable-features=AcceleratedVideoDecoder\n'
            'CHROMIUM_FLAGS="$CHROMIUM_FLAGS --disable-features=AcceleratedVideoDecoder"\n')
        self.assertIsNone(verify_kernel_compatibility(self.rootfs, self.image))

    def test_feature_parameter_syntax_is_detected(self):
        self.manifest.unlink()
        put(self.rootfs / "overlay/etc/chromium.d/99-y4",
            'CHROMIUM_FLAGS="--enable-features=AcceleratedVideoDecoder:arg/value"\n')
        with self.assertRaises(ValueError):
            kernel_requirement(self.rootfs)

    def test_invalid_policy_fields(self):
        for field, value in [("schema", True), ("schema", 2),
                             ("image_sha256", "f" * 63), ("reason", ""),
                             ("source", {"base_commit": "bad", "patch_sha256": "2" * 64}),
                             ("source", {"base_commit": "1" * 40, "patch_sha256": ""})]:
            original = self.policy[field]
            with self.subTest(field=field, value=value):
                self.policy[field] = value
                self.write_policy()
                with self.assertRaises(ValueError):
                    kernel_requirement(self.rootfs)
            self.policy[field] = original
        self.write_policy()

    def test_broken_manifest_symlink_is_rejected(self):
        self.manifest.unlink()
        self.manifest.symlink_to("missing-policy.json")
        with self.assertRaises(ValueError):
            kernel_requirement(self.rootfs)

    def test_manifest_symlink_is_rejected(self):
        other = put(self.base / "other.json", json.dumps(self.policy))
        self.manifest.unlink()
        self.manifest.symlink_to(other)
        with self.assertRaises(ValueError):
            kernel_requirement(self.rootfs)

    def test_bundle_with_self_consistent_old_hashes_is_rejected_before_staging(self):
        b = self.builder()
        b.kernel_bundle = self.bundle()
        with self.assertRaisesRegex(ValueError, "not validated"):
            b.kernel_from_bundle()
        self.assertFalse((b.out / "kernel").exists())

    def test_matching_bundle_is_adopted_and_recorded(self):
        b = self.builder()
        b.kernel_bundle = self.bundle()
        self.policy["image_sha256"] = build.sha(b.kernel_bundle / "Image")
        self.write_policy()
        b.kernel_from_bundle()
        self.assertEqual(build.sha(b.kernel_objects / "arch/arm64/boot/Image"),
                         self.policy["image_sha256"])
        self.assertEqual(b.report["inputs"]["kernel_rootfs_compatibility"]["verified_image_sha256"],
                         self.policy["image_sha256"])

    def test_component_reuse_cannot_bypass_pin(self):
        b = self.builder()
        previous = self.base / "previous"
        put(previous / "kernel/objects/arch/arm64/boot/Image", b"old kernel")
        with self.assertRaisesRegex(ValueError, "not validated"):
            b.reuse_components(previous)

    def test_rootfs_reuse_cannot_bypass_pin(self):
        b = self.builder()
        b.kernel_objects = self.base / "objects"
        put(b.kernel_objects / "arch/arm64/boot/Image", b"old kernel")
        b.args = types.SimpleNamespace(reuse_rootfs=True)
        b.try_rootfs_reuse = lambda: self.fail("reuse must not precede kernel compatibility")
        with self.assertRaisesRegex(ValueError, "not validated"):
            b.filesystem()

    def test_package_checks_actual_staged_defaults_before_creating_output(self):
        b = self.builder()
        self.manifest.unlink()
        (self.rootfs / "overlay/etc/chromium.d/99-y4").unlink()
        b.kernel_objects = self.base / "objects"
        put(b.kernel_objects / "arch/arm64/boot/Image", b"old kernel")
        b.fs = self.base / "staged"
        self.enable(b.fs)
        with self.assertRaisesRegex(ValueError, "requires"):
            b.package()
        self.assertFalse((b.out / "package").exists())

    def test_repackage_uses_retained_rootfs_policy(self):
        b = self.builder()
        b.repackage_from = self.base / "previous"
        old_rootfs = b.repackage_from / "source/rootfs"
        self.enable(old_rootfs / "overlay")
        previous_policy = dict(self.policy, image_sha256=hashlib.sha256(b"previous valid Image").hexdigest())
        put(old_rootfs / "fetch/inputs-kernel.json", json.dumps(previous_policy))
        b.kernel_bundle = self.bundle()
        # Current checkout pin does match this bundle, previous rootfs pin does not.
        self.policy["image_sha256"] = build.sha(b.kernel_bundle / "Image")
        self.write_policy()
        with self.assertRaisesRegex(ValueError, "not validated"):
            b.repackage()
        self.assertFalse(b.out.exists())

    def test_historical_repackage_does_not_borrow_current_checkout_pin(self):
        b = self.builder()
        b.repackage_from = self.base / "previous"
        put(b.repackage_from / "source/rootfs/overlay/etc/chromium.d/old", "# legacy disabled\n")
        self.assertIsNone(b.verify_kernel_rootfs(put(self.base / "historical-image", b"legacy kernel")))

    def test_build_fails_before_hardware_work_on_bad_selected_bundle(self):
        b = self.builder()
        b.args = types.SimpleNamespace(component="all", reuse_components=None)
        b.kernel_bundle = self.bundle()
        b.preflight = lambda: None
        b.snapshot = lambda: None
        b.uefi = lambda: self.fail("UEFI must not start with incompatible kernel")
        with self.assertRaisesRegex(ValueError, "not validated"):
            b.build()

    def pin_bundle(self, bundle):
        self.policy.update(image_sha256=build.sha(bundle / "Image"),
                           dtb_sha256=build.sha(bundle / "sky1-y4.dtb"),
                           modules_archive_sha256=build.sha(bundle / ("modules-" + VERSION + ".tar.zst")))
        self.write_policy()

    def test_optional_payload_digest_fields_require_exact_sha256(self):
        for field in ("dtb_sha256", "modules_archive_sha256"):
            for value in (None, 7, "", "f" * 63, "F" * 64):
                with self.subTest(field=field, value=value):
                    self.policy[field] = value
                    self.write_policy()
                    with self.assertRaisesRegex(ValueError, field):
                        kernel_requirement(self.rootfs)
            del self.policy[field]
        self.write_policy()

    def test_strict_matching_bundle_records_all_payloads(self):
        b = self.builder()
        b.kernel_bundle = self.bundle()
        self.pin_bundle(b.kernel_bundle)
        b.kernel_from_bundle()
        proof = b.report["inputs"]["kernel_rootfs_compatibility"]
        for field in ("image_sha256", "dtb_sha256", "modules_archive_sha256"):
            self.assertEqual(proof["verified_" + field], self.policy[field])

    def test_same_image_different_dtb_or_modules_is_rejected_before_staging(self):
        bundle = self.bundle()
        self.pin_bundle(bundle)
        for name in ("sky1-y4.dtb", "modules-" + VERSION + ".tar.zst"):
            original = (bundle / name).read_bytes()
            with self.subTest(name=name):
                (bundle / name).write_bytes(original + b" unvalidated")
                sums = [build.sha(bundle / line.split()[1]) + "  " + line.split()[1]
                        for line in (bundle / "SHA256SUMS").read_text().splitlines()]
                (bundle / "SHA256SUMS").write_text("\n".join(sums) + "\n")
                b = self.builder()
                b.kernel_bundle = bundle
                with self.assertRaisesRegex(ValueError, "not validated"):
                    b.kernel_from_bundle()
                self.assertFalse((b.out / "kernel").exists())
            (bundle / name).write_bytes(original)

    def test_strict_payloads_cannot_be_omitted(self):
        dtb = put(self.base / "sky1-y4.dtb", b"accepted dtb")
        modules = put(self.base / "modules.tar.zst", b"accepted modules")
        self.policy.update(dtb_sha256=build.sha(dtb), modules_archive_sha256=build.sha(modules))
        self.write_policy()
        for kwargs in ({}, {"dtb": dtb}, {"modules_archive": modules}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "requires the"):
                    verify_kernel_compatibility(self.rootfs, self.image, **kwargs)
        with self.assertRaisesRegex(ValueError, "requires the Image"):
            verify_kernel_compatibility(self.rootfs, dtb=dtb, modules_archive=modules)
        with self.assertRaisesRegex(ValueError, "missing"):
            verify_kernel_compatibility(self.rootfs, self.image, dtb=self.base / "missing", modules_archive=modules)

    def test_dtb_only_and_archive_only_constraints_are_independent(self):
        for field, argument in (("dtb_sha256", "dtb"), ("modules_archive_sha256", "modules_archive")):
            payload = put(self.base / argument, b"accepted payload")
            self.policy[field] = build.sha(payload)
            self.write_policy()
            proof = verify_kernel_compatibility(self.rootfs, self.image, **{argument: payload})
            self.assertEqual(proof["verified_" + field], self.policy[field])
            del self.policy[field]

    def test_strict_pin_rejects_staged_dtb_even_with_matching_selected_bundle(self):
        b = self.builder()
        b.kernel_bundle = self.bundle()
        self.pin_bundle(b.kernel_bundle)
        b.kernel_from_bundle()
        (b.kernel_objects / "arch/arm64/boot/dts/cix/sky1-y4.dtb").write_bytes(b"wrong staged DTB")
        with self.assertRaisesRegex(ValueError, "DTB is not validated"):
            b.verify_kernel_rootfs(b.kernel_objects / "arch/arm64/boot/Image")

    def test_strict_pin_rejects_missing_or_ambiguous_archive(self):
        b = self.builder()
        b.kernel_bundle = self.bundle()
        self.pin_bundle(b.kernel_bundle)
        archive = b.kernel_bundle / ("modules-" + VERSION + ".tar.zst")
        original = archive.read_bytes()
        archive.unlink()
        with self.assertRaisesRegex(ValueError, "requires the modules archive"):
            b.verify_kernel_rootfs(b.kernel_bundle / "Image")
        archive.write_bytes(original)
        put(b.kernel_bundle / "modules-extra.tar.zst", original)
        with self.assertRaisesRegex(ValueError, "requires the modules archive"):
            b.verify_kernel_rootfs(b.kernel_bundle / "Image")

    def test_strict_pin_blocks_unarchived_source_build_before_hardware_work(self):
        b = self.builder()
        self.policy["modules_archive_sha256"] = "3" * 64
        self.write_policy()
        b.kernel_bundle = None
        b.args = types.SimpleNamespace(component="all", reuse_components=None)
        b.preflight = b.snapshot = lambda: None
        b.uefi = lambda: self.fail("UEFI must not start without required accepted payloads")
        with self.assertRaisesRegex(ValueError, "requires the"):
            b.build()

    def test_constructor_forbids_selected_bundle_and_component_reuse(self):
        bundle = self.bundle()
        config = put(self.base / "build.json", json.dumps({
            "schema": 1, "hardware": "Y4-6187", "inputs": {"kernel_bundle": str(bundle)}}))
        for selected in (bundle, None):
            with self.subTest(explicit_bundle=selected is not None):
                args = types.SimpleNamespace(config=config, board=None, kernel_bundle=selected,
                    pg_from=None, kernel_source_build=False, repackage_from=None,
                    reuse_components=self.base / "previous", reuse_rootfs=False)
                with self.assertRaisesRegex(ValueError, "kernel-bundle and --reuse-components are mutually exclusive"):
                    build.Builder(args)

    def test_same_image_dtb_old_objects_cannot_bypass_archive_pin_with_reuse(self):
        bundle = self.bundle()
        self.pin_bundle(bundle)
        previous = self.base / "previous"
        image = put(previous / "kernel/objects/arch/arm64/boot/Image", (bundle / "Image").read_bytes())
        put(image.parent / "dts/cix/sky1-y4.dtb", (bundle / "sky1-y4.dtb").read_bytes())
        put(previous / "kernel/objects/drivers/misc/xh2a/xh2a_drv.ko", b"old unvalidated module")
        b = self.builder()
        b.kernel_bundle = None
        # A matching Image and DTB cannot stand in for the actual module source.
        with self.assertRaisesRegex(ValueError, "requires the modules archive"):
            b.reuse_components(previous)


if __name__ == "__main__":
    unittest.main()
