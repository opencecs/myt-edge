#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build pinned vendor PG support; never execute the PG binary or load modules."""
import argparse,hashlib,json,pathlib,shutil,subprocess,time
ROOT=pathlib.Path(__file__).resolve().parents[1]
def sha(path):
 h=hashlib.sha256()
 with path.open("rb")as f:
  for data in iter(lambda:f.read(1048576),b""):h.update(data)
 return h.hexdigest()
def main():
 p=argparse.ArgumentParser(description=__doc__)
 for name in ["vendor-source","kernel-objects","cross-prefix","output"]:p.add_argument("--"+name,type=pathlib.Path,required=True)
 p.add_argument("--jobs",type=int,default=32);a=p.parse_args()
 if not 1<=a.jobs<=32:raise ValueError("jobs must be1..32")
 pin=json.loads((ROOT/"third_party/realtek-pg/inputs.json").read_text());source=a.vendor_source.resolve();kernel=a.kernel_objects.resolve();release=(kernel/"include/config/kernel.release").read_text().strip()
 if not release.startswith("6.1.44-cix-"):raise ValueError("explicit CIX6.1.44 kernel required")
 config=(kernel/".config").read_text()
 if "CONFIG_MODULE_SIG_FORCE=y"in config:raise ValueError("kernel requires signed modules; provide an explicit signing integration")
 for row in pin["files"]:
  path=source/row["file"]
  if path.is_symlink()or not path.is_file()or path.stat().st_size!=row["bytes"]or sha(path)!=row["sha256"]:raise ValueError("vendor input hash mismatch: "+row["file"])
 out=a.output.absolute();out.mkdir(parents=True,exist_ok=False);work=out/"source";work.mkdir();stage=out/"stage";(stage/"lib/modules").mkdir(parents=True);(stage/"usr/libexec/opencecs").mkdir(parents=True)
 for row in pin["files"]:shutil.copy2(source/row["file"],work/row["file"])
 cross=str(a.cross_prefix.absolute());commands=[]
 def run(name,args):
  args=list(map(str,args));start=time.monotonic();q=subprocess.run(args,capture_output=True)
  (out/(name+".stdout")).write_bytes(q.stdout);(out/(name+".stderr")).write_bytes(q.stderr);commands.append({"stage":name,"command":args,"rc":q.returncode,"seconds":round(time.monotonic()-start,3)})
  (out/"COMMANDS.json").write_text(json.dumps(commands,indent=2)+"\n")
  if q.returncode:raise RuntimeError("failed "+name+"; evidence retained")
  return q.stdout.decode()
 run("module-build",["make","-C",kernel,"M="+str(work),"ARCH=arm64","CROSS_COMPILE="+cross,"-j"+str(a.jobs),"modules"])
 module=stage/"lib/modules/pgdrv.ko";binary=stage/"usr/libexec/opencecs/rtnicpg"
 run("module-strip",[cross+"strip","--strip-debug","-o",module,work/"pgdrv.ko"]);shutil.copy2(work/"rtnicpg-aarch64-armv8",binary);binary.chmod(0o755)
 vermagic=run("module-vermagic",["modinfo","-F","vermagic",module]).strip();license=run("module-license",["modinfo","-F","license",module]).strip();depends=run("module-depends",["modinfo","-F","depends",module]).strip()
 if not vermagic.startswith(release+" ")or "aarch64"not in vermagic:raise ValueError("module kernel ABI mismatch")
 elf=run("binary-header",["readelf","-h",binary]);dynamic=run("binary-dynamic",["readelf","-d",binary]);program=run("binary-program",["readelf","-l",binary])
 if "AArch64"not in elf or "(NEEDED)"in dynamic or "INTERP"in program:raise ValueError("unexpected PG runtime ABI or dynamic dependencies")
 record={"vendor_version":pin["version"],"driver_module_version":pin["driver_module_version"],"vermagic":vermagic,"kernel_release":release,"kernel_config_sha256":sha(kernel/".config"),"kernel_symvers_sha256":sha(kernel/"Module.symvers"),"compiler":run("compiler-version",[cross+"gcc","--version"]).splitlines()[0],"module_license":license,"binary_license":pin["binary_license"],"binary_open_source":False,"binary_dynamic_dependencies":[],"module_dependencies":depends.split(",")if depends else[],"board_access":False,"module_loaded":False,"vendor_executed":False,"efuse_written":False,"jobs":a.jobs,"files":{str(p.relative_to(stage)):{"bytes":p.stat().st_size,"sha256":sha(p)}for p in[module,binary]}}
 (out/"BUILD.json").write_text(json.dumps(record,indent=2)+"\n");print(json.dumps({"stage":str(stage),"build":str(out/"BUILD.json"),"vermagic":vermagic,"vendor_executed":False}))
if __name__=="__main__":main()
