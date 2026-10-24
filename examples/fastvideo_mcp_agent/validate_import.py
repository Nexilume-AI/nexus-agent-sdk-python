"""CPU-only sandboxed safetensors validation. Never import checkpoint Python."""
import hashlib
import json
from pathlib import Path
import struct
import torch
from safetensors import safe_open


def header(path):
    with path.open('rb') as f:
        size = struct.unpack('<Q', f.read(8))[0]
        if not 0 < size <= 16 * 1024 * 1024: raise ValueError('Invalid safetensors header')
        value = json.loads(f.read(size))
    return {k: v for k, v in value.items() if k != '__metadata__'}


def main():
    spec = json.loads(Path('/input/import.json').read_text())
    expected = {}
    for p in Path('/base/transformer').glob('*.safetensors'): expected.update(header(p))
    found, weight_map, total = {}, {}, 0
    for item in spec['files']:
        p = Path('/input/uploads') / item['name']
        if p.stat().st_size != item['size_bytes']: raise ValueError('File size mismatch')
        h = hashlib.sha256()
        with p.open('rb') as f:
            while data := f.read(4*1024*1024): h.update(data)
        if h.hexdigest() != item['sha256']: raise ValueError('File SHA256 mismatch')
        meta = header(p)
        for name, info in meta.items():
            if name in found or name not in expected or info['shape'] != expected[name]['shape'] or info['dtype'] not in ('F32','F16','BF16'):
                raise ValueError('Incompatible tensor: '+name[:120])
            found[name] = info; weight_map[name] = item['name']
        with safe_open(p, framework='pt', device='cpu') as f:
            if set(f.keys()) != set(meta): raise ValueError('Tensor index mismatch')
            for name in f.keys():
                tensor = f.get_tensor(name)
                if not torch.isfinite(tensor).all().item(): raise ValueError('Non-finite tensor: '+name[:120])
                total += tensor.numel() * tensor.element_size()
    if set(found) != set(expected): raise ValueError('Missing transformer tensors')
    Path('/output/validated.json').write_text(json.dumps({'valid': True, 'tensor_count':len(found),
        'total_size':total, 'weight_map':weight_map, 'files':spec['files'], 'all_tensors_finite':True}))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        Path('/output/validation-error.json').write_text(json.dumps({'error':str(exc)[:300]}))
        raise
