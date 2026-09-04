interface ShouldLoadConversationMessagesOptions {
  isNewConversation: boolean;
  isStreaming: boolean;
  isUserInteracting: boolean;
  isForkingInProgress: boolean;
  localMessageCount: number;
  historyMessageCount: number;
}

/**
 * Decide whether a history snapshot may replace the messages already rendered.
 *
 * A completed streamed response is immediately available in local state, while
 * the history endpoint can briefly return an older snapshot. Only a genuinely
 * newer snapshot may replace messages for the active conversation. Switching
 * conversations remains an unconditional load.
 */
export function shouldLoadConversationMessages({
  isNewConversation,
  isStreaming,
  isUserInteracting,
  isForkingInProgress,
  localMessageCount,
  historyMessageCount,
}: ShouldLoadConversationMessagesOptions): boolean {
  if (isUserInteracting || isForkingInProgress) return false;
  if (isNewConversation) return true;
  if (isStreaming) return false;

  return historyMessageCount > localMessageCount;
}
