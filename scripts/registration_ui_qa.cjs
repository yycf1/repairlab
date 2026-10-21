// Run against an isolated service: REPAIRLAB_TEST_URL=http://127.0.0.1:8767
const { chromium } = require('../.data/browser-qa/node_modules/playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const source = path.join(root, '.data', 'discovery-input');
  fs.mkdirSync(path.join(source, 'tests'), {recursive: true});
  fs.writeFileSync(path.join(source, 'parser.py'), 'def parse_tags(text):\n    return text.split(",")\n');
  fs.writeFileSync(path.join(source, 'tests', 'test_parser.py'), 'from parser import parse_tags\ndef test_tags():\n    assert parse_tags(" a, ,b ") == ["a", "b"]\n');
  fs.writeFileSync(path.join(source, 'pyproject.toml'), '[project]\nname="browser-selected-project"\n');
  const noTests = path.join(root, '.data', 'discovery-no-tests');
  fs.mkdirSync(noTests, {recursive:true}); fs.writeFileSync(path.join(noTests, 'module.py'), 'value = 1\n');
  const browser = await chromium.launch({executablePath: process.env.REPAIRLAB_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1050}});
    const errors = [];
    let selection = 0;
    page.on('pageerror', error => errors.push(error.message));
    // Native dialog selection/cancellation is separately verified by picker_smoke.py.
    // Here simulate its returned selection, while calling the real discovery API.
    await page.route('**/api/repositories/pick-directory', async route => {
      selection += 1;
      if (selection === 1) return route.fulfill({json:{cancelled:true}});
      const response = await page.request.post((process.env.REPAIRLAB_TEST_URL || 'http://127.0.0.1:8767') + '/api/repositories/discover',
        {data:{source:selection === 2 ? source : noTests, authorize_directory:true}});
      await route.fulfill({json:{cancelled:false,...await response.json()}});
    });
    await page.goto(process.env.REPAIRLAB_TEST_URL || 'http://127.0.0.1:8767');
    await page.locator('#fixture option').first().waitFor({state:'attached'});
    await page.click('#register-repository');
    if (await page.locator('#repo-name').isVisible() || !(await page.locator('#save-repository').isDisabled())) throw new Error('Initial form requires technical configuration');
    await page.click('#choose-repository-directory');
    await page.waitForFunction(() => !document.querySelector('#repository-fields').disabled);
    if (await page.locator('#repo-source').inputValue() !== '') throw new Error('Cancel changed selected directory');
    const inspected = page.waitForResponse(r => r.url().endsWith('/api/repositories/preflight'));
    await page.click('#choose-repository-directory');
    if (!(await (await inspected).json()).ready) throw new Error('Valid repository preflight failed');
    await page.waitForFunction(() => !document.querySelector('#repository-fields').disabled);
    if (await page.locator('#repo-name').inputValue() !== 'browser-selected-project') throw new Error('Name not inferred');
    if (await page.locator('#repo-editable').inputValue() !== 'parser.py') throw new Error('Source scope not inferred');
    if (await page.locator('#repo-tests').inputValue() !== 'tests') throw new Error('Tests not inferred');
    if (await page.locator('#repo-name').isVisible()) throw new Error('Advanced settings expanded unnecessarily');
    const images = path.join(root, 'docs', 'images'); fs.mkdirSync(images, {recursive:true});
    await page.screenshot({path:path.join(images, 'repository-registration-desktop.png'), fullPage:true});
    const savedResponse = page.waitForResponse(r => r.url().endsWith('/api/repositories') && r.request().method() === 'POST');
    await page.click('#save-repository');
    const saved = await (await savedResponse).json();
    await page.waitForFunction(id => document.querySelector('#fixture').value === id, saved.id);
    if (await page.locator('#mode').inputValue() !== 'model' || !(await page.locator('#mode option[value="demo"]').isDisabled())) throw new Error('Custom repository allowed scripted demo');
    await page.click('#collect-repository');
    if (!(await page.locator('#form-error').innerText()).includes('请先确认')) throw new Error('Test execution consent missing');
    await page.check('#allow-local');
    const collected = page.waitForResponse(r => r.url().endsWith(`/${saved.id}/preflight`));
    await page.click('#collect-repository');
    const report = await (await collected).json();
    if (!report.ready || report.collection.exit_code !== 0) throw new Error('Test collection failed');
    await page.waitForFunction(() => !document.querySelector('#start').disabled);
    if (fs.readFileSync(path.join(source,'parser.py'),'utf8') !== 'def parse_tags(text):\n    return text.split(",")\n') throw new Error('Source changed during collection');
    await page.click('#start');
    if (!(await page.locator('#form-error').innerText()).includes('尚未配置')) throw new Error('Missing model was not explained');
    await page.click('#edit-repository');
    await page.locator('#repository-dialog').waitFor({state:'visible'});
    await page.locator('#repository-advanced > summary').click();
    await page.fill('#repo-name', '已更新的本地仓库');
    await page.fill('#repo-required-acceptance', 'tests/test_parser.py::test_tags');
    await page.fill('#repo-required-regression', 'tests/test_parser.py::test_tags');
    const updated = page.waitForResponse(r => r.url().endsWith(`/api/repositories/${saved.id}`) && r.request().method() === 'PUT');
    await page.click('#save-repository'); await updated;
    await page.waitForFunction(() => document.querySelector('#fixture option:checked').textContent === '已更新的本地仓库');
    await page.reload();
    await page.locator(`#fixture option[value="${saved.id}"]`).waitFor({state:'attached'});
    await page.selectOption('#fixture', saved.id);
    await page.setViewportSize({width:390,height:844});
    await page.click('#edit-repository');
    await page.locator('#repository-dialog').waitFor({state:'visible'});
    if (await page.locator('#repo-required-acceptance').inputValue() !== 'tests/test_parser.py::test_tags' ||
        await page.locator('#repo-required-regression').inputValue() !== 'tests/test_parser.py::test_tags') throw new Error('Required test lists were not preserved');
    await page.screenshot({path:path.join(images,'repository-registration-mobile.png'),fullPage:true});
    const overflow = await page.evaluate(() => {
      const dialog = document.querySelector('dialog').getBoundingClientRect();
      return document.documentElement.scrollWidth > innerWidth + 1 || dialog.right > innerWidth || dialog.left < 0;
    });
    if (overflow || errors.length) throw new Error(JSON.stringify({overflow,errors}));
    await page.click('#close-repository'); await page.click('#register-repository');
    await page.click('#choose-repository-directory');
    await page.waitForFunction(() => !document.querySelector('#repository-fields').disabled);
    if (!(await page.locator('#save-repository').isDisabled()) || !(await page.locator('#repository-error').innerText()).includes('测试')) throw new Error('Missing tests did not request configuration');
    console.log(JSON.stringify({passed:true, repositoryId:saved.id, automaticName:true, automaticSource:true, automaticTests:true, selectionCancel:true, missingTests:true, staticPreflight:true, collection:true, persisted:true, updated:true, sourceUnchanged:true, mobileOverflow:overflow, consoleErrors:errors}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
