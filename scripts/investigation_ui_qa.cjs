// Use the isolated real-model smoke data served on port 8767; do not fake model decisions here.
const {chromium} = require('../.data/browser-qa/node_modules/playwright');
const path = require('path');
const fs = require('fs');

(async () => {
  const url = process.env.REPAIRLAB_TEST_URL || 'http://127.0.0.1:8767';
  const browser = await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#fixture option').first().waitFor({state:'attached'});
    await page.selectOption('#task-kind','inspect');
    if (await page.inputValue('#issue') !== '') throw new Error('Inspection should not receive a prefilled known defect');
    if (await page.inputValue('#mode') !== 'model') throw new Error('Inspection must use real model mode');
    if (!await page.locator('#strategy').isDisabled()) throw new Error('Inspection must require feedback strategy');
    await page.selectOption('#backend','local');
    await page.check('#allow-local');
    await page.click('#start');
    if (!(await page.locator('#form-error').innerText()).includes('尚未配置真实模型')) throw new Error('Missing model guard');
    const tasks = await (await page.request.get(url + '/api/tasks')).json();
    const task = tasks.find(t => t.task_kind === 'inspect' && t.status === 'passed');
    if (!task) throw new Error('Run smoke_investigation.py successfully before browser QA');
    await page.locator(`.task-item[data-id="${task.id}"]`).click();
    await page.waitForFunction(() => document.querySelector('#status').textContent === '验收通过');
    if (!(await page.locator('#candidates').innerText()).includes('已复现断言不匹配')) throw new Error('Candidate evidence missing');
    if (!(await page.locator('#workflow').innerText()).includes('通过，尚未复现缺陷')) throw new Error('Original passing tests and probe must remain distinct');
    const link = page.locator('#candidates a').first();
    const artifact = await page.request.get(new URL(await link.getAttribute('href'), url).href);
    if (artifact.status() !== 200 || !(await artifact.text()).includes('assert')) throw new Error('Reproducer download failed');
    const artifacts = path.join(__dirname,'..','docs','images');
    fs.mkdirSync(artifacts,{recursive:true});
    await page.screenshot({path:path.join(artifacts,'investigation-desktop.png'),fullPage:true});
    await page.click('[data-tab="verification"]');
    if (!(await page.locator('#verification').innerText()).includes('生成测试，非独立验收依据')) throw new Error('Generated probe attribution missing');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(artifacts,'investigation-mobile.png'),fullPage:true});
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (overflow || errors.length) throw new Error(JSON.stringify({overflow,errors}));
    console.log(JSON.stringify({passed:true,taskId:task.id,emptyIssue:true,candidateEvidence:true,reproducerDownload:true,finalProbe:true,mobileOverflow:overflow,consoleErrors:errors}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error.message);process.exit(1);});
