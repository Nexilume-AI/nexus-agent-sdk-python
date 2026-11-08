"""One public HTTPS completion; secrets enter only through stdin."""
import json
import sys
from urllib.request import Request
from model_transport import open_model_request, validate_base_url


def main():
    value = json.loads(sys.stdin.buffer.read(786432))
    grant = value['grant']
    base = grant['base_url'].rstrip('/')
    validate_base_url(base)
    incoming=value['request']
    data={k:v for k,v in incoming.items() if k in ('messages','tools','tool_choice','response_format','parallel_tool_calls')}
    data.update(model=grant['model'],max_completion_tokens=4096,stream=False)
    req = Request(base+'/chat/completions', data=json.dumps(data).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer '+grant['secret'], 'User-Agent': 'Nexus-VERISURE-Agent/1'})
    with open_model_request(req, timeout=180) as response:
        raw = b''
        while len(raw) <= 1048576:
            chunk = response.read1(65536)
            if not chunk:
                break
            raw += chunk
    if len(raw) > 1048576:
        raise ValueError('MODEL_RESPONSE_LIMIT')
    data = json.loads(raw)
    if not isinstance(data.get('choices'),list) or not data['choices']:raise ValueError('INVALID_MODEL_RESPONSE')
    print(json.dumps(data).replace(grant['secret'],'[REDACTED]'))



if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
