import { chromium } from 'playwright';

const APP_URL = String(process.env.APP_URL || '').trim();
if (!APP_URL) {
  console.error('APP_URL is required');
  process.exit(2);
}

const ARTISTS = String(process.env.SMOKE_ARTISTS || 'j-hope|TWICE')
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
  const groups = page.locator('[role="radiogroup"]');
  await groups.first().waitFor({ state: 'visible', timeout: 30000 });
  await page.waitForFunction(() => document.querySelectorAll('[role="radiogroup"]').length >= 2, null, { timeout: 30000 });
  return groups;
}

async function clickRadioOption(page, groupIndex, optionIndex) {
  const groups = await waitForRadioGroups(page);
  const group = groups.nth(groupIndex);
  const radio = group.locator('[role="radio"]').nth(optionIndex);
  await radio.waitFor({ state: 'visible', timeout: 30000 });
  await radio.scrollIntoViewIfNeeded();
  await radio.click({ force: true });
}

async function switchToArtistMode(page) {
  const artistInput = page.getByPlaceholder('Search and select artists').first();
  if (await artistInput.isVisible().catch(() => false)) {
    return;
  }

  await clickRadioOption(page, 1, 1);
  await artistInput.waitFor({ state: 'visible', timeout: 30000 });
}

async function chooseArtist(page, artistName) {
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
  };

  try {
    await waitForBasePage(page);
    attempt.checks.page_loaded = true;

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
