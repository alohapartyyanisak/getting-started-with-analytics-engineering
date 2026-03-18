import { chromium } from 'playwright';

const APP_URL = String(process.env.APP_URL || '').trim();
if (!APP_URL) {
  console.error('APP_URL is required');
  process.exit(2);
}

const ARTISTS = String(process.env.SMOKE_ARTISTS || 'j-hope|TWICE').split('|').map((s) => s.trim()).filter(Boolean);
const eventLog = {
  console: [],
  page_errors: [],
  request_failures: [],
};

async function waitForAppReady(page) {
  await page.getByText('Startup Health: Healthy', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  try {
    await page.getByText('Self Mix', { exact: true }).waitFor({ state: 'visible', timeout: 45000 });
    return;
  } catch (_firstError) {
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.getByText('Startup Health: Healthy', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    await page.getByText('Self Mix', { exact: true }).waitFor({ state: 'visible', timeout: 45000 });
  }
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

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 2000 } });
const page = await context.newPage();
page.on('console', (msg) => {
  if (eventLog.console.length < 20) {
    eventLog.console.push(`${msg.type()}: ${msg.text()}`);
  }
});
page.on('pageerror', (error) => {
  if (eventLog.page_errors.length < 20) {
    eventLog.page_errors.push(String(error?.message || error));
  }
});
page.on('requestfailed', (request) => {
  if (eventLog.request_failures.length < 20) {
    eventLog.request_failures.push(`${request.method()} ${request.url()} :: ${request.failure()?.errorText || 'failed'}`);
  }
});

const result = {
  status: 'fail',
  app_url: APP_URL,
  artists: ARTISTS,
  checks: {},
};

try {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await waitForAppReady(page);
  result.checks.page_loaded = true;

  await page.getByText('Self Mix', { exact: true }).click();
  await page.getByText('Pick Artists', { exact: true }).click();
  await page.getByText('Artist Picks', { exact: false }).waitFor({ state: 'visible', timeout: 15000 });
  result.checks.artist_mode_ready = true;

  for (const artist of ARTISTS) {
    await chooseArtist(page, artist);
  }
  result.checks.artist_selection_persists = true;

  await page.getByText('Final Picks (2/5)', { exact: false }).waitFor({ state: 'visible', timeout: 15000 });
  result.checks.final_pick_box_updated = true;

  await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 30000 });
  result.checks.playable_playlist_visible = true;

  const playlistLink = page.getByRole('link', { name: 'Open Temporary YouTube Playlist' });
  await playlistLink.waitFor({ state: 'visible', timeout: 30000 });
  result.checks.temp_playlist_link_visible = true;

  result.status = 'pass';
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
  process.exit(0);
} catch (error) {
  result.error = String(error?.message || error);
  result.events = eventLog;
  const screenshotPath = 'recommendation_app/go-live/code/prod/.artifacts/hosted_interactive_smoke_failure.png';
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
  result.screenshot_path = screenshotPath;
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
  process.exit(1);
}
