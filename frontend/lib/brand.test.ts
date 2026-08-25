import assert from "node:assert/strict";
import { describe, it } from "node:test";

const brandModule = "./brand.ts";
const { buildSettingsTabAccess, canShowRbacGatedSettingsTab } = await import(
  brandModule
);

describe("Retrieval settings tab access", () => {
  it("allows config:write in the SaaS RBAC policy", () => {
    const access = buildSettingsTabAccess({
      isIbmAuthMode: false,
      cloudContext: true,
      isNoAuthMode: false,
      rbacEnforced: true,
      permissions: new Set(["config:write"]),
    });

    assert.equal(canShowRbacGatedSettingsTab("config:write", access), true);
  });

  it("hides config settings without config:write in the SaaS RBAC policy", () => {
    const access = buildSettingsTabAccess({
      isIbmAuthMode: false,
      cloudContext: true,
      isNoAuthMode: false,
      rbacEnforced: true,
      permissions: new Set(),
    });

    assert.equal(canShowRbacGatedSettingsTab("config:write", access), false);
  });
});
