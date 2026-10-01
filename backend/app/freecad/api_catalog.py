"""Read-only, paged lookup of a catalog measured in the pinned runtime."""
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class CapabilityQuery(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    category: Literal['api', 'command', 'workbench', 'object_type'] = 'api'
    module: str | None = None
    symbol: str | None = None
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=8,ge=1,le=32)


def query_capabilities(query: CapabilityQuery):
    path=Path(__file__).with_name('api_catalog.json')
    if not path.is_file():
        raise ValueError('native API catalog has not been generated for this release')
    catalog=json.loads(path.read_text())
    if catalog['freecad']!='1.1.3':
        raise ValueError('native API catalog runtime version mismatch')
    if query.category != 'api':
        if query.module is not None:
            raise ValueError('module filter applies only to API queries')
        key = {'command':'registered_commands', 'workbench':'registered_workbenches',
               'object_type':'registered_object_types'}[query.category]
        if key not in catalog:
            raise ValueError('this capability category has not been probed in the release runtime')
        entries = [{'name': name, 'status': 'registered_unverified'} for name in catalog[key]]
        if query.symbol:
            entries = [entry for entry in entries if query.symbol.casefold() in entry['name'].casefold()]
    elif query.module is None:
        entries=[{k:v for k,v in m.items() if k!='symbols'} for m in catalog['modules']]
    else:
        modules=[m for m in catalog['modules'] if m['module']==query.module]
        if not modules:
            raise ValueError('module is absent from the measured native catalog')
        if modules[0]['status']!='importable_unverified':
            return {**modules[0],'freecad':catalog['freecad']}
        entries=modules[0]['symbols']
        if query.symbol:
            entries=[e for e in entries if query.symbol.casefold() in e['name'].casefold()]
    return {'freecad':catalog['freecad'],'environment':'gui' if catalog['gui'] else 'headless',
        'functional_coverage':catalog['functional_coverage'],'total':len(entries),
        'entries':entries[query.offset:query.offset+query.limit],
        'next_offset':query.offset+query.limit if query.offset+query.limit<len(entries) else None}
