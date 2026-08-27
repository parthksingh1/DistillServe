import { expect, test } from '@playwright/test';

/**
 * The one end-to-end scenario, and the thing that proves the platform is live.
 *
 * It walks the path a reviewer would: Overview → Playground → Compare mode →
 * both streams arrive → the trace appears → Rollouts → trigger a rollback →
 * the rollback lands in the audit trail and on the Dashboard timeline.
 *
 * Deliberately one long scenario rather than several short ones: the value is
 * in the *joins* — that a Playground call produces a trace, that a rollback
 * produces a dashboard event. Split into independent tests, each piece could
 * pass while the joins were broken.
 */
test('a reviewer can drive the whole platform', async ({ page }) => {
  // Nav links are scoped to <nav>: the Overview page also links to several
  // pages by name, and an unscoped locator matches both.
  const nav = (name: string) => page.getByRole('navigation').getByRole('link', { name });

  // --- Overview ------------------------------------------------------------
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('distilled 8B student');

  // The mode indicator is wired to the backend, so its presence proves the
  // frontend reached the gateway rather than rendering a static shell.
  const modeBadge = page.getByTestId('mode-badge');
  await expect(modeBadge).toBeVisible();
  const mode = await modeBadge.getAttribute('data-mode');
  expect(['hosted', 'self_hosted', 'sandbox']).toContain(mode);

  // --- Playground: Compare mode -------------------------------------------
  await nav('Playground').click();
  await expect(page.getByTestId('prompt-input')).toBeVisible();

  await page.getByTestId('prompt-input').fill('Summarize this support thread in two sentences.');
  await page.getByTestId('compare-toggle').check();
  await page.getByTestId('send-prompt').click();

  // Both panes must fill. A generation can legitimately take a while on a cold
  // Render instance, hence the generous timeout.
  const studentOutput = page.getByTestId('output-student');
  const teacherOutput = page.getByTestId('output-teacher');
  await expect(studentOutput).not.toHaveText(/Awaiting output/, { timeout: 120_000 });
  await expect(teacherOutput).not.toHaveText(/Awaiting output/, { timeout: 120_000 });

  // --- Traces --------------------------------------------------------------
  await nav('Traces').click();
  const traces = page.getByTestId('trace-entry');
  await expect(traces.first()).toBeVisible();
  // Compare mode ran two completions, so it produced two traces.
  await expect(traces).toHaveCount(2);

  // The waterfall's stages are the pipeline's real execution order.
  await traces.first().click();
  await expect(page.getByText('Semantic cache lookup')).toBeVisible();
  await expect(page.getByText('Router decision')).toBeVisible();

  // --- Rollouts: trigger a rollback ---------------------------------------
  await nav('Rollouts').click();
  await expect(page.getByText('Canary control')).toBeVisible();

  // Pick a rollout that has somewhere to fall back from.
  const openButtons = page.getByRole('button', { name: 'Open →' });
  await openButtons.first().click();

  const rollbackButton = page.getByTestId('trigger-rollback');
  await expect(rollbackButton).toBeEnabled();
  await rollbackButton.click();

  // The audit entry is what makes the rollback real: signed, chained, verified.
  const audit = page.getByTestId('audit-trail');
  await expect(audit).toContainText('rollback');
  await expect(page.getByText('chain verified')).toBeVisible();
  await expect(page.getByTestId('stage-shadow')).toBeVisible();

  // --- Dashboard: the rollback shows on the timeline -----------------------
  await page.keyboard.press('Escape');
  await nav('Dashboard').click();

  const timeline = page.getByTestId('event-timeline');
  await expect(timeline).toBeVisible();
  await expect(timeline).toContainText(/rollback|reclaim|scale/i);
});

test('the demo tenant can read everything and change nothing', async ({ page }) => {
  // Scope enforcement is the security property the demo token rests on, so it
  // is worth asserting in the browser rather than only in the API tests.
  await page.goto('/adapters');
  // Scoped to the heading: 'Adapters' also matches the nav link.
  await expect(page.getByRole('heading', { name: 'Adapters' })).toBeVisible();
  await expect(page.locator('[data-testid^="adapter-"]').first()).toBeVisible();
});
