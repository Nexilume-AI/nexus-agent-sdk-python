"""One public HTTPS completion; secrets enter only through stdin."""
import json
import sys
from urllib.request import Request
from model_transport import open_model_request, validate_base_url


def main():
    value = json.loads(sys.stdin.buffer.read(262144))
    grant = value['grant']
    base = grant['base_url'].rstrip('/')
    validate_base_url(base)
    data = {'model': grant['model'], 'messages': value['messages'], 'max_completion_tokens': 4096, 'stream': False}
    req = Request(base+'/chat/completions', data=json.dumps(data).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer '+grant['secret'], 'User-Agent': 'Nexus-AVA-Agent/1'})
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
    text = data['choices'][0]['message']['content']
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 131072:
        raise ValueError('INVALID_MODEL_RESPONSE')
    print(json.dumps({'text': text.replace(grant['secret'], '[REDACTED]'),
                      'usage': {k: v for k, v in data.get('usage', {}).items() if type(v) == int}}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
