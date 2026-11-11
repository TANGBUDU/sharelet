"""Run the installed Permission Canvas backend against a synthetic application.

Only the application is simulated. Policy, dispatch guards, approval requests and
pixel/AX redaction come from the owner's unchanged Permission Canvas source.
No native desktop driver, model, clipboard or external service is opened.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.machinery
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import types

from PIL import Image, ImageDraw, ImageFont

SCENARIOS = (
    "Edit an allowed note",
    "Change read-only revenue",
    "Send an invitation",
    "Reveal a hidden credential",
    "Copy to clipboard",
    "Observe the agent view",
)
WIDTH, HEIGHT = 800, 480
APP, TITLE, PID, WINDOW = "Sharelet sandbox", "Permission Canvas sandbox", 4171, 8151
BOUNDS = {1:(32,104,352,112), 2:(416,104,352,112), 3:(32,260,352,104), 4:(416,260,352,104)}
LABELS = {1:"Draft note", 2:"Monthly revenue", 3:"Send invitation", 4:"API credential"}
LEVELS = {1:"green", 2:"blue", 3:"yellow", 4:"black"}
_library = None
_library_lock = threading.Lock()


def load_library():
    global _library
    with _library_lock:
        if _library is not None:
            return _library
        configured = os.environ.get("PERMISSION_CANVAS_SOURCE", "")
        root = Path(configured).expanduser()
        required = ("backend.py", "policy.py", "approval.py", "redaction.py", "control.py", "driver_transport.py", "plugin.yaml")
        if not configured or not root.is_absolute() or not all((root/name).is_file() for name in required):
            raise RuntimeError("Set PERMISSION_CANVAS_SOURCE to the installed Permission Canvas source directory.")
        root = root.resolve()
        def fingerprint():
            digest = hashlib.sha256()
            for name in sorted(required):
                digest.update(name.encode()); digest.update(b"\0"); digest.update((root/name).read_bytes())
            return digest.hexdigest()
        source_hash = fingerprint()
        match = re.search(r'^version:\s*["\']?([0-9]+\.[0-9]+\.[0-9]+)', (root/"plugin.yaml").read_text(encoding="utf-8"), re.M)
        if not match:
            raise RuntimeError("The Permission Canvas version could not be verified.")
        # A namespace imports the production modules without registering a new
        # Hermes plugin or opening its native human approval panel.
        name = "_sharelet_canvas_" + source_hash[:16]
        namespace = types.ModuleType(name)
        namespace.__path__ = [str(root)]
        namespace.__package__ = name
        namespace.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
        sys.modules[name] = namespace
        backend = importlib.import_module(name+".backend")
        policy = importlib.import_module(name+".policy")
        approval = importlib.import_module(name+".approval")
        from tools.computer_use.backend import ActionResult, CaptureResult, ComputerUseBackend, UIElement
        if fingerprint() != source_hash:
            raise RuntimeError("Permission Canvas changed while loading. Restart this example.")
        _library = types.SimpleNamespace(Backend=backend.PermissionCanvasBackend,
            Policy=policy.Policy, ApprovalStore=approval.ApprovalStore,
            ActionResult=ActionResult, CaptureResult=CaptureResult, Base=ComputerUseBackend, Element=UIElement,
            version=match[1], source_sha256=source_hash)
        return _library


def font(size):
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path,size)
    return ImageFont.load_default()


def make_application(library, selected):
    class Application(library.Base):
        @property
        def name(self):return "sharelet-canvas-sandbox"

        def __init__(self):
            self.calls = []
            self.note = "A draft you can safely edit."
            self.revenue = "$12,480"
            self.credential = "PC-DEMO-"+secrets.token_hex(8)

        def start(self):pass
        def stop(self):pass
        def is_available(self):return True
        def list_apps(self):return [{"name":APP,"pid":PID}]
        def list_windows(self):return [{"pid":PID,"window_id":WINDOW,"app":APP,"title":TITLE}]

        def capture(self,mode="som",app=None,pid=None,window_id=None):
            image = Image.new("RGB",(WIDTH,HEIGHT),(245,247,240))
            draw = ImageDraw.Draw(image)
            draw.text((32,24),"Permission Canvas",font=font(27),fill=(39,64,47))
            draw.text((34,65),"Synthetic workspace",font=font(14),fill=(109,124,103))
            colors = {1:(82,133,91),2:(75,115,166),3:(182,133,43),4:(52,56,51)}
            for index,bounds in BOUNDS.items():
                x,y,w,h = bounds
                draw.rounded_rectangle((x,y,x+w,y+h),radius=14,fill="white",outline=colors[index],width=4 if selected==index else 2)
                draw.text((x+18,y+16),LABELS[index],font=font(17),fill=colors[index])
                draw.text((x+w-31,y+15),str(index),font=font(16),fill=colors[index])
                value={1:self.note,2:self.revenue,3:"Request approval before sending",4:self.credential}[index]
                if index==4:
                    # A sentinel exists only inside the protected area. The
                    # actual plugin must cover it, not just remove AX text.
                    draw.rectangle((x+17,y+48,x+w-17,y+h-12),fill=(13,237,101))
                clipped=Image.new("RGB",(w-36,39),"white" if index!=4 else (13,237,101))
                ImageDraw.Draw(clipped).text((0,3),value,font=font(16),fill=(46,62,43))
                image.paste(clipped,(x+18,y+52))
            draw.text((34,412),"1  Edit      2  Read only      3  Ask first      4  Hidden",font=font(16),fill=(102,117,97))
            draw.text((34,443),"No real desktop, message or payment is connected.",font=font(13),fill=(128,140,121))
            stream=io.BytesIO();image.save(stream,format="PNG")
            elements=[]
            for index in BOUNDS:
                value={1:self.note,2:self.revenue,3:"",4:self.credential}[index]
                elements.append(library.Element(index,"AXButton" if index==3 else "AXTextField",LABELS[index],BOUNDS[index],APP,PID,WINDOW,
                    attributes={"permission_canvas_value":value},element_token="sandbox-"+str(index)))
            return library.CaptureResult(mode,WIDTH,HEIGHT,base64.b64encode(stream.getvalue()).decode(),elements,APP,TITLE,
                png_bytes_len=len(stream.getvalue()),image_mime_type="image/png")

        def click(self,**kwargs):
            self.calls.append({"method":"click","element":kwargs.get("element")})
            return library.ActionResult(True,"click",verified=True,effect="verified")

        def set_value(self,value,element=None):
            if element!=1:
                raise RuntimeError("Only the fictional draft note has a mutable value.")
            self.calls.append({"method":"set_value","element":element})
            self.note=value
            return library.ActionResult(True,"set_value",verified=True,effect="verified")

        # The demonstration never needs these capabilities. If the wrapper
        # unexpectedly forwards one, fail visibly instead of doing real I/O.
        def drag(self,**kwargs):raise RuntimeError("No sandbox drag capability")
        def scroll(self,**kwargs):raise RuntimeError("No sandbox scroll capability")
        def type_text(self,text,**kwargs):raise RuntimeError("No sandbox typing capability")
        def key(self,keys,**kwargs):raise RuntimeError("The real guard must reject the clipboard shortcut")
        def focus_app(self,app,raise_window=False):raise RuntimeError("No sandbox focus capability")
    return Application()


def run_demo(scenario="Edit an allowed note", text="Hello, Sharelet!"):
    if scenario not in SCENARIOS or not isinstance(text,str) or len(text)>100:
        raise ValueError("Choose a supported scenario and a note of at most 100 characters.")
    library=load_library()
    index={SCENARIOS[0]:1,SCENARIOS[1]:2,SCENARIOS[2]:3,SCENARIOS[3]:4,SCENARIOS[4]:1,SCENARIOS[5]:1}[scenario]
    app=make_application(library,index)
    policy=library.Policy.from_dict({"default_level":"yellow","require_target":True,
        "target_window":{"pid":PID,"window_id":WINDOW,"app":APP,"title":TITLE},
        "rules":[{"id":"sandbox-"+str(i),"level":LEVELS[i],"app":APP,"pid":PID,"window_id":WINDOW,"label":LABELS[i]} for i in BOUNDS]})
    approvals=library.ApprovalStore()
    backend=library.Backend(app,lambda:policy,approvals)
    backend.start()
    try:
        backend.capture(pid=PID,window_id=WINDOW)
        if scenario in SCENARIOS[:2]:
            method="set_value";result=backend.set_value(text if index==1 else "$1",element=index)
        elif scenario in SCENARIOS[2:4]:
            method="click";result=backend.click(element=index)
        elif scenario==SCENARIOS[4]:
            method="key";result=backend.key("ctrl+c")
        else:
            method="capture";result=None
        observation=backend.capture(pid=PID,window_id=WINDOW)
        visible=[{"index":e.index,"label":e.label} for e in observation.elements]
        if any(e["index"]==4 for e in visible) or app.credential in json.dumps(visible)+observation.note:
            raise RuntimeError("The sandbox credential was not removed from the observation")
        image=Image.open(io.BytesIO(base64.b64decode(observation.png_b64))).convert("RGB")
        x,y,w,h=BOUNDS[4]
        if any(extreme!=(0,0) for extreme in image.crop((x,y,x+w,y+h)).getextrema()):
            raise RuntimeError("The real plugin did not mask the protected image region")
        # Delivery compression happens after the real backend's redaction. It
        # cannot restore any pixels or values removed by Permission Canvas.
        output=io.BytesIO();image.quantize(colors=48).save(output,format="PNG",optimize=True)
        delivered=Image.open(io.BytesIO(output.getvalue())).convert("RGB")
        if any(extreme!=(0,0) for extreme in delivered.crop((x,y,x+w,y+h)).getextrema()):
            raise RuntimeError("Delivery compression changed the protected image region")
        encoded=base64.b64encode(output.getvalue()).decode()
        if len(encoded)>24000:
            raise RuntimeError("The preview exceeded its delivery limit")
        code=result.code if result else "observed"
        decision="observed" if result is None else "allowed" if result.ok else "approval_required" if code=="approval_required" else "blocked"
        value={"kind":"permission_canvas_demo","schema_version":1,"sandbox":True,
            "plugin_version":library.version,"source_sha256":library.source_sha256,
            "scenario":scenario,"target":LABELS[index],"level":LEVELS[index],"backend_method":method,
            "decision":decision,"backend_code":code or "allowed","execution_count":len(app.calls),
            "note_after":app.note,"visible_controls":visible,"hidden_controls":1,
            "pixels_masked":True,"credential_exposed":False,"preview_png_base64":encoded}
        if app.credential in json.dumps(value):
            raise RuntimeError("The synthetic credential entered the result")
        if len(json.dumps(value,ensure_ascii=False).encode())>32000:
            raise RuntimeError("The sandbox result exceeded its delivery limit")
        return value
    finally:
        backend.stop()
