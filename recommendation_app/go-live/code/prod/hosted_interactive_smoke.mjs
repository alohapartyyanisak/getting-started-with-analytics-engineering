import { chromium } from 'playwright';

const APP_URL = String(process.env.APP_URL || '').trim();
if (!APP_URL) {
  console.error('APP_URL is required');
  process.exit(2);
}

const ARTISTS = String(process.env.SMOKE_ARTISTS || 'j-hope|TWICE').split('|').map((s) => s.trim()).filter(Boolean);

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
const context = await browser.newContext();
const page = await context.newPage();

const result = {
  status: 'fail',
  app_url: APP_URL,
  artists: ARTISTS,
  checks: {},
};

try {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 45000 });
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
  const screenshotPath = 'recommendation_app/go-live/code/prod/.artifacts/hosted_interactive_smoke_failure.png';
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
  result.screenshot_path = screenshotPath;
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
  process.exit(1);
}
