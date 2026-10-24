"""Resumable transfer client. Requires requests. Paste secret link at hidden prompt."""
import argparse
import getpass
import hashlib
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import requests


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while chunk:=f.read(4*1024*1024):h.update(chunk)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['manifest','upload','download']);parser.add_argument('file',type=Path)
    args=parser.parse_args()
    if args.operation=='manifest':
        import json
        print(json.dumps({'name':args.file.name,'size_bytes':args.file.stat().st_size,'sha256':digest(args.file)}));return
    parsed=urlsplit(getpass.getpass('Fresh MCP file link (hidden): '))
    if parsed.scheme!='https' or parsed.netloc!='cloud.nexilume.com' or not parsed.path.startswith('/api/v1/agent-file-bridges/'):raise ValueError('Unexpected file origin')
    ticket=parse_qs(parsed.fragment).get('ticket',[''])[0]
    if not ticket:raise ValueError('Complete link with ticket required')
    base='https://cloud.nexilume.com'+parsed.path
    with requests.Session() as s:
        r=s.post(base+'redeem/',json={'ticket':ticket},timeout=30,allow_redirects=False);r.raise_for_status();body=r.json();info=body.get('data',body)
        url='https://cloud.nexilume.com'+info['url']
        if info['mode']!=args.operation:raise ValueError('Wrong transfer direction')
        if args.operation=='upload':
            if args.file.stat().st_size!=info['size_bytes'] or digest(args.file)!=info['sha256']:raise ValueError('File manifest mismatch')
            h=s.head(url,timeout=30);h.raise_for_status();offset=int(h.headers['X-Nexus-Upload-Offset'])
            with args.file.open('rb') as f:
                f.seek(offset)
                while chunk:=f.read(info['chunk_bytes']):
                    r=s.put(url,data=chunk,headers={'Content-Type':'application/octet-stream','X-Nexus-Upload-Offset':str(offset),'X-Nexus-Chunk-SHA256':hashlib.sha256(chunk).hexdigest()},timeout=60,allow_redirects=False)
                    r.raise_for_status();offset+=len(chunk);print(f'{offset}/{info["size_bytes"]}',flush=True)
            print('Upload complete. Call finalize_model_import through MCP.');return
        part=args.file.with_name(args.file.name+'.part')
        if args.file.exists():raise ValueError('Destination already exists')
        offset=part.stat().st_size if part.exists() else 0
        if offset>info['size_bytes']:raise ValueError('Partial file too large')
        if offset<info['size_bytes']:
            with s.get(url,headers={'Range':f'bytes={offset}-','If-Range':'"'+info['sha256']+'"'},stream=True,timeout=60,allow_redirects=False) as r:
                r.raise_for_status()
                if r.status_code!=206 or not r.headers.get('Content-Range','').startswith(f'bytes {offset}-'):raise ValueError('Server did not honor resume range')
                with part.open('ab') as f:
                    for chunk in r.iter_content(1024*1024):f.write(chunk)
        if part.stat().st_size!=info['size_bytes'] or digest(part)!=info['sha256']:raise ValueError('Integrity check failed; partial file preserved')
        part.replace(args.file);print('Download complete; SHA256 verified.')


if __name__=='__main__':
    try:main()
    except requests.RequestException:raise SystemExit('Transfer interrupted. Request a fresh link through MCP and rerun to resume. No credentials are logged.')
