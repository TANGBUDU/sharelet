import concurrent.futures
import copy
import json
import pathlib
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sharelet import CapsuleError, CapsuleManager, normalise_schema
from webserver import create_server

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 40},
    "action": {"type": "string", "enum": ["lookup"], "maxLength": 10},
    "limit": {"type": "integer", "minimum": 1, "maximum": 7}
}, "required": ["query", "action"]}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.now = [1000.0]
        def call(server, tool, args):
            self.calls.append((server, tool, args))
            return {"ok": True, "result": {"answer": args["query"]}}
        self.dispatch = call
        self.m = CapsuleManager(self.temp.name, call, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **overrides):
        args = dict(title="Public lookup", server="public-tools", tool="repo.lookup", schema=copy.deepcopy(SCHEMA),
                    public_safe_approved=True, max_calls=2, expires_seconds=60)
        args.update(overrides)
        return self.m.create(**args)

    def test_research_regression_enum_cannot_be_ignored(self):
        token = self.create()
        with self.assertRaises(CapsuleError):
            self.m.invoke(token, {"query":"public/repo", "action":"delete"})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.m.lookup(token)["calls"], 0)

    def test_research_regression_unknown_validation_fails_closed(self):
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["query"]["pattern"] = "^public/"
        with self.assertRaisesRegex(CapsuleError, "pattern"):
            self.create(schema=schema)
        self.assertEqual(self.m.list_shares(), [])

    def test_fixed_arguments_hidden_and_unforgeable(self):
        token = self.create(fixed_args={"action":"lookup"})
        guest = self.m.public_info(token)
        self.assertNotIn("action", guest["schema"]["properties"])
        for private in ("server","tool","tool_id","token","fixed_args","owner_token"):
            self.assertNotIn(private, guest)
        with self.assertRaises(CapsuleError):
            self.m.invoke(token, {"query":"public/repo", "action":"delete"})
        self.m.invoke(token, {"query":"public/repo"})
        self.assertEqual(self.calls[0], ("public-tools","repo.lookup", {"query":"public/repo","action":"lookup"}))

    def test_const_is_automatically_fixed(self):
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["action"]["const"] = "lookup"
        token = self.create(schema=schema)
        self.m.invoke(token, {"query":"public/repo"})
        self.assertEqual(self.calls[0][2]["action"], "lookup")

    def test_unknown_fields_and_wrong_types_never_dispatch(self):
        token = self.create()
        for data in ({"query":"x","action":"lookup","tool":"delete"},
                     {"query":"x","action":"lookup","limit":True},
                     {"query":"x","action":"lookup","limit":99},
                     {"query":"","action":"lookup"}, {"query":"x"}):
            with self.assertRaises(CapsuleError): self.m.invoke(token,data)
        self.assertEqual(self.calls, [])

    def test_missing_required_list_is_normalised(self):
        schema = {"type":"object", "properties":{"query":{"type":"string", "maxLength":40}}}
        token = self.create(schema=schema)
        self.m.invoke(token,{"query":"works"})
        self.assertEqual(len(self.calls),1)

    def test_actual_expiry_without_sleep(self):
        token = self.create()
        self.now[0] += 61
        with self.assertRaises(CapsuleError): self.m.invoke(token,{"query":"x","action":"lookup"})
        self.assertEqual(self.m.public_info(token)["status"],"expired")
        self.assertEqual(self.calls,[])

    def test_revoke_blocks_saved_guest_link(self):
        token=self.create()
        self.m.revoke(token)
        with self.assertRaises(CapsuleError): self.m.invoke(token,{"query":"x","action":"lookup"})
        self.assertEqual(self.m.public_info(token)["status"],"revoked")
        self.assertEqual(self.calls,[])

    def test_restart_preserves_quota_and_capability(self):
        token=self.create()
        self.m.invoke(token,{"query":"x","action":"lookup"})
        restarted=CapsuleManager(self.temp.name,self.dispatch,clock=lambda:self.now[0])
        self.assertEqual(restarted.lookup(token)["remaining"],1)
        self.assertEqual(restarted.list_shares()[0]["token"],token)
        restarted.invoke(token,{"query":"y","action":"lookup"})
        with self.assertRaises(CapsuleError): self.m.invoke(token,{"query":"z","action":"lookup"})
        self.assertEqual(len(self.calls),2)

    def test_two_independent_managers_cannot_overspend(self):
        token=self.create(max_calls=3)
        second=CapsuleManager(self.temp.name,self.dispatch,clock=lambda:self.now[0])
        def attempt(index):
            for _ in range(30):
                try:
                    (self.m if index%2 else second).invoke(token,{"query":"x","action":"lookup"})
                    return "success"
                except CapsuleError as error:
                    if error.code != "busy": return error.code
                    time.sleep(.005)
            return "busy"
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            outcomes=list(pool.map(attempt,range(15)))
        self.assertEqual(outcomes.count("success"),3)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.m.public_info(token)["remaining"],0)

    def test_dispatch_error_charged_without_private_error_leak(self):
        def broken(*_): raise RuntimeError("private-token=DO_NOT_EXPOSE")
        manager=CapsuleManager(self.temp.name,broken,clock=lambda:self.now[0])
        token=self.create()
        with self.assertRaises(CapsuleError) as error: manager.invoke(token,{"query":"x","action":"lookup"})
        self.assertNotIn("DO_NOT_EXPOSE",str(error.exception))
        self.assertEqual(manager.lookup(token)["calls"],1)
        self.assertEqual(manager.activity()["runs"][0]["state"],"failed")
        data=pathlib.Path(self.temp.name,"sharelet.sqlite3").read_bytes()
        self.assertNotIn(b"DO_NOT_EXPOSE",data)

    def test_unapproved_and_invalid_manifests_rejected(self):
        for changes in ({"public_safe_approved":False},{"max_calls":True},{"expires_seconds":1},
                        {"fixed_args":{"action":"delete"}},{"server":"../private"}):
            with self.assertRaises(CapsuleError): self.create(**changes)
        self.assertEqual(self.m.list_shares(),[])

    def test_forged_capabilities_rejected(self):
        token=self.create()
        for forged in (token[:-1]+("a" if token[-1]!="a" else "b"),"bad",token+"中文"):
            with self.assertRaises(CapsuleError): self.m.public_info(forged)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.calls=[]
        cls.m=CapsuleManager(cls.temp.name,lambda s,t,a:(cls.calls.append((s,t,a)) or {"ok":True,"result":{"done":True}}))
        cls.catalog=lambda:[{"id":"public::lookup","server":"public","tool":"lookup","title":"Lookup","schema":SCHEMA,"supported":True,"read_only":True}]
        cls.servers=[create_server(cls.m,cls.catalog,public_origin='https://unit.trycloudflare.com'),create_server(cls.m,lambda:[],guest_only=True)]
        cls.threads=[]
        for server in cls.servers:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();cls.threads.append(thread)
        cls.owner="http://127.0.0.1:"+str(cls.servers[0].server_port)
        cls.guest="http://127.0.0.1:"+str(cls.servers[1].server_port)

    @classmethod
    def tearDownClass(cls):
        for server,thread in zip(cls.servers,cls.threads):
            server.shutdown();server.server_close();thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self,path,body=None,*,owner=False,guest=False,headers=None,raw=None):
        hdr={"Content-Type":"application/json"}
        if owner: hdr["Authorization"]="Bearer "+self.m.owner_token
        hdr.update(headers or {})
        data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
        try:
            with urlopen(Request((self.guest if guest else self.owner)+path,data=data,headers=hdr),timeout=4) as response:
                return response.status,json.load(response)
        except HTTPError as response:
            return response.code,json.load(response)

    def publish(self):
        code,data=self.request('/api/owner/shares',{"tool_id":"public::lookup","title":"A useful tool","fixed_args":{"action":"lookup"},"public_safe_approved":True,"public":True},owner=True)
        self.assertEqual(code,201)
        return data["url"].split('/s/')[1]

    def test_owner_create_guest_run_and_owner_revoke(self):
        token=self.publish()
        code,data=self.request('/api/s/'+token,{"query":"hello"},guest=True)
        self.assertEqual(code,200);self.assertEqual(data["share"]["remaining"],19)  # default allowance is 20
        share_id=token.split('.')[0]
        self.assertEqual(self.request('/api/owner/shares/'+share_id+'/revoke',{},owner=True)[0],200)
        self.assertEqual(self.request('/api/s/'+token,{"query":"hello"},guest=True)[0],410)

    def test_guest_listener_has_no_owner_access_even_with_owner_token(self):
        self.assertEqual(self.request('/api/owner/state',owner=True,guest=True)[0],401)
        self.assertEqual(self.request('/api/owner/shares',{},owner=True,guest=True)[0],401)
        self.assertEqual(self.request('/api/owner/state')[0],401)

    def test_guest_metadata_never_contains_secrets_or_bound_identity(self):
        token=self.publish()
        code,data=self.request('/api/s/'+token,guest=True)
        self.assertEqual(code,200)
        rendered=json.dumps(data)
        self.assertNotIn(self.m.owner_token,rendered)
        for private in ('server','tool_id','fixed_args','owner_token'):
            self.assertNotIn(private,data['share'])

    def test_host_and_origin_checks(self):
        token=self.publish()
        self.assertEqual(self.request('/healthz',headers={'Host':'evil.example'})[0],421)
        before=len(self.calls)
        self.assertEqual(self.request('/api/s/'+token,{"query":"x"},guest=True,headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(len(self.calls),before)

    def test_guest_cannot_pick_a_tool_or_override_fixed_fields(self):
        token=self.publish()
        before=len(self.calls)
        for body in ({"query":"x","tool":"delete"},{"query":"x","action":"delete"}):
            self.assertEqual(self.request('/api/s/'+token,body,guest=True)[0],400)
        self.assertEqual(len(self.calls),before)

    def test_ambiguous_nonfinite_and_oversized_json_rejected(self):
        token=self.publish()
        for raw in (b'{"query":"a","query":"b"}',b'{"query":NaN}',b'{"query":"'+b'a'*17000+b'"}'):
            self.assertIn(self.request('/api/s/'+token,raw=raw,guest=True)[0],(400,413))

    def test_owner_cannot_override_catalog_identity(self):
        code,_=self.request('/api/owner/shares',{"tool_id":"public::lookup","title":"bad","server":"private","public_safe_approved":True},owner=True)
        self.assertEqual(code,400)


if __name__=='__main__':
    unittest.main(verbosity=2)
