import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
import parallel_zip

class ZipCompatibilityTests(unittest.TestCase):
    def test_levels_preserve_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for entry in ("signed.y4pkg","images/rootfs.img"):
                package=root/entry.replace("/","-")
                package.mkdir()
                source=package/entry
                source.parent.mkdir(parents=True,exist_ok=True)
                payload=(b"fixture-signed-payload"+bytes(range(256)))*8192
                source.write_bytes(payload)
                (package/"README.txt").write_text("fixture")
                sizes=[]
                for level in (1,6,9):
                    target=root/(entry.replace("/","-")+str(level)+".zip")
                    meta=parallel_zip.create(package,target,2,root/"log",entry,level)
                    self.assertEqual(meta["level"],level)
                    with zipfile.ZipFile(target) as archive:
                        self.assertEqual(set(archive.namelist()),{entry,"README.txt"})
                        self.assertEqual(hashlib.sha256(archive.read(entry)).digest(),hashlib.sha256(payload).digest())
                        self.assertIsNone(archive.testzip())
                    sizes.append(target.stat().st_size)
                self.assertLess(sizes[1],sizes[0])
    def test_invalid_levels(self):
        for level in (0,10,-1,"6"):
            with self.assertRaises(ValueError):
                parallel_zip.create(".","unused.zip",1,"unused.log",level=level)
if __name__=="__main__": unittest.main()
