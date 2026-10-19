#!/usr/bin/env python3
"""Minimal stdlib pairing gate. Never persists the pairing credential."""
import argparse, base64, hashlib, json, os, re, shlex, ssl, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler

def validate_url(value):
    if not value or any(c.isspace() or ord(c)<32 for c in value):
        raise ValueError("server URL contains whitespace")
    u=urlsplit(value)
    if u.scheme not in ("http","https") or not u.hostname or u.username is not None or u.password is not None or u.query or u.fragment or u.path not in ("", "/"):
        raise ValueError("server must be an http(s) origin without credentials, path, query or fragment")
    if not re.fullmatch(r"[A-Za-z0-9.:[\]-]+",u.netloc):
        raise ValueError("invalid server authority")
    u.port
    return value.rstrip("/")

def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".binding-",dir=str(path.parent))
    try:
        with os.fdopen(fd,"w") as out:
            os.fchmod(out.fileno(),0o600); json.dump(data,out); out.flush(); os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def main():
    p=argparse.ArgumentParser(); p.add_argument("--ready",action="store_true"); p.add_argument("--rebind",action="store_true"); p.add_argument("--validate-only",action="store_true")
    a=p.parse_args(); e=os.environ
    server=validate_url(e["PAIR_SERVER"])
    if a.validate_only: return
    token=e["PAIR_TOKEN"]; device=e["PAIR_DEVICE"]; key=Path(e["PAIR_KEY"]); state=Path(e["PAIR_STATE"])/"server-binding.json"
    old=json.loads(state.read_text()) if state.exists() else {}
    env=Path(e["PAIR_ENV"])
    prior_servers=[old.get("server_url","")]
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SMART_TEXTBOOK_AGENT_SERVER_URL="):
                parts=shlex.split(line.split("=",1)[1]); prior_servers += parts
    changed=any(s and validate_url(s)!=server for s in prior_servers)
    if changed and not a.rebind: raise RuntimeError("different server already bound; explicitly use --rebind")
    key.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not key.exists():
        fd,name=tempfile.mkstemp(prefix=".identity-",dir=str(key.parent)); os.close(fd)
        try:
            subprocess.run(["openssl","genpkey","-algorithm","ED25519","-out",name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); os.chmod(name,0o600); os.replace(name,key)
        finally:
            if os.path.exists(name): os.unlink(name)
    os.chmod(key,0o600)
    public=subprocess.check_output(["openssl","pkey","-in",str(key),"-pubout","-outform","DER"],stderr=subprocess.DEVNULL)[-32:]
    binding={"server_url":server,"device_id":device,"device_public_key_fingerprint":hashlib.sha256(public).hexdigest(),"status":"pending"}
    if old.get("server_url")==server: binding.update({k:v for k,v in old.items() if k.startswith("artifact_signer")})
    # Preserve an approved old binding until a new server actually approves.
    if not changed: atomic_json(state,binding)
    facts=json.loads(subprocess.check_output([sys.executable,str(Path(__file__).with_name("preflight.py")),"--pretty"]))
    payload={"device_id":device,"display_name":e["PAIR_NAME"],"public_key":base64.urlsafe_b64encode(public).decode().rstrip("="),"capabilities":{"provider":"orangepi","runtime_profile":"ascend-npu","artifact_sync_required":True,"facts":facts}}
    context=ssl.create_default_context() if e["PAIR_TLS"]=="true" else ssl._create_unverified_context()
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError("pairing endpoint redirects are forbidden")
    opener=build_opener(NoRedirect(),HTTPSHandler(context=context))
    deadline=time.monotonic()+int(e["PAIR_TIMEOUT"])
    def request(path,data=None):
        remaining=deadline-time.monotonic()
        if remaining<=0: raise RuntimeError("pairing approval timed out")
        req=Request(server+path,data=json.dumps(data).encode() if data is not None else None,headers={"Content-Type":"application/json","X-Runtime-Agent-Enrollment-Token":token})
        with opener.open(req,timeout=min(60,remaining)) as response: return json.load(response)
    response=request("/api/runtime-agent/enroll",payload)
    while True:
        status=response.get("status","")
        if status in ("rejected","denied","revoked","disabled"): raise RuntimeError("server rejected pairing")
        approved=response.get("approved") is True or status in ("approved","ready","approved_waiting_for_compatible_artifact","approved_waiting_for_artifact_secret")
        signer=response.get("artifact_signer_public_key")
        pin=binding.get("artifact_signer_public_key")
        if approved:
            if pin and signer and pin!=signer: raise RuntimeError("approved server signer changed")
            if a.ready and pin and not signer: raise RuntimeError("ready response omitted pinned signer")
            if signer: binding["artifact_signer_public_key"]=signer
            for field in ("artifact_signer_key_id","artifact_signer_fingerprint","artifact_signer_identity"):
                identity=response.get(field)
                if binding.get(field) and identity and binding[field]!=identity:
                    raise RuntimeError("approved server signer identity changed")
                if identity: binding[field]=identity
            binding["status"]="approved"; atomic_json(state,binding)
            if not a.ready or status=="ready": print(json.dumps(response)); return
        if time.monotonic()>=deadline: raise RuntimeError("pairing approval timed out")
        time.sleep(min(int(e["PAIR_POLL"]),max(0,deadline-time.monotonic())))
        response=request("/api/runtime-agent/enroll/"+device)

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print("pairing failed: "+str(exc),file=sys.stderr); sys.exit(1)
