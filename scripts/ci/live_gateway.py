"""Transparent provider relay with reservations for JSON and SSE completions."""
from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from scripts.ci.live_budget import BudgetExceeded


class StreamUsage:
    """Only a complete provider stream may release a cost reservation."""

    def __init__(self):
        self.data = []
        self.usage = None
        self.done = False

    def feed(self, line):
        if line.startswith('data:'):
            if self.done:
                raise ValueError('provider data after stream completion')
            self.data.append(line[5:].lstrip(' '))
        elif not line and self.data:
            value = '\n'.join(self.data)
            self.data.clear()
            if value == '[DONE]':
                self.done = True
                return
            payload = json.loads(value)
            if payload.get('error'):
                raise ValueError('provider reported a stream error')
            if payload.get('usage') is not None:
                if self.usage is not None:
                    raise ValueError('duplicate provider usage')
                self.usage = payload['usage']

    def completed_usage(self):
        if not self.done or self.data or not isinstance(self.usage, dict):
            raise ValueError('provider stream lacks completion or usage')
        return self.usage


def create_app(budget, provider_url, provider_key, failures, *, changed=lambda: None):
    app = FastAPI()

    @app.post('/v1/chat/completions')
    async def completions(request: Request):
        payload = await request.json()
        try:
            slot = budget.reserve(payload.get('model'), payload.get(
                'max_completion_tokens', payload.get('max_tokens', budget.limits.output)))
            changed()  # Persist the reservation before sending a paid request.
        except (BudgetExceeded, ValueError) as error:
            failures.append(str(error))
            changed()
            return JSONResponse({'error': str(error)}, status_code=402)
        if payload.get('stream'):
            payload['stream_options'] = {**payload.get('stream_options', {}), 'include_usage': True}
        # Retrying connection establishment cannot repeat an HTTP inference;
        # read/write failures are never retried here.
        client = httpx.AsyncClient(timeout=300, transport=httpx.AsyncHTTPTransport(retries=2))
        response = None
        try:
            response = await client.send(client.build_request('POST',
                provider_url.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + provider_key}, json=payload), stream=True)
            if not payload.get('stream') or response.status_code >= 400:
                data = json.loads(await response.aread())
                if not isinstance(data, dict):
                    raise ValueError('provider JSON response must be an object')
                if response.status_code >= 400:
                    failures.append('provider HTTP ' + str(response.status_code))
                else:
                    budget.settle(slot, data.get('usage'))
                    if not budget.records[slot]['usage_verified']:
                        failures.append('provider JSON lacks usage')
                changed()
                return JSONResponse(data, status_code=response.status_code)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            budget.release_unsubmitted(slot, type(error).__name__)
            changed()
            await client.aclose()
            return JSONResponse({'error': 'provider connection was not established'}, status_code=502)
        except (httpx.HTTPError, ValueError) as error:
            failures.append(type(error).__name__)
            changed()
            if response is not None:
                await response.aclose()
            await client.aclose()
            return JSONResponse({'error': 'live evaluation request/usage failed'}, status_code=502)
        finally:
            if response is not None and (not payload.get('stream') or response.status_code >= 400):
                await response.aclose()
                await client.aclose()

        async def relay():
            usage = StreamUsage()
            settled = False
            try:
                async for line in response.aiter_lines():
                    usage.feed(line)
                    if usage.done and not settled:
                        budget.settle(slot, usage.completed_usage())
                        if not budget.records[slot]['usage_verified']:
                            raise ValueError('provider stream lacks token counts')
                        settled = True
                        changed()
                    yield line + '\n'
                if not settled:
                    raise ValueError('provider stream lacks completion')
            except BaseException as error:
                # Includes client disconnect/cancellation: retain its reservation.
                if not settled or not isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                    failures.append(type(error).__name__)
                raise
            finally:
                changed()
                await response.aclose()
                await client.aclose()

        return StreamingResponse(relay(), media_type='text/event-stream')

    return app
