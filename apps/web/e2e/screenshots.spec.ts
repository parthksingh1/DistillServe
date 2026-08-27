import { expect, test, type Page } from '@playwright/test';

/**
 * Capture the README's screenshot grid.
 *
 * Run with `pnpm --filter @distillserve/web screenshots`. Not part of the
 * normal test run: it writes files, and a test that writes into the repository
 * has no business running on every commit.
 *
 * Every shot waits for real data to land before capturing. A screenshot of a
 * skeleton is worse than no screenshot — it advertises a loading state as the
 * product.
 */

const DIR = '../../docs/screenshots';

// 2x on a 1440-wide viewport: crisp on a retina display, and 1440 is the width
// GitHub renders a README image column at without downscaling into mush.
test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });

async function settle(page: Page): Promise<void> {
  // Let entry animations finish. Capturing mid-transition produces a shot with
  // half-faded cards that reads as a rendering bug.
  await page.waitForTimeout(900);
}

test('overview', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('mode-badge')).toBeVisible();
  await settle(page);
  await page.screenshot({ path: `${DIR}/overview.png`, fullPage: false });

  // The full page too, since the architecture grid and decisions are below the
  // fold and are most of what the Overview is for.
  await page.screenshot({ path: `${DIR}/overview-full.png`, fullPage: true });
});

test('dashboard', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByTestId('event-timeline')).toBeVisible();
  // Charts animate in; capture after they have drawn.
  await page.waitForTimeout(1800);
  await page.screenshot({ path: `${DIR}/dashboard.png` });
});

test('adapters', async ({ page }) => {
  await page.goto('/adapters');
  await expect(page.locator('[data-testid^="adapter-"]').first()).toBeVisible();
  await settle(page);
  await page.screenshot({ path: `${DIR}/adapters.png` });

  // The detail drawer, which is where the per-slice heatmap lives.
  await page.locator('[data-testid^="adapter-"]').first().click();
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${DIR}/adapter-detail.png` });
});

test('rollouts', async ({ page }) => {
  await page.goto('/rollouts');
  await expect(page.getByRole('button', { name: 'Open →' }).first()).toBeVisible();
  await settle(page);
  await page.screenshot({ path: `${DIR}/rollouts.png` });

  // The drawer, on a rollout that is still live — and with a rollback actually
  // performed, so the audit trail in the shot has entries in it. A screenshot
  // of an empty audit trail does not show what the audit trail is for.
  await page
    .getByRole('row')
    .filter({ hasText: 'canary' })
    .first()
    .getByRole('button', { name: 'Open →' })
    .click();
  await expect(page.getByTestId('stage-shadow')).toBeVisible();
  await page.getByTestId('trigger-rollback').click();
  await expect(page.getByTestId('audit-trail')).toContainText('rollback');
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${DIR}/rollout-detail.png` });
});

test('evals', async ({ page }) => {
  await page.goto('/evals');
  await expect(page.getByTestId('parity-heatmap')).toBeVisible();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${DIR}/evals.png` });
});

test('registry', async ({ page }) => {
  await page.goto('/registry');
  await expect(page.locator('[data-testid^="registry-"]').first()).toBeVisible();
  await settle(page);
  await page.screenshot({ path: `${DIR}/registry.png` });
});

test('playground', async ({ page }) => {
  await page.goto('/playground');
  await expect(page.getByTestId('prompt-input')).toBeVisible();
  await settle(page);
  await page.screenshot({ path: `${DIR}/playground.png` });
});

test('traces', async ({ page }) => {
  await page.goto('/traces');
  await settle(page);
  await page.screenshot({ path: `${DIR}/traces.png` });
});

test('command palette', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('mode-badge')).toBeVisible();
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeVisible();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${DIR}/command-palette.png` });
});

test('demo access', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('mode-badge')).toBeVisible();
  await page.getByTestId('identity-button').click();
  await expect(page.getByRole('dialog', { name: 'Access' })).toBeVisible();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${DIR}/demo-access.png` });
});
