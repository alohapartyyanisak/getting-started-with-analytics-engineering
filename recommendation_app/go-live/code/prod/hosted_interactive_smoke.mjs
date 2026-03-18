import { chromium } from 'playwright';

const APP_URL = String(process.env.APP_URL || '').trim();
if (!APP_URL) {
  console.error('APP_URL is required');
  process.exit(2);
}

const ARTISTS = String(process.env.SMOKE_ARTISTS || 'Ed Sheeran|Bruno Mars')
  .split('|')
  .map((s) => s.trim())
  .filter(Boolean);
const MAX_ATTEMPTS = Math.max(1, Number.parseInt(process.env.SMOKE_MAX_ATTEMPTS || '3', 10) || 3);
const eventLog = {
  console: [],
  page_errors: [],
  request_failures: [],
};

function appendEvent(target, value, limit = 40) {
  if (target.length < limit) {
    target.push(value);
  }
}

function shouldBlockRequest(url) {
  return [
    'youtube.com',
    'youtube-nocookie.com',
    'ytimg.com',
    'googlevideo.com',
    'doubleclick.net',
    'googleads.g.doubleclick.net',
  ].some((needle) => url.includes(needle));
}

async function attachNetworkGuards(page) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    if (shouldBlockRequest(url)) {
      appendEvent(eventLog.request_failures, `${request.method()} ${url} :: blocked_for_smoke`);
      await route.abort();
      return;
    }
    await route.continue();
  });

  page.on('console', (msg) => appendEvent(eventLog.console, `${msg.type()}: ${msg.text()}`));
  page.on('pageerror', (error) => appendEvent(eventLog.page_errors, String(error?.message || error)));
  page.on('requestfailed', (request) => {
    appendEvent(
      eventLog.request_failures,
      `${request.method()} ${request.url()} :: ${request.failure()?.errorText || 'failed'}`,
    );
  });
}

async function waitForBasePage(page) {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.getByText('Startup Health: Healthy', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  await page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
}

async function waitForRadioGroups(page) {
  const groupSelectors = [
    'div[data-testid="stRadio"]',
    '[role="radiogroup"]',
  ];

  for (const selector of groupSelectors) {
    const groups = page.locator(selector);
    const count = await groups.count();
    if (count >= 2) {
      await groups.first().waitFor({ state: 'visible', timeout: 30000 });
      return groups;
    }
  }

  throw new Error('Could not find two radio groups on the page');
}

async function clickRadioLabel(page, labelText) {
  const locators = [
    page.locator('label[data-baseweb="radio"]').filter({ hasText: labelText }).first(),
    page.getByText(labelText, { exact: true }).first(),
  ];

  for (const locator of locators) {
    const count = await locator.count();
    if (count < 1) {
      continue;
    }
    await locator.waitFor({ state: 'visible', timeout: 30000 }).catch(() => null);
    await locator.scrollIntoViewIfNeeded().catch(() => null);
    try {
      await locator.click({ force: true });
      return;
    } catch (_error) {
      continue;
    }
  }

  throw new Error(`Could not click radio label ${labelText}`);
}

async function waitForLabelSelection(page, labelText) {
  await page.waitForFunction(
    (targetText) => {
      const labels = Array.from(document.querySelectorAll('label[data-baseweb="radio"]'));
      return labels.some((label) => {
        const text = (label.textContent || '').trim();
        if (!text.includes(targetText)) {
          return false;
        }
        const input = label.querySelector('input[type="radio"]');
        return Boolean(input && input.checked);
      });
    },
    labelText,
    { timeout: 30000 },
  );
}

async function switchToArtistMode(page) {
  const artistInput = page.getByPlaceholder('Search and select artists').first();
  if (await artistInput.isVisible().catch(() => false)) {
    return;
  }

  await waitForRadioGroups(page);
  await clickRadioLabel(page, 'Self Mix');
  await waitForLabelSelection(page, 'Self Mix').catch(() => null);
  await clickRadioLabel(page, 'Pick Artists');
  await waitForLabelSelection(page, 'Pick Artists').catch(() => null);
  await page.waitForTimeout(1500);
  await artistInput.waitFor({ state: 'visible', timeout: 30000 });
}

async function chooseArtist(page, artistName) {
  const quickChip = page.getByRole('button', { name: artistName }).first();
  if (await quickChip.isVisible().catch(() => false)) {
    await quickChip.click({ force: true });
    const expectedChip = page.getByText(`Artist: ${artistName}`, { exact: false }).first();
    await expectedChip.waitFor({ state: 'visible', timeout: 15000 });
    return;
  }

  const input = page.getByPlaceholder('Search and select artists').first();
  await input.click();
  await input.fill(artistName);

  const option = page.locator('[role="option"]').filter({ hasText: artistName }).first();
  await option.waitFor({ state: 'visible', timeout: 15000 });
  await option.click();

  const expectedChip = page.getByText(`Artist: ${artistName}`, { exact: false }).first();
  await expectedChip.waitFor({ state: 'visible', timeout: 15000 });
}

async function runAttempt(attemptNumber) {
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-dev-shm-usage'],
  });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 2000 },
    serviceWorkers: 'block',
  });
  const page = await context.newPage();
  await attachNetworkGuards(page);

  const attempt = {
    attempt: attemptNumber,
    status: 'fail',
    checks: {},
    dom_debug: {},
  };

  try {
    await waitForBasePage(page);
    attempt.checks.page_loaded = true;
    attempt.dom_debug = await page.evaluate(() => ({
      stRadioCount: document.querySelectorAll('div[data-testid="stRadio"]').length,
      ariaRadioGroupCount: document.querySelectorAll('[role="radiogroup"]').length,
      basewebRadioCount: document.querySelectorAll('label[data-baseweb="radio"]').length,
      ariaRadioCount: document.querySelectorAll('[role="radio"]').length,
      inputRadioCount: document.querySelectorAll('input[type="radio"]').length,
      radioLabels: Array.from(document.querySelectorAll('label[data-baseweb="radio"]'))
        .slice(0, 12)
        .map((label) => ({
          text: (label.textContent || '').trim(),
          checked: Boolean(label.querySelector('input[type="radio"]')?.checked),
        })),
    }));

    await switchToArtistMode(page);
    attempt.checks.artist_mode_ready = true;

    for (const artist of ARTISTS) {
      await chooseArtist(page, artist);
    }
    attempt.checks.artist_selection_persists = true;

    await page.getByText('Final Picks (2/5)', { exact: false }).waitFor({ state: 'visible', timeout: 30000 });
    attempt.checks.final_pick_box_updated = true;

    await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 30000 });
    attempt.checks.playable_playlist_visible = true;

    const playlistLink = page.getByRole('link', { name: 'Open Temporary YouTube Playlist' });
    await playlistLink.waitFor({ state: 'visible', timeout: 30000 });
    attempt.checks.temp_playlist_link_visible = true;

    attempt.status = 'pass';
    await browser.close();
    return attempt;
  } catch (error) {
    attempt.error = String(error?.message || error);
    const screenshotPath = `recommendation_app/go-live/code/prod/.artifacts/hosted_interactive_smoke_failure_attempt_${attemptNumber}.png`;
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
    attempt.screenshot_path = screenshotPath;
    await browser.close();
    return attempt;
  }
}

const result = {
  status: 'fail',
  app_url: APP_URL,
  artists: ARTISTS,
  max_attempts: MAX_ATTEMPTS,
  attempts: [],
  checks: {},
};

for (let attemptNumber = 1; attemptNumber <= MAX_ATTEMPTS; attemptNumber += 1) {
  const attempt = await runAttempt(attemptNumber);
  result.attempts.push(attempt);
  if (attempt.status === 'pass') {
    result.status = 'pass';
    result.checks = attempt.checks;
    break;
  }
}

if (result.status !== 'pass') {
  const lastAttempt = result.attempts[result.attempts.length - 1] || {};
  result.error = lastAttempt.error || 'interactive smoke failed';
  result.checks = lastAttempt.checks || {};
  result.screenshot_path = lastAttempt.screenshot_path;
  result.events = eventLog;
}

console.log(JSON.stringify(result, null, 2));
process.exit(result.status === 'pass' ? 0 : 1);
