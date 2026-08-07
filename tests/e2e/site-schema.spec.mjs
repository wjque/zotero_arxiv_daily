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

test("rejects an unsupported recommendation schema", async ({ page }) => {
  await page.goto("/unsupported/");

  await expect(page.locator("#status")).toHaveText("Recommendations could not be loaded safely.");
  await expect(page.locator(".error")).toContainText("Unsupported recommendation schema.");
  await expect(page.locator("article")).toHaveCount(0);
});
