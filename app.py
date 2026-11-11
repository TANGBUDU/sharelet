"""Standalone tool sharing, with optional image generation; native boot lives in dev/."""
import argparse
import json
import signal
import threading
from pathlib import Path

from connections import PortableBridge
from relay import DEFAULT_RELAY
from service import Runtime

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description='Sharelet tool sharing workspace')
    parser.add_argument('--mode',choices=['local'],default='local')
    parser.add_argument('--port',type=int,default=9271)
    parser.add_argument('--guest-port',type=int,default=9272)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'.state')
    parser.add_argument('--cloudflared',type=Path)
    parser.add_argument('--generation-limit',type=int,default=30)
    parser.add_argument('--execution-limit',type=int,default=10000)
    parser.add_argument('--relay-url',default=DEFAULT_RELAY,help='Stable-link relay; pass an empty string to disable')
    parser.add_argument('--public-origin',default='',help='An https address you run yourself for the guest port')
    args=parser.parse_args()
    data=args.data_dir.resolve()
    bridge=PortableBridge(data)
    runtime=Runtime(data,bridge.call,bridge.catalog,connection={'mode':'local','label':'Portable tool service'},
                    port=args.port,guest_port=args.guest_port,binary=args.cloudflared,generation_limit=args.generation_limit,
                    execution_limit=args.execution_limit,connector=bridge,
                    relay_url=args.relay_url,public_origin=args.public_origin).start()
    if runtime.info.get('service')=='attached':
        print('Using the existing Sharelet service for this workspace.',flush=True)
        runtime.close();bridge.close();return
    metadata=json.loads((data/'service.json').read_text())
    access={'owner_url':(data/'owner-entry.txt').read_text(),'owner_port':metadata['owner_port'],
            'guest_port':metadata['guest_port'],'mode':'local'}
    access_file=data/'preview-access.json'
    access_file.write_text(json.dumps(access),encoding='utf-8');access_file.chmod(0o600)
    print('Sharelet ready: http://127.0.0.1:'+str(metadata['owner_port'])+'/',flush=True)
    print('Private creator entry: '+str(data/'owner-entry.txt'),flush=True)
    stopped=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set());signal.signal(signal.SIGINT,lambda *_:stopped.set())
    try:
        while not stopped.wait(1):pass
    finally:runtime.close();bridge.close()


if __name__=='__main__':main()
