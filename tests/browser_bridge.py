"""Adapta Playwright a ASGI en entornos donde el navegador no alcanza localhost."""

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

from cancha.web import create_app


async def run(app):
    async with app.router.lifespan_context(app):
        print('ready', flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            event = asyncio.Event()
            messages = []
            body = request['body'].encode()

            async def receive():
                if not event.is_set():
                    event.set()
                    return {'type': 'http.request', 'body': body}
                await asyncio.Future()

            async def send(message):
                messages.append(message)

            headers = {k.lower(): v for k, v in request['headers'].items()}
            headers.update(host='127.0.0.1', **{'content-length': str(len(body))})
            await app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1',
                       'method': request['method'], 'path': request['path'], 'query_string': b'',
                       'headers': [(k.encode(), v.encode()) for k, v in headers.items()],
                       'scheme': 'http', 'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8765)}, receive, send)
            response, *chunks = messages
            content = b''.join(m.get('body', b'') for m in chunks)
            print(json.dumps(dict(status=response['status'],
                                  headers={k.decode(): v.decode() for k, v in response['headers']},
                                  body=base64.b64encode(content).decode())), flush=True)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(path=Path(directory) / 'test.db', password='cancha-test-local-2026', demo=True, run_jobs=False)
        asyncio.run(run(app))
