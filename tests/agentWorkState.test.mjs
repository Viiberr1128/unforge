import { test } from 'node:test';
import assert from 'node:assert/strict';
import { agentWorkView, attentionMessage, providerSummary } from '../src/agentWorkState.js';

test('each durable job state has one plain title and primary action', () => {
  const expected = {
    running:'stop', completed:'apply', failed:'none', cancelled:'none', timed_out:'none',
    applied:'none', rejected:'none', archived:'none', unknown:'recover',
  };
  for (const [status, action] of Object.entries(expected)) {
    const view = agentWorkView({status, changedFiles:[{path:'a'}]});
    assert.ok(view.title);
    assert.equal(view.action, action);
  }
});

test('no-change completion cannot be applied and observed phase wins', () => {
  assert.equal(agentWorkView({status:'completed',changedFiles:[]}).action, 'none');
  assert.equal(agentWorkView({status:'running',phase:{label:'Checking the result'}}).phase, 'Checking the result');
  assert.equal(agentWorkView({status:'applied',phase:{label:'Ready to try'}}).phase, null);
});

test('an interrupted caller watches a still-active exact Fleet operation instead of offering recovery early', () => {
  const view = agentWorkView({status:'unknown',bridgeActive:true,phase:{label:'Making the changes'}});
  assert.equal(view.action, 'none');
  assert.equal(view.phase, 'Making the changes');
  assert.equal(agentWorkView({status:'unknown',bridgeActive:false,bridgeRecoverable:false}).action, 'none');
  assert.equal(agentWorkView({status:'unknown',bridgeRecoverable:true}).action, 'recover');
});

test('provider readiness stays distinct', () => {
  assert.equal(providerSummary({available:false}), 'Not installed');
  assert.equal(providerSummary({available:true,state:'needs-refresh'}), 'Usage needs refreshing');
  assert.equal(providerSummary({available:true,state:'ready'}), 'Ready');
});

test('authentication, quota, and check failures stay distinct', () => {
  assert.match(attentionMessage('sign_in'), /sign in/);
  assert.match(attentionMessage('quota'), /usage/);
  assert.match(attentionMessage('failed_check'), /check/);
  assert.notEqual(attentionMessage('sign_in'), attentionMessage('quota'));
});
