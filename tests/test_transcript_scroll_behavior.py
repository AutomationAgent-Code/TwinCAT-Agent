"""Behavior tests for the WebView transcript scroll state machine.

The controller is extracted from the production HTML and exercised in Node
with a small fake scroll container.  These are interaction tests, not string
presence checks: every case drives history, user intent, layout, or a stale
async callback and asserts the resulting scroll position.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "tc_agent" / "static" / "index.html"


def test_transcript_scroll_state_machine_behaviors() -> None:
    html = HTML.read_text(encoding="utf-8")
    start = html.index("  function createTranscriptScrollController(")
    end = html.index("  // === transcript-scroll-controller:end ===", start)
    controller_source = html[start:end]

    script = f"""
const assert = require('node:assert/strict');
const createTranscriptScrollController = (() => {{
{controller_source}
  return createTranscriptScrollController;
}})();

function make() {{
  let clock = 0;
  const jobs = [];
  const positions = new Map();
  const log = {{scrollTop: 0, scrollHeight: 700, clientHeight: 100, children: []}};
  Object.defineProperty(log, 'childElementCount', {{get: () => log.children.length}});
  const controller = createTranscriptScrollController({{
    log,
    schedule: callback => jobs.push(callback),
    getPosition: threadId => positions.has(threadId) ? positions.get(threadId) : null,
    setPosition: (threadId, top) => positions.set(threadId, top),
    isCurrentThread: () => true,
    now: () => clock,
  }});
  const flush = () => {{ while (jobs.length) jobs.shift()(); }};
  const tick = ms => {{ clock += ms; }};
  return {{log, positions, controller, flush, tick}};
}}

function mount(env, threadId, reason, withContent = true) {{
  const token = env.controller.beginHistory(threadId, reason);
  env.log.children = withContent ? [{{id: 'history'}}] : [];
  assert.equal(env.controller.finishHistory(threadId, reason, token), true);
  env.flush();
}}

// Reload/initial mount ignores an old local position and opens at the latest
// transcript content.
{{
  const e = make();
  e.positions.set('A', 42);
  mount(e, 'A', 'initial');
  assert.equal(e.log.scrollTop, 600);
  assert.equal(e.positions.get('A'), 600);
}}

// An empty shell is never allowed to replace a previously saved position.
{{
  const e = make();
  e.positions.set('A', 123);
  e.log.scrollHeight = 0;
  mount(e, 'A', 'refresh', false);
  assert.equal(e.log.scrollTop, 0);
  assert.equal(e.positions.get('A'), 123);
}}

// Same-thread refresh restores the read position after an async image/layout
// change, while a fast switch cannot be completed by an old token.
{{
  const e = make();
  mount(e, 'A', 'initial');
  e.tick(200); e.log.scrollTop = 120; e.controller.markUserIntent('A'); e.controller.onScroll('A');
  const stale = e.controller.beginHistory('A', 'refresh');
  e.log.children = [{{id: 'history'}}];
  assert.equal(e.controller.finishHistory('A', 'refresh', stale), true);
  e.flush(); assert.equal(e.log.scrollTop, 120);
  e.log.scrollHeight = 980; e.tick(10); e.controller.layoutChanged('A'); e.flush();
  assert.equal(e.log.scrollTop, 120);

  const switchToken = e.controller.beginHistory('B', 'switch');
  e.log.children = [{{id: 'history-b'}}];
  assert.equal(e.controller.finishHistory('B', 'switch', switchToken), true);
  e.flush();
  const beforeLateCallback = e.log.scrollTop;
  assert.equal(e.controller.finishHistory('A', 'refresh', stale), false);
  assert.equal(e.log.scrollTop, beforeLateCallback);
}}

// Reconnect preserves whether the user was reading up or following the
// bottom; streaming follows only while that intent remains active.
{{
  const e = make();
  mount(e, 'A', 'initial');
  e.tick(200); e.log.scrollTop = 150; e.controller.markUserIntent('A'); e.controller.onScroll('A');
  const reconnect = e.controller.beginHistory('A', 'reconnect');
  e.log.children = [{{id: 'history'}}];
  e.controller.finishHistory('A', 'reconnect', reconnect); e.flush();
  assert.equal(e.log.scrollTop, 150);
  e.log.scrollHeight = 900; e.tick(10); e.controller.layoutChanged('A'); e.flush();
  assert.equal(e.log.scrollTop, 150);

  e.log.scrollTop = 800; e.controller.markUserIntent('A'); e.controller.onScroll('A');
  e.log.scrollHeight = 1100; e.controller.contentChanged('A', true); e.flush();
  assert.equal(e.log.scrollTop, 1000);
  e.tick(200); e.log.scrollTop = 300; e.controller.markUserIntent('A'); e.controller.onScroll('A');
  e.log.scrollHeight = 1400; e.controller.contentChanged('A', true); e.flush();
  assert.equal(e.log.scrollTop, 300);
}}

// No-history and short-content cases are safe and remain clamped.
{{
  const e = make();
  mount(e, 'A', 'initial', false);
  e.log.children = [{{id: 'short'}}]; e.log.scrollHeight = 80;
  e.controller.layoutChanged('A'); e.flush();
  assert.equal(e.log.scrollTop, 0);
}}

console.log('transcript scroll behavior: ok');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, json.dumps(
        {"stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False
    )
    assert "transcript scroll behavior: ok" in completed.stdout
