import test from 'node:test';
import assert from 'node:assert/strict';
import {beginReply, takeReply, customAnswer} from '../../apps/api-agent-ui/public/reply.js';
const question = (kind = 'clarification') => ({id: 'message', question: {kind, id: 'question', options: [{id: 'yes', label: 'Proceed'}]}, requested: {model: 'model'}});
const stateFor = message => ({draft: 'Unrelated draft', messages: [message], reply: null});
test('question answer restores earlier draft and keeps server identity', () => {
  const message = question(), state = stateFor(message);
  assert.equal(beginReply(state, message), true);
  assert.equal(state.draft, '');
  assert.equal(beginReply(state, message), false);
  assert.deepEqual(takeReply(state, 'My edited suggestion'), {kind: 'clarification', id: 'question', text: 'My edited suggestion'});
  assert.equal(state.draft, 'Unrelated draft');
  assert.equal(state.reply, null);
  assert.equal(message.resolved, true);
});
test('approval requires an explicit option identity', () => {
  const message = question('approval'), state = stateFor(message);
  beginReply(state, message);
  assert.equal(takeReply(state, 'Proceed'), null);
  assert.equal(takeReply(state, 'Proceed', 'other'), null);
  assert.equal(message.resolved, undefined);
  assert.deepEqual(takeReply(state, 'Proceed', 'yes'), {kind: 'approval', id: 'question', text: 'Proceed', option_id: 'yes'});
});
test('unavailable questions cannot enter answer mode', () => {
  for (const message of [null, {}, {...question(), resolved: true}, {...question(), pending: true}, {...question(), error: 'failed'}]) {
    assert.equal(beginReply(stateFor(message), message), false);
  }
});
test('empty or orphaned answers preserve pending question', () => {
  const message = question(), state = stateFor(message);
  assert.equal(takeReply(state, 'answer'), null);
  beginReply(state, message);
  assert.equal(takeReply(state, '   '), null);
  message.resolved = true;
  assert.equal(takeReply(state, 'answer'), null);
});
test('custom answer clears only an exact suggestion', () => {
  assert.equal(customAnswer('Suggestion', ['Suggestion']), '');
  assert.equal(customAnswer('Suggestion edited', ['Suggestion']), 'Suggestion edited');
});
