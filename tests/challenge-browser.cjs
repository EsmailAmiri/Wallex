const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 try{
  await page.goto('http://127.0.0.1:8766/#settings');
  await page.waitForFunction(()=>document.querySelector('#challenge-status').textContent.includes('طرح فعال'));
  assert.equal(await page.locator('#capital').getAttribute('readonly'),'');
  assert.equal(await page.locator('#capital').inputValue(),'5000000');
  await page.locator('.blocked-details summary').click();
  await page.waitForFunction(()=>document.querySelectorAll('#blocked-list span').length===345);
  await page.locator('#blocked-search').fill('1BBABYDOGE');
  assert.equal(await page.locator('#blocked-list span').count(),2);
  await page.locator('#blocked-search').fill('');
  const before=await(await page.request.get('http://127.0.0.1:8766/api/challenge/rules')).json();
  const response=await page.request.post('http://127.0.0.1:8766/api/config',{data:{capital:6000000}});
  assert.equal(response.status(),400);
  const after=await(await page.request.get('http://127.0.0.1:8766/api/challenge/rules')).json();
  assert.equal(before.expires_at,after.expires_at);
  fs.mkdirSync('artifacts',{recursive:true});
  await page.screenshot({path:'artifacts/challenge-rules.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:'artifacts/challenge-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({challengeUI:'passed',blockedMarkets:345,principalLocked:true,overrideRejected:true,deadlinePreserved:true,mobileOverflow:false,errors}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
