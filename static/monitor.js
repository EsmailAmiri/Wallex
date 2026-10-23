'use strict';
(() => {
  const byId=id=>document.getElementById(id);
  const number=new Intl.NumberFormat('fa-IR');
  let deadline=null,received=0;
  async function status(){
    try{
      const response=await fetch('/api/product',{cache:'no-store'});
      if(!response.ok)throw Error();
      const data=await response.json();
      byId('ops-health').textContent='متصل · نسخه '+data.version;
      deadline=data.competition.remaining_seconds;received=performance.now();
      byId('ops-votes').textContent=number.format(data.evidence.llm_valid_votes||0)+' رأی';
      byId('ops-dataset').textContent=number.format(data.evidence.dataset_bars||0)+' کندل';
      byId('ops-evidence').textContent=data.evidence.description||'هنوز شواهد پژوهشی بارگذاری نشده است.';
    }catch{byId('ops-health').textContent='ارتباط قطع است';}
    setTimeout(status,30000);
  }
  setInterval(()=>{
    if(deadline!==null){const left=Math.max(0,Math.floor(deadline-(performance.now()-received)/1000));
      byId('ops-countdown').textContent=left?number.format(Math.floor(left/3600))+' ساعت و '+number.format(Math.floor(left%3600/60))+' دقیقه':'مهلت اجرای سایه پایان یافته';}
  },1000);
  byId('run-preflight').addEventListener('click',async event=>{
    const button=event.currentTarget;button.disabled=true;
    byId('preflight-result').textContent='در حال بررسی اتصال خواندنی رسمی…';
    try{
      const response=await fetch('/api/preflight',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      const data=await response.json();if(!response.ok)throw Error();
      byId('preflight-result').textContent=(data.connected?'MCP متصل؛ ':'MCP تأیید نشد؛ ')+
        (data.balance_read?'خواندن حساب موفق؛ ':'خواندن حساب تأیید نشد؛ ')+
        'خرید و فروش فرضی: '+(data.paper_roundtrip.status==='passed'?'موفق':'ناموفق')+'؛ سفارش واقعی ارسال نشده است.';
    }catch{byId('preflight-result').textContent='آزمون کامل نشد؛ شبکه و دسترسی خواندن را بررسی کنید. سفارش واقعی ارسال نشده است.';}
    finally{button.disabled=false;}
  });
  status();
})();
