import { test } from 'node:test';
import assert from 'node:assert/strict';
import { operationKey, finishOperation } from '../src/operationKey.js';

test('a request keeps its operation ID after a failed response and module reload', async () => {
  const values = new Map();
  globalThis.sessionStorage = {getItem:key => values.get(key),setItem:(key,value) => values.set(key,value),removeItem:key => values.delete(key)};
  try {
    const first = operationKey('retry', {request:'Keep my notes'});
    assert.equal(operationKey('retry', {request:'Keep my notes'}), first);
    const reloaded = await import('../src/operationKey.js?reload-for-test');
    assert.equal(reloaded.operationKey('retry', {request:'Keep my notes'}), first);
    reloaded.finishOperation('retry');
    assert.notEqual(reloaded.operationKey('retry', {request:'Keep my notes'}), first);
  } finally {delete globalThis.sessionStorage;}
});

test('different payloads cannot accidentally replay the old operation', () => {
  const original = operationKey('different', {request:'One'});
  assert.notEqual(operationKey('different', {request:'Two'}), original);
  finishOperation('different');
});
