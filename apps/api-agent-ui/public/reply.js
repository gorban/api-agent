// Answers are scoped to a server question and preserve the user's earlier draft.
export function beginReply(state, message) {
  if (!message?.question || message.resolved || message.pending || message.error || state.reply) return false;
  state.reply = {messageId: message.id, savedDraft: state.draft};
  state.draft = '';
  return true;
}
export function takeReply(state, text, optionId) {
  const reply = state.reply;
  const message = state.messages.find(m => m.id === reply?.messageId && !m.resolved);
  if (!message || !text.trim()) return null;
  const question = message.question;
  if (question.kind === 'approval' && !question.options.some(x => x.id === optionId)) return null;
  message.resolved = true;
  state.draft = reply.savedDraft;
  state.reply = null;
  return {kind: question.kind, id: question.id, text, ...(optionId === undefined ? {} : {option_id: optionId})};
}
export function customAnswer(current, options) {
  return options.includes(current) ? '' : current;
}
