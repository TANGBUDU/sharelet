"""Native plugin grants and real dispatch boundaries; upstream E2E is separate."""
import json
from pathlib import Path
import tempfile
import unittest

from catalog import reviewed_catalog
from host import HermesBridge
from service import Runtime
from sharelet import CapsuleError


class Context:
    plugin_id='sharelet'
    def __init__(self):self.loaded=True;self.calls=[];self.mcp_calls=[];self.error=False
    def has_plugin(self,name):return self.loaded and name=='weather'
    def dispatch_tool(self,name,args):
        self.calls.append((name,args))
        return json.dumps({'error':'Unavailable'} if self.error else {'accepted':args})
    def call_mcp(self,*args,**kwargs):self.mcp_calls.append(args);raise AssertionError('A native tool must not use MCP')
    def get_config(self,name,default=None):return default


class NativeGrantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'existing-plugin';self.source.mkdir()
        (self.source/'plugin.yaml').write_text('name: weather\nversion: 2.1.0\n')
        self.code=self.source/'handler.py';self.code.write_text('def existing_handler(args):\n    return args\n')
        self.context=Context()
        self.grant={'plugin':'weather','tool':'wx_global','toolset':'weather','source_dir':str(self.source),
            'bounds':{'lat':{'minimum':-90,'maximum':90},'lon':{'minimum':-180,'maximum':180}}}
        self.config={'plugins':{'enabled':['sharelet','weather'],'entries':{'sharelet':{'native_tool_allowlist':[self.grant]}}}}
        self.inventory=[{'name':'wx_global','toolset':'weather','handler_source':str(self.code),
            'handler_module':'hermes_plugins.weather.handler','schema':{'name':'wx_global','description':'Existing weather tool',
                'parameters':{'type':'object','properties':{'lat':{'type':'number'},'lon':{'type':'number'}},'required':['lat','lon']}}}]
        self.bridge=HermesBridge(self.context,inventory=lambda:self.inventory,configuration=lambda:self.config)

    def tearDown(self):self.temp.cleanup()

    def test_exact_existing_tool_is_dispatched_through_public_context(self):
        row=self.bridge.catalog()[0]
        self.assertEqual(row['tool'],'wx_global');self.assertEqual(row['provenance']['kind'],'hermes_plugin')
        self.assertEqual(row['schema']['properties']['lat']['maximum'],90)
        result=self.bridge.call(row['server'],row['tool'],{'lat':35.6,'lon':139.6})
        self.assertEqual(result,{'ok':True,'result':{'accepted':{'lat':35.6,'lon':139.6}}})
        self.assertEqual(self.context.calls,[('wx_global',{'lat':35.6,'lon':139.6})]);self.assertFalse(self.context.mcp_calls)

    def test_revoked_grant_never_falls_back_to_mcp(self):
        self.config['plugins']['entries']['sharelet']['native_tool_allowlist']=[]
        self.assertFalse(self.bridge.catalog())
        with self.assertRaises(PermissionError):self.bridge.call('hermes-plugin-weather','wx_global',{'lat':1,'lon':2})
        self.assertFalse(self.context.calls);self.assertFalse(self.context.mcp_calls)

    def test_unloaded_disabled_or_different_source_cannot_be_shared(self):
        self.context.loaded=False;self.assertFalse(self.bridge.catalog())
        self.context.loaded=True;self.config['plugins']['enabled']=['sharelet'];self.assertFalse(self.bridge.catalog())
        self.config['plugins']['enabled'].append('weather')
        self.inventory[0]['handler_source']=str(self.root/'other-plugin.py');self.assertFalse(self.bridge.catalog())

    def test_management_and_mcp_names_cannot_be_exposed_as_native(self):
        for name in ('sharelet_status','mcp__weather__wx_global'):
            self.grant['tool']=name;self.inventory[0]['name']=name
            self.assertFalse(self.bridge.catalog())

    def test_original_handler_errors_are_not_success(self):
        self.context.error=True
        self.assertEqual(self.bridge.call('hermes-plugin-weather','wx_global',{'lat':1,'lon':2}),{'ok':False})

    def test_explicit_destructive_annotation_is_preserved_and_blocked(self):
        self.inventory[0]['schema']['annotations']={'destructiveHint':True}
        entry=self.bridge.catalog()[0]
        self.assertEqual(entry['safety'],'blocked');self.assertFalse(entry['supported'])
        with self.assertRaises(PermissionError):self.bridge.call(entry['server'],entry['tool'],{'lat':1,'lon':2})
        self.assertFalse(self.context.calls)

    def test_changed_plugin_source_blocks_existing_share_before_charging(self):
        runtime=Runtime(self.root/'data',self.bridge.call,self.bridge.catalog,connection={'mode':'test'},port=0,guest_port=0).start()
        try:
            tool=reviewed_catalog(self.bridge.catalog())[0]
            token=runtime.manager.create(title='Approved native tool',server=tool['server'],tool=tool['tool'],
                schema=tool['schema'],tool_id=tool['id'],public_safe_approved=True,public_enabled=False,
                side_effects_approved=True,tool_review_hash=tool['review_hash'])
            self.code.write_text('def changed_handler(args):\n    return {"different": args}\n')
            with self.assertRaises(CapsuleError) as rejected:runtime.jobs.submit(token,{'lat':35,'lon':139},'a'*32)
            self.assertEqual(rejected.exception.code,'tool_approval_required')
            self.assertEqual(runtime.jobs.usage()['used'],0);self.assertFalse(self.context.calls)
        finally:
            runtime.close();runtime.jobs.executor.shutdown(wait=True,cancel_futures=True)


if __name__=='__main__':unittest.main()
