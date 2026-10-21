// Optional UI verification: npm install --no-save --prefix .data/browser-qa playwright
const { chromium } = require('../.data/browser-qa/node_modules/playwright');
const path = require('path');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.REPAIRLAB_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true
  });
  const page = await browser.newPage({viewport: {width: 1440, height: 1050}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(process.env.REPAIRLAB_TEST_URL || 'http://127.0.0.1:8766');
  await page.locator('#fixture option').first().waitFor({state: 'attached'});
  await page.selectOption('#fixture', 'csv-tags');
  await page.selectOption('#backend', 'local');
  await page.click('#start');
  if (!(await page.locator('#form-error').innerText()).includes('请先确认')) throw new Error('Local opt-in guard failed');
  await page.check('#allow-local');
  await page.click('#start');
  await page.waitForFunction(() => document.querySelector('#status').textContent === '验收通过', {timeout: 60000});
  await page.getByText('任务结束', {exact: true}).waitFor({timeout: 10000});
  const eventCount = await page.locator('.event').count();
  const failureCount = await page.locator('.event.error').count();
  if (failureCount < 2) throw new Error('Failed test feedback is missing');
  if (!(await page.locator('#workflow').innerText()).includes('修复假设')) throw new Error('Workflow evidence is missing');
  await page.click('[data-tab="patch"]');
  if (!(await page.locator('#patch').innerText()).includes('if tag.strip()')) throw new Error('Patch was not rendered');
  const artifacts = path.join(__dirname, '..', 'docs', 'images');
  fs.mkdirSync(artifacts, {recursive: true});
  await page.screenshot({path: path.join(artifacts, 'workbench-desktop.png'), fullPage: true});
  await page.click('[data-tab="verification"]');
  if (await page.locator('.verification-case').count() !== 2) throw new Error('Verification blocks missing');
  await page.click('.task-item.selected');
  await page.waitForFunction(count => document.querySelectorAll('.event').length === count, eventCount, {timeout: 10000});
  if (await page.locator('.event').count() !== eventCount) throw new Error('Event replay changed record count');
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(artifacts, 'workbench-mobile.png'), fullPage: true});
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  if (overflow) throw new Error('Mobile page overflows horizontally');
  if (errors.length) throw new Error(errors.join('\n'));
  console.log(JSON.stringify({passed: true, eventCount, failureCount, taskId: await page.locator('.task-item.selected').getAttribute('data-id'), mobileOverflow: overflow, consoleErrors: errors}));
  await browser.close();
})().catch(error => {console.error(error.message); process.exit(1);});
