"""Copy selected immutable FST outputs, never expose loopback URLs or worker tokens."""
import json
import re
import subprocess
import tempfile
from pathlib import Path
from nexus_agent import current_run
from nexus_agent.wave_views import RunWaveViews


def unpack(value):
    if value.get('isError'):
        raise ValueError('Wave viewer operation failed in isolated worker.')
    data = value.get('structuredContent')
    if data is None:
        data = json.loads(next(c['text'] for c in value.get('content', []) if c.get('type') == 'text'))
    if isinstance(data, dict) and set(data) == {'result'}:
        data = data['result']
    if not isinstance(data, dict) or data.get('status') == 'error' or data.get('available') is False:
        raise ValueError('Waveform viewer is unavailable or arguments were rejected.')
    return data


def rpc(worker, name, args):
    return unpack(worker.rpc('tools/call', {'name': name, 'arguments': args}))


def call(worker, name, args):
    context = current_run()
    cloud = RunWaveViews(context)
    if not hasattr(worker, 'public_views'):
        worker.public_views = {}
    views = worker.public_views
    if name == 'list_wave_views':
        return {'views': [dict(view_id=vid, **cloud.action(pid)) for vid, pid in views.items()]}
    if name == 'close_wave_view':
        targets = list(views) if args.get('all_views') else [args.get('view_id')]
        for vid in targets:
            if vid not in views:
                raise ValueError('VIEW_NOT_FOUND')
            cloud.action(views[vid], 'close')
            rpc(worker, 'close_wave_view', {'view_id': vid})
            del views[vid]
        return {'closed': targets}
    if name == 'open_wave_view':
        if len(views) >= 4:
            raise ValueError('VIEW_LIMIT; close an existing view first.')
        opened = rpc(worker, name, args)
        vid = opened['view_id']
        try:
            exported = rpc(worker, '_nexus_export_wave_view', {'view_id': vid, 'export': True})
            ids = []
            with tempfile.TemporaryDirectory(prefix='nexus-wave-') as directory:
                for i, source in enumerate(exported['files']):
                    if source != f'/work/.nexus-exports/{vid}/{i}.fst' or not re.fullmatch('[a-f0-9]{8}', vid):
                        raise ValueError('Invalid isolated export path.')
                    dest = Path(directory) / f'waveform-{i+1}.fst'
                    # Docker's archive API cannot see tmpfs mounts on all engines.
                    # Read the fixed, bridge-created path inside the isolated worker.
                    with dest.open('xb') as output:
                        subprocess.run(['docker', 'exec', worker.name, 'cat', source], check=True,
                                       stdout=output, stderr=subprocess.DEVNULL, timeout=45)
                    if dest.is_symlink() or not dest.is_file() or dest.stat().st_size > 268435456:
                        raise ValueError('Invalid waveform export.')
                    ids.append(context.files.upload(dest, timeout=120)['file_id'])
            published = cloud.create(ids, exported['snapshot'])
            views[vid] = published['public_view_id']
            return {'view_id': vid, 'available': True, **published,
                    'message': 'Open the complete URL in a browser. This one-time link grants access only to this waveform view. Use get_view_state for a fresh link.'}
        except Exception:
            rpc(worker, 'close_wave_view', {'view_id': vid})
            raise
        finally:
            if re.fullmatch('[a-f0-9]{8}', vid):
                subprocess.run(['docker','exec',worker.name,'rm','-rf','/work/.nexus-exports/'+vid],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    vid = args.get('view_id')
    if vid not in views:
        raise ValueError('VIEW_NOT_FOUND')
    if name == 'update_wave_view':
        rpc(worker, name, args)
        exported = rpc(worker, '_nexus_export_wave_view', {'view_id': vid})
        value = cloud.action(views[vid], 'update', snapshot=exported['snapshot'])
        return {'view_id': vid, **value, 'message': 'Updated browser view through server events.'}
    return {'view_id': vid, **cloud.action(views[vid])}


def close_all(worker):
    cloud = RunWaveViews(current_run())
    for pid in list(getattr(worker, 'public_views', {}).values()):
        cloud.action(pid, 'close')
    worker.public_views = {}
