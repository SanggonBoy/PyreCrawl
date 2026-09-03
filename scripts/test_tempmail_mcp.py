"""End-to-end: temp-mail.org email via MCP stdio using the new js/wait_for params.

The whole point: the email exists ONLY in a live DOM property — proof that the
MCP server correctly threads a JS expression string through JSON-RPC into
page_action and returns the evaluated value in meta.js_result.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

proc = subprocess.Popen(
    [str(VENV_PY), "-m", "pyrecrawl.server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, bufsize=1, cwd=ROOT,
)

_nid = 0
def rpc(method, params=None, timeout=180):
    global _nid
    _nid += 1
    msg = {"jsonrpc": "2.0", "id": _nid, "method": method, "params": params or {}}
    proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server died")
        line = line.strip()
        if line:
            return json.loads(line)
    raise TimeoutError(method)

def payload_of(out):
    return json.loads(out["result"]["content"][0]["text"])

rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}})
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()

print("Calling scrape(temp-mail.org, prefer=stealth, js=read #mail value, wait_for=...)", flush=True)
out = rpc("tools/call", {
    "name": "scrape",
    "arguments": {
        "url": "https://temp-mail.org/id",
        "prefer": "stealth",
        "timeout": 90,
        # wait until the mailbox input actually carries an address
        "wait_for": "document.getElementById('mail') && document.getElementById('mail').value.includes('@')",
        "js": "document.getElementById('mail') ? document.getElementById('mail').value : null",
    },
}, timeout=240)
p = payload_of(out)

meta = p.get("meta") or {}
email = meta.get("js_result")
print(f"status:      {p.get('status')}")
print(f"method:      {p.get('method')}")
print(f"js_result:   {email!r}")
print(f"js_error:    {meta.get('js_error')!r}")
print(f"wait_for_err:{meta.get('wait_for_error')!r}")

ok = isinstance(email, str) and "@" in email
print(f"\nRESULT: {'PASS' if ok else 'FAIL'} — email={email!r}")
proc.terminate()
sys.exit(0 if ok else 1)
