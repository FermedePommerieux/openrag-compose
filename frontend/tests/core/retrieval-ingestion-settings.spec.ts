import type { Page } from "@playwright/test";
import { expect, test } from "../utils/fixtures";

type Knowledge = Record<string, boolean | number | string | undefined>;

type SettingsMock = {
  getCalls: number;
  posts: Array<Record<string, unknown>>;
  knowledge: Knowledge;
};

const BASE_KNOWLEDGE: Knowledge = {
  embedding_model: "",
  embedding_provider: "openai",
  chunk_size: 1000,
  chunk_overlap: 200,
  chunking_strategy: "character",
  hybrid_max_tokens: 512,
  hybrid_merge_peers: true,
  table_structure: true,
  ocr: false,
  picture_descriptions: false,
  disable_ingest_with_langflow: false,
  retrieval_strategy: "rrf",
  retrieval_mode: "hybrid",
  retrieval_lexical_candidates: 50,
  retrieval_vector_candidates: 50,
  retrieval_rrf_k: 60,
  retrieval_max_chunks_per_document: 3,
  retrieval_debug: false,
};

async function mockSettings(
  page: Page,
  initialKnowledge: Partial<Knowledge> = {},
  resolvePost?: (body: Record<string, unknown>) => Partial<Knowledge>,
): Promise<SettingsMock> {
  const state: SettingsMock = {
    getCalls: 0,
    posts: [],
    knowledge: { ...BASE_KNOWLEDGE, ...initialKnowledge },
  };

  await page.route("**/api/auth/me", async (route) => {
    await route.fulfill({
      json: {
        authenticated: false,
        no_auth_mode: true,
        ibm_auth_mode: false,
        run_mode: "cpu",
      },
    });
  });
  await page.route("**/api/users/me", async (route) => {
    await route.fulfill({
      json: { permissions: ["config:write"], rbac_enforced: false },
    });
  });
  await page.route("**/api/onboarding-status", async (route) => {
    await route.fulfill({ json: { onboarded: true, current_step: 99 } });
  });
  await page.route("**/api/settings*", async (route) => {
    if (route.request().method() === "GET") {
      state.getCalls += 1;
      await route.fulfill({
        json: {
          edited: true,
          onboarding: { current_step: 4 },
          knowledge: state.knowledge,
          providers: {
            openai: { configured: false },
            anthropic: { configured: false },
            watsonx: { configured: false },
            ollama: { configured: false },
          },
        },
      });
      return;
    }

    const body = route.request().postDataJSON() as Record<string, unknown>;
    state.posts.push(body);
    state.knowledge = {
      ...state.knowledge,
      ...body,
      ...(resolvePost?.(body) ?? {}),
    } as Knowledge;
    await route.fulfill({
      json: { message: "Configuration updated successfully" },
    });
  });

  return state;
}

async function openSettings(page: Page, tab: "langflow" | "retrieval") {
  await page.goto(`/settings/${tab}`);
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
}

test.describe("Retrieval and ingestion settings contracts", () => {
  test("loads Character controls, persists Hybrid controls, and refetches", async ({
    page,
  }) => {
    const settings = await mockSettings(page, {}, () => ({
      hybrid_max_tokens: 900,
    }));
    await openSettings(page, "langflow");

    await expect(page.getByLabel("Chunk size")).toHaveValue("1000");
    await expect(page.getByLabel("Chunk overlap")).toHaveValue("200");
    await expect(page.getByLabel("Hybrid max tokens")).toHaveCount(0);
    await expect(page.getByText(/Index-affecting settings/i)).toBeVisible();

    await page.getByRole("button", { name: "Hybrid", exact: true }).click();
    await page.getByLabel("Hybrid max tokens").fill("768");
    await page.getByRole("switch", { name: "Merge peers" }).click();
    const getCallsBeforeSave = settings.getCalls;
    await page.getByRole("button", { name: "Save ingest settings" }).click();

    await expect.poll(() => settings.posts.length).toBe(1);
    expect(settings.posts[0]).toMatchObject({
      chunking_strategy: "hybrid",
      hybrid_max_tokens: 768,
      hybrid_merge_peers: false,
    });
    await expect
      .poll(() => settings.getCalls)
      .toBeGreaterThan(getCallsBeforeSave);
    await expect(page.getByLabel("Hybrid max tokens")).toHaveValue("900");
  });

  test("loads Hybrid controls without presenting Character parameters as active", async ({
    page,
  }) => {
    await mockSettings(page, {
      chunking_strategy: "hybrid",
      hybrid_max_tokens: 640,
      hybrid_merge_peers: false,
    });
    await openSettings(page, "langflow");

    await expect(page.getByLabel("Hybrid max tokens")).toHaveValue("640");
    await expect(
      page.getByRole("switch", { name: "Merge peers" }),
    ).not.toBeChecked();
    await expect(page.getByLabel("Chunk size")).toHaveCount(0);
    await expect(page.getByLabel("Chunk overlap")).toHaveCount(0);
  });

  test("keeps weighted untouched and activates Search mode only after switching to RRF", async ({
    page,
  }) => {
    const settings = await mockSettings(page, {
      retrieval_strategy: "weighted",
      retrieval_mode: "vector",
    });
    await openSettings(page, "retrieval");

    await expect(
      page.getByText("Legacy weighted retrieval").first(),
    ).toBeVisible();
    await expect(
      page.getByRole("combobox", { name: "Search mode" }),
    ).toHaveCount(0);
    await expect(
      page.getByText(/Available with Standard \(RRF\)/i),
    ).toBeVisible();
    expect(settings.posts).toEqual([]);

    await page
      .getByRole("combobox", { name: "Compatibility retrieval strategy" })
      .click();
    await page.getByRole("option", { name: /Standard \(RRF\)/ }).click();
    await expect(
      page.getByRole("combobox", { name: "Search mode" }),
    ).toBeVisible();
  });

  test("persists each RRF search mode and validates RRF parameter bounds", async ({
    page,
  }) => {
    const settings = await mockSettings(page);
    await openSettings(page, "retrieval");

    const mode = page.getByRole("combobox", { name: "Search mode" });
    await expect(
      page.getByRole("combobox", { name: "Compatibility retrieval" }),
    ).toHaveText("Standard (RRF) — Recommended");
    for (const [label, value] of [
      ["Lexical only", "lexical"],
      ["Vector only", "vector"],
      ["Hybrid (lexical + semantic)", "hybrid"],
    ] as const) {
      await mode.click();
      await page.getByRole("option", { name: label, exact: true }).click();
      const postCount = settings.posts.length;
      await page
        .getByRole("button", { name: "Save retrieval settings" })
        .click({ force: true });
      await expect.poll(() => settings.posts.length).toBe(postCount + 1);
      expect(settings.posts.at(-1)).toMatchObject({
        retrieval_strategy: "rrf",
        retrieval_mode: value,
      });
      await expect(
        page.getByRole("button", { name: "Save retrieval settings" }),
      ).toBeDisabled();
      await page.reload();
      await expect(
        page.getByRole("heading", { name: "Settings" }),
      ).toBeVisible();
    }

    const invalidCases = [
      [
        "Lexical candidates",
        "501",
        "Lexical candidates must be between 1 and 500.",
      ],
      [
        "Vector candidates",
        "501",
        "Vector candidates must be between 1 and 500.",
      ],
      ["RRF k", "1001", "RRF k must be between 1 and 1000."],
      [
        "Max chunks per document",
        "101",
        "Max chunks per document must be between 1 and 100.",
      ],
    ] as const;
    for (const [label, value, error] of invalidCases) {
      const postCount = settings.posts.length;
      await page.getByLabel(label).fill(value);
      await page
        .getByRole("button", { name: "Save retrieval settings" })
        .click({ force: true });
      await expect(page.getByText(error, { exact: true })).toBeVisible();
      expect(settings.posts).toHaveLength(postCount);
      await page.reload();
      await expect(
        page.getByRole("heading", { name: "Settings" }),
      ).toBeVisible();
    }
  });

  test("recognizes the Retrieval settings route and navigation tab", async ({
    page,
  }) => {
    await mockSettings(page);
    await openSettings(page, "retrieval");

    await expect(page.getByRole("tab", { name: "Retrieval" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("surfaces FastAPI validation and permission errors from settings mutations", async ({
    page,
  }) => {
    await mockSettings(page);
    const failures = [
      {
        status: 422,
        json: {
          detail: [
            {
              loc: ["body", "retrieval_rrf_k"],
              msg: "Input should be less than or equal to 1000",
              type: "less_than_equal",
            },
          ],
        },
      },
      { status: 403, json: { detail: "Insufficient permissions" } },
    ];
    await page.route("**/api/settings*", async (route) => {
      if (route.request().method() !== "POST") {
        await route.fallback();
        return;
      }
      const failure = failures.shift();
      await route.fulfill({
        status: failure?.status ?? 500,
        json: failure?.json ?? { detail: "Unexpected failure" },
      });
    });
    await openSettings(page, "retrieval");

    await page.getByLabel("RRF k").fill("59");
    await page.getByRole("button", { name: "Save retrieval settings" }).click();
    await expect(
      page.getByText(
        "retrieval_rrf_k: Input should be less than or equal to 1000",
      ),
    ).toBeVisible();

    await page.getByLabel("RRF k").fill("58");
    await page.getByRole("button", { name: "Save retrieval settings" }).click();
    await expect(page.getByText("Insufficient permissions")).toBeVisible();
  });
});
