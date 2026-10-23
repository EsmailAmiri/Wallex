'use strict';
const $ = (id) => document.getElementById(id);
const ICONS = {
 dashboard:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
 pulse:'<path d="M2 12h4l3-8 5 16 3-8h5"/>',
 flask:'<path d="M9 3h6M10 3v6L4 19a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2L14 9V3M7 15h10"/><path d="M10 17h.01M14 19h.01"/>',
 settings:'<path d="m10 3-1 3-3 1-3 3 2 3-1 4 4 1 2 3 4-1 3-3 3-1-1-4 1-3-4-2-2-2z"/><circle cx="12" cy="12" r="3"/>',
 download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
 play:'<path d="m8 4 12 8-12 8z"/>', pause:'<path d="M8 4v16M16 4v16"/>',
 wallet:'<path d="M20 8V5H5a2 2 0 0 0 0 4h16v11H5a2 2 0 0 1-2-2V7m18 6h-6v4h6m-3-2h.01"/>',
 trend:'<path d="m3 17 6-6 4 4 8-10m-6 0h6v6"/>', target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
 shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
 chevron:'<path d="m6 9 6 6 6-6"/>', brain:'<path d="M12 4v16M12 5C8 0 4 5 6 8c-5 0-5 7-1 8-1 5 5 7 7 3m0-14c4-5 8 0 6 3 5 0 5 7 1 8 1 5-5 7-7 3M6 8l3 2M5 16l4-2m9-6-3 2m4 6-4-2"/>',
 database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
 sliders:'<path d="M4 7h4m4 0h8M4 17h10m4 0h2"/><circle cx="10" cy="7" r="2"/><circle cx="16" cy="17" r="2"/>',
 check:'<path d="m4 12 5 5L20 6"/>', link:'<path d="m10 13 4-4m-6 5-2 2a4 4 0 0 0 6 6l4-4a4 4 0 0 0 0-6m0-2 2-2a4 4 0 0 0-6-6L8 6a4 4 0 0 0 0 6"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>'
};
const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.pulse}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
const fa = new Intl.NumberFormat('fa-IR', {maximumFractionDigits:0});
const dec = new Intl.NumberFormat('fa-IR', {minimumFractionDigits:2, maximumFractionDigits:2});
const fmt = (n, digits = 0) => digits ? dec.format(n) : fa.format(n);
const fmtPrice = n => new Intl.NumberFormat('fa-IR',{maximumFractionDigits:Number(n)<100?6:0}).format(n);
const esc = (text) => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const time = (t) => new Date(t * 1000).toLocaleTimeString('fa-IR',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Tehran'});
const date = (t) => new Date(t * 1000).toLocaleDateString('fa-IR',{month:'short',day:'numeric',timeZone:'Asia/Tehran'});
const MODELS = {logistic:'Logistic regression',knn:'k-Nearest neighbors',ensemble:'Ensemble',hybrid_llm:'Ensemble + AI decision'};
const STRATEGIES = {adaptive:'تطبیقی',momentum:'دنبال‌کننده روند',reversion:'بازگشت به میانگین',breakout:'شکست سقف',trend_hold:'روند قوی',ta_trend:'EMA + ADX + ATR',ai_regime:'تشخیص رژیم و سد کارمزد'};
const ACTIONS = {BUY:'خرید',SELL:'فروش',HOLD:'انتظار'};
const MARKETS = {BTCTMN:['بیت‌کوین','BTC','₿'],ETHTMN:['اتریوم','ETH','Ξ'],USDTTMN:['تتر','USDT','₮'],PEPETMN:['پپه','PEPE','₱'],SOLTMN:['سولانا','SOL','◎'],XRPTMN:['ریپل','XRP','✕']};
let state, chartMode='price', currentPage='dashboard', lastBacktest=null, toastTimer, pending=false, chartData=[];

function toast(message, error=false){
  $('toast').textContent=message; $('toast').classList.toggle('error',error); $('toast').hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,error?7000:3500);
}
async function api(path,payload){
  const response=await fetch('/api/'+path,{method:payload===undefined?'GET':'POST',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload)});
  const data=await response.json(); if(!response.ok) throw Error(data.error||'ارتباط با سرور برقرار نشد.'); return data;
}
async function action(task){
  if(pending)return; pending=true;
  try{await task();}catch(error){toast(error.message,true);}finally{pending=false;}
}
function syncForms(){
  if(!state)return;
  for(const [key,value] of Object.entries(state.config)) if($(key))$(key).value=value;
}
function setPage(page){
  if(!['dashboard','activity','backtest','settings'].includes(page))page='dashboard';
  currentPage=page;
  document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!=='view-'+page);
  document.querySelectorAll('[data-page]').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  const titles={dashboard:['نمای کلی','نبض بازار، در مدار شما','از اولین سیگنال تا آخرین تصمیم؛ همه‌چیز اینجا قابل دیدن است.'],activity:['فعالیت ایجنت','پشت هر تصمیم، یک منطق','دلیل سیگنال‌ها، اطمینان مدل و نتیجه اجرا را بررسی کنید.'],backtest:['آزمایش استراتژی','اول آزمایش، بعد تصمیم','عملکرد موتور را روی داده‌های گذشته، قابل تکرار ارزیابی کنید.'],settings:['تنظیمات و اتصال','قواعد مدار را شما تعیین کنید','منبع داده، سرمایه اولیه و محدوده اختیار ایجنت را تنظیم کنید.']};
  $('breadcrumb').textContent=titles[page][0]; $('page-title').textContent=titles[page][1]; $('page-description').textContent=titles[page][2];
  if(state){renderTables();if(page==='settings')syncForms();if(page==='dashboard')drawMainChart();}
  history.replaceState(null,'','#'+page);
}
document.querySelectorAll('[data-page]').forEach(el=>el.addEventListener('click',()=>setPage(el.dataset.page)));
document.querySelectorAll('[data-goto]').forEach(el=>el.addEventListener('click',()=>{setPage(el.dataset.goto);window.scrollTo({top:0,behavior:'smooth'});}));
window.addEventListener('hashchange',()=>setPage(location.hash.slice(1)));

function render(){
  if($('ops-freshness'))$('ops-freshness').textContent=state.last_update?time(state.last_update):'هنوز دریافت نشده';
  const s=state,c=s.config,m=s.metrics,a=s.account,running=s.running;
  document.body.classList.toggle('running',running);
  $('toggle-agent').innerHTML=icon(running?'pause':'play')+`<span>${running?'توقف ایجنت':'شروع ایجنت'}</span>`;
  $('mode-badge').textContent=c.source==='demo'?'داده نمایشی':c.source==='wallex_mcp'?'تست سایه · MCP':'تست سایه · والکس';
  $('stat-equity').textContent=fmt(m.equity);$('stat-pnl').textContent=fmt(m.realized);
  $('stat-pnl').className=m.realized<0?'negative':m.realized>0?'positive':'';
  $('stat-return').textContent=(m.net_return>0?'+':'')+fmt(m.net_return,2)+'٪';$('stat-return').classList.toggle('negative',m.net_return<0);
  $('stat-win').textContent=s.trades.some(t=>t.pnl!==null)?fmt(m.win_rate,2):'—';
  $('stat-trades').textContent=fmt(m.trades)+' سفارش اجراشده';$('win-progress').style.width=m.win_rate+'%';
  $('stat-dd').textContent=fmt(m.max_drawdown,2);$('dd-limit').textContent='سقف '+fmt(c.max_drawdown)+'٪';
  const recentEquity=s.curve.slice(-12).map(p=>p.equity);
  const lo=Math.min(...recentEquity), hi=Math.max(...recentEquity);
  document.querySelectorAll('.mini-bars b').forEach((el,i)=>el.style.height=recentEquity.length>1?(10+70*(recentEquity[i%recentEquity.length]-lo)/(hi-lo||1))+'%':'4%');
  const spark=recentEquity.length>1?recentEquity.map((value,i)=>`${i/(recentEquity.length-1)*120} ${25-20*(value-lo)/(hi-lo||1)}`).join(' L'):'0 20 L120 20';
  document.querySelector('.spark path').setAttribute('d','M'+spark);
  $('risk-pill').textContent=a.halted?'توقف ریسک فعال':'در محدوده مجاز';$('risk-pill').classList.toggle('negative',a.halted);
  const market=MARKETS[c.symbol];$('market-name').innerHTML=esc(market[0])+' <span>/ تومان</span>';
  $('market-symbol').textContent=market[1]+' / TMN';$('coin').textContent=market[2];$('market-price').textContent=fmtPrice(s.bars.at(-1).c)+' TMN';
  $('side-status').textContent=running?'در حال تحلیل بازار':a.halted?'متوقف با قاعده ریسک':'متوقف · آماده اجرا';
  $('agent-live-label').innerHTML='<span class="status-dot"></span>'+(running?'در حال اجرا':'متوقف');
  $('chart-status').textContent=running?'ایجنت در حال بررسی بازار است':'ایجنت متوقف است';
  $('chart-provenance').textContent=c.source==='demo'?'داده ساختگی • هر ۳ ثانیه یک گام':'والکس • کندل ۱۵ دقیقه • اجرای فرضی';
  $('execution-label').textContent=c.source==='demo'?'معامله کاغذی':'سایه · بدون سفارش واقعی';
  $('error-banner').hidden=!s.error;$('error-banner').textContent=s.error||'';
  $('decision-count').textContent=fmt(s.cycles);$('nav-count').textContent=fmt(s.cycles);
  if($('ai-votes'))$('ai-votes').textContent=fmt(s.ai?.votes||0)+' رأی · '+fmt(s.ai?.approved||0)+' تأیید';
  $('risk-allocation').textContent=fmt(c.allocation)+'٪ سرمایه';$('allocation-track').style.width=c.allocation+'%';
  $('risk-stop').textContent=fmt(c.stop_loss,2)+'٪';$('risk-take').textContent=fmt(c.take_profit,2)+'٪';
  $('pipeline-data').textContent=fmt(s.bars.length)+' کندل بسته‌شده';
  const last=s.decisions.at(-1);
  if(last){
    $('pipeline-regime').textContent=last.regime==='momentum'?'رژیم رونددار':'رژیم بازگشتی';
    $('pipeline-model').textContent=fmt(last.confidence)+'٪ اطمینان';$('training-samples').textContent=fmt(last.samples);
    $('pipeline-risk').textContent=a.halted?'توقف ریسک':a.quantity?'موقعیت باز':'آماده ورود';
    $('latest-action').textContent=ACTIONS[last.action];$('latest-reason').textContent=last.reason;
    $('cycle-label').textContent='چرخه '+fmt(s.cycles)+' · '+time(last.t);
  }else{
    $('pipeline-regime').textContent='روند و نوسان';$('pipeline-model').textContent='آماده آموزش';$('training-samples').textContent='—';
    $('pipeline-risk').textContent='محدودیت سرمایه';$('latest-action').textContent='آماده';$('cycle-label').textContent='در انتظار شروع';
    $('latest-reason').textContent='ایجنت را شروع کنید تا مدل با داده‌های گذشته آموزش ببیند و دلیل هر تصمیم اینجا نمایش داده شود.';
  }
  $('wallex-connection').textContent=c.source!=='demo'?(s.error?'خطای دریافت داده':c.source==='wallex_mcp'?'دریافت از MCP':'دریافت مستقیم REST'):'در حالت نمایشی استفاده نمی‌شود';
  renderMCP(s.mcp);
  renderChallenge(s.challenge);
  $('backtest-summary').innerHTML=`<span>مدل فعال</span><b>${esc(MODELS[c.model])}</b><span>استراتژی</span><b>${esc(STRATEGIES[c.strategy])}</b><span>سرمایه</span><b>${fmt(c.capital)} تومان</b><span>بازار</span><b>${market[1]} / TMN</b>`;
  $('position-detail').innerHTML=a.quantity?`<span>مقدار: <b dir="ltr">${a.quantity.toFixed(8)} ${market[1]}</b></span><span>بهای تمام‌شده: ${fmt(a.cost)} تومان</span><span>سود تحقق‌نیافته: ${fmt(m.unrealized)} تومان</span>`:'موقعیت بازی وجود ندارد؛ سرمایه به تومان نگهداری می‌شود.';
  renderTables();drawMainChart();
}
let blockedMarkets=[];
function renderChallenge(rule){
  if(!rule)return;
  const left=rule.remaining_seconds,days=Math.floor(left/86400),hours=Math.floor(left%86400/3600),minutes=Math.floor(left%3600/60);
  $('challenge-countdown').textContent=rule.status==='expired'?'مهلت طرح پایان یافته':`${fmt(days)} روز و ${fmt(hours)} ساعت و ${fmt(minutes)} دقیقه باقی‌مانده`;
  $('challenge-countdown').classList.toggle('negative',rule.status==='expired');
  $('challenge-status').textContent=rule.status==='active'?'طرح فعال · قواعد اجباری':rule.status==='expired'?'طرح منقضی شده':'پیش از شروع طرح';
  $('blocked-count').textContent='· '+fmt(rule.blocked_market_count)+' بازار';
}
function renderBlocked(){
  const query=$('blocked-search').value.trim().toUpperCase();
  const list=blockedMarkets.filter(symbol=>symbol.includes(query));
  $('blocked-list').replaceChildren(...list.map(symbol=>{const span=document.createElement('span');span.textContent=symbol;return span;}));
}
$('blocked-search').addEventListener('input',renderBlocked);
function renderMCP(status){
  if(!status)return;
  $('mcp-state-label').textContent=status.connected?(status.provider==='wallex-official'?'MCP رسمی متصل است':'MCP متصل است'):'MCP متصل نیست';
  $('mcp-transport').textContent=status.connected?(status.provider==='wallex-official'?'والکس رسمی · HTTP':status.transport==='stdio'?'پل محلی · stdio':'سرور تنظیم‌شده · HTTP'):'متصل نیست';
  $('mcp-transport').classList.toggle('positive',status.connected);
  $('mcp-market').textContent=status.market_verified?'داده واقعی دریافت شد':'بررسی نشده';
  $('mcp-key').textContent=status.api_key_configured?'کلید تنظیم شده':'در .env وارد نشده';
  $('mcp-account').textContent=status.account_message;
  $('mcp-account').classList.toggle('positive',status.account_verified);
  $('mcp-tools').textContent=status.tools.length?(status.provider==='wallex-official'?fmt(status.discovered_tools_count)+' ابزار در سرور رسمی کشف شد؛ فقط ابزارهای مجاز خواندنی فعال‌اند: ':'ابزارها: ')+status.tools.join(' · '):'برای مشاهده ابزارها، اتصال را بررسی کنید.';
}
$('connect-mcp').addEventListener('click',()=>action(async()=>{
  const button=$('connect-mcp');button.disabled=true;button.innerHTML=icon('link')+'در حال اتصال و آزمون…';
  try{
    const status=await api('mcp/connect',{});renderMCP(status);
    if(state)state.mcp=status;
    toast(status.account_verified?'MCP، داده بازار و خواندن موجودی حساب تأیید شدند.':'MCP و داده بازار متصل شدند؛ '+status.account_message);
  }finally{button.disabled=false;button.innerHTML=icon('link')+'اتصال و آزمون MCP';}
}));
$('use-mcp').addEventListener('click',()=>action(async()=>{
  state=await api('config',{source:'wallex_mcp'});render();syncForms();toast('منبع داده ایجنت به MCP تغییر کرد. اجرای سفارش‌ها همچنان آزمایشی است.');
}));
function rows(decisions){
  if(!decisions.length)return '<tr><td colspan="7" class="empty-row">'+icon('pulse')+'هنوز فعالیتی ثبت نشده؛ برای مشاهده تصمیم‌ها، ایجنت را شروع کنید.</td></tr>';
  return decisions.map(d=>`<tr><td><time>${time(d.t)}</time><span class="date">${date(d.t)}</span></td><td dir="ltr">${esc(MARKETS[state.config.symbol][1])}/TMN</td><td><span class="action-pill ${d.action}">${ACTIONS[d.action]}</span></td><td>${fmtPrice(d.trade?d.trade.price:d.price)}</td><td><div class="confidence-cell"><span><b style="width:${Number(d.confidence)}%"></b></span>${fmt(d.confidence)}٪</div></td><td class="reason-cell">${esc(d.reason)}</td><td class="execution ${d.trade?'filled':''}">${d.trade?'اجرای آزمایشی':'بدون سفارش'}</td></tr>`).join('');
}
function renderTables(){
  if(!state)return;
  $('recent-decisions').innerHTML=rows(state.decisions.slice(-5).reverse());
  let decisions=state.decisions.slice().reverse();const filter=$('activity-filter').value;
  if(filter==='trades')decisions=decisions.filter(d=>d.trade);else if(filter!=='all')decisions=decisions.filter(d=>d.action===filter);
  $('all-decisions').innerHTML=rows(decisions);
}
$('activity-filter').addEventListener('change',renderTables);

function compact(n){return new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(n);}
function drawChart(target,data,{candles=false,trades=[],color='#e5b16b',baseline=null}={}){
  if(!data.length){target.innerHTML='<text x="450" y="160" text-anchor="middle">پس از شروع ایجنت، منحنی سرمایه نمایش داده می‌شود.</text>';return;}
  const W=Math.max(300,Math.min(900,target.clientWidth||900)),H=target.clientHeight||330,left=12,right=76,top=17,bottom=34,gw=W-left-right,gh=H-top-bottom;
  target.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const values=candles?data.flatMap(b=>[b.l,b.h]):data.map(b=>b.equity);
  let min=Math.min(...values),max=Math.max(...values);if(baseline!==null){min=Math.min(min,baseline);max=Math.max(max,baseline);}
  const pad=(max-min)*.13||max*.003;min-=pad;max+=pad;
  const y=v=>top+(max-v)/(max-min)*gh;const x=i=>left+(i+.5)/data.length*gw;
  let html='';
  for(let i=0;i<5;i++){
    const yy=top+i*gh/4;const value=max-i*(max-min)/4;
    html+=`<line x1="${left}" y1="${yy}" x2="${W-right}" y2="${yy}" stroke="#2a3042" stroke-dasharray="3 5" opacity=".8"/><text x="${W-right+12}" y="${yy+4}" style="font:10px Consolas,monospace">${compact(value)}</text>`;
  }
  for(let i=0;i<5;i++){
    const index=Math.round(i*(data.length-1)/4);const xx=x(index);
    html+=`<line x1="${xx}" y1="${top}" x2="${xx}" y2="${H-bottom}" stroke="#262c3d" stroke-dasharray="3 5"/><text x="${xx}" y="${H-10}" text-anchor="middle">${time(data[index].t)}</text>`;
  }
  if(baseline!==null)html+=`<line x1="${left}" y1="${y(baseline)}" x2="${W-right}" y2="${y(baseline)}" stroke="#747c95" stroke-dasharray="5 5"/>`;
  if(candles){
    const width=Math.max(1,gw/data.length*.58);
    for(let i=0;i<data.length;i++){
      const b=data[i],up=b.c>=b.o,c=up?'#51c5aa':'#e9879b';
      html+=`<line x1="${x(i)}" y1="${y(b.h)}" x2="${x(i)}" y2="${y(b.l)}" stroke="${c}" stroke-width="1"/><rect x="${x(i)-width/2}" y="${Math.min(y(b.o),y(b.c))}" width="${width}" height="${Math.max(1.4,Math.abs(y(b.o)-y(b.c)))}" fill="${c}" rx=".7"/>`;
    }
    for(const trade of trades){
      const i=data.findIndex(b=>b.t===trade.t);if(i<0)continue;
      const buy=trade.action==='BUY', yy=buy?y(data[i].l)+16:y(data[i].h)-16,c=buy?'#54d9b4':'#f18f9e';
      html+=`<path d="M${x(i)-5} ${yy+(buy?5:-5)} L${x(i)} ${yy+(buy?-4:4)} L${x(i)+5} ${yy+(buy?5:-5)} Z" fill="${c}" stroke="#191d2b" stroke-width="1"/>`;
    }
  }else{
    const points=data.map((b,i)=>`${x(i)},${y(b.equity)}`).join(' '),id=target.id+'-fill';
    html+=`<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".20"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><polygon points="${x(0)},${H-bottom} ${points} ${x(data.length-1)},${H-bottom}" fill="url(#${id})"/><polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>`;
  }
  const last=candles?data.at(-1).c:data.at(-1).equity, yy=y(last);
  html+=`<line x1="${left}" y1="${yy}" x2="${W-right}" y2="${yy}" stroke="${color}" opacity=".6" stroke-dasharray="4 4"/><rect x="${W-right+2}" y="${yy-10}" width="76" height="20" fill="${color}" rx="4"/><text x="${W-right+40}" y="${yy+4}" text-anchor="middle" style="font:10px Consolas,monospace;fill:#171426">${compact(last)}</text>`;
  target.innerHTML=html;
}
function drawMainChart(){
  if(!state)return;
  chartData=(chartMode==='price'?state.bars:state.curve).slice(window.innerWidth<600?-48:-85);
  drawChart($('price-chart'),chartData,{candles:chartMode==='price',trades:state.trades,baseline:chartMode==='equity'?state.config.capital:null});
  document.querySelector('.legend').style.visibility=chartMode==='price'?'visible':'hidden';
}
document.querySelectorAll('[data-chart]').forEach(el=>el.addEventListener('click',()=>{
  chartMode=el.dataset.chart;document.querySelectorAll('[data-chart]').forEach(b=>b.classList.toggle('active',b===el));drawMainChart();
}));
$('main-chart').addEventListener('pointermove',e=>{
  if(!chartData.length)return;
  const rect=$('price-chart').getBoundingClientRect(),ratio=(e.clientX-rect.left)/rect.width;
  const width=$('price-chart').viewBox.baseVal.width;
  const index=Math.max(0,Math.min(chartData.length-1,Math.floor((ratio*width-12)/(width-88)*chartData.length)));
  const b=chartData[index],tip=$('chart-tooltip');
  tip.textContent=date(b.t)+' · '+time(b.t)+' | '+(chartMode==='price'?fmtPrice(b.c):fmt(b.equity))+' تومان';
  tip.hidden=false;tip.style.left=Math.min(Math.max(8,e.clientX-rect.left+10),Math.max(8,rect.width-250))+'px';
});
$('main-chart').addEventListener('pointerleave',()=>$('chart-tooltip').hidden=true);
let resizeTimer;
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{
  drawMainChart();if(lastBacktest)drawChart($('backtest-chart'),lastBacktest.curve,{baseline:lastBacktest.config.capital,color:lastBacktest.metrics.net_return>=0?'#54d9b4':'#f18f9e'});
},100);});

$('toggle-agent').addEventListener('click',()=>action(async()=>{
  if(!state)throw Error('ابتدا اتصال به سرور را بررسی کنید.');
  state=await api('control',{command:state.running?'pause':'start'});render();toast(state.running?'ایجنت شروع شد؛ تصمیم‌ها در نمودار ثبت می‌شوند.':'ایجنت متوقف شد.');
}));
$('quick-config').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
  state=await api('config',{model:$('model').value,strategy:$('strategy').value});render();syncForms();toast('مدل و الگوریتم به‌روزرسانی شدند.');
});});
$('settings-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
  const payload=Object.fromEntries(new FormData(e.currentTarget));
  state=await api('config',payload);render();syncForms();toast('تنظیمات ذخیره شد.');
});});
$('settle').addEventListener('click',()=>action(async()=>{state=await api('control',{command:'settle'});render();toast('ایجنت متوقف و موقعیت آزمایشی به تومان تسویه شد.');}));
$('reset-session').addEventListener('click',()=>{
  if(state?.running){toast('ابتدا ایجنت را متوقف کنید.',true);return;}
  $('confirm-dialog').showModal();
});
$('confirm-dialog').addEventListener('close',()=>{if($('confirm-dialog').returnValue==='confirm')action(async()=>{state=await api('control',{command:'reset'});render();syncForms();toast('جلسه تازه آماده است.');});});
$('export-top').addEventListener('click',()=>{window.location.href='/api/export';});
$('backtest-source').addEventListener('change',()=>$('csv-input-wrap').hidden=$('backtest-source').value!=='csv');
$('run-backtest').addEventListener('click',()=>action(async()=>{
  const button=$('run-backtest'),source=$('backtest-source').value;
  let csv;
  if(source==='csv'){
    const file=$('csv-file').files[0];if(!file)throw Error('یک فایل CSV انتخاب کنید.');
    if(file.size>1500000)throw Error('حجم فایل باید کمتر از ۱٫۵ مگابایت باشد.');csv=await file.text();
  }
  button.disabled=true;button.innerHTML=icon('flask')+'در حال آموزش و ارزیابی…';
  try{
    lastBacktest=await api('backtest',{config:{source:['wallex','wallex_mcp'].includes(source)?source:'demo'},csv});
    $('backtest-empty').hidden=true;$('backtest-output').hidden=false;
    const m=lastBacktest.metrics;
    $('backtest-stats').innerHTML=`<div class="bt-stat"><small>بازدهی خالص پس از تسویه</small><b class="${m.net_return>=0?'positive':'negative'}">${fmt(m.net_return,2)}٪</b></div><div class="bt-stat"><small>بیشترین افت سرمایه</small><b>${fmt(m.max_drawdown,2)}٪</b></div><div class="bt-stat"><small>معاملات کامل‌شده</small><b>${fmt(m.round_trips)}</b></div>`;
    $('backtest-source-label').textContent={synthetic:'داده ساختگی',wallex:'داده واقعی والکس',wallex_mcp:'والکس از MCP',csv:'فایل CSV شما'}[lastBacktest.source];
    $('backtest-detail').textContent=`${fmt(m.trades)} سفارش · کارمزد ${fmt(m.fees)} تومان · برد ${fmt(m.win_rate)}٪`;
    drawChart($('backtest-chart'),lastBacktest.curve,{baseline:lastBacktest.config.capital,color:m.net_return>=0?'#54d9b4':'#f18f9e'});
    toast('بک‌تست کامل شد و گزارش آن ذخیره شد.');
  }finally{button.disabled=false;button.innerHTML=icon('play')+'اجرای بک‌تست';}
}));
$('download-backtest').addEventListener('click',()=>{
  if(!lastBacktest)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(lastBacktest,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download='madar-backtest.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
async function poll(){
  try{
    const initial=!state;state=await api('state');render();if(initial)syncForms();
    $('toggle-agent').disabled=false;
  }catch(error){$('error-banner').hidden=false;$('error-banner').textContent='ارتباط با سرور قطع است. سرور را با python server.py اجرا کنید.';$('toggle-agent').disabled=true;}
  setTimeout(poll,2500);
}
async function boot(){
  try{
    const runtime=await (await fetch('/runtime.json',{cache:'no-store'})).json();
    if(Number.isInteger(runtime.port)&&runtime.port>=1024&&runtime.port<=65535&&String(runtime.port)!==location.port){
      location.replace('http://127.0.0.1:'+runtime.port+location.hash);return;
    }
  }catch{}
  setPage(location.hash.slice(1)||'dashboard');poll();
  try{const rules=await api('challenge/rules');blockedMarkets=rules.blocked_markets;renderBlocked();}catch{}
}
boot();
