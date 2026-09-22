"""Standalone mock AWS REST server — credential-free, demo-only.

Speaks AWS-shaped JSON so the pipeline's client can parse it exactly like a
boto3 response, but it is backed entirely by :class:`MockState` (a local JSON
file). No AWS account, no boto3, no browser login — ever.

Run it::

    python -m src.mock_aws.server 8788
    # or:  python mock_aws.py 8788

Endpoints
---------
GET  /                                       live console (HTML)
GET  /health                                 {"ok": true}
GET  /aws/ce/cost-and-usage?group_by=SERVICE&granularity=DAILY&days=30
GET  /aws/cw/metric-statistics?namespace=AWS/EC2&metric=CPUUtilization&hours=24
GET  /aws/ec2/instances
POST /aws/ec2/instances/<id>/stop|start|terminate
POST /aws/ec2/instances/<id>/resize     {"instance_type": "t3.micro"}   (optional)
POST /aws/ec2/instances/<id>/tags       {"tags": {"Project": "X"}}
GET  /aws/budgets
POST /aws/budgets                       {"name": "...", "limit": 500, "scope": "service:EC2"}
POST /aws/reset
"""

from __future__ import annotations

import json
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

for stream in (sys.stdout, sys.stderr):  # UTF-8 on Windows consoles
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from ..config import load_config
from ..utils.logger import get_logger
from .state import MockState

log = get_logger("mock_aws.server")

_CFG = load_config()
STATE_PATH = _CFG.output_folder / "mock_state.json"
DOCS_FOLDER = _CFG.docs_folder

_LOCK = threading.Lock()
STATE = MockState.load(STATE_PATH, DOCS_FOLDER)


def _persist() -> None:
    STATE.save(STATE_PATH)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # ----- helpers -----
    def _json(self, status: HTTPStatus, obj: dict) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # ----- GET -----
    def do_GET(self):
        parsed = urlparse(self.path)
        path, q = parsed.path, parse_qs(parsed.query)
        if path in ("/", ""):
            return self._html(CONSOLE_HTML)
        if path == "/health":
            return self._json(HTTPStatus.OK, {"ok": True, "instances": len(STATE.instances)})
        if path == "/aws/ce/cost-and-usage":
            return self._json(HTTPStatus.OK, STATE.cost_and_usage(
                granularity=(q.get("granularity") or ["DAILY"])[0],
                group_by=(q.get("group_by") or ["SERVICE"])[0],
                days=int((q.get("days") or [STATE.window_days])[0]),
            ))
        if path == "/aws/cw/metric-statistics":
            return self._json(HTTPStatus.OK, STATE.metric_statistics(
                namespace=(q.get("namespace") or ["AWS/EC2"])[0],
                metric_name=(q.get("metric") or ["CPUUtilization"])[0],
                hours=int((q.get("hours") or [24])[0]),
                instance_id=(q.get("instance_id") or [None])[0],
            ))
        if path == "/aws/ec2/instances":
            return self._json(HTTPStatus.OK, STATE.describe_instances())
        if path == "/aws/budgets":
            return self._json(HTTPStatus.OK, STATE.list_budgets())
        return self._json(HTTPStatus.NOT_FOUND, {"error": f"Unknown GET {path}"})

    # ----- POST -----
    def do_POST(self):
        path = urlparse(self.path).path
        with _LOCK:
            result = self._dispatch_post(path)
        if result is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": f"Unknown POST {path}"})
        status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.BAD_REQUEST
        if result.get("_mutated"):
            _persist()
            result.pop("_mutated", None)
        return self._json(status, result)

    def _dispatch_post(self, path: str) -> dict | None:
        if path == "/aws/reset":
            STATE.reset(DOCS_FOLDER)
            return {"ok": True, "message": "State re-seeded from CSV.", "_mutated": True,
                    "instances": len(STATE.instances)}
        if path == "/aws/budgets":
            body = self._read_body()
            res = STATE.set_budget(body.get("name", "budget"),
                                   float(body.get("limit", 0)),
                                   body.get("scope", "service:EC2"))
            res["_mutated"] = True
            return res

        parts = [p for p in path.split("/") if p]  # ['aws','ec2','instances','<id>','<op>']
        if len(parts) == 5 and parts[:3] == ["aws", "ec2", "instances"]:
            instance_id, op = parts[3], parts[4]
            body = self._read_body()
            if op == "stop":
                res = STATE.stop_instance(instance_id)
            elif op == "start":
                res = STATE.start_instance(instance_id)
            elif op == "resize":
                res = STATE.resize_instance(instance_id, body.get("instance_type"))
            elif op == "terminate":
                res = STATE.terminate_instance(instance_id)
            elif op == "tags":
                res = STATE.tag_instance(instance_id, body.get("tags", {}))
            else:
                return {"ok": False, "message": f"Unknown op '{op}'"}
            res["_mutated"] = res.get("ok", False)
            return res
        return None


# Minimal live console so you can watch (and drive) the mock in a browser.
CONSOLE_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Mock AWS Console</title><style>
 body{font:14px/1.5 system-ui,Segoe UI,sans-serif;background:#0b0d12;color:#cdd3df;margin:0;padding:24px}
 h1{font-size:20px}h1 small{color:#7c8aa5;font-weight:400;font-size:13px}
 .totals{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}
 .card{background:#151a23;border:1px solid #232a36;border-radius:10px;padding:12px 16px}
 .card b{display:block;font-size:22px;color:#7c5cff}
 table{width:100%;border-collapse:collapse;margin-top:10px}
 th,td{padding:8px 10px;border-bottom:1px solid #1d2430;text-align:left}
 th{color:#7c8aa5;font-weight:600;font-size:12px;text-transform:uppercase}
 .run{color:#34d399}.stopped{color:#fbbf24}
 button{background:#1d2430;color:#cdd3df;border:1px solid #2b3340;border-radius:6px;padding:4px 9px;cursor:pointer;margin-right:4px;font-size:12px}
 button:hover{background:#2b3340}.danger{border-color:#7f1d1d;color:#f87171}
 .bar{margin:8px 0 18px}.bar button{padding:6px 14px}
</style></head><body>
<h1>Mock AWS Console <small>credential-free · demo only · state persists to outputs/mock_state.json</small></h1>
<div class=bar>
 <button onclick="reset()">↺ Reset from CSV</button>
 <span id=msg style=color:#34d399></span>
</div>
<div class=totals id=totals></div>
<table><thead><tr><th>Instance</th><th>Type</th><th>Region</th><th>Project</th><th>CPU%</th><th>State</th><th>$/mo</th><th>Actions</th></tr></thead>
<tbody id=rows></tbody></table>
<script>
const HM=730;
async function load(){
 const inv=await (await fetch('/aws/ec2/instances')).json();
 const insts=inv.Reservations[0].Instances;
 const rows=document.getElementById('rows');rows.innerHTML='';
 let total=0,running=0;
 for(const i of insts){
  const cost=i.MonthlyCost||0; if(i.State.Name==='running'){total+=cost;running++;}
  const proj=(i.Tags.find(t=>t.Key==='Project')||{}).Value||'';
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${i.InstanceId}</td><td>${i.InstanceType}</td><td>${i.Region}</td>
   <td>${proj}</td><td>${i.CpuUtilization??''}</td>
   <td class=${i.State.Name==='running'?'run':'stopped'}>${i.State.Name}</td>
   <td>$${cost.toFixed(2)}</td>
   <td>
    ${i.State.Name==='running'?`<button onclick="op('${i.InstanceId}','stop')">stop</button>`:`<button onclick="op('${i.InstanceId}','start')">start</button>`}
    <button onclick="op('${i.InstanceId}','resize')">resize↓</button>
    <button class=danger onclick="op('${i.InstanceId}','terminate')">terminate</button>
   </td>`;
  rows.appendChild(tr);
 }
 document.getElementById('totals').innerHTML=
  `<div class=card>EC2 running<b>${running}</b></div>
   <div class=card>EC2 cost / mo<b>$${total.toFixed(2)}</b></div>
   <div class=card>Instances<b>${insts.length}</b></div>`;
}
async function op(id,op){
 const r=await (await fetch(`/aws/ec2/instances/${id}/${op}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
 flash(r.message||(r.ok?'done':'failed'));load();
}
async function reset(){const r=await (await fetch('/aws/reset',{method:'POST'})).json();flash(r.message);load();}
function flash(m){const e=document.getElementById('msg');e.textContent=m;setTimeout(()=>e.textContent='',2500);}
load();setInterval(load,5000);
</script></body></html>"""


_BG_SERVER = None


def serve_in_background(port: int = 8788) -> bool:
    """Start the mock server in a daemon thread (idempotent).

    Lets the pipeline auto-start the mock when "Mock AWS" is selected but no
    standalone server is running, so the user never has to launch a second
    process. Returns False only if the port is taken by something else (in
    which case a separate server is presumably already serving it).
    """
    global _BG_SERVER
    if _BG_SERVER is not None:
        return True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return False  # port in use — likely a standalone `python mock_aws.py`
    threading.Thread(target=server.serve_forever, daemon=True, name="mock-aws-bg").start()
    _BG_SERVER = server
    log.info("Auto-started embedded mock AWS on http://127.0.0.1:%s", port)
    return True


def main(port: int = 8788) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("Mock AWS serving on http://127.0.0.1:%s  (console at /)", port)
    log.info("State file: %s | seed docs: %s", STATE_PATH, DOCS_FOLDER)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down mock AWS")
        server.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    main(port)
