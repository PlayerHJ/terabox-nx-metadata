"""One compact name/date feed; preserve the legacy name-only endpoint byte format.

Version dates mean earliest recorded observation, not official release dates.
Product release dates are separate DLC fields and never substitute for a patch.
"""
import argparse,collections,datetime,gzip,hashlib,json,re,shutil,subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
try:
 from . import build_dlc_names as names
except ImportError:
 import build_dlc_names as names

POLICY='version-dates-v2'
WIRE_LIMIT=1024*1024
RAW_LIMIT=4*1024*1024

def day(value):
 if type(value) not in (str,int):return None
 value=str(value)
 if re.fullmatch(r'\d{8}',value):value=value[:4]+'-'+value[4:6]+'-'+value[6:]
 if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):return None
 try:d=datetime.date.fromisoformat(value)
 except ValueError:return None
 return int(d.strftime('%Y%m%d')) if 2000<=d.year<=2100 else None

def versions(text):
 rows=text.splitlines()
 if not rows or rows[0] not in ['id|version','id|rightsId|version']:raise ValueError('Invalid version header')
 result={}
 for line in rows[1:]:
  fields=line.split('|')
  if len(fields) not in (2,3) or not re.fullmatch(r'[0-9A-Fa-f]{16}',fields[0]):raise ValueError('Invalid version record')
  if not fields[-1]:continue
  if not re.fullmatch(r'\d{1,10}',fields[-1]):raise ValueError('Invalid numeric version')
  value=int(fields[-1]);title=fields[0].upper()
  if value>0xffffffff:raise ValueError('Version overflow')
  result[title]=max(value,result.get(title,0))
 return result

def extract_dates(history,latest,default,korean,english,japanese):
 if not isinstance(history,dict):raise ValueError('Invalid version history')
 history={str(k).upper():v for k,v in history.items() if re.fullmatch(r'[0-9A-Fa-f]{16}',str(k)) and isinstance(v,dict)}
 dates={}
 for title,version in latest.items():
  if not version:continue
  candidates=[history.get(title,{})]
  # The upstream scanner records patch history under its application ID.
  # Only the standard patch relation is permitted; never guess a DLC family.
  if int(title,16)&0x1fff==0x800:candidates.append(history.get(f'{int(title,16)-0x800:016X}',{}))
  valid=[d for row in candidates if (d:=day(row.get(str(version))))]
  if valid:dates[title]=[version,min(valid)]
 links=collections.defaultdict(set)
 for key,row in default.items():
  if not isinstance(row,dict):continue
  title=names.dlc_id(row.get('id',key))
  if title and row.get('nsuId'):links[str(row['nsuId'])].add(title)
 releases={}
 for rank,rows in enumerate([korean,english,japanese,default]):
  for key,row in rows.items():
   if not isinstance(row,dict) or not (d:=day(row.get('releaseDate'))):continue
   title=names.dlc_id(row.get('id',key) if rank==3 else row.get('id'))
   titles=[title] if title else links.get(str(row.get('nsuId') or key),()) if not row.get('id') else ()
   for title in titles:
    if title not in releases:releases[title]=d
 return dict(sorted(dates.items())),dict(sorted(releases.items()))

def application_id(value):
 value=str(value or '')
 return value.upper() if re.fullmatch(r'[0-9A-Fa-f]{16}',value) and int(value,16) and int(value,16)&0x1fff==0 else None

def extract_game_releases(default,korean,english,japanese):
 # Only an application's own eShop product is eligible. Never reuse a DLC,
 # patch observation date, or another title in a similarly named family.
 links=collections.defaultdict(set);canonical={}
 for key,row in default.items():
  if not isinstance(row,dict):continue
  title=application_id(row.get('id',key))
  if title and row.get('nsuId'):links[str(row['nsuId'])].add(title);canonical[title]=str(row['nsuId'])
 chosen={}
 for rank,rows in enumerate([korean,english,japanese,default]):
  for key,row in rows.items():
   if not isinstance(row,dict) or not (date:=day(row.get('releaseDate'))):continue
   title=application_id(row.get('id',key) if rank==3 else row.get('id'))
   products=[title] if title else links.get(str(row.get('nsuId') or key),()) if not row.get('id') else ()
   for title in products:
    priority=(rank,0 if str(row.get('nsuId') or key)==canonical.get(title) else 1)
    if title not in chosen or priority<chosen[title][0]:chosen[title]=(priority,date)
 return {title:value[1] for title,value in sorted(chosen.items())}

def encode(names_map,dates,releases,revision,games=None):
 document={'schema':1,'source':names.SOURCE,'revision':revision,'language':'ko-first','names':names_map,'dates':dates,'releases':releases}
 if games is not None:document['games']=games
 raw=json.dumps(document,ensure_ascii=False,separators=(',',':')).encode('utf8')
 if len(raw)>RAW_LIMIT:raise ValueError('Metadata exceeds decoded bound')
 wire=gzip.compress(raw,compresslevel=6,mtime=0)
 if len(wire)>WIRE_LIMIT:raise ValueError('Metadata exceeds wire bound')
 summary(wire)
 return wire

def summary(wire):
 if len(wire)>WIRE_LIMIT:raise ValueError('Metadata exceeds wire bound')
 import io
 with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:raw=stream.read(RAW_LIMIT+1)
 if len(raw)>RAW_LIMIT:raise ValueError('Metadata exceeds decoded bound')
 document=json.loads(raw)
 fields={'schema','source','revision','language','names','dates','releases'}
 if not isinstance(document,dict) or set(document) not in [fields,fields|{'games'}] or document['schema']!=1 or document['source']!=names.SOURCE or document['language']!='ko-first' or not re.fullmatch('[0-9a-f]{40}',str(document['revision'])):raise ValueError('Invalid metadata envelope')
 for field in ['names','dates','releases']+(['games'] if 'games' in document else []):
  rows=document[field]
  if not isinstance(rows,dict) or len(rows)>100000:raise ValueError('Invalid metadata map')
  for title,value in rows.items():
   if not re.fullmatch('[0-9A-F]{16}',title) or int(title,16)==0:raise ValueError('Invalid metadata ID')
   if field=='names':
    if names.dlc_id(title)!=title or not isinstance(value,str) or not value or names.clean_name(value)!=value:raise ValueError('Invalid DLC name')
   elif field=='dates':
    if not isinstance(value,list) or len(value)!=2 or type(value[0]) is not int or not 0<value[0]<=0xffffffff or day(value[1])!=value[1]:raise ValueError('Invalid version date')
   elif (application_id(title) if field=='games' else names.dlc_id(title))!=title or day(value)!=value:raise ValueError('Invalid product release date')
 if len(document['names'])<1000 or len(document['dates'])<1000:raise ValueError('Incomplete metadata')
 return {'metadata_revision':document['revision'],'metadata_policy':POLICY if 'games' in document else 'version-dates-v1','metadata_names':len(document['names']),'version_dates':len(document['dates']),'dlc_release_dates':len(document['releases']),'game_release_dates':len(document.get('games',{})),'metadata_gzip_bytes':len(wire),'metadata_raw_bytes':len(raw),'metadata_sha256':hashlib.sha256(wire).hexdigest()}

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--metadata-output',type=Path,required=True);parser.add_argument('--force',action='store_true');args=parser.parse_args()
 revision=json.loads(names.request('https://api.github.com/repos/'+names.SOURCE+'/git/ref/heads/master',65536))['object']['sha']
 if not re.fullmatch('[0-9a-f]{40}',revision):raise ValueError('Invalid upstream revision')
 if not args.force and args.output.exists() and args.metadata_output.exists():
  try:
   old=summary(args.metadata_output.read_bytes());legacy=json.loads(gzip.decompress(args.output.read_bytes()))
   if old['metadata_policy']==POLICY and old['metadata_revision']==revision and legacy.get('revision')==revision and legacy.get('names')==json.loads(gzip.decompress(args.metadata_output.read_bytes()))['names']:
    print(json.dumps(dict(changed=False,**old)));return
  except (OSError,ValueError,EOFError):pass
 zstd=shutil.which('zstd') or ('C:/devkitPro/msys2/usr/bin/zstd.exe' if Path('C:/devkitPro/msys2/usr/bin/zstd.exe').exists() else None)
 if not zstd:raise RuntimeError('Install the official zstd command-line tool')
 base='https://raw.githubusercontent.com/'+names.SOURCE+'/'+revision+'/'
 sources=[('titles.json.zst',32<<20),('KR.ko.json',128<<20),('US.en.json',128<<20),('JP.ja.json',128<<20),('versions.json',16<<20),('versions.txt',8<<20)]
 with ThreadPoolExecutor(max_workers=4) as pool:
  jobs=[pool.submit(names.request,base+name,limit) for name,limit in sources];compressed,kr,en,jp,history,latest=[job.result() for job in jobs]
 decoded=subprocess.run([zstd,'-d','-q','-c'],input=compressed,capture_output=True,timeout=60)
 if decoded.returncode or len(decoded.stdout)>256<<20:raise ValueError('Invalid compressed source')
 default,korean,english,japanese=[json.loads(v) for v in [decoded.stdout,kr,en,jp]]
 names_map,_=names.extract(default,korean,english,japanese)
 dates,releases=extract_dates(json.loads(history),versions(latest.decode('utf-8-sig')),default,korean,english,japanese)
 games=extract_game_releases(default,korean,english,japanese)
 if len(games)<1000:raise ValueError('Incomplete game release dates')
 legacy=names.encode(names_map,revision);wire=encode(names_map,dates,releases,revision,games)
 # Adding the date feed must not rewrite identical legacy names merely
 # because another Python/zlib build emits a different gzip byte stream.
 if args.output.exists() and args.output.stat().st_size<=WIRE_LIMIT:
  previous=args.output.read_bytes()
  try:
   if json.loads(gzip.decompress(previous))==json.loads(gzip.decompress(legacy)):legacy=previous
  except (ValueError,EOFError,OSError):pass
 for path,data in [(args.output,legacy),(args.metadata_output,wire)]:
  if path.exists() and path.read_bytes()==data:continue
  path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_bytes(data);temporary.replace(path)
 print(json.dumps(dict(changed=True,**summary(wire))))
if __name__=='__main__':main()
