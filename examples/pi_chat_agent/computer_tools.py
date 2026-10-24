"""Pi tools for the caller's Run-bound Computer; credentials stay in the SDK."""
import json
from dataclasses import asdict
from nexus_agent.reporting import NexusComputerError
import browser_tools

SCOPES = {'computer_list': 'files.list', 'computer_read': 'files.read',
          'computer_write': 'files.write', 'computer_exec': 'command.execute'}
SCOPES.update({name: 'browser.control' for name in browser_tools.NAMES})


def descriptors(ctx):
    if not ctx.computer.enabled:
        return []
    definitions = [
        ('computer_list', 'List files on the attached Computer, relative to the current authorized working directory.',
         {'path': {'type': 'string', 'default': '.'}}, []),
        ('computer_read', 'Read a UTF-8 text file on the attached Computer. Paths are relative to its current authorized working directory.',
         {'path': {'type': 'string'}}, ['path']),
        ('computer_write', 'Create or replace a UTF-8 text file on the attached Computer. This changes the user device. Read existing files first; only make changes requested by the user.',
         {'path': {'type': 'string'}, 'content': {'type': 'string', 'maxLength': 262144}}, ['path', 'content']),
        ('computer_exec', 'Execute a command on the attached Computer using cmd.exe on Windows or the system shell on Linux/macOS. For PowerShell syntax on Windows, explicitly invoke powershell.exe -NoProfile -Command. This operates the user device, not the offline Linux sandbox. Use cwd relative to the authorized working directory; output appears in Private Display Shell. First inspect the environment with a harmless command. Do only what the user requested; ask before destructive or unrelated operations. Never collect credentials.',
         {'command': {'type': 'string', 'maxLength': 16000}, 'cwd': {'type': 'string', 'default': '.'},
          'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 60, 'default': 30}}, ['command']),
    ]
    return [{'name': name, 'label': name.replace('_', ' ').title(), 'description': description,
             'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
            for name, description, properties, required in definitions if SCOPES[name] in ctx.workspace_capabilities] + browser_tools.descriptors(ctx)


def invoke(ctx, name, args):
    # Only an explicit fixed operation set. Never accept a URL, connection ID,
    # token or method name from the model. Cloud revalidates the Run delegate,
    # capability, expiry and bound Computer on every SDK request.
    if not ctx.computer.enabled:
        raise ValueError('COMPUTER_NOT_ATTACHED')
    if name not in SCOPES or SCOPES[name] not in ctx.workspace_capabilities:
        raise ValueError('COMPUTER_CAPABILITY_DENIED')
    if name in browser_tools.NAMES:
        return browser_tools.invoke(ctx, name, args)
    allowed = {'computer_list': {'path'}, 'computer_read': {'path'},
               'computer_write': {'path', 'content'}, 'computer_exec': {'command', 'cwd', 'timeout'}}[name]
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError('INVALID_COMPUTER_ARGUMENTS')
    def string(key, default=None, limit=4096):
        value = args.get(key, default)
        if not isinstance(value, str) or not value or len(value) > limit or '\0' in value:
            raise ValueError('INVALID_COMPUTER_ARGUMENTS')
        return value
    try:
        if name == 'computer_list':
            entries = ctx.workspace.list(string('path', '.'))
            result = {'entries': [asdict(x) for x in entries[:500]], 'truncated': len(entries) > 500}
        elif name == 'computer_read':
            text = ctx.workspace.read_text(string('path'))
            result = {'content': text[:262144], 'truncated': len(text) > 262144}
        elif name == 'computer_write':
            content = args.get('content')
            if not isinstance(content, str) or len(content) > 262144:
                raise ValueError('INVALID_COMPUTER_ARGUMENTS')
            ctx.workspace.write_text(string('path'), content)
            result = {'written': True}
        else:
            timeout = args.get('timeout', 30)
            if type(timeout) is not int or not 1 <= timeout <= 60:
                raise ValueError('INVALID_COMPUTER_ARGUMENTS')
            output = ctx.terminal.run(string('command', limit=16000), cwd=string('cwd', '.'), timeout=timeout)
            result = {k: output.get(k) for k in ('exit_code', 'status', 'duration_ms')}
            for k in ('stdout', 'stderr'):
                text = str(output.get(k) or '')
                result[k] = text[:131072]
                result[k + '_truncated'] = len(text) > 131072
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
    except NexusComputerError as exc:
        # Safe SDK diagnostic only; do not return raw HTTP bodies or credentials.
        return {'content': [{'type': 'text', 'text': 'Attached Computer operation failed: ' + str(exc)}], 'isError': True}
