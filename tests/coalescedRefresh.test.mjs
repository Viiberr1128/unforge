import test from 'node:test';
import assert from 'node:assert/strict';
import { coalescedRefresh } from '../src/coalescedRefresh.js';

const flush = () => new Promise(resolve => setImmediate(resolve));

test('timer and event storms share one read and one pending read', async () => {
  let calls = 0, active = 0, peak = 0;
  const releases = [], values = [];
  const reads = coalescedRefresh(() => {
    calls++; active++; peak = Math.max(peak, active);
    return new Promise(resolve => releases.push(value => { active--; resolve(value); }));
  }, {onValue:value => values.push(value)});
  const first = reads.request();
  const requests = Array.from({length:100}, () => reads.request());
  assert.equal(calls, 1);
  releases[0]('first'); await flush();
  assert.equal(calls, 2); assert.equal(peak, 1);
  releases[1]('latest');
  assert.equal(await first, 'latest');
  assert.deepEqual(await Promise.all(requests), Array(100).fill('latest'));
  assert.deepEqual(values, ['first', 'latest']);
  assert.equal(calls, 2);
});

test('hidden and disposed views stop queued requests and response writes', async () => {
  let visible = true, calls = 0, release;
  const values = [];
  const reads = coalescedRefresh(() => { calls++; return new Promise(resolve => { release = resolve; }); },
    {visible:() => visible, onValue:value => values.push(value)});
  const initial = reads.request(); reads.request();
  visible = false;
  await reads.request();
  release('visible request'); await initial;
  assert.equal(calls, 1);
  visible = true;
  const next = reads.request(); reads.request();
  reads.dispose(); release('obsolete response'); await next;
  await reads.request();
  assert.equal(calls, 2);
  assert.deepEqual(values, ['visible request']);
});

test('a failed read releases the slot and a later request can recover', async () => {
  let calls = 0;
  const values = [];
  const reads = coalescedRefresh(async () => { calls++; if (calls === 1) throw Error('temporary fixture error'); return 'recovered'; },
    {onValue:value => values.push(value)});
  await assert.rejects(reads.request(), /temporary fixture/);
  assert.equal(await reads.request(), 'recovered');
  assert.equal(calls, 2); assert.deepEqual(values, ['recovered']);
});

test('a disposed view does not report an obsolete request failure', async () => {
  let reject;
  const reads = coalescedRefresh(() => new Promise((_, fail) => { reject = fail; }));
  const request = reads.request();
  reads.dispose(); reject(Error('obsolete fixture failure'));
  assert.equal(await request, undefined);
});
