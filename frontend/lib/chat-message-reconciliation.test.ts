import assert from "node:assert/strict";
import { describe, it } from "node:test";

const reconciliationModule = "./chat-message-reconciliation.ts";
const { shouldLoadConversationMessages } = await import(reconciliationModule);

const activeConversation = {
  isNewConversation: false,
  isStreaming: false,
  isUserInteracting: false,
  isForkingInProgress: false,
};

describe("shouldLoadConversationMessages", () => {
  it("keeps a completed local response when history is still behind", () => {
    assert.equal(
      shouldLoadConversationMessages({
        ...activeConversation,
        localMessageCount: 4,
        historyMessageCount: 3,
      }),
      false,
    );
  });

  it("does not replace local messages with an equivalent snapshot", () => {
    assert.equal(
      shouldLoadConversationMessages({
        ...activeConversation,
        localMessageCount: 4,
        historyMessageCount: 4,
      }),
      false,
    );
  });

  it("loads a newer history snapshot for the active conversation", () => {
    assert.equal(
      shouldLoadConversationMessages({
        ...activeConversation,
        localMessageCount: 4,
        historyMessageCount: 6,
      }),
      true,
    );
  });

  it("loads a different conversation regardless of message count", () => {
    assert.equal(
      shouldLoadConversationMessages({
        ...activeConversation,
        isNewConversation: true,
        localMessageCount: 10,
        historyMessageCount: 2,
      }),
      true,
    );
  });

  it("does not reconcile while streaming or during a local interaction", () => {
    assert.equal(
      shouldLoadConversationMessages({
        ...activeConversation,
        isStreaming: true,
        localMessageCount: 2,
        historyMessageCount: 4,
      }),
      false,
    );
    assert.equal(
      shouldLoadConversationMessages({
        ...activeConversation,
        isUserInteracting: true,
        localMessageCount: 2,
        historyMessageCount: 4,
      }),
      false,
    );
  });
});
