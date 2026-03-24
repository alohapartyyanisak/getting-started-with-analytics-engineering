import { chromium } from 'playwright';

const APP_URL = String(process.env.APP_URL || '').trim();
if (!APP_URL) {
  console.error('APP_URL is required');
  process.exit(2);
}

const PARALLEL_USERS = Math.max(1, Number.parseInt(process.env.CAPACITY_PARALLEL_USERS || '5', 10) || 5);
const WAVES = Math.max(1, Number.parseInt(process.env.CAPACITY_WAVES || '3', 10) || 3);
const STAGGER_MS = Math.max(0, Number.parseInt(process.env.CAPACITY_STAGGER_MS || '750', 10) || 750);
const SUCCESS_THRESHOLD = Math.min(1, Math.max(0, Number.parseFloat(process.env.CAPACITY_SUCCESS_THRESHOLD || '0.9') || 0.9));
const CAPACITY_PROFILE = String(process.env.CAPACITY_PROFILE || 'full').trim().toLowerCase() || 'full';
const SERIES_ID = String(process.env.CAPACITY_SERIES_ID || '').trim() || null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function percentile(values, p) {
  if (!values.length) {
    return null;
  }
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[index];
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

async function waitForStartupHealthReady(page, timeoutMs = 60000) {
  const hiddenHealth = page.locator('[data-testid="startup-health-status"]').first();
  const visibleHealth = page.getByText('Startup Health: Healthy', { exact: false }).first();

  const hiddenReady = hiddenHealth
    .waitFor({ state: 'attached', timeout: timeoutMs })
    .then(async () => {
      const text = ((await hiddenHealth.textContent()) || '').trim();
      if (text !== 'Healthy') {
        throw new Error(`Hidden startup health was ${text || 'empty'}`);
      }
      return true;
    })
    .catch(() => false);

  const visibleReady = visibleHealth
    .waitFor({ state: 'visible', timeout: timeoutMs })
    .then(() => true)
    .catch(() => false);

  const results = await Promise.all([hiddenReady, visibleReady]);
  if (!results.some(Boolean)) {
    throw new Error(`Startup health did not become ready within ${timeoutMs}ms`);
  }
}

async function waitForCapacityBasePage(page) {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await waitForStartupHealthReady(page, 60000);
  await page.getByText('Choose your move', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });

  const generateButton = page.getByRole('button', { name: 'Generate Playlist' }).first();
  const lighterShell = await generateButton
    .waitFor({ state: 'visible', timeout: 3000 })
    .then(() => true)
    .catch(() => false);

  if (lighterShell) {
    if (CAPACITY_PROFILE === 'shell-only') {
      return { app_kind: 'lighter', generated: false };
    }

    await generateButton.click({ force: true });
    await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    await page.getByText('Build Temporary YouTube Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    await page.getByText('Now Playing', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
    return { app_kind: 'lighter', generated: true };
  }

  const lighterAuto = await page.waitForFunction(
    () => {
      const bodyText = document.body?.innerText || '';
      return (
        bodyText.includes('Playable Playlist') &&
        bodyText.includes('Now Playing')
      );
    },
    null,
    { timeout: 3000 },
  ).then(() => true).catch(() => false);

  if (lighterAuto) {
    return { app_kind: 'lighter', generated: true };
  }

  await page.getByText('Playable Playlist', { exact: false }).waitFor({ state: 'visible', timeout: 60000 });
  return { app_kind: 'developer', generated: false };
}

async function attachNetworkGuards(page, failureBucket) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    if (shouldBlockRequest(url)) {
      failureBucket.push(`${request.method()} ${url} :: blocked_for_capacity`);
      await route.abort();
      return;
    }
    await route.continue();
  });

  page.on('requestfailed', (request) => {
    failureBucket.push(`${request.method()} ${request.url()} :: ${request.failure()?.errorText || 'failed'}`);
  });
}

async function prewarmApp(browser) {
  const requestFailures = [];
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1600 },
    serviceWorkers: 'block',
  });
  const page = await context.newPage();
  await attachNetworkGuards(page, requestFailures);

  try {
    await waitForCapacityBasePage(page);
    await sleep(3000);
  } catch (error) {
    console.warn(`prewarm_failed: ${String(error?.message || error)}`);
  } finally {
    await context.close().catch(() => {});
  }
}

async function runSession(browser, sessionId) {
  const startedAt = Date.now();
  const requestFailures = [];
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1600 },
    serviceWorkers: 'block',
  });
  const page = await context.newPage();
  await attachNetworkGuards(page, requestFailures);

  try {
    const pageKind = await waitForCapacityBasePage(page);
    if (CAPACITY_PROFILE !== 'shell-only' && pageKind.app_kind !== 'lighter') {
      await page.waitForFunction(
        () => Array.from(document.querySelectorAll('label[data-baseweb="radio"]')).length >= 5,
        null,
        { timeout: 60000 },
      );
    }

    const domDebug = await page.evaluate(() => ({
      bodyLength: (document.body?.innerText || '').trim().length,
      radioCount: document.querySelectorAll('label[data-baseweb="radio"]').length,
      title: document.title || '',
    }));
    await context.close();
    return {
      session_id: sessionId,
      status: 'pass',
      elapsed_ms: Date.now() - startedAt,
      dom_debug: domDebug,
      request_failures: requestFailures.slice(0, 20),
    };
  } catch (error) {
    const screenshotPath = `recommendation_app/go-live/code/prod/.artifacts/hosted_capacity_failure_session_${sessionId}.png`;
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
    await context.close().catch(() => {});
    return {
      session_id: sessionId,
      status: 'fail',
      elapsed_ms: Date.now() - startedAt,
      error: String(error?.message || error),
      screenshot_path: screenshotPath,
      request_failures: requestFailures.slice(0, 20),
    };
  }
}

async function runWave(browser, waveNumber) {
  const sessionPromises = [];
  for (let idx = 0; idx < PARALLEL_USERS; idx += 1) {
    const sessionId = `wave${waveNumber}-user${idx + 1}`;
    sessionPromises.push(
      (async () => {
        if (idx > 0 && STAGGER_MS > 0) {
          await sleep(idx * STAGGER_MS);
        }
        return runSession(browser, sessionId);
      })(),
    );
  }
  const sessions = await Promise.all(sessionPromises);
  const passed = sessions.filter((session) => session.status === 'pass').length;
  return {
    wave: waveNumber,
    total_sessions: sessions.length,
    passed_sessions: passed,
    failed_sessions: sessions.length - passed,
    sessions,
  };
}

const browser = await chromium.launch({
  headless: true,
  args: ['--disable-dev-shm-usage'],
});

await prewarmApp(browser);
await sleep(2000);

const waveResults = [];
for (let wave = 1; wave <= WAVES; wave += 1) {
  waveResults.push(await runWave(browser, wave));
  if (wave < WAVES) {
    await sleep(1500);
  }
}
await browser.close();

const sessions = waveResults.flatMap((wave) => wave.sessions);
const passed = sessions.filter((session) => session.status === 'pass').length;
const total = sessions.length;
const successRate = total > 0 ? passed / total : 0;
const elapsedValues = sessions.map((session) => session.elapsed_ms).filter((value) => Number.isFinite(value));

const result = {
  status: successRate >= SUCCESS_THRESHOLD ? 'pass' : 'fail',
  app_url: APP_URL,
  capacity_series_id: SERIES_ID,
  config: {
    parallel_users: PARALLEL_USERS,
    waves: WAVES,
    stagger_ms: STAGGER_MS,
    success_threshold: SUCCESS_THRESHOLD,
    capacity_profile: CAPACITY_PROFILE,
  },
  summary: {
    total_sessions: total,
    passed_sessions: passed,
    failed_sessions: total - passed,
    success_rate: Number(successRate.toFixed(4)),
    p50_elapsed_ms: percentile(elapsedValues, 50),
    p95_elapsed_ms: percentile(elapsedValues, 95),
    max_elapsed_ms: elapsedValues.length ? Math.max(...elapsedValues) : null,
  },
  waves: waveResults,
};

console.log(JSON.stringify(result, null, 2));
process.exit(result.status === 'pass' ? 0 : 1);
