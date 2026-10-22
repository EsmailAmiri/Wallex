// Optional integration QA: PLAYWRIGHT_MODULE may point to an installed Playwright package.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async()=>{
  fs.mkdirSync('artifacts',{recursive:true});
  const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1500,height:1100}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const post=async(path,payload)=>{
    const response=await page.request.post('http://127.0.0.1:8766/api/'+path,{data:payload});
    assert.equal(response.status(),200,await response.text());return response.json();
  };
  try{
    await page.goto('http://127.0.0.1:8766');
    await page.waitForFunction(()=>document.querySelector('#market-price').textContent.length>0);
    await post('control',{command:'pause'});
    await post('control',{command:'settle'});
    await post('config',{source:'demo',model:'logistic',strategy:'adaptive'});
    await post('control',{command:'reset'});
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#market-price').textContent.length>0);
    await page.screenshot({path:'artifacts/dashboard.png',fullPage:true});
    await page.getByRole('button',{name:'شروع ایجنت',exact:true}).click();
    await page.waitForFunction(()=>Number(document.querySelector('#decision-count').textContent.replace('۰','0').replace('۱','1'))>0,{timeout:12000});
    assert.ok(await page.locator('#recent-decisions .action-pill').count()>0);
    await page.getByRole('button',{name:'توقف ایجنت',exact:true}).click();
    await page.locator('#model').selectOption('ensemble');
    await page.locator('#strategy').selectOption('momentum');
    await page.getByRole('button',{name:'اعمال تنظیمات',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#toast').textContent==='مدل و الگوریتم به‌روزرسانی شدند.');
    const configured=await (await page.request.get('http://127.0.0.1:8766/api/state')).json();
    assert.equal(configured.config.model,'ensemble');assert.equal(configured.config.strategy,'momentum');
    await page.locator('[data-page="backtest"]').click();
    await page.getByRole('button',{name:'اجرای بک‌تست',exact:true}).click();
    await page.locator('#backtest-output').waitFor({state:'visible',timeout:60000});
    assert.ok(await page.locator('#backtest-chart polyline').count());
    await page.screenshot({path:'artifacts/backtest.png',fullPage:true});
    await page.locator('[data-page="settings"]').click();
    await page.locator('#allocation').fill('15');
    await page.getByRole('button',{name:'ذخیره تنظیمات',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#toast').textContent==='تنظیمات ذخیره شد.');
    const configured2=await (await page.request.get('http://127.0.0.1:8766/api/state')).json();assert.equal(configured2.config.allocation,15);
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-page="dashboard"]').click();
    await page.screenshot({path:'artifacts/mobile.png',fullPage:true});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);assert.equal(overflow,false,'Mobile viewport overflows');
    const exportResponse=await page.request.get('http://127.0.0.1:8766/api/export');assert.equal(exportResponse.status(),200);
    const report=await exportResponse.json();assert.ok(report.events.length>0);assert.ok(report.code_sha256['engine.py']);
    assert.deepEqual(errors,[],'Browser errors');
    console.log(JSON.stringify({browser:'passed',actions:['start','pause','switch-model','switch-strategy','backtest','save-risk','mobile','export'],consoleErrors:errors}));
  }finally{
    await post('control',{command:'pause'});
    await post('control',{command:'settle'});
    await post('config',{model:'logistic',strategy:'adaptive',allocation:20});
    await post('control',{command:'reset'});
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
