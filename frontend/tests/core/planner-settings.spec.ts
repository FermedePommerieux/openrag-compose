import { expect, type Page, test } from "@playwright/test";

async function mockPlannerSettings(
  page: Page,
  options: { canWrite?: boolean; offline?: boolean; failSave?: boolean } = {},
) {
  const state = {
    posts: [] as Record<string, string>[],
    planner: {
      llm_provider: options.offline ? "ollama" : "openai",
      llm_model: options.offline ? "unlisted-model" : "chat-model",
      configured_source: options.offline
        ? "workspace_config.agent.planner"
        : "workspace_config.agent.agent_fallback",
    },
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let json: unknown = {};
    if (path === "/api/auth/me") {
      json = {
        authenticated: true,
        no_auth_mode: false,
        local_auth_enabled: true,
        run_mode: "oss",
        user: {
          user_id: "planner-admin",
          name: "Planner admin",
          provider: "local",
        },
      };
    } else if (path === "/api/users/me") {
      json = {
        permissions: [
          "config:read",
          "config:write",
          "providers:read",
          ...(options.canWrite === false ? [] : ["providers:write"]),
        ],
        rbac_enforced: true,
      };
    } else if (path === "/api/onboarding-status") {
      json = { onboarded: true, current_step: 99 };
    } else if (path.startsWith("/api/models/")) {
      json = {
        language_models: options.offline
          ? []
          : [
              { value: "chat-model", label: "Chat model" },
              { value: "small-planner", label: "Small planner" },
            ],
        embedding_models: [],
      };
    } else if (path === "/api/settings") {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        state.posts.push(body);
        if (options.failSave) {
          await route.fulfill({
            status: 503,
            json: { error: "Provider unavailable" },
          });
          return;
        }
        state.planner = body.planner_model
          ? {
              llm_model: body.planner_model,
              llm_provider: body.planner_provider,
              configured_source: "workspace_config.agent.planner",
            }
          : {
              llm_model: "chat-model",
              llm_provider: "openai",
              configured_source: "workspace_config.agent.agent_fallback",
            };
        json = { message: "Configuration updated successfully" };
      } else {
        json = {
          edited: true,
          onboarding: { current_step: 4 },
          agent: {
            llm_model: "chat-model",
            llm_provider: "openai",
            system_prompt: "Answer clearly.",
          },
          planner: state.planner,
          knowledge: {},
          providers: {
            openai: { configured: true },
            ollama: { configured: true, endpoint: "http://localhost:11434" },
            anthropic: { configured: false },
            watsonx: { configured: false },
          },
        };
      }
    }
    await route.fulfill({ json });
  });
  await page.goto("/settings/langflow");
  await expect(
    page.getByLabel("Search planning model", { exact: true }),
  ).toBeVisible();
  return state;
}

test("planner selection saves its provider independently, reloads, and resets to chat", async ({
  page,
}) => {
  const state = await mockPlannerSettings(page);
  const selector = page.getByLabel("Search planning model", { exact: true });
  await expect(selector).toContainText("Use chat model");
  await selector.click();
  await page.getByTestId("model-option-ollama:small-planner").click();
  await expect(selector).toContainText("Small planner");
  expect(state.posts).toEqual([
    { planner_model: "small-planner", planner_provider: "ollama" },
  ]);
  await expect(
    page.getByText("Currently: ollama / small-planner"),
  ).toBeVisible();
  await page.reload();
  await expect(selector).toContainText("Small planner");
  await selector.click();
  await page.getByTestId("model-option-__use_chat_model__").click();
  await expect(selector).toContainText("Use chat model");
  expect(state.posts[1]).toEqual({ planner_model: "", planner_provider: "" });
  await page.reload();
  await expect(selector).toContainText("Use chat model");
  await expect(page.getByText("Currently: openai / chat-model")).toBeVisible();
  expect(state.posts).toHaveLength(2);
});

test("an unavailable model remains selected until explicitly reset", async ({
  page,
}) => {
  const state = await mockPlannerSettings(page, { offline: true });
  const selector = page.getByLabel("Search planning model", { exact: true });
  await expect(selector).toContainText("ollama:unlisted-model");
  expect(state.posts).toHaveLength(0);
  await selector.click();
  await page.getByTestId("model-option-__use_chat_model__").click();
  await expect(selector).toContainText("Use chat model");
  expect(state.posts).toEqual([{ planner_model: "", planner_provider: "" }]);
});

test("planner selection requires provider write permission", async ({
  page,
}) => {
  const state = await mockPlannerSettings(page, { canWrite: false });
  await expect(
    page.getByLabel("Search planning model", { exact: true }),
  ).toBeDisabled();
  expect(state.posts).toHaveLength(0);
});

test("a failed save retains the persisted choice and displays the error", async ({
  page,
}) => {
  await mockPlannerSettings(page, { failSave: true });
  const selector = page.getByLabel("Search planning model", { exact: true });
  await selector.click();
  await page.getByTestId("model-option-openai:small-planner").click();
  await expect(
    page.getByText("Failed to update settings", { exact: true }),
  ).toBeVisible();
  await expect(selector).toContainText("Use chat model");
});
