#!/usr/bin/env python3
"""Run in the worker environment; query the provider catalogue without generation."""
import asyncio
import json
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAIError
from app.config import settings
from app.llm import provider_error_details
from app.llm_composition import create_llm_client
from app.services.public_errors import public_generation_error


async def main():
    client = None
    try:
        client = create_llm_client(settings)
        result = {**await client.check_connection(), 'scope': 'current_process'}
    except (OpenAIError, RuntimeError) as exc:
        result = {'status': 'unavailable', 'scope': 'current_process', 'generation_tested': False,
                  'error': public_generation_error(exc), 'details': provider_error_details(exc)}
    finally:
        if client is not None:
            await client.raw_client.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'ready' else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
