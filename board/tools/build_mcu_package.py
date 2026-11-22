#!/usr/bin/env python3
"""Create MCU1 signed update envelopes, without flashing or device access."""
import argparse,hashlib,json,os,re,struct
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
SEMVER=re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
def create(image,key,public,output,version,sequence,host_min='0.1.0',host_max='0.1.99',protocol=2,board='Y4'):
 if board not in ('Y4','Y12'):raise ValueError('unsupported MCU board')
 image,key,public,output=map(Path,(image,key,public,output))
 if output.exists()or output.is_symlink()or not output.parent.is_dir():raise ValueError('fresh MCU package under existing output required')
 for p in (image,key,public):
  if p.is_symlink()or not p.is_file():raise ValueError('regular immutable MCU inputs required')
 for v in (version,host_min,host_max):
  if not SEMVER.fullmatch(v):raise ValueError('canonical three-part MCU/host version required')
 if any(int(x)>255 for x in version.split('.'))or not 1<=sequence<2**64 or protocol!=2:raise ValueError('MCU version/sequence/protocol out of range')
 if tuple(map(int,host_min.split('.')))>tuple(map(int,host_max.split('.'))):raise ValueError('host range reversed')
 data=image.read_bytes()
 if not 64<=len(data)<=1048576:raise ValueError('MCU image size out of range')
 stack,reset=struct.unpack_from('<II',data)
 if stack&7 or not 0x20000000<stack<=0x20020000 or not reset&1 or not 0x08000008<=reset&~1<0x08000000+len(data):raise ValueError('GD32F407 stack/reset vectors invalid')
 if key.stat().st_mode&0o077:raise ValueError('private signing key must not be accessible to group/others')
 private=serialization.load_pem_private_key(key.read_bytes(),None)
 if not isinstance(private,Ed25519PrivateKey):raise ValueError('Ed25519 MCU signing key required')
 expected=public.read_bytes();actual=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
 if len(expected)!=32 or expected!=actual:raise ValueError('MCU signing key does not match configured device trust key')
 digest=hashlib.sha256(data).hexdigest()
 manifest=('Y4MCU1\nboard=MYT-AI-NAS-'+board+'\nsoc=6187\nmcu=GD32F407\nprotocol='+str(protocol)+'\nversion='+version+'\nhost-min='+host_min+'\nhost-max='+host_max+'\nsequence='+str(sequence)+'\nimage-bytes='+str(len(data))+'\nsha256='+digest+'\n').encode()
 header=struct.pack('<8sHHIIHHQQ32s',b'Y4UPKG1\0',1,72,2,len(manifest),64,0,len(data),0,bytes.fromhex(digest))
 signature=private.sign(header+manifest);private.public_key().verify(signature,header+manifest)
 package=header+manifest+signature+data
 with os.fdopen(os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as f:f.write(package)
 report={'object':'MCU','version':version,'sequence':sequence,'image_bytes':len(data),'image_sha256':digest,'package_bytes':len(package),'package_sha256':hashlib.sha256(package).hexdigest(),'host_min':host_min,'host_max':host_max,'protocol':protocol,'signed':True,'flashed':False}
 if board == "Y12":report["board"]=board
 return report
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ('image','key','public-key','output'):p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--version',required=True);p.add_argument('--sequence',type=int,required=True);p.add_argument('--host-min',default='0.1.0');p.add_argument('--host-max',default='0.1.99');a=p.parse_args();r=create(a.image,a.key,a.public_key,a.output,a.version,a.sequence,a.host_min,a.host_max);print(json.dumps(r,indent=2))
