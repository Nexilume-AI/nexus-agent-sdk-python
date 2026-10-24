"""Private checkpoint registry, bounded import and safe native model assembly."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from file_bridge import Bridges, save, sha256
from h3_recipe import H3_ID, status as h3_status

BASE_ID = 'wan2.1-t2v-1.3b'
VALIDATORS = threading.BoundedSemaphore(2)


class Registry:
    def __init__(self, w, base, image, root):
        self.w, self.base, self.image, self.root = w, base, image, root
        self.bridges = Bridges(w)
        self.rows = {}
        for p in (w['folder'] / 'imports').glob('*/import.json'):
            row = json.loads(p.read_text())
            if row['state'] == 'validating':
                subprocess.run(['docker','rm','-f','nexus-fastvideo-import-'+row['id']],capture_output=True,timeout=30)
                row['state'] = 'interrupted'; save(p, row)
            self.rows[row['id']] = row

    def folder(self, row): return self.w['folder'] / 'imports' / row['id']
    def persist(self, row): save(self.folder(row) / 'import.json', row)
    def base_for(self, family):
        if family == BASE_ID: return self.base
        if family == H3_ID: return self.root / 'models/minimax-h3'
        raise ValueError('Unsupported model family')

    def resolve(self, mid):
        if mid == BASE_ID:
            return self.base, {'id':BASE_ID, 'name':'Wan 2.1 T2V 1.3B', 'family':'wan2.1-t2v-1.3b', 'source':'official', 'revision':'0fad780a534b6463e45facd96134c9f345acfa5b', 'state':'ready'}
        if mid == H3_ID:
            return self.base_for(H3_ID), h3_status(self.root)
        row = self.rows.get(mid)
        if not row or row['state'] != 'ready': raise ValueError('MODEL_NOT_READY_OR_NOT_OWNED')
        return self.folder(row) / 'model', self.public(row)

    def public(self, row):
        out = {k:row[k] for k in ('id','name','state','files')}
        out.update(family=row.get('family', BASE_ID), source='private', quality_validated=False)
        out['uploaded_bytes'] = {f['name']: (self.folder(row) / 'uploads' / f['name']).stat().st_size if (self.folder(row) / 'uploads' / f['name']).exists() else 0 for f in row['files']}
        if row.get('error'): out['error'] = row['error']
        if row.get('tensor_count'): out['tensor_count'] = row['tensor_count']
        return out

    def begin(self, args, ctx):
        if len(self.rows) >= 4: raise ValueError('MODEL_IMPORT_LIMIT: four per workspace')
        name, files = args.get('name'), args.get('weight_files')
        family = args.get('family', BASE_ID)
        self.base_for(family)
        if family == H3_ID and not h3_status(self.root)['ready']:
            raise ValueError('H3_RUNTIME_NOT_READY: wait for real-weight acceptance before importing H3.')
        max_files, max_total = (64,160) if family == H3_ID else (8,16)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80: raise ValueError('Invalid model name')
        if not isinstance(files, list) or not 1 <= len(files) <= max_files: raise ValueError(f'Provide 1..{max_files} transformer safetensors files')
        seen = set(); total = 0; clean = []
        for f in files:
            n, size, sha = f.get('name'), f.get('size_bytes'), f.get('sha256')
            if not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,120}\.safetensors',n) or n in seen: raise ValueError('Invalid or duplicate filename')
            if type(size) is not int or not 8 < size <= 8*1024**3 or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha): raise ValueError('Provide size and SHA256 for every file')
            seen.add(n); total += size; clean.append({'name':n,'size_bytes':size,'sha256':sha})
        if total > max_total*1024**3: raise ValueError(f'Maximum import size is {max_total} GiB')
        row = {'id':uuid.uuid4().hex, 'name':name.strip(), 'state':'uploading', 'files':clean, 'family':family}
        self.rows[row['id']] = row; self.persist(row)
        # Large H3 checkpoints can exceed the active file-capability quota.
        # Issue bounded batches; remaining shards obtain a fresh link on demand.
        links = [self.upload(row, f['name'], ctx) for f in clean[:8]]
        return {**self.public(row), 'upload_links':links,
                'remaining_upload_files':[f['name'] for f in clean[8:]],
                'next_step':'Upload this batch. Use renew_model_upload for remaining shards; completed upload links are revoked to free capacity. Then finalize_model_import.'}

    def upload(self, row, filename, ctx):
        if row['state'] not in ('uploading','failed','interrupted'): raise ValueError('Import is sealed')
        item = next((f for f in row['files'] if f['name'] == filename), None)
        if not item: raise ValueError('FILE_NOT_FOUND')
        for f in row['files']:
            uploaded = self.folder(row) / 'uploads' / f['name']
            if uploaded.is_file() and uploaded.stat().st_size == f['size_bytes']:
                self.bridges.revoke_path(uploaded)
        path = self.folder(row) / 'uploads' / filename
        self.bridges.revoke_path(path)
        return {'name':filename, **self.bridges.create(ctx, path, 'upload', item['sha256'], item['size_bytes'])}

    def finalize(self, row):
        if row['state'] in ('validating','ready'): return self.public(row)
        if any(r['state']=='validating' for r in self.rows.values()): raise ValueError('Another import is validating')
        for f in row['files']:
            p = self.folder(row) / 'uploads' / f['name']
            self.bridges.revoke_path(p)
            if not p.is_file() or p.stat().st_size != f['size_bytes']: raise ValueError('UPLOAD_INCOMPLETE')
        row['state'] = 'validating'; row.pop('error',None); self.persist(row)
        threading.Thread(target=self.validate, args=(row,), daemon=True).start()
        return self.public(row)

    def validate(self, row):
        with VALIDATORS: self._validate(row)

    def _validate(self, row):
        folder = self.folder(row); output = folder / 'validation'; output.mkdir(exist_ok=True)
        for p in output.glob('*.json'): p.unlink()
        try:
            base = self.base_for(row.get('family',BASE_ID))
            cmd = ['docker','run','--rm','--init','--name','nexus-fastvideo-import-'+row['id'],'--network','none','--read-only',
                '--user',f'{os.getuid()}:{os.getgid()}','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--memory','16g','--cpus','4','--pids-limit','128','--tmpfs','/tmp:rw,nosuid,nodev,size=256m',
                '--mount',f'type=bind,src={base},dst=/base,readonly',
                '--mount',f'type=bind,src={folder},dst=/input,readonly','--mount',f'type=bind,src={output},dst=/output',
                '--mount',f'type=bind,src={self.root / "validate_import.py"},dst=/validate.py,readonly',
                '--entrypoint','python',self.image,'/validate.py']
            with (output/'validation.log').open('wb') as log:
                p = subprocess.Popen(cmd, stdout=log, stderr=log)
                try: code = p.wait(timeout=3600 if row.get('family') == H3_ID else 900)
                except subprocess.TimeoutExpired:
                    subprocess.run(['docker','rm','-f','nexus-fastvideo-import-'+row['id']],capture_output=True,timeout=30);p.wait(timeout=30);raise ValueError('Validation timed out')
            if code:
                error = output/'validation-error.json'
                raise ValueError(json.loads(error.read_text())['error'] if error.exists() else 'Validation failed')
            report = json.loads((output/'validated.json').read_text())
            model = folder/'model'
            if model.exists(): shutil.rmtree(model)
            # Base shared components are immutable. Hard links avoid copying 23 GB.
            shutil.copytree(base, model, copy_function=os.link, ignore=shutil.ignore_patterns('transformer','READY.json','WEIGHTS_VERIFIED.json','*.partial','*.chunks.json'))
            transformer = model/'transformer'; transformer.mkdir()
            shutil.copy2(base/'transformer/config.json',transformer/'config.json')
            for item in row['files']:
                os.link(folder/'uploads'/item['name'], transformer/item['name'])
            (transformer/'diffusion_pytorch_model.safetensors.index.json').write_text(json.dumps({'metadata':{'total_size':report['total_size']},'weight_map':report['weight_map']}))
            row['tensor_count'] = report['tensor_count']; row['state'] = 'ready'
        except Exception as exc:
            row['state'] = 'failed'; row['error'] = str(exc)[:300]
        finally: self.persist(row)

    def from_job(self, j, name):
        if j['state'] != 'succeeded': raise ValueError('Successful completed job required')
        if len(self.rows) >= 4: raise ValueError('MODEL_IMPORT_LIMIT')
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80: raise ValueError('Invalid model name')
        row = {'id':uuid.uuid4().hex,'name':name.strip(),'state':'uploading','files':[], 'family':j.get('family',BASE_ID)}
        for p in (j['folder']/'student/transformer').glob('*.safetensors'):
            row['files'].append({'name':p.name,'size_bytes':p.stat().st_size,'sha256':sha256(p)})
        if not row['files']: raise ValueError('No exported transformer')
        self.rows[row['id']] = row; self.persist(row)
        upload = self.folder(row)/'uploads';upload.mkdir()
        for f in row['files']: os.link(j['folder']/'student/transformer'/f['name'], upload/f['name'])
        return self.finalize(row)
