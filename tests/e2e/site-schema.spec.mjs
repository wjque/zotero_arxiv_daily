import { expect, test } from "@playwright/test";

const passphrase = "synthetic browser fixture passphrase";

test("decrypts and renders schema v5 recommendations", async ({ page }) => {
  page.on("dialog", (dialog) => dialog.accept(passphrase));

  await page.goto("/current/");

  await expect(page.locator("#status")).toContainText("1 recommendation(s) shown.");
  await expect(page.locator("article")).toHaveCount(1);
  await expect(page.locator("article h2")).toHaveText("Synthetic Browser Validation Paper");
  await expect(page.locator("article")).toContainText("74% quality");
  await expect(page.locator("article")).toContainText("22% uncertainty");
  await expect(page.locator("article")).toContainText("80% implementation evidence");
  await expect(page.locator("article")).toContainText("Evidence provenance");
  await expect(page.locator("article")).toContainText("Limitations");
  await expect(page.locator("article")).toContainText(
    "The fixture does not make a scientific quality claim.",
  );
});

test("exports distinct reading and worthwhile feedback", async ({ page }) => {
  page.on("dialog", (dialog) => dialog.accept(passphrase));
  await page.goto("/current/");

  const card = page.locator("article");
  const read = card.getByRole("button", { name: "Read", exact: true });
  const worthwhile = card.getByRole("button", { name: "Worthwhile", exact: true });
  await expect(worthwhile).toBeDisabled();

  await read.click();
  await expect(read).toHaveAttribute("aria-pressed", "true");
  await expect(worthwhile).toBeEnabled();
  await worthwhile.click();
  await expect(worthwhile).toHaveAttribute("aria-pressed", "true");

  const issue = page.locator("#feedback-issue");
  await expect(issue).toBeVisible();
  const target = new URL(await issue.getAttribute("href"));
  const payload = JSON.parse(target.searchParams.get("body"));
  expect(payload.schema_version).toBe(2);
  expect(payload.feedback).toHaveLength(1);
  expect(payload.feedback[0].batch_id).toBe("published-2026-08-04T08:00:00+00:00");
  expect(payload.feedback[0].actions.map((value) => value.action)).toEqual([
    "read",
    "worthwhile",
  ]);

  await page.locator("#feedback-confirm").click();
  await expect(page.locator("#feedback-export")).toBeHidden();
  await expect(worthwhile).toHaveAttribute("aria-pressed", "true");
});

test("migrates legacy browser feedback without false batch attribution", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem(
      "zotero-arxiv-daily-feedback-v1",
      JSON.stringify({
        schema_version: 1,
        actions: {
          "2401.00001": {
            action: "read",
            updated_at: "2026-08-03T08:00:00.000Z",
          },
        },
      }),
    );
  });
  page.on("dialog", (dialog) => dialog.accept(passphrase));
  await page.goto("/current/");

  const issue = page.locator("#feedback-issue");
  await expect(issue).toBeVisible();
  const target = new URL(await issue.getAttribute("href"));
  const payload = JSON.parse(target.searchParams.get("body"));

  expect(payload.schema_version).toBe(2);
  expect(payload.feedback).toEqual([
    {
      arxiv_id: "2401.00001",
      batch_id: "legacy-unattributed",
      actions: [{ action: "read", updated_at: "2026-08-03T08:00:00.000Z" }],
    },
  ]);
});

test("rejects an unsupported recommendation schema", async ({ page }) => {
  await page.goto("/unsupported/");

  await expect(page.locator("#status")).toHaveText("Recommendations could not be loaded safely.");
  await expect(page.locator(".error")).toContainText("Unsupported recommendation schema.");
  await expect(page.locator("article")).toHaveCount(0);
});
