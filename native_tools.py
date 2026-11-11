"""Explicit grants for existing Hermes plugin tools, dispatched through public ctx.

The registered handler must come from the configured plugin's source directory.
Its source fingerprint is part of the owner review; no tool implementation lives here.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

try:
    from .catalog import CONTROL_TOOLS,UTILITY_TOOLS,tool_binding
except ImportError:
    from catalog import CONTROL_TOOLS,UTILITY_TOOLS,tool_binding

PREFIX='hermes-plugin-'
NAME=re.compile(r'[A-Za-z0-9_.-]{1,70}')


def configured_grants(config,plugin_id):
    values=config.get('plugins',{}).get('entries',{}).get(plugin_id,{}).get('native_tool_allowlist',[])
    if not isinstance(values,list) or len(values)>16:return []
    return [grant for grant in values if isinstance(grant,dict)]


def source_fingerprint(root):
    paths=[root/'plugin.yaml',*sorted(root.rglob('*.py'))]
    if len(paths)>256:return None
    total=0;digest=hashlib.sha256()
    for path in paths:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):return None
        size=path.stat().st_size;total+=size
        if total>4*1024*1024:return None
        digest.update(path.relative_to(root).as_posix().encode());digest.update(b'\0');digest.update(path.read_bytes())
    return digest.hexdigest()


def native_catalog(ctx,config,inventory):
    result=[]
    enabled=config.get('plugins',{}).get('enabled')
    for grant in configured_grants(config,ctx.plugin_id):
        plugin,tool,toolset=(grant.get(key) for key in ('plugin','tool','toolset'))
        if any(not isinstance(value,str) or not NAME.fullmatch(value) for value in (plugin,tool,toolset)):continue
        if tool in CONTROL_TOOLS or tool in UTILITY_TOOLS or tool.startswith('mcp__'):continue
        if isinstance(enabled,list) and plugin not in enabled:continue
        if not ctx.has_plugin(plugin):continue
        current=next((row for row in inventory if row['name']==tool and row['toolset']==toolset),None)
        if not current or not isinstance(current.get('handler_source'),str) or not isinstance(current.get('schema'),dict):continue
        if not isinstance(grant.get('source_dir'),str):continue
        root=Path(grant['source_dir']).expanduser()
        if not root.is_absolute():continue
        root=root.resolve();handler=Path(current['handler_source']).resolve()
        if not handler.is_relative_to(root) or not (root/'plugin.yaml').is_file():continue
        fingerprint=source_fingerprint(root)
        if fingerprint is None:continue
        schema=deepcopy(current['schema'].get('parameters',{}))
        bounds=grant.get('bounds',{})
        if not isinstance(bounds,dict):continue
        valid=True
        for name,limits in bounds.items():
            field=schema.get('properties',{}).get(name)
            if (not isinstance(field,dict) or field.get('type') not in ('number','integer')
                    or not isinstance(limits,dict) or set(limits)-{'minimum','maximum'}
                    or any(type(value) not in (int,float) for value in limits.values())):
                valid=False;break
            # Host constraints only narrow the original native tool's contract.
            if 'minimum' in limits:field['minimum']=max(field.get('minimum',-1000000),limits['minimum'])
            if 'maximum' in limits:field['maximum']=min(field.get('maximum',1000000),limits['maximum'])
        if not valid:continue
        entry=tool_binding(PREFIX+plugin,tool,schema,title=tool,
            description=current['schema'].get('description',''),
            annotations=current.get('annotations',current['schema'].get('annotations')))
        entry['source_revision']=hashlib.sha256(json.dumps({'grant':grant,'source_sha256':fingerprint,
            'handler':handler.relative_to(root).as_posix(),'module':current.get('handler_module')},sort_keys=True).encode()).hexdigest()
        version=re.search(r'^version:\s*[\"\']?([^\s\"\']+)',(root/'plugin.yaml').read_text(encoding='utf-8'),re.M)
        entry.update(source='Hermes plugin: '+plugin,
            provenance={'kind':'hermes_plugin','plugin':plugin,'version':version[1] if version else '',
                'tool':tool,'source_sha256':fingerprint,'handler_file':handler.relative_to(root).as_posix()})
        if grant.get('presentation')=='weather':entry['style']='weather'
        result.append(entry)
    return result
