"""Official wave-mcp server with bounded, workspace-local file transfer tools."""
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any
from wave_mcp import server

ROOT = Path('/work')


def safe_file(filename):
    if not isinstance(filename, str) or not filename or '\\' in filename:
        raise ValueError('Use a relative workspace filename.')
    path = ROOT / filename
    if path.is_absolute() and not path.resolve().is_relative_to(ROOT):
        raise ValueError('File must be inside /work.')
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('Symlinks are not allowed.')
    if any(part in ('..', '.') for part in Path(filename).parts):
        raise ValueError('Invalid filename.')
    return path


@server.mcp.tool()
def upload_wave_file(filename: str, data_base64: str, offset: int = 0) -> dict[str, Any]:
    """Upload at most 1024 decoded bytes per call. filename is relative to /work; offset is a byte offset. Identical chunk retries are idempotent; conflicting writes are refused. Use next_offset until complete, then prepare_session/open_static_session with /work paths. New filenames are recommended for changed designs."""
    path = safe_file(filename)
    chunk = base64.b64decode(data_base64, validate=True)
    if not 0 < len(chunk) <= 1024 or offset < 0:
        raise ValueError('Use 1..1024 bytes and nonnegative offset.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('r+b' if path.exists() else 'w+b') as file:
        file.seek(0, 2); size = file.tell()
        if offset > size: raise ValueError('Upload offset is beyond EOF.')
        if offset < size:
            file.seek(offset)
            if file.read(len(chunk)) != chunk: raise ValueError('Conflicting retry; use a new filename.')
        else:
            file.write(chunk)
    return {'filename':filename, 'path':str(path), 'next_offset':offset+len(chunk), 'size':path.stat().st_size}


@server.mcp.tool()
def read_wave_file(filename: str, offset: int = 0, limit: int = 24576) -> dict[str, Any]:
    """Download a workspace file in base64 pages. Offsets are bytes; continue with next_offset until eof. limit is 1..24576 bytes. SHA256 covers the complete file. Download needed artifacts before the workspace expires or closes."""
    path = safe_file(filename)
    if not path.is_file() or not 0 <= offset <= path.stat().st_size or not 1 <= limit <= 24576:
        raise ValueError('Invalid file, offset or limit.')
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024*1024), b''): digest.update(chunk)
        file.seek(offset); data = file.read(limit)
    return {'filename':filename, 'data_base64':base64.b64encode(data).decode(), 'sha256':digest.hexdigest(),
            'next_offset':offset+len(data), 'size':path.stat().st_size, 'eof':offset+len(data)>=path.stat().st_size}


@server.mcp.tool()
def list_wave_files(prefix: str = '', offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """List workspace files and sizes, up to 100 per page. prefix filters relative filenames. offset/next_offset paginate; paths are under /work. The filesystem has a hard 256 MiB workspace quota."""
    if offset < 0 or not 1 <= limit <= 100: raise ValueError('Invalid page.')
    files = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and not p.is_symlink())
    files = [p for p in files if p.startswith(prefix)]
    return {'files':[{'filename':p,'size':safe_file(p).stat().st_size} for p in files[offset:offset+limit]],
            'next_offset':offset+limit, 'eof':offset+limit>=len(files), 'total':len(files)}


@server.mcp.tool()
def _nexus_export_wave_view(view_id: str, export: bool = False) -> dict[str, Any]:
    """Private bridge RPC; never advertise through the public agent."""
    import copy
    from wave_mcp.viewer.manager import ViewManager, _fst_meta
    from wave_mcp.viewer.translate import desired_to_sucl
    if not re.fullmatch(r'[a-f0-9]{8}', view_id):
        raise ValueError('Invalid view ID.')
    view = ViewManager.instance()._views.get(view_id)
    if not view:
        raise ValueError('View has expired.')
    desired = view['state'].snapshot()['desired']
    sources = desired['waveform']['sources']
    if not 1 <= len(sources) <= 2:
        raise ValueError('Use one or two waveforms.')
    panels, files = [], []
    for i, source in enumerate(sources):
        state = copy.deepcopy(desired)
        state['waveform']['sources'] = [source]
        state['signals'] = [s for s in state.get('signals', []) if not s.get('source') or s['source'] == source['id']]
        _, timescale = _fst_meta(source['path'])
        if timescale is None:
            raise ValueError('Waveform timescale is unavailable.')
        panels.append({'label': source.get('label') or 'Waveform '+str(i+1),
                       'commands': desired_to_sucl(state, timescale)})
        if export:
            path = Path(source['path']).resolve()
            # Converted cache files can be in /tmp; copy only an already-open, resolved FST.
            if not (path.is_relative_to('/work') or path.is_relative_to('/tmp')) or not path.is_file() or path.stat().st_size > 268435456:
                raise ValueError('Waveform outside workspace or too large.')
            dest = safe_file('.nexus-exports/'+view_id+'/'+str(i)+'.fst')
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, dest)
            except OSError:
                shutil.copyfile(path, dest)
            files.append(str(dest))
    notes = desired.get('annotations') or []
    return {'snapshot': {'panels': panels, 'annotation': '\n'.join(str(n.get('markdown', '')) for n in notes)}, 'files': files}


if __name__ == '__main__':
    if os.environ.get('WAVE_SAMPLE') == '1':
        import importlib.util
        spec = importlib.util.spec_from_file_location('sample', '/opt/wave-mcp/examples/sample/make_sample.py')
        sample = importlib.util.module_from_spec(spec); spec.loader.exec_module(sample)
        dest = ROOT/'sample'; dest.mkdir(exist_ok=True)
        for name, value in [('dump.vcd',sample.VCD),('counter.sv',sample.SV),('top_tb.sv',sample.TB),('rtl.f','counter.sv\ntop_tb.sv\n')]:
            (dest/name).write_text(value)
    server.main()
