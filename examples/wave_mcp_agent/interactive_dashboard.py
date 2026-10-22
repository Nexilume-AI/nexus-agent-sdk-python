"""An ordinary SDK client: no custom Cloud route or inbound Agent port."""
import json
from pathlib import Path
from nexus_agent import current_run
from public_viewer import unpack


def open_dashboard(service, worker, auth, identity, template):
    views = getattr(worker, 'interactive_views', [])
    if len(views) >= 2:
        # Replace the oldest dashboard, keeping the worker's UI resource use bounded.
        views.pop(0).close()
    state = {'summary': 'Explore your private Wave workspace. Queries execute on the Agent and stream results back here.',
             'metrics': [{'label': 'Workspace', 'value': worker.id[:8]}, {'label': 'Queries', 'value': 0}], 'logs': []}
    count = 0

    def refresh(args, view):
        nonlocal count
        data = unpack(service.call('list_wave_files', auth, identity))
        count += 1
        state['metrics'][1]['value'] = count
        state['logs'] = [json.dumps(data, ensure_ascii=False)[:16000]]
        view.set_state(state)
        return {'refreshed': True}

    def query(args, view):
        nonlocal count
        data = unpack(service.call('signal_values', {**auth, 'session_id': args['session_id'],
            'paths': [args['path']], 'start': args['start'], 'end': args['end']}, identity))
        count += 1
        state['metrics'][1]['value'] = count
        state['logs'] = [json.dumps(data, ensure_ascii=False)[:16000]]
        view.set_state(state)
        return {'queried': True}

    actions = {
        'refresh': {'label': 'List workspace files', 'input_schema': {'type': 'object', 'additionalProperties': False}},
        'query': {'label': 'Inspect signal', 'input_schema': {'type': 'object', 'properties': {
            'session_id': {'type': 'string', 'maxLength': 128}, 'path': {'type': 'string', 'maxLength': 256},
            'start': {'type': 'string', 'default': '0ns', 'maxLength': 32},
            'end': {'type': 'string', 'default': '50ns', 'maxLength': 32}},
            'required': ['session_id', 'path', 'start', 'end'], 'additionalProperties': False}}}
    html = Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8') if template == 'custom' else None
    view = current_run().views.create(title='Wave workspace', state=state, actions=actions,
                                     handlers={'refresh': refresh, 'query': query}, html=html)
    views.append(view); worker.interactive_views = views
    return view.public_info
