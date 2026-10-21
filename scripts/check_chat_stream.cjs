// Optional browser regression check. Requires Node.js, Playwright and Chromium.
// Install Playwright outside the checkout, then expose it via NODE_PATH.
// CHROMIUM_PATH may point to a system Chromium; otherwise Playwright's is used.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
    args: ['--no-sandbox']
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 950 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const answer = 'Bonjour ! Le prix est **10 €** [S1]. Contactez notre support pour plus de précisions.';
    let scenario = 'burst';
    let networkEnded = false;
    const event = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
    await page.route('http://inferpilot.test/**', async route => {
      if (route.request().url().endsWith('/api/chat/stream')) {
        const tokens = answer.match(/.{1,3}/gu).map(text => event('token', { text })).join('');
        const ending = scenario === 'error' ? event('error', {}) : event('done', {});
        const body = event('start', { mode: 'groq' }) + tokens + ending;
        await route.fulfill({ contentType: 'text/event-stream', body });
        networkEnded = true;
      } else {
        await route.fulfill({ contentType: 'text/html', body: fs.readFileSync(
          path.join(__dirname, '../app/static/index.html'), 'utf8') });
      }
    });
    await page.goto('http://inferpilot.test/');
    await page.evaluate(() => {
      window.paintedMessages = [];
      const sample = () => {
        const bubble = document.querySelectorAll('.message.assistant .bubble')[1];
        window.paintedMessages.push(bubble?.textContent || '');
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });
    async function submit(question) {
      networkEnded = false;
      await page.locator('#question').fill(question);
      await page.locator('#question').press('Enter');
    }
    async function waitComplete() {
      await page.waitForFunction(() => document.querySelector('#status').textContent === 'À votre écoute');
    }
    async function reset() {
      await page.getByRole('button', { name: 'Nouvelle discussion' }).click();
    }
    await submit('Politique de retour ?');
    await page.waitForFunction(() => document.querySelector('.streaming .bubble')?.textContent.length > 3);
    assert(networkEnded, 'Test must exercise a whole network response received in one burst.');
    assert.equal(await page.locator('#reset').isDisabled(), true);
    const partial = await page.locator('.message.assistant .bubble').last().innerText();
    assert(partial.length < 50, 'Writing must not jump straight to the full reply.');
    // A draft composed during playback must survive completion.
    await page.locator('#question').fill('Mon prochain message');
    await waitComplete();
    assert.equal(await page.locator('#question').inputValue(), 'Mon prochain message');
    const samples = await page.evaluate(() => window.paintedMessages);
    const intermediate = new Set(samples.filter(text => text.length > 0 && text.length < 60));
    assert(intermediate.size >= 10, 'Multiple intermediate texts must actually be painted.');
    assert(samples.every(text => !text.includes('[S') && !text.includes('**')), 'No partial source or bold markers: ' + JSON.stringify(samples.filter(text => text.includes('[S') || text.includes('**'))));
    assert.equal(await page.locator('.message.assistant strong').count(), 1);
    assert.equal(await page.locator('.streaming').count(), 0);
    const finalText = await page.locator('.message.assistant .bubble').last().innerText();
    assert(finalText.includes('10 €') && finalText.includes('précisions.'));

    // The selector changes the visible pace, not the API or its generation speed.
    const durations = {};
    for (const speed of ['30', '120']) {
      await reset();
      await page.locator('#typing-speed').selectOption(speed);
      const start = Date.now();
      await submit('Comparer les vitesses');
      await waitComplete();
      durations[speed] = Date.now() - start;
    }
    assert(durations['30'] > durations['120'] * 2, 'Slow must be visibly slower than fast.');

    // Error received in a burst must cancel queued writing, even before its first paint.
    await reset();
    scenario = 'error';
    await submit('Demande en erreur');
    await page.waitForFunction(() => document.querySelector('.message.error'));
    const errorText = await page.locator('.message.error .bubble').innerText();
    await page.waitForTimeout(500);
    assert.equal(await page.locator('.message.error .bubble').innerText(), errorText);
    assert.equal(await page.locator('.streaming').count(), 0);
    assert.equal(await page.locator('#question').inputValue(), 'Demande en erreur');
    assert.equal(await page.locator('#send').isDisabled(), false);

    await reset();
    scenario = 'burst';
    await page.setViewportSize({ width: 390, height: 844 });
    await submit('Test mobile');
    await waitComplete();
    assert(await page.locator('#typing-speed').isVisible());
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('PASS: paced burst playback, intermediate painted frames, hidden citations, safe formatting, draft preservation, adjustable speed, cancellation and mobile.', durations);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
