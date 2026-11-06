"""Exercise the Web's real event handlers and their HTTP request contracts.

The small Node DOM below supplies form controls, not an alternate implementation
of the application. Each scenario executes the shipped JavaScript. Channel
mutations go to a real temporary HTTP server; protocol scenarios supply a
deterministic transport snapshot so no hardware is required.
"""
from __future__ import annotations

import http.client
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from scdfe_s.web.server import make_server


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "scdfe_s" / "web" / "static"
NODE = shutil.which("node")


NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = fs.readFileSync(input.html, 'utf8');
const nodes = new Map();
const fields = new Map();
const documentHandlers = {};
const writes = [];
let pending = 0;
let sampled = false;
const config = {mode: input.mode || 'master', rtu_id: 101,
  serial_port:'COM_TEST', baudrate:9600, bytesize:8, parity:'N', stopbits:1,
  unit_id:1, response_timeout:1, simulation:1, interface:'virtual',
  channel:'review', bitrate:500000, data_bitrate:2000000, fd:0,
  receive_own_messages:0, receive_timeout:0.1, is_extended_id:0, keep_alive:0};
function attrs(text) {
  const result = {};
  for (const item of text.matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) {
    result[item[1]] = item[2] ?? item[3];
  }
  return result;
}
for (const match of html.matchAll(/<(input|select|button|form|div|section|tbody|pre|span)\b([^>]*)>/g)) {
  const a = attrs(match[2]);
  if (!a.id && !a['data-field']) continue;
  const node = {type:a.type || (match[1] === 'select' ? 'select-one' : 'text'),
    checked:/\bchecked\b/.test(match[2]), disabled:false, innerHTML:'',
    textContent:'', className:'', handlers:{}, dataset:{},
    addEventListener(name, handler) { this.handlers[name] = handler; },
    reset() {}, setAttribute(name, value) { this[name] = String(value); },
    removeAttribute(name) { delete this[name]; }};
  // The protocol table updates existing cells on polling; emulate those DOM nodes.
  if (a.id === 'points') {
    Object.defineProperty(node, 'innerHTML', {
      set(html) {
        this.rows=Array.from(html.matchAll(/<tr>(.*?)<\/tr>/g), row => ({
          cells:Array.from(row[1].matchAll(/<td[^>]*>(.*?)<\/td>/g), cell => ({textContent:cell[1]}))
        }));
      },
      get() { return (this.rows || []).map(r=>'<tr>'+r.cells.map(c=>'<td>'+c.textContent+'</td>').join('')+'</tr>').join(''); }
    });
  }
  let value = a.value || '';
  Object.defineProperty(node, 'value', {get:() => value, set:v => { value=String(v); }});
  if (a.id) nodes.set(a.id, node);
  if (a['data-field']) fields.set(a['data-field'], node);
}
const document = {
  body:{dataset:{protocol:input.slug}}, hidden:false, activeElement:null,
  querySelector(selector) {
    if (selector.startsWith('#')) return nodes.get(selector.slice(1)) || null;
    const field = selector.match(/^\[data-field=['"]([^'"]+)['"]\]$/);
    return field ? fields.get(field[1]) || null : null;
  },
  addEventListener(name, handler) { documentHandlers[name] = handler; }
};
async function request(path, options={}) {
  pending += 1;
  try {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    let data, status = 200;
    if (input.baseUrl) {
      const response = await fetch(input.baseUrl + path, options);
      status=response.status; data=await response.json();
    } else if (path.endsWith('/status')) {
      data={item:{running:true,config:{...config},rtu_id:101,point_count:1,open_connections:1}};
    } else if (path.endsWith('/poll')) {
      sampled=true; data={item:{count:1,items:[{kind:'yc',pnt_no:1,value:42,status:0}]}};
    } else if (path.endsWith('/points?view=table')) {
      data={items:[{kind:'yc',pnt_no:1,name:'sample',value:sampled?42:1,status:0}]};
    } else if (path.endsWith('/config')) {
      Object.assign(config,body || {}); data={item:{config:{...config}}};
    } else if (path.includes('/commands/') || path.endsWith('/update/incremental')) {
      data={item:{...body}};
    } else { data={items:[]}; }
    if (method !== 'GET') writes.push({path,method,body,status,data});
    return {ok:status < 400,status,json:async () => data};
  } finally { pending -= 1; }
}
function node(id) {
  const found=nodes.get(id);
  if (!found) throw Error('Missing real HTML node '+id);
  return found;
}
function set(values) { for(const [key,value] of Object.entries(values)) node(key).value=value; }
async function settle() {
  let quiet=0;
  for(let n=0;n<300;n++) {
    await new Promise(resolve => setTimeout(resolve,10));
    quiet=pending === 0 ? quiet+1 : 0;
    if(quiet >= 2) return;
  }
  throw Error('Application requests did not settle');
}
async function submit(id) {
  await node(id).onsubmit({preventDefault(){},target:node(id)}); await settle();
}
async function clickData(selector,dataset) {
  await documentHandlers.click({target:{closest:q => q === selector ? {dataset} : null}});
  await settle();
}
const context = {document,fetch:request,console,setTimeout,clearTimeout,
  setInterval:() => 0,clearInterval(){},prompt:() => input.prompt ?? '1',
  confirm:() => true,window:{},Event:class{},URLSearchParams,
  location:{search:input.search || '?rtu_id=101'}};
vm.runInNewContext(fs.readFileSync(input.script,'utf8'),context,{filename:input.script});
(async () => {
  await settle();
  let observed={};
  if(input.scenario === 'channels') {
    set({rtu_id:101,channel_id:101,channel_type:'modbus-tcp',code:'JS101',name:'JS channel',process_enable:0});
    await submit('channel-form');
    const created=writes.find(r => r.path === '/api/channels' && r.method === 'POST');
    if(!created || created.status !== 201) throw Error('Channel create failed '+JSON.stringify(created));
    await clickData('button[data-edit]',{edit:JSON.stringify(created.data.item)});
    set({name:'JS channel edited'});
    await node('update-channel').onclick(); await settle();
    observed={channelMessage:node('channel-message').textContent};
  } else if(input.scenario === 'points') {
    set({'point-kind':'yc','point-no':1,'point-code':'YC1','point-name':'JS point','point-value':1.25});
    await submit('point-form');
    const added=writes.find(r => r.path.endsWith('/points/yc') && r.method === 'POST');
    if(added && added.status === 201) {
      await clickData('button[data-point-edit]',{pointEdit:JSON.stringify({...added.data.item,kind:'yc'})});
    }
    set({'point-name':'JS point edited','point-value':9.5});
    await node('update-point').onclick(); await settle();
    observed={pointMessage:node('point-message').textContent};
  } else if(input.scenario === 'point-selection') {
    observed.owner=node('point-rtu').value;
    observed.choices=node('point-rtu').innerHTML;
    observed.points=node('points').innerHTML;
  } else if(input.scenario === 'simulation') {
    observed.checkedBefore=fields.get('simulation').checked;
    fields.get('serial_port').value='COM_CHANGED';
    await submit('config-form');
  } else if(input.scenario === 'booleans') {
    for(const field of input.booleanFields) fields.get(field).value='0';
    await submit('config-form');
  } else if(input.scenario === 'command') {
    const button={dataset:{update:input.kind,point:'7'}};
    await node('points').onclick({target:{closest:() => button}}); await settle();
  } else if(input.scenario === 'poll') {
    const first=node('points').rows[0];
    observed.before=node('points').innerHTML;
    await node('poll').onclick(); await settle();
    observed.after=node('points').innerHTML;
    observed.kept=first === node('points').rows[0];
  }
  process.stdout.write(JSON.stringify({writes,observed}));
})().catch(error => { console.error(error.stack); process.exitCode=1; });
"""


class AssetsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.styles = 0
        self.inline_handlers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.scripts.append(attrs.get("src"))
        if tag == "style":
            self.styles += 1
        self.inline_handlers.extend(name for name in attrs if name.startswith("on"))


class ReviewSWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.httpd = make_server("127.0.0.1", 0, db_path=Path(self.temp.name) / "test.sqlite3")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def call(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            conn.close()

    def run_js(self, script, html, **options):
        if NODE is None:
            self.skipTest("Node.js is required to execute the browser event-handler regressions")
        args = {"script": str(STATIC / script), "html": str(STATIC / html), **options}
        result = subprocess.run([NODE, "-e", NODE_DRIVER], input=json.dumps(args),
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_iec104_and_homepage_assets_are_compatible_with_served_csp(self):
        for route, expected in (("/", "/static/index.js"), ("/iec104", "/static/iec104.js")):
            with self.subTest(route=route):
                status, headers, page = self.call(route)
                self.assertEqual(status, 200)
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                parser = AssetsParser()
                parser.feed(page)
                self.assertIn(expected, parser.scripts)
                self.assertNotIn(None, parser.scripts, "CSP blocks inline scripts")
                self.assertEqual(parser.styles, 0, "CSP blocks inline styles")
                self.assertEqual(parser.inline_handlers, [])
                for src in parser.scripts:
                    status, asset_headers, content = self.call(src)
                    self.assertEqual(status, 200, src)
                    self.assertIn("javascript", asset_headers["Content-Type"])
                    self.assertTrue(content.strip())

    def test_point_link_after_first_page_selects_exact_64bit_rtu(self):
        left, right = 2**53, 2**53+1
        with self.httpd.store.connection() as con:
            con.executemany("INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type) VALUES(?,?,'modbus-tcp')",
                            [(n,n) for n in [*range(1,501),left,right]])
            con.executemany("INSERT INTO fes_yc_define(rtu_id,pnt_no,name) VALUES(?,1,?)",
                            [(left,'first-RTU'),(right,'second-RTU')])
            con.commit()
        result=self.run_js("channels.js", "points.html", scenario="point-selection",
                           search=f"?rtu_id={right}", baseUrl=f"http://127.0.0.1:{self.httpd.server_port}")
        self.assertEqual(result["observed"]["owner"],str(right))
        self.assertIn("second-RTU",result["observed"]["points"])
        self.assertNotIn("first-RTU",result["observed"]["points"])

    def test_real_channel_form_create_and_edit_requests_are_accepted(self):
        result = self.run_js("channels.js", "channels.html", scenario="channels",
                             baseUrl=f"http://127.0.0.1:{self.httpd.server_port}")
        points = self.run_js("channels.js", "points.html", scenario="points",
                             baseUrl=f"http://127.0.0.1:{self.httpd.server_port}")
        writes = result["writes"] + points["writes"]
        self.assertEqual([(r["method"], r["status"]) for r in writes],
                         [("POST", 201), ("PATCH", 200), ("POST", 201), ("PATCH", 200)], writes)
        channel_edit, point_add, point_edit = writes[1:]
        self.assertNotIn("kind", point_add["body"])
        self.assertNotIn("kind", point_edit["body"])
        self.assertNotIn("pnt_no", point_edit["body"])
        self.assertNotIn("rtu_id", channel_edit["body"])
        self.assertEqual(self.httpd.store.get_row("fes_yc_define", [101, 1])["value"], 9.5)
        self.assertEqual(self.httpd.store.get_row("fes_rtu_define", [101])["name"], "JS channel edited")

    def test_simulation_checkbox_survives_editing_another_setting(self):
        result = self.run_js("protocol.js", "modbus_rtu_server.html", scenario="simulation",
                             slug="modbus-rtu-server", mode="server")
        self.assertTrue(result["observed"]["checkedBefore"])
        self.assertEqual(result["writes"][0]["body"]["simulation"], 1)
        self.assertEqual(result["writes"][0]["body"]["serial_port"], "COM_CHANGED")

    def test_boolean_selects_submit_numbers_for_false(self):
        cases = (("can", "can.html", ["fd", "receive_own_messages", "is_extended_id"]),
                 ("modbus-tcp", "modbus_tcp.html", ["keep_alive"]))
        for slug, html, fields in cases:
            with self.subTest(slug=slug):
                result = self.run_js("protocol.js", html, scenario="booleans", slug=slug,
                                     booleanFields=fields)
                for field in fields:
                    value = result["writes"][0]["body"][field]
                    self.assertEqual(value, 0)
                    self.assertIs(type(value), int)

    def test_master_control_buttons_call_command_api_with_engineering_value(self):
        for kind, raw, expected in (("yk", "0", False), ("yt", "22.5", 22.5)):
            with self.subTest(kind=kind):
                result = self.run_js("protocol.js", "modbus_tcp.html", scenario="command",
                                     slug="modbus-tcp", mode="master", kind=kind, prompt=raw)
                request = result["writes"][0]
                self.assertEqual(request["path"], f"/api/modbus-tcp/commands/{kind}/7")
                self.assertEqual(request["body"], {"value": expected})

    def test_server_point_buttons_keep_using_incremental_update(self):
        result = self.run_js("protocol.js", "modbus_tcp_server.html", scenario="command",
                             slug="modbus-tcp-server", mode="server", kind="yk", prompt="1")
        request = result["writes"][0]
        self.assertEqual(request["path"], "/api/modbus-tcp-server/update/incremental")
        self.assertEqual(request["body"], {"kind": "yk", "pnt_no": 7, "value": True})

    def test_poll_renders_new_point_snapshot(self):
        result = self.run_js("protocol.js", "modbus_tcp.html", scenario="poll", slug="modbus-tcp")
        self.assertIn("<td>1</td>", result["observed"]["before"])
        self.assertIn("<td>42</td>", result["observed"]["after"])
        self.assertTrue(result["observed"]["kept"])
        self.assertEqual(result["writes"][0]["path"], "/api/modbus-tcp/poll")


if __name__ == "__main__":
    unittest.main()
