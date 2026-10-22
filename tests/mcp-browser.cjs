const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 try{
  await page.goto('http://127.0.0.1:8765/#settings');
  await page.waitForURL('http://127.0.0.1:8766/#settings');
  await page.waitForFunction(()=>document.querySelector('#mcp-state-label').textContent==='MCP رسمی متصل است');
  assert.equal(await page.locator('#mcp-market').textContent(),'داده واقعی دریافت شد');
  const done=page.waitForResponse(response=>response.url().endsWith('/api/mcp/connect')&&response.request().method()==='POST');
  await page.locator('#connect-mcp').click();
  const response=await done;assert.equal(response.status(),200);
  const result=await response.json();assert.equal(result.connected,true);assert.equal(result.provider,'wallex-official');assert.equal(result.server_name,'api.wallex.ir');assert.equal(result.market_verified,true);
  if(result.api_key_configured)assert.equal(result.account_verified,true);
  await page.waitForFunction(()=>!document.querySelector('#connect-mcp').disabled);
  assert.equal(await page.locator('#source').inputValue(),'wallex_mcp');
  fs.mkdirSync('artifacts',{recursive:true});
  await page.screenshot({path:'artifacts/mcp-settings.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  const local=await page.evaluate(async()=>{
    const report=await(await fetch('/api/export')).json();
    return {source:report.state.config.source,connected:report.state.mcp.connected};
  });
  assert.equal(local.source,'wallex_mcp');assert.deepEqual(errors,[]);
  console.log(JSON.stringify({mcpUI:'passed',redirect:'passed',publicData:result.market_verified,accountVerified:result.account_verified,source:local.source,consoleErrors:errors}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
