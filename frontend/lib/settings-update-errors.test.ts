import assert from "node:assert/strict";
import { describe, it } from "node:test";

const settingsUpdateErrorsModule = "./settings-update-errors.ts";
const { formatSettingsUpdateError } = await import(settingsUpdateErrorsModule);

describe("formatSettingsUpdateError", () => {
  it("prefers a string error", () => {
    assert.equal(
      formatSettingsUpdateError({ error: "Configuration is locked" }),
      "Configuration is locked",
    );
  });

  it("reads FastAPI string details including permission failures", () => {
    assert.equal(
      formatSettingsUpdateError({ detail: "Insufficient permissions" }),
      "Insufficient permissions",
    );
  });

  it("formats FastAPI validation detail arrays", () => {
    assert.equal(
      formatSettingsUpdateError({
        detail: [
          {
            loc: ["body", "retrieval_rrf_k"],
            msg: "Input should be less than or equal to 1000",
            type: "less_than_equal",
          },
        ],
      }),
      "retrieval_rrf_k: Input should be less than or equal to 1000",
    );
  });

  it("uses structured messages and a safe fallback", () => {
    assert.equal(
      formatSettingsUpdateError({ detail: { message: "Invalid setting" } }),
      "Invalid setting",
    );
    assert.equal(formatSettingsUpdateError({}), "Failed to update settings");
  });
});
