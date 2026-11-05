const $ = id => document.getElementById(id);
const won = value => new Intl.NumberFormat('ko-KR', {maximumFractionDigits:0}).format(Number(value)) + '원';
const time = value => value ? new Date(value).toLocaleString('ko-KR', {timeZone:'Asia/Seoul',hour12:false}) : '—';
const strategyNames={ma_cross:'이동평균 교차',dca:'정액 분할매수',rebalance:'목표 비중 리밸런싱',consecutive:'봉 연속 전략'};
function strategyLabel(cfg){const name=cfg.strategy||'ma_cross';const offset=Number(cfg.dca_offset_minutes||0);return name==='consecutive'?`${durationLabel(cfg.consecutive_seconds??60)}봉 연속 전략 · ${cfg.consecutive_count??3}회 · 진입 ${Number(cfg.consecutive_entry_return??.005)*100}% / 익절 ${Number(cfg.consecutive_take_profit??.03)*100}% / 손절 ${Number(cfg.consecutive_stop_loss??.10)*100}%`:name==='dca'?`정액 분할매수 · ${cfg.dca_interval_hours||24}시간`+(offset?` · +${offset}분`:''):name==='rebalance'?`리밸런싱 · 목표 ${Number(cfg.target_exposure||.2)*100}% ±${Number(cfg.rebalance_tolerance||.02)*100}%p`:`이동평균 교차 · MA ${cfg.short_window}/${cfg.long_window}`;}
let busy = false, history = [], marketError = false;
function color(el, value) { el.classList.toggle('positive', Number(value)>0); el.classList.toggle('negative', Number(value)<0); }
function empty(el, message) { const p=document.createElement('p'); p.className='empty'; p.textContent=message; el.replaceChildren(p); }
function renderLogs(id, rows, decision) {
  const el=$(id); if(!rows.length) { empty(el,'아직 기록이 없습니다.'); return; }
  el.replaceChildren(...rows.map(row=>{const p=document.createElement('p'),t=document.createElement('time'),b=document.createElement('strong');t.textContent=time(row.time);b.textContent=decision?({buy:'매수 신호',sell:'매도 신호',hold:'대기'}[row.action]):row.kind;p.append(t,b,document.createTextNode(decision?row.reason:row.message));return p;}));
}
function renderGateLogs(id, logs) {
  const el=$(id); if(!el) return;
  if(!logs||!logs.length){empty(el,'아직 게이트 호출 기록이 없습니다. 매수·매도·hold 참고 때 JSON이 쌓입니다.');return;}
  el.replaceChildren(...logs.map(row=>{const pre=document.createElement('pre');pre.textContent=JSON.stringify(row,null,2);return pre;}));
}
function renderMovingAverage(prefix, ma) {
  const id=name=>$(prefix?`${prefix}-${name}`:name);
  const chip=id('ma-ready'), shortEl=id('ma-short'), longEl=id('ma-long'), posEl=id('ma-position');
  if(!chip||!ma)return;
  const positions={above:'장기 위',below:'장기 아래',equal:'동일'};
  const stored=ma.stored_candles??ma.candle_count??0, required=ma.required_candles||0;
  let mode='unknown', label='확인 중';
  if(ma.ready){mode='running';label='준비됨';}
  else if(ma.gap){mode='stopped';label='캔들 누락';}
  else if(stored>0){mode='stopped';label='캔들 부족';}
  else{mode='unknown';label='데이터 없음';}
  chip.className='run-chip is-'+mode;chip.textContent=label;
  shortEl.textContent=ma.short_ma!=null?won(ma.short_ma):'—';
  longEl.textContent=ma.long_ma!=null?won(ma.long_ma):'—';
  id('ma-short-meta').textContent=`MA ${ma.short_window||'—'} · ${ma.candle_minutes||'—'}분봉`;
  id('ma-long-meta').textContent=`MA ${ma.long_window||'—'} · ${ma.candle_minutes||'—'}분봉`;
  posEl.textContent=ma.ready?(positions[ma.position]||ma.position):'—';
  color(posEl, ma.ready?(ma.position==='above'?1:ma.position==='below'?-1:0):0);
  id('ma-spread').textContent=ma.spread!=null?'차이 '+won(ma.spread):'교차 판단 대기';
  id('ma-count').textContent=`${stored} / ${required}`;
  id('ma-latest').textContent=ma.latest_candle?'최근 확정 '+time(ma.latest_candle):'확정 봉 대기';
  const note=id('ma-note');
  if(note){
    const uses=ma.strategy==='ma_cross'?'이동평균 교차 전략이 이 값을 사용합니다.':'현재 전략은 이동평균을 쓰지 않습니다. 참고용으로만 표시합니다.';
    note.textContent=(ma.reason||'시세 수집 후 이동평균 준비 상태를 표시합니다.')+' '+uses;
  }
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
  const cfg=s.settings, gate=s.decision_gate||{}, pairs=[['전략',strategyLabel(cfg)],['시세 수집 주기',`${cfg.strategy==='consecutive'&&cfg.consecutive_seconds<60?'WebSocket · 실시간':(cfg.strategy==='consecutive'?10:cfg.poll_seconds)+'초'}`],['주문 금액',won(cfg.order_krw)],['최대 BTC 비중',Number(cfg.max_exposure)*100+'%'],['일일 손실 한도',Number(cfg.daily_loss_limit)*100+'%'],['가정 수수료 / 슬리피지',Number(cfg.fee_rate)*100+'% / '+Number(cfg.slippage)*100+'%'],['디시젼 게이트',gate.enabled?(gate.configured?'켜짐':'켜짐 · 키 필요'):'꺼짐']];
  $('settings').replaceChildren(...pairs.map(([key,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);return div;}));
  const fills=$('fills');fills.replaceChildren(...s.fills.map(f=>{const tr=document.createElement('tr');[time(f.time),f.side==='buy'?'매수':'매도',won(f.price),Number(f.quantity).toFixed(8),won(f.fee),won(f.realized_pnl)].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.append(td);});return tr;}));
  if(!s.fills.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=6;td.className='empty';td.textContent='아직 체결이 없습니다. 거래 시작 후 선택한 전략의 매매 조건을 기다립니다.';tr.append(td);fills.append(tr);}
  renderLogs('decisions',s.decisions,true);renderLogs('events',s.events,false);
  renderGateLogs('decision-gate-logs',(s.decision_gate||{}).logs);
  renderMovingAverage('',s.moving_average);renderConsecutive('paper',s);history=s.history;chart();
  if(s.last_error){marketError=true;$('notice').textContent=s.last_error+' 정상 시세 수신 후 거래 시작을 눌러 재개하세요.';$('notice').classList.add('error');}
  else if(marketError){marketError=false;$('notice').textContent='시세 수신이 복구되었습니다. 거래 시작을 눌러 재개할 수 있습니다.';$('notice').classList.remove('error');}
  window.currentTradeStatus=s;document.dispatchEvent(new CustomEvent('trade-status',{detail:s}));
}
async function refresh(){if(busy)return;try{const r=await fetch('/api/status');if(!r.ok)throw new Error('상태 조회 실패');render(await r.json());}catch(e){$('status').textContent='서버 연결 끊김';$('notice').textContent=e.message;$('notice').classList.add('error');document.dispatchEvent(new CustomEvent('trade-disconnected'));}}
document.querySelectorAll('button[data-action]').forEach(button=>button.addEventListener('click',async()=>{
  if(busy)return;busy=true;document.querySelectorAll('button[data-action]').forEach(b=>b.disabled=true);
  try{const response=await fetch('/api/control/'+button.dataset.action,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),data=await response.json();if(!response.ok)throw new Error(data.detail||'명령 처리 실패');$('notice').textContent='설정을 적용했습니다. 실제 주문은 발생하지 않습니다.';$('notice').classList.remove('error');render(data);}catch(e){$('notice').textContent=e.message;$('notice').classList.add('error');}finally{busy=false;document.querySelectorAll('button[data-action]').forEach(b=>b.disabled=false);await refresh();}
}));
window.addEventListener('resize',chart);refresh();setInterval(refresh,5000);

function renderConsecutive(prefix,data){
  const enabled=data.settings.strategy==='consecutive',panel=$(prefix+'-consecutive-panel');
  if(!panel)return;panel.hidden=!enabled;const ma=$(prefix==='paper'?'paper-ma-panel':'real-ma-panel');if(ma)ma.hidden=enabled;
  if(!enabled)return;const count=data.settings.consecutive_count??3,period=durationLabel(count*(data.settings.consecutive_seconds??60));const state=data.consecutive?.state||{},pct=v=>v==null?'—':(Number(v)*100).toFixed(3)+'%';
  panel.querySelector('p').textContent=`${count}회 연속 상승 + 최근 ${period} ${pct(data.settings.consecutive_entry_return)} 이상 상승 시 Jev 매수 검토. ${count}회 연속 하락 + 예상 순이익 ${pct(data.settings.consecutive_take_profit)} 이상일 때 Jev 매도 검토. 순손실 ${pct(data.settings.consecutive_stop_loss)}에서는 Jev 없이 손절을 시도합니다. 보유 중 추가 매수하지 않습니다.`;
  const pairs=[['현재 연속 구간',(state.direction==='up'?'상승':state.direction==='down'?'하락':'대기')+' '+(state.count||0)+'회'],[`최근 ${period} 변화율`,pct(state.window_return)],['비용 반영 예상 순손익률',data.consecutive?.net_return_pct==null?'보유 없음':Number(data.consecutive.net_return_pct).toFixed(3)+'%'],['현재 구간 Jev 검토',state.reviewed?'검토 요청 완료':'아직 없음']];
  $(prefix+'-consecutive-state').replaceChildren(...pairs.map(([key,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);return div;}));
  const gate=data.decision_gate||{};$(prefix+'-consecutive-gate').textContent=gate.enabled&&gate.configured?'Jev 승인 후 매수·익절합니다. 손절은 Jev를 호출하지 않습니다.':'Jev 게이트를 켜고 API 키를 저장해야 매수·익절할 수 있습니다. 거래 실행 중 손절 검사는 게이트와 무관하게 유지합니다.';
}

// Keep inactive values for strategy switching and API compatibility; show only applicable controls.
function updateStrategyFields(form) {
  const consecutive=form.elements.strategy.value==='consecutive';
  const inactive=['short_window','long_window','dca_interval_hours','dca_offset_minutes','target_exposure','rebalance_tolerance','candle_minutes'];
  const thresholds=['consecutive_seconds','consecutive_count','consecutive_entry_return','consecutive_take_profit','consecutive_stop_loss'];
  for(const name of inactive){const field=form.elements[name];if(field)field.closest('label').hidden=consecutive;}
  for(const name of thresholds){const field=form.elements[name];if(field)field.closest('label').hidden=!consecutive;}
  if(consecutive&&form.elements.candle_minutes)form.elements.candle_minutes.value='1';
  let note=form.querySelector('[data-strategy-note]');
  if(!note){note=document.createElement('p');note.dataset.strategyNote='';note.className='muted';form.querySelector('.bt-fields').after(note);}
  note.hidden=!consecutive;
  const count=form.elements.consecutive_count.value||3,seconds=Number(form.elements.consecutive_seconds.value||60),period=durationLabel(count*seconds);
  note.textContent=`${durationLabel(seconds)}봉 · ${count}회 연속 · 최근 ${period} 누적 상승률로 매수 검토. `+(form.id==='bt-form'?`${seconds<60?'최근 3개월 중 하루':'최대 7일'}. Jev 호출 없이 룰만 검증합니다.`:'매수·익절은 Jev 승인 후 실행하며 손절은 승인 없이 시도합니다.');
}

for(const id of ['strategy-form','real-settings','bt-form']){
  const form=document.getElementById(id);
  form.addEventListener('input',event=>{if(['consecutive_count','consecutive_seconds'].includes(event.target.name))updateStrategyFields(form);});
}

function durationLabel(seconds){const minutes=Math.floor(seconds/60),rest=seconds%60;return (minutes?minutes+'분':'')+(rest?rest+'초':'')||'0초';}
