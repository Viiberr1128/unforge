import { test } from 'node:test';
import assert from 'node:assert/strict';
import { previewDocument } from '../src/preview.js';

test('untrusted project markup cannot appear before the restrictive preview policy', () => {
  const payload = '<script src="https://example.invalid/payload.js"></script><img src="https://example.invalid/track"><form action="http://127.0.0.1:4319/api/projects"></form>';
  const result = previewDocument(payload);
  assert.ok(result.indexOf('Content-Security-Policy') < result.indexOf(payload));
  assert.ok(result.includes("default-src 'none'"));
  assert.ok(result.includes("form-action 'none'"));
  assert.ok(result.includes("base-uri 'none'"));
  assert.ok(!result.includes('allow-scripts'));
});
