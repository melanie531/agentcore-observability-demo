// Browser E2E for obsdemo UX (headless chromium via playwright)
const path = '/opt/homebrew/lib/node_modules/n8n/node_modules/playwright';
let pw;
try { pw = require('playwright'); } catch { pw = require(path); }
(async () => {
  const browser = await pw.chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined });
  const page = await browser.newPage();
  const out = { steps: [] };
  const step = (n, ok, d) => { out.steps.push({ n, ok, d }); console.log(`${ok ? 'PASS' : 'FAIL'} ${n}: ${d}`); };
  try {
    await page.goto('http://127.0.0.1:8931/', { waitUntil: 'load', timeout: 15000 });
    step('load', (await page.title()).includes('Travel Preference Assistant'), await page.title());
    // send a chat message as Traveler A
    await page.fill('#prompt', 'Quick hello: which city is driest?');
    await page.click('#send');
    await page.waitForSelector('.msg.agent .bubble', { timeout: 60000 });
    const reply = await page.textContent('.msg.agent .bubble');
    step('chat-reply', reply.trim().length > 20, reply.trim().slice(0, 90));
    // telemetry panel
    const tBtn = await page.$('.meta-toggle');
    if (tBtn) { await tBtn.click(); }
    const telem = await page.textContent('.telemetry').catch(() => '');
    step('telemetry-panel', /trace_id|session/i.test(telem || ''), (telem || 'none').slice(0, 120).replace(/\s+/g, ' '));
    // New Session button exists and works
    const ns = await page.$('#new-session');
    if (ns) { await ns.click(); step('new-session', true, 'clicked'); } else { step('new-session', false, 'button not found'); }
    // actor switch + scenario select exist
    step('actor-switch', !!(await page.$('#actor')), 'select#actor present');
    step('scenario-switch', !!(await page.$('#scenario')), 'select#scenario present');
    // no AWS creds in served content
    const html = await page.content();
    step('no-creds-in-dom', !/AKIA|ASIA[0-9A-Z]{16}|aws_secret/i.test(html), 'scanned DOM');
  } catch (e) {
    step('exception', false, String(e).slice(0, 200));
  }
  await browser.close();
  const fails = out.steps.filter(s => !s.ok).length;
  console.log(`SUMMARY: ${out.steps.length - fails}/${out.steps.length} pass`);
  process.exit(fails ? 1 : 0);
})();
