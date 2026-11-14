/* CPU-only comparison of the actual shared log-browser highlighters. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const cp = require('node:child_process');
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const root = path.resolve(__dirname,'..');
const sourcePath = 'core/web_static/log-browser.js';
const before = cp.execFileSync('git',['show','fb8561f:'+sourcePath],{cwd:root,encoding:'utf8'});
const after = fs.readFileSync(path.join(root,sourcePath),'utf8');
const esc = text => String(text ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function load(source,name) {
  const start = source.indexOf('  function '+name+'('), end = source.indexOf('  async function search(',start);
  assert(start>=0 && end>start);
  return vm.runInNewContext(source.slice(start,end)+'\n'+name,{esc});
}
const oldHighlight = load(before,'highlight'), newHighlighter = load(after,'highlighter');
const rows = Array.from({length:5000},(_,i)=>`22:00:00 [日志] @example.1 业务事件 更新 点号=${i} 值=1.2.*[x] <html>&  质量=0`);
const keyword = '值=1.2.*[x]';
const run = {
  before:()=>rows.map(line=>oldHighlight(line,keyword)),
  after:()=>rows.map(newHighlighter(keyword)),
};
const expected = run.before();
const samples = {before:[],after:[]};
for (let repeat=-3;repeat<11;repeat++) {
  for (const variant of repeat%2 ? ['before','after'] : ['after','before']) {
    const started = performance.now(), actual = run[variant](), elapsed = performance.now()-started;
    assert.deepEqual(actual,expected);
    if(repeat>=0) samples[variant].push(elapsed);
  }
}
const median = values=>values.slice().sort((a,b)=>a-b)[Math.floor(values.length/2)];
const medians = Object.fromEntries(Object.entries(samples).map(([k,v])=>[k,median(v)]));
const report = {baseline:'fb8561f',rows:rows.length,keyword,html_identical:true,median_ms:medians,samples_ms:samples,
  reduction_percent:100*(1-medians.after/medians.before),regexp_constructions:{before:5000,after:1},
  scope:'Same HTML escaping and literal case-insensitive matching; CPU highlighting only, excludes HTTP and DOM layout.'};
const target = path.join(root,'output/efficiency_20261114_business/highlight.json');
fs.mkdirSync(path.dirname(target),{recursive:true});
fs.writeFileSync(target,JSON.stringify(report,null,2));
console.log(JSON.stringify(report));
