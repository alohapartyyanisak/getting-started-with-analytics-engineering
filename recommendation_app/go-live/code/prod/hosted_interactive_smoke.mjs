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
const SMOKE_PROFILE = String(process.env.SMOKE_PROFILE || 'developer').trim().toLowerCase();
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

async function waitForStartupHealthReady(page, timeoutMs = 60000) {
  const hiddenHealth = page.locator('[data-testid="startup-health-status"]').first();
  const hiddenHealthReady = hiddenHealth
    .waitFor({ state: 'attached', timeout: timeoutMs })
    .then(async () => {
      const value = await hiddenHealth.textContent();
      return String(value || '').trim() === 'Healthy';
    })
    .catch(() => false);

  const visibleHealthReady = page
    .getByText('Startup Health: Healthy', { exact: false })
    .waitFor({ state: 'visible', timeout: timeoutMs })
    .then(() => true)
    .catch(() => false);

  const results = await Promise.all([hiddenHealthReady, visibleHealthReady]);
  if (results.some(Boolean)) {
    return;
  }
  throw new Error(`Startup health did not become ready within ${timeoutMs}ms`);
}

async function waitForDeveloperShellReady(page, timeoutMs = 60000) {
  await page.waitForFunction(
    () => {
      const bodyText = document.body?.innerText || '';
      const hasMoveSelector = bodyText.includes('Choose your move');
      const hasSeedInstructions =
        bodyText.includes('Choose artists') ||
        bodyText.includes('Choose songs') ||
        bodyText.includes('Choose songs/artists');
      const hasModeControls =
        bodyText.includes('Pick Artists') ||
        bodyText.includes('Pick Songs') ||
        bodyText.includes('Self Mix');
      return hasMoveSelector && (hasSeedInstructions || hasModeControls);
    },
    null,
    { timeout: timeoutMs },
  );
}

async function waitForBasePage(page) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (attempt === 0) {
      await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } else {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    }
    await sleep(4000);

    const readyChecks = [
      waitForStartupHealthReady(page, 35000).then(() => true).catch(() => false),
      page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 35000 }).then(() => true).catch(() => false),
    ];
    if (SMOKE_PROFILE === 'lighter') {
      readyChecks.push(
        page.waitForFunction(
          () => {
            const bodyText = document.body?.innerText || '';
            return bodyText.includes('Generate Playlist') || bodyText.includes('Now Playing');
          },
          null,
          { timeout: 35000 },
        ).then(() => true).catch(() => false),
      );
    } else {
      readyChecks.push(
        waitForDeveloperShellReady(page, 35000).then(() => true).catch(() => false),
      );
    }

    const ready = await Promise.all(readyChecks);
    if (ready.every(Boolean)) {
      return;
    }

    const bodyLength = await page.evaluate(() => (document.body?.innerText || '').trim().length).catch(() => 0);
    appendEvent(eventLog.console, `base_page_retry attempt=${attempt + 1} body_length=${bodyLength}`);
  }

  await waitForStartupHealthReady(page, 60000);
  await page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  if (SMOKE_PROFILE === 'lighter') {
    await page.waitForFunction(
      () => {
        const bodyText = document.body?.innerText || '';
        return bodyText.includes('Generate Playlist') || bodyText.includes('Now Playing');
      },
      null,
      { timeout: 60000 },
    );
  } else {
    await waitForDeveloperShellReady(page, 60000);
  }
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
    await sleep(5000);
    const recoveredWithoutReload = await waitForInteractiveControls(page).then(() => true).catch(() => false);
    if (recoveredWithoutReload) {
      return false;
    }
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(4000);
    await waitForStartupHealthReady(page, 60000);
    await waitForDeveloperShellReady(page, 60000);
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

async function waitForDeveloperResultSurface(page) {
  await page.waitForFunction(
    () => {
      const bodyText = document.body?.innerText || '';
      const hiddenResultReady = (document.querySelector('[data-testid="dev-result-surface-ready"]')?.textContent || '').trim() === 'true';
      const hiddenPlayerReady = (document.querySelector('[data-testid="dev-player-surface-ready"]')?.textContent || '').trim() === 'true';
      const hiddenTempPlaylistReady = (document.querySelector('[data-testid="dev-temp-playlist-ready"]')?.textContent || '').trim() === 'true';
      const hiddenDebugReady = (document.querySelector('[data-testid="dev-debug-table-ready"]')?.textContent || '').trim() === 'true';
      return (
        hiddenResultReady ||
        hiddenPlayerReady ||
        hiddenTempPlaylistReady ||
        hiddenDebugReady ||
        bodyText.includes('Now Playing') ||
        bodyText.includes('Playback') ||
        bodyText.includes('Open Temporary YouTube Playlist') ||
        bodyText.includes('Temporary playlist coverage:')
      );
    },
    null,
    { timeout: 30000 },
  );
}

async function waitForLighterReady(page) {
  await page.waitForFunction(
    () => {
      const bodyText = document.body?.innerText || '';
      return (
        bodyText.includes('DJ Mixing Station Studio') &&
        bodyText.includes('Choose your move') &&
        (bodyText.includes('Generate Playlist') || bodyText.includes('Now Playing'))
      );
    },
    null,
    { timeout: 30000 },
  );
}

async function runDeveloperFlow(page, attempt) {
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

  await waitForDeveloperResultSurface(page);
  attempt.checks.playable_playlist_visible = true;

  const developerResultDebug = await page.evaluate(() => ({
    hiddenStartupHealth: (document.querySelector('[data-testid="startup-health-status"]')?.textContent || '').trim(),
    hiddenFinalPicks: (document.querySelector('[data-testid="dev-final-picks-count"]')?.textContent || '').trim(),
    hiddenResultSurfaceReady: (document.querySelector('[data-testid="dev-result-surface-ready"]')?.textContent || '').trim(),
    hiddenPlayerSurfaceReady: (document.querySelector('[data-testid="dev-player-surface-ready"]')?.textContent || '').trim(),
    hiddenTempPlaylistReady: (document.querySelector('[data-testid="dev-temp-playlist-ready"]')?.textContent || '').trim(),
    hiddenDebugTableReady: (document.querySelector('[data-testid="dev-debug-table-ready"]')?.textContent || '').trim(),
  }));
  attempt.dom_debug = { ...(attempt.dom_debug || {}), ...developerResultDebug };

  const playlistOutcome = await waitForPlaylistGenerationOutcome(page);
  attempt.checks.temp_playlist_link_visible = playlistOutcome.tempLinkVisible;
  attempt.checks.temp_playlist_outcome_visible = Boolean(
    playlistOutcome.tempLinkVisible ||
    playlistOutcome.insufficientIdsVisible ||
    playlistOutcome.coverageVisible,
  );
}

async function runLighterFlow(page, attempt) {
  await waitForLighterReady(page);
  attempt.checks.lighter_ready = true;

  const generateButton = page.getByRole('button', { name: 'Generate Playlist' }).first();
  const hasGenerateButton = await generateButton.waitFor({ state: 'visible', timeout: 2000 }).then(() => true).catch(() => false);
  if (hasGenerateButton) {
    await generateButton.click({ force: true });
    attempt.checks.generate_clicked = true;
  } else {
    attempt.checks.generate_clicked = false;
  }

  await page.waitForFunction(
    () => {
      const bodyText = document.body?.innerText || '';
      return (
        bodyText.includes('Now Playing') &&
        bodyText.includes('Playback')
      );
    },
    null,
    { timeout: 30000 },
  );
  attempt.checks.results_rendered = true;

  const lighterDebug = await page.evaluate(() => {
    const bodyText = document.body?.innerText || '';
    const selectLabels = Array.from(document.querySelectorAll('label')).map((label) => (label.textContent || '').trim()).filter(Boolean);
    const buttonTexts = Array.from(document.querySelectorAll('button')).map((button) => (button.textContent || '').trim()).filter(Boolean);
    return {
      bodyHasHero: bodyText.includes('DJ Mixing Station Studio'),
      bodyHasChooseMove: bodyText.includes('Choose your move'),
      bodyHasPlayablePlaylist: bodyText.includes('Playable Playlist'),
      bodyHasNowPlaying: bodyText.includes('Now Playing'),
      hiddenStartupHealth: (document.querySelector('[data-testid="startup-health-status"]')?.textContent || '').trim(),
      selectLabels: selectLabels.slice(0, 12),
      buttonTexts: buttonTexts.slice(0, 20),
    };
  });
  attempt.dom_debug = lighterDebug;
  attempt.checks.playable_playlist_visible = Boolean(lighterDebug.bodyHasPlayablePlaylist);
  attempt.checks.now_playing_visible = Boolean(lighterDebug.bodyHasNowPlaying);
}

async function chooseArtist(page, artistName, expectedCount) {
  const quickChip = page.getByRole('button', { name: artistName }).first();
  await quickChip.waitFor({ state: 'visible', timeout: 15000 });
  await quickChip.click({ force: true });
  await waitForFinalPickCount(page, expectedCount);
}

async function runAttempt(attemptNumber) {
  await warmApp(APP_URL);

  const attempt = {
    attempt: attemptNumber,
    status: 'fail',
    checks: {},
    dom_debug: {},
  };
  let browser;
  let page;

  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--disable-dev-shm-usage'],
    });
    const context = await browser.newContext({
      viewport: { width: 1600, height: 2000 },
      serviceWorkers: 'block',
    });
    page = await context.newPage();
    await attachNetworkGuards(page);

    await waitForBasePage(page);
    attempt.checks.page_loaded = true;
    if (SMOKE_PROFILE === 'lighter') {
      await runLighterFlow(page, attempt);
    } else {
      await runDeveloperFlow(page, attempt);
    }

    attempt.status = 'pass';
    await browser?.close();
    return attempt;
  } catch (error) {
    attempt.error = String(error?.message || error);
    const screenshotPath = `recommendation_app/go-live/code/prod/.artifacts/hosted_interactive_smoke_failure_attempt_${attemptNumber}.png`;
    if (page) {
      await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
    }
    attempt.screenshot_path = screenshotPath;
    await browser?.close().catch(() => {});
    return attempt;
  }
}

const result = {
  status: 'fail',
  app_url: APP_URL,
  artists: ARTISTS,
  smoke_profile: SMOKE_PROFILE,
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
