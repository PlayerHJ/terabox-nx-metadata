"""Produce public ID/name-only metadata; never export keys or other fields."""
import argparse,collections,gzip,hashlib,json,re,shutil,subprocess,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
SOURCE='borntohonk/titledb'
WIRE_LIMIT=1024*1024
RAW_LIMIT=4*1024*1024
def request(url,limit):
 with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'TeraBox-NX-name-index/1'}),timeout=45) as response:
  data=response.read(limit+1)
 if len(data)>limit:raise ValueError('Source exceeds size bound')
 return data
def dlc_id(value):
 value=str(value or '')
 if re.fullmatch(r'[0-9A-Fa-f]{16}',value) and int(value,16)&0x1fff>=0x1000:return value.upper()
def clean_name(value):
 if not isinstance(value,str):return None
 value=' '.join(value.split())
 if value and len(value.encode('utf8'))<=1024 and not any(ord(c)<32 or ord(c)==127 for c in value):return value
def extract(default,korean):
 names={};links=collections.defaultdict(set)
 for key,row in default.items():
  if not isinstance(row,dict):continue
  title=dlc_id(row.get('id',key));name=clean_name(row.get('name'))
  if title:
   if name:names[title]=name
   if row.get('nsuId'):links[str(row['nsuId'])].add(title)
 preferred={}
 for key,row in korean.items():
  if not isinstance(row,dict):continue
  name=clean_name(row.get('name'))
  if not name:continue
  title=dlc_id(row.get('id'))
  if title:preferred[title]=name
  elif not row.get('id'):
   for linked in links.get(str(row.get('nsuId') or key),()):preferred[linked]=name
 names.update(preferred)
 if len(names)<1000 or len(preferred)<100:raise ValueError('Incomplete source; retain the previously published index')
 if len(names)>100000:raise ValueError('Too many names')
 return dict(sorted(names.items())),len(preferred)
def encode(names,revision):
 document={'schema':1,'source':SOURCE,'revision':revision,'language':'ko-first','names':names}
 raw=json.dumps(document,ensure_ascii=False,separators=(',',':')).encode('utf8')
 if len(raw)>RAW_LIMIT:raise ValueError('Uncompressed index exceeds bound')
 wire=gzip.compress(raw,compresslevel=6,mtime=0)
 if len(wire)>WIRE_LIMIT:raise ValueError('Compressed index exceeds bound')
 assert set(json.loads(gzip.decompress(wire)))=={'schema','source','revision','language','names'}
 return wire
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--force',action='store_true');args=parser.parse_args()
 ref=json.loads(request('https://api.github.com/repos/'+SOURCE+'/git/ref/heads/master',65536));revision=ref['object']['sha']
 if not re.fullmatch('[0-9a-f]{40}',revision):raise ValueError('Invalid upstream revision')
 if args.output.exists() and not args.force:
  wire=args.output.read_bytes()
  if len(wire)<=WIRE_LIMIT:
   old=json.loads(gzip.decompress(wire))
   if old.get('schema')==1 and old.get('source')==SOURCE and old.get('revision')==revision and old.get('language')=='ko-first' and len(old.get('names',{}))>=1000:
    print(json.dumps({'changed':False,'revision':revision,'names':len(old['names'])}));return
 zstd=shutil.which('zstd') or ('C:/devkitPro/msys2/usr/bin/zstd.exe' if Path('C:/devkitPro/msys2/usr/bin/zstd.exe').exists() else None)
 if not zstd:raise RuntimeError('Install the official zstd command-line tool')
 base='https://raw.githubusercontent.com/'+SOURCE+'/'+revision+'/'
 with ThreadPoolExecutor(max_workers=2) as pool:
  a=pool.submit(request,base+'titles.json.zst',32*1024*1024);b=pool.submit(request,base+'KR.ko.json',80*1024*1024);compressed=a.result();korean=b.result()
 decoded=subprocess.run([zstd,'-d','-q','-c'],input=compressed,capture_output=True,timeout=60)
 if decoded.returncode or len(decoded.stdout)>256*1024*1024:raise ValueError('Invalid compressed source')
 names,preferred=extract(json.loads(decoded.stdout),json.loads(korean));wire=encode(names,revision)
 args.output.parent.mkdir(parents=True,exist_ok=True);temp=args.output.with_suffix(args.output.suffix+'.tmp');temp.write_bytes(wire);temp.replace(args.output)
 print(json.dumps({'changed':True,'revision':revision,'names':len(names),'Korean_region_names':preferred,'gzip_bytes':len(wire),'sha256':hashlib.sha256(wire).hexdigest()}))
if __name__=='__main__':main()
