"""Produce public ID/name-only metadata; never export keys or other fields."""
import argparse,collections,gzip,hashlib,json,re,shutil,subprocess,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
SOURCE='borntohonk/titledb'
POLICY='ko-en-ja-v1'
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
def language_rank(name):
 if re.search('[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]',name):return 0
 if re.search('[\u3040-\u30ff\u3400-\u9fff]',name):return 2
 if re.search('[A-Za-z]',name):return 1
 return 3

def extract(default,korean,english=None,japanese=None):
 links=collections.defaultdict(set);chosen={}
 for key,row in default.items():
  if isinstance(row,dict):
   title=dlc_id(row.get('id',key))
   if title and row.get('nsuId'):links[str(row['nsuId'])].add(title)
 for source_rank,rows in [(3,default),(2,japanese or {}),(1,english or {}),(0,korean)]:
  for key,row in rows.items():
   if not isinstance(row,dict):continue
   name=clean_name(row.get('name'))
   if not name:continue
   title=dlc_id(row.get('id',key) if source_rank==3 else row.get('id'))
   titles=[title] if title else links.get(str(row.get('nsuId') or key),()) if not row.get('id') else ()
   for title in titles:
    candidate=(language_rank(name),source_rank,name)
    if title not in chosen or candidate<chosen[title]:chosen[title]=candidate
 names={title:item[2] for title,item in chosen.items()};preferred=sum(item[0]==0 for item in chosen.values())
 if len(names)<1000 or preferred<100:raise ValueError('Incomplete source; retain the previously published index')
 if len(names)>100000:raise ValueError('Too many names')
 return dict(sorted(names.items())),preferred

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
 if args.output.exists() and not args.force and args.output.with_name('refresh-status.json').exists() and json.loads(args.output.with_name('refresh-status.json').read_text()).get('language_priority')==POLICY:
  wire=args.output.read_bytes()
  if len(wire)<=WIRE_LIMIT:
   old=json.loads(gzip.decompress(wire))
   if old.get('schema')==1 and old.get('source')==SOURCE and old.get('revision')==revision and old.get('language')=='ko-first' and len(old.get('names',{}))>=1000:
    print(json.dumps({'changed':False,'revision':revision,'names':len(old['names'])}));return
 zstd=shutil.which('zstd') or ('C:/devkitPro/msys2/usr/bin/zstd.exe' if Path('C:/devkitPro/msys2/usr/bin/zstd.exe').exists() else None)
 if not zstd:raise RuntimeError('Install the official zstd command-line tool')
 base='https://raw.githubusercontent.com/'+SOURCE+'/'+revision+'/'
 with ThreadPoolExecutor(max_workers=4) as pool:
  jobs=[pool.submit(request,base+name,limit) for name,limit in [('titles.json.zst',32*1024*1024),('KR.ko.json',128*1024*1024),('US.en.json',128*1024*1024),('JP.ja.json',128*1024*1024)]]
  compressed,korean,english,japanese=[job.result() for job in jobs]
 decoded=subprocess.run([zstd,'-d','-q','-c'],input=compressed,capture_output=True,timeout=60)
 if decoded.returncode or len(decoded.stdout)>256*1024*1024:raise ValueError('Invalid compressed source')
 names,preferred=extract(json.loads(decoded.stdout),json.loads(korean),json.loads(english),json.loads(japanese));wire=encode(names,revision)
 args.output.parent.mkdir(parents=True,exist_ok=True);temp=args.output.with_suffix(args.output.suffix+'.tmp');temp.write_bytes(wire);temp.replace(args.output)
 print(json.dumps({'changed':True,'revision':revision,'names':len(names),'Korean_region_names':preferred,'language_priority':POLICY,'gzip_bytes':len(wire),'sha256':hashlib.sha256(wire).hexdigest()}))
if __name__=='__main__':main()
