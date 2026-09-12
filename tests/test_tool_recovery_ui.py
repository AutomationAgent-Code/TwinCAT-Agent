"""Drive production recovery/result event handlers without a running XAE."""
from pathlib import Path
import subprocess
import re


def test_recovery_live_and_history_presentation():
    html = (Path(__file__).resolve().parents[1] / 'tc_agent/static/index.html').read_text(encoding='utf-8')
    cases = html[html.index('      case "tool_recovery": {'):html.index('      case "result": {', html.index('      case "tool_recovery": {'))]
    presentation = html[html.index('  function toolResultPresentation('):html.index('  // Render one transcript event.')]
    script = r'''
const assert = require('node:assert/strict');
const window = {};
const document = {createElement: () => ({})};
const scroll = () => {};
const showWorking = () => {};
const renderDeferredToolDetails = () => {throw new Error('must remain collapsed')};
function mount() {
  const title = {textContent: 'plc_diagnostics'};
  const summary = {children: [], appendChild(n) {this.children.push(n)}};
  const classes = new Set(['collapsed']);
  const el = {_toolName: 'plc_diagnostics', classList: {contains: k=>classes.has(k), add: k=>classes.add(k)},
    querySelector: q=>q.endsWith('.tname') ? title : summary};
  window._pendingTools = new Map([['call', el]]);
  window._pendingTool = el;
  return {el, title, summary, classes};
}
''' + presentation + '\nfunction event(ev, live) {const was=true; switch(ev.type) {\n' + cases + r'''
}}
for (const live of [true, false]) {
  let m = mount();
  event({type:'tool_recovery', tool_use_id:'call'}, live);
  assert.match(m.title.textContent, /正在恢复/);
  assert.equal(m.summary.children.length, 0);
  assert.equal(m.classes.has('tool-error'), false);
  event({type:'tool_result', tool_use_id:'call', ok:true, content:JSON.stringify({
    recovery:{recovered:true, history:[{error:'private raw diagnostic'}]}
  })}, live);
  assert.match(m.title.textContent, /已恢复/);
  assert.equal(m.summary.children.length, 0);
  assert.equal(m.classes.has('tool-error'), false);
  assert.match(m.el._toolResult.content, /private raw diagnostic/);

  m=mount();
  event({type:'tool_result', tool_use_id:'call', ok:false, content:JSON.stringify({
    status:'incomplete', error:'service unavailable', next_action:'inspect extension',
    recovery:{recovered:false, message:'recovery exhausted'}
  })}, live);
  assert.equal(m.summary.children.length, 1);
  assert.match(m.summary.children[0].textContent, /service unavailable/);
  assert.match(m.title.textContent, /恢复失败/);

  m=mount();
  event({type:'tool_result', tool_use_id:'call', ok:false, content:JSON.stringify({
    status:'write_result_unknown', written:'unknown'
  })}, live);
  assert.match(m.title.textContent, /禁止重试/);
}
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True, encoding='utf-8')
    # Parse all inline JavaScript too, not just the extracted event cases.
    for source in re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S):
        subprocess.run(['node', '-e', 'new Function(require("fs").readFileSync(0,"utf8"))'],
                       input=source, check=True, capture_output=True, text=True, encoding='utf-8')
