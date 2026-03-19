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
const MAX_ATTEMPTS = Math.max(1, Number.parseInt(process.env.SMOKE_MAX_ATTEMPTS || '1', 10) || 1);
const SMOKE_SERIES_ID = String(process.env.SMOKE_SERIES_ID || '').trim();
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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

async function warmApp(appUrl) {
  const baseUrl = new URL(appUrl);
  const targets = [
    appUrl,
    new URL('/_stcore/health', baseUrl).toString(),
    new URL('/_stcore/host-config', baseUrl).toString(),
  ];

  for (const target of targets) {
    try {
      await fetch(target, {
        method: 'GET',
        redirect: 'follow',
        cache: 'no-store',
      });
    } catch (_error) {
      // Best-effort warmup only.
    }
    await sleep(500);
  }
}

async function waitForBasePage(page) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (attempt === 0) {
      await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } else {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    }

    const ready = await Promise.all([
      page.getByText('Startup Health: Healthy', { exact: false }).waitFor({ state: 'visible', timeout: 20000 }).then(() => true).catch(() => false),
      page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 20000 }).then(() => true).catch(() => false),
      page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 20000 }).then(() => true).catch(() => false),
    ]);
    if (ready.every(Boolean)) {
      return;
    }

    const bodyLength = await page.evaluate(() => (document.body?.innerText || '').trim().length).catch(() => 0);
    appendEvent(eventLog.console, `base_page_retry attempt=${attempt + 1} body_length=${bodyLength}`);
  }

  await page.getByText('Startup Health: Healthy', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  await page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
}

async function waitForInteractiveControls(page) {
  await page.waitForFunction(
    () => {
      const labels = Array.from(document.querySelectorAll('label[data-baseweb="radio"]'));
      if (labels.length < 5) {
        return false;
      }
      const checkedCount = labels.filter((label) => label.querySelector('input[type="radio"]')?.checked).length;
      return checkedCount >= 2;
    },
    null,
    { timeout: 30000 },
  );
}

async function waitForHydratedInteractiveControls(page) {
  try {
    await waitForInteractiveControls(page);
    return false;
  } catch (_error) {
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.getByText('Startup Health: Healthy', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    await page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    await waitForInteractiveControls(page);
    return true;
  }
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
  const inputLocator = page
    .locator('label[data-baseweb="radio"]')
    .filter({ hasText: labelText })
    .locator('input[type="radio"]')
    .first();
  if (await inputLocator.count()) {
    await inputLocator.scrollIntoViewIfNeeded().catch(() => null);
    try {
      await inputLocator.check({ force: true });
      return;
    } catch (_error) {
      // Fall back to clicking the label wrapper.
    }
  }

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

async function waitForQuickArtistChips(page) {
  const expectedNames = ['Ed Sheeran', 'Bruno Mars', 'Imagine Dragons', 'Maroon 5'];
  await page.waitForFunction(
    (names) => {
      const buttonTexts = Array.from(document.querySelectorAll('button'))
        .map((button) => (button.textContent || '').trim())
        .filter(Boolean);
      return names.some((name) => buttonTexts.includes(name));
    },
    expectedNames,
    { timeout: 30000 },
  );
}

async function switchToArtistMode(page) {
  await waitForInteractiveControls(page);

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const alreadyReady = await page.waitForFunction(() => {
      const bodyText = document.body?.innerText || '';
      return bodyText.includes('Choose artists (search enabled).');
    }, null, { timeout: 1200 }).catch(() => null);
    if (alreadyReady) {
      await waitForQuickArtistChips(page);
      return;
    }

    await clickRadioLabel(page, 'Self Mix');
    await waitForLabelSelection(page, 'Self Mix');
    await clickRadioLabel(page, 'Pick Artists');
    await waitForLabelSelection(page, 'Pick Artists');

    const artistModeReady = await page.waitForFunction(() => {
      const bodyText = document.body?.innerText || '';
      return bodyText.includes('Choose artists (search enabled).');
    }, null, { timeout: 3000 }).catch(() => null);
    if (artistModeReady) {
      await waitForQuickArtistChips(page);
      return;
    }
  }

  throw new Error('Artist mode did not stick');
}

async function switchToYouTubePlatform(page) {
  await clickRadioLabel(page, 'YouTube');
  await page.getByText('Playback', { exact: false }).waitFor({ state: 'visible', timeout: 30000 });
}

async function waitForFinalPickCount(page, expectedCount) {
  await page.waitForFunction(
    (count) => {
      const bodyText = document.body?.innerText || '';
      return bodyText.includes(`Final Picks (${count}/5)`);
    },
    expectedCount,
    { timeout: 20000 },
  );
}

async function waitForPlaylistGenerationOutcome(page) {
  const tempLink = page.locator('a.platform-link').filter({ hasText: 'Open Temporary YouTube Playlist' }).first();
  const insufficientIds = page.getByText('Temporary YouTube playlist link requires at least 2 playable YouTube IDs.', { exact: false }).first();
  const coverage = page.getByText('Temporary playlist coverage:', { exact: false }).first();

  await page.waitForFunction(
    () => {
      const bodyText = document.body?.innerText || '';
      return (
        bodyText.includes('Open Temporary YouTube Playlist') ||
        bodyText.includes('Temporary YouTube playlist link requires at least 2 playable YouTube IDs.') ||
        bodyText.includes('Temporary playlist coverage:')
      );
    },
    null,
    { timeout: 30000 },
  );

  return {
    tempLinkVisible: await tempLink.isVisible().catch(() => false),
    insufficientIdsVisible: await insufficientIds.isVisible().catch(() => false),
    coverageVisible: await coverage.isVisible().catch(() => false),
  };
}

async function chooseArtist(page, artistName, expectedCount) {
  const quickChip = page.getByRole('button', { name: artistName }).first();
  await quickChip.waitFor({ state: 'visible', timeout: 15000 });
  await quickChip.click({ force: true });
  await waitForFinalPickCount(page, expectedCount);
}

async function runAttempt(attemptNumber) {
  await warmApp(APP_URL);

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

    const reloadedForHydration = await waitForHydratedInteractiveControls(page);
    if (reloadedForHydration) {
      attempt.checks.reloaded_for_hydration = true;
    }

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

    await switchToYouTubePlatform(page);
    attempt.checks.youtube_platform_selected = true;

    await switchToArtistMode(page);
    attempt.checks.artist_mode_reconfirmed = true;

    for (let idx = 0; idx < ARTISTS.length; idx += 1) {
      await chooseArtist(page, ARTISTS[idx], idx + 1);
    }
    attempt.checks.artist_selection_persists = true;

    await waitForFinalPickCount(page, ARTISTS.length);
    attempt.checks.final_pick_box_updated = true;

    await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 30000 });
    attempt.checks.playable_playlist_visible = true;

    const playlistOutcome = await waitForPlaylistGenerationOutcome(page);
    attempt.checks.temp_playlist_link_visible = playlistOutcome.tempLinkVisible;
    attempt.checks.temp_playlist_outcome_visible = Boolean(
      playlistOutcome.tempLinkVisible ||
      playlistOutcome.insufficientIdsVisible ||
      playlistOutcome.coverageVisible,
    );

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
  smoke_series_id: SMOKE_SERIES_ID || null,
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
