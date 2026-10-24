"""Check exported tensors and produce a download integrity manifest."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import torch
from safetensors import safe_open


def verify_student(student, base):
    student, base = Path(student), Path(base)
    manifest = []
    changed = False
    count = 0
    seen = set()
    with ExitStack() as stack:
        originals = {}
        for path in sorted((base / 'transformer').glob('*.safetensors')):
            handle = stack.enter_context(safe_open(str(path), framework='pt', device='cpu'))
            originals.update({key: handle for key in handle.keys()})
        if not originals:
            raise ValueError('No base transformer tensors to compare')
        for path in sorted((student / 'transformer').glob('*.safetensors')):
            handle = stack.enter_context(safe_open(str(path), framework='pt', device='cpu'))
            for key in handle.keys():
                tensor = handle.get_tensor(key)
                if key in seen or key not in originals:
                    raise ValueError('Unexpected or duplicate student tensor: ' + key)
                seen.add(key)
                if list(tensor.shape) != originals[key].get_slice(key).get_shape():
                    raise ValueError('Student/base tensor shape mismatch: ' + key)
                if tensor.is_floating_point() and not bool(torch.isfinite(tensor).all()):
                    raise ValueError('Nonfinite student tensor: ' + key)
                if not changed:
                    reference = originals[key].get_tensor(key)
                    if tensor.shape != reference.shape:
                        raise ValueError('Student/base tensor shape mismatch: ' + key)
                    # A dtype conversion alone is not a learned weight update.
                    changed = not torch.equal(tensor, reference.to(tensor.dtype))
                count += 1
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while data := stream.read(8 * 1024 * 1024):
                    digest.update(data)
            manifest.append({'path': str(path.relative_to(student)),
                'size_bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
        if seen != set(originals):
            raise ValueError('Student export is missing transformer tensors')
    if not count or not changed:
        raise ValueError('Exported student has no changed transformer weights')
    return {'all_tensors_finite': True, 'changed_from_base': changed,
        'tensor_count': count, 'files': manifest}
