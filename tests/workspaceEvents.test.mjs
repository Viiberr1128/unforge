import test from 'node:test';
import assert from 'node:assert/strict';
import { followWorkspaceChanges } from '../src/workspaceEvents.js';

test('workspace changes coalesce during refresh and stop while hidden or disposed', async () => {
  const document = new EventTarget();
  document.hidden = false;
  const window = new EventTarget();
  const sources = [];
  class EventSource {
    constructor() { sources.push(this); }
    close() { this.closed = true; }
  }
  const old = { document: globalThis.document, window: globalThis.window, EventSource: globalThis.EventSource };
  Object.assign(globalThis, { document, window, EventSource });
  let calls = 0, dispatched = 0, release;
  window.addEventListener('unforge-workspace-changed', () => dispatched++);
  const dispose = followWorkspaceChanges(() => { calls++; return new Promise(resolve => { release = resolve; }); });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  try {
    await flush();
    for (let n = 0; n < 100; n++) sources[0].onmessage();
    assert.equal(calls, 1);
    release(); await flush();
    assert.equal(calls, 2); assert.equal(dispatched, 1);
    document.hidden = true;
    document.dispatchEvent(new Event('visibilitychange'));
    assert.equal(sources[0].closed, true);
    release(); await flush();
    assert.equal(dispatched, 1);
    document.hidden = false;
    document.dispatchEvent(new Event('visibilitychange')); await flush();
    assert.equal(calls, 3);
    dispose(); release(); await flush();
    assert.equal(sources[1].closed, true); assert.equal(dispatched, 1);
  } finally {
    dispose();
    for (const key of Object.keys(old)) {
      if (old[key] === undefined) delete globalThis[key]; else globalThis[key] = old[key];
    }
  }
});
