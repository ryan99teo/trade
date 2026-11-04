const $ = id => document.getElementById(id);
const won = value => new Intl.NumberFormat('ko-KR', {maximumFractionDigits:0}).format(Number(value)) + '원';
const time = value => value ? new Date(value).toLocaleString('ko-KR', {timeZone:'Asia/Seoul',hour12:false}) : '—';
let busy = false, history = [], marketError = false;
function color(el, value) { el.classList.toggle('positive', Number(value)>0); el.classList.toggle('negative', Number(value)<0); }
function empty(el, message) { const p=document.createElement('p'); p.className='empty'; p.textContent=message; el.replaceChildren(p); }
function renderLogs(id, rows, decision) {
  const el=$(id); if(!rows.length) { empty(el,'아직 기록이 없습니다.'); return; }
  el.replaceChildren(...rows.map(row=>{const p=document.createElement('p'),t=document.createElement('time'),b=document.createElement('strong');t.textContent=time(row.time);b.textContent=decision?({buy:'매수 신호',sell:'매도 신호',hold:'대기'}[row.action]):row.kind;p.append(t,b,document.createTextNode(decision?row.reason:row.message));return p;}));
}
function chart() {
  const c=$('chart'), ratio=window.devicePixelRatio||1, width=c.clientWidth, height=170;
  c.width=width*ratio;c.height=height*ratio;const ctx=c.getContext('2d');ctx.scale(ratio,ratio);
  if(!history.length){ctx.fillStyle='#8a99af';ctx.font='12px sans-serif';ctx.fillText('시세 수집 후 자산 흐름이 표시됩니다.',12,80);return;}
  const vals=history.map(r=>Number(r.value)), lo=Math.min(...vals), hi=Math.max(...vals), margin=Math.max((hi-lo)*.15,1);
  ctx.strokeStyle='#243247';ctx.lineWidth=1;for(let y=20;y<height;y+=40){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();}
  ctx.beginPath();vals.forEach((v,i)=>{const x=10+i/Math.max(vals.length-1,1)*(width-20),y=height-15-(v-lo+margin)/(hi-lo+margin*2)*(height-30);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.strokeStyle='#e9b454';ctx.lineWidth=2;ctx.stroke();
  ctx.fillStyle='#93a1b9';ctx.font='11px sans-serif';ctx.fillText('최저 '+won(lo)+'  /  최고 '+won(hi),10,12);
}
function render(s) {
  $('status').textContent=s.last_error?'오류 · 거래 정지':s.running?(s.buy_halted?'실행 중 · 신규 매수 중지':'모의매매 실행 중'):'거래 정지';
  $('indicator').classList.toggle('on',s.running&&s.quote_fresh);
  $('updated').textContent='마지막 수신 '+time(s.last_received)+(s.quote_fresh?' · 최신 시세':' · 시세 확인 필요');
  $('equity').textContent=won(s.equity);$('pnl').textContent=won(s.total_pnl);color($('pnl'),s.total_pnl);
  $('pnl-detail').textContent='실현 '+won(s.realized_pnl)+' / 평가 '+won(s.unrealized_pnl);
  $('cash').textContent=won(s.cash);$('btc').textContent=Number(s.btc).toFixed(8)+' BTC';$('price').textContent=Number(s.price)>0?won(s.price):'—';$('daily').textContent='당일 손익 '+won(s.daily_pnl);
  const toggle=$('buy-toggle');toggle.dataset.action=s.buy_halted?'resume_buys':'pause_buys';toggle.textContent=s.buy_halted?'신규 매수 재개':'신규 매수 중지';
  document.querySelector('[data-action="start"]').disabled=busy||s.running||!s.quote_fresh||!!s.last_error;
  const cfg=s.settings, pairs=[['전략',`${cfg.candle_minutes}분봉 · MA ${cfg.short_window}/${cfg.long_window}`],['시세 수집 주기',`${cfg.poll_seconds}초`],['주문 금액',won(cfg.order_krw)],['최대 BTC 비중',Number(cfg.max_exposure)*100+'%'],['일일 손실 한도',Number(cfg.daily_loss_limit)*100+'%'],['가정 수수료 / 슬리피지',Number(cfg.fee_rate)*100+'% / '+Number(cfg.slippage)*100+'%']];
  $('settings').replaceChildren(...pairs.map(([key,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);return div;}));
  const fills=$('fills');fills.replaceChildren(...s.fills.map(f=>{const tr=document.createElement('tr');[time(f.time),f.side==='buy'?'매수':'매도',won(f.price),Number(f.quantity).toFixed(8),won(f.fee),won(f.realized_pnl)].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.append(td);});return tr;}));
  if(!s.fills.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=6;td.className='empty';td.textContent='아직 체결이 없습니다. 거래 시작 후 새로운 교차 신호를 기다립니다.';tr.append(td);fills.append(tr);}
  renderLogs('decisions',s.decisions,true);renderLogs('events',s.events,false);history=s.history;chart();
  if(s.last_error){marketError=true;$('notice').textContent=s.last_error+' 정상 시세 수신 후 거래 시작을 눌러 재개하세요.';$('notice').classList.add('error');}
  else if(marketError){marketError=false;$('notice').textContent='시세 수신이 복구되었습니다. 거래 시작을 눌러 재개할 수 있습니다.';$('notice').classList.remove('error');}
}
async function refresh(){if(busy)return;try{const r=await fetch('/api/status');if(!r.ok)throw new Error('상태 조회 실패');render(await r.json());}catch(e){$('status').textContent='서버 연결 끊김';$('notice').textContent=e.message;$('notice').classList.add('error');}}
document.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',async()=>{
  if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{const response=await fetch('/api/control/'+button.dataset.action,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),data=await response.json();if(!response.ok)throw new Error(data.detail||'명령 처리 실패');$('notice').textContent='설정을 적용했습니다. 실제 주문은 발생하지 않습니다.';$('notice').classList.remove('error');render(data);}catch(e){$('notice').textContent=e.message;$('notice').classList.add('error');}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);await refresh();}
}));
window.addEventListener('resize',chart);refresh();setInterval(refresh,5000);
