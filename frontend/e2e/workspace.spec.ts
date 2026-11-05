import { test, expect } from "@playwright/test";

test("staff signs in, maintains a product, creates a campaign and prints a PDF", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Username", { exact: true })
    .fill(process.env.E2E_USERNAME!);
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.E2E_PASSWORD!);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Dashboard", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Products", exact: true }).click();
  await page.getByRole("button", { name: "Add product", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Product description").fill("E2E Polish Cheese");
  await dialog.getByLabel("Internal product code").fill("E2E-" + Date.now());
  await dialog.getByLabel("Primary barcode").fill("5900820005535");
  await dialog.getByLabel("Brand", { exact: true }).fill("Demo Brand");
  await dialog.getByLabel("Weight / size").fill("150G");
  await dialog
    .getByRole("button", { name: "Save product", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await page.getByLabel("Search products").fill("E2E Polish Cheese");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByText("E2E Polish Cheese", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Select E2E Polish Cheese", { exact: true }).check();
  await page
    .getByRole("button", { name: "Create promotion (1)", exact: true })
    .click();
  await dialog.getByLabel("Campaign name").fill("E2E weekly campaign");
  await dialog
    .getByLabel("Price for E2E Polish Cheese", { exact: true })
    .fill("1.99");
  await dialog
    .getByRole("button", { name: "Save campaign", exact: true })
    .click();
  await expect(
    page.getByText("E2E weekly campaign", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Design & print", exact: true })
    .first()
    .click();
  await expect(page.getByTitle("A4 label PDF preview")).toBeVisible();
  await page
    .getByLabel("Orientation", { exact: true })
    .selectOption("landscape");
  await expect(
    page.getByRole("button", { name: "Generate 1 A4 labels", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Generate 1 A4 labels", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Download / reprint PDF" }).first(),
  ).toBeVisible({ timeout: 30000 });
  const url = await page
    .getByRole("link", { name: "Download / reprint PDF" })
    .first()
    .getAttribute("href");
  const response = await page.request.get(url!);
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toContain("application/pdf");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome back" }),
  ).toBeVisible();
});

test("mobile login has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
