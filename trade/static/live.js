(() => {
  const get=id=>document.getElementById(id),form=get('real-settings');
  get('real-fields').append(document.querySelector('#strategy-fields .bt-fields').cloneNode(true));
  let current=null,busy=false,dirty=false,gateDirty=false,exchangeDirty=false;
  const gateForm=get('real-decision-gate-form');
  const exchangeForm=get('real-exchange-form');
  const states={prepared:'접수 준비',submitting:'접수 확인 중',unknown:'접수 불명 · 새 주문 중지',wait:'체결 대기',watch:'예약 대기',cancel_pending:'취소 확인 중',done:'체결 완료',cancel:'취소 완료',aborted:'미전송 폐기',rejected:'주문 거부'};
  const active=new Set(['prepared','submitting','unknown','wait','watch','cancel_pending']);
  async function api(path,body){const response=await fetch('/api/live/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw new Error(Array.isArray(data.detail)?data.detail.map(d=>d.msg).join(' / '):data.detail||'실거래 요청을 처리하지 못했습니다.');return data;}
  function locked(){return !current||busy||current.running||current.orders.some(o=>active.has(o.state));}
  function controls(){
    const ready=current&&!busy;
    get('real-connect').disabled=!ready||!current.configured;get('real-test').disabled=!ready||!current.configured||current.running;
    get('real-start').disabled=!ready||!current.enabled||!current.configured||!current.connected||!!current.error||!current.quote_fresh||!current.tested_at||Date.now()-Date.parse(current.tested_at)>300000||current.running||!get('real-ack').checked||current.orders.some(o=>active.has(o.state));
    get('real-stop').disabled=!current;get('real-buy-toggle').disabled=!ready;
    get('real-fields').disabled=locked();
    get('real-save').disabled=get('real-fields').disabled||!dirty;
    get('real-decision-gate-fields').disabled=locked();
    get('real-decision-gate-save').disabled=get('real-decision-gate-fields').disabled||!gateDirty;
    get('real-decision-gate-reset').disabled=busy||!gateDirty;
    get('real-exchange-fields').disabled=locked();
    get('real-exchange-save').disabled=get('real-exchange-fields').disabled||!exchangeDirty;
    get('real-exchange-reset').disabled=busy||!exchangeDirty;
    const gate=current?.decision_gate||{};
    get('real-decision-gate-state').textContent=!current?'상태 확인 중':locked()?'수정 잠김':(gate.enabled?(gate.configured?'사용 중':'사용 · 키 필요'):'꺼짐 · 수정 가능');
    get('real-exchange-state').textContent=!current?'상태 확인 중':locked()?'수정 잠김':(current.configured?(current.enabled?'키 설정됨 · 실제 주문 활성':'키 설정됨 · 실제 주문 비활성'):'키 미설정');
  }
  function fillGate(data){
    const cfg=data.settings;
    gateForm.elements.decision_gate_enabled.value=String(!!cfg.decision_gate_enabled);
    gateForm.elements.decision_threshold_buy.value=Number(cfg.decision_threshold_buy)*100;
    gateForm.elements.decision_threshold_sell.value=Number(cfg.decision_threshold_sell)*100;
    gateForm.elements.decision_timeout_seconds.value=cfg.decision_timeout_seconds;
    gateForm.elements.openrouter_api_key.value='';
    gateForm.elements.openrouter_api_key.placeholder=cfg.openrouter_api_key_set?'저장됨 · 비우면 유지':'비우면 기존 키 유지';
  }
  function fillExchange(data){
    exchangeForm.elements.enabled.value=String(!!data.enabled);
    exchangeForm.elements.access_key.value='';
    exchangeForm.elements.secret_key.value='';
    const keep=data.keys_set?'저장됨 · 비우면 유지':'비우면 기존 키 유지';
    exchangeForm.elements.access_key.placeholder=keep;
    exchangeForm.elements.secret_key.placeholder=keep;
  }
  function runStatus(data){
    if(!data)return {mode:'unknown',label:'상태 확인 중',detail:'서버 상태를 확인하고 있습니다.',chip:'상태 확인 중',tab:'실거래'};
    if(data.running){
      const detail=data.buy_halted?'신호 판단과 주문은 계속하되 신규 매수만 중지된 상태입니다.':'전략 신호에 따라 실제 주문을 접수할 수 있는 상태입니다.';
      return {mode:'running',label:'동작 중',detail,chip:'동작 중',tab:'실거래 · 동작 중'};
    }
    if(!data.enabled)return {mode:'disabled',label:'동작 중지',detail:'실제 주문 활성화가 꺼져 있습니다. 조회·주문 테스트만 가능합니다.',chip:'동작 중지',tab:'실거래 · 동작 중지'};
    return {mode:'stopped',label:'동작 중지',detail:'실거래 엔진이 정지되어 새 주문을 넣지 않습니다.',chip:'동작 중지',tab:'실거래 · 동작 중지'};
  }
  function paintRun(data){
    const run=runStatus(data);
    const banner=get('real-run-banner'),chip=get('real-state'),dot=get('real-run-dot'),tab=get('real-tab');
    banner.className='run-banner is-'+run.mode;
    chip.className='run-chip is-'+run.mode;
    get('real-run-label').textContent=run.label;
    get('real-run-detail').textContent=run.detail;
    chip.textContent=run.chip;
    dot.classList.toggle('on',run.mode==='running');
    if(tab)tab.textContent=run.tab;
    return run;
  }
  function show(data){
    current=data;
    paintRun(data);
    if(!data)return;
    get('real-guide').textContent=!data.configured?'위에서 업비트 Access Key · Secret Key를 저장한 뒤 계좌 연결을 확인하세요.':data.error||(!data.enabled?'조회·주문 테스트만 가능합니다. 실제 주문을 쓰려면 업비트 연동에서 실제 주문 활성화를 켜세요.':'계좌 연결 확인 → 주문 테스트 → 실제 주문 동의 → 실거래 시작 순서로 실행하세요.');
    get('real-budget').textContent=won(data.budget);get('real-cap').textContent='1회 매수 상한 '+won(data.order_cap);
    get('real-cash').textContent=won(data.cash);get('real-btc').textContent=Number(data.btc).toFixed(8);get('real-pnl').textContent=won(data.total_pnl);color(get('real-pnl'),data.total_pnl);
    get('real-available').textContent=data.available_krw===null?'계좌 조회 전':'업비트 사용 가능 원화 '+won(data.available_krw);
    get('real-buy-toggle').textContent=data.buy_halted?'신규 매수 재개':'신규 매수 중지';
    if(!dirty){for(const name of ['strategy','dca_interval_hours','dca_offset_minutes','short_window','long_window','order_krw'])form.elements[name].value=data.settings[name];for(const name of ['max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])form.elements[name].value=Number(data.settings[name])*100;}
    if(!gateDirty)fillGate(data);
    if(!exchangeDirty)fillExchange(data);
    form.elements.order_krw.max=data.order_cap;
    get('real-orders').replaceChildren(...data.orders.map(order=>{
      const tr=document.createElement('tr');for(const value of [time(order.created_at),order.side==='buy'?'매수':'매도',states[order.state]||order.state,Number(order.quantity).toFixed(8),won(order.funds),won(order.fee)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
      const td=document.createElement('td');if(active.has(order.state)){const button=document.createElement('button');button.textContent='취소 요청';button.disabled=busy;button.addEventListener('click',()=>perform('orders/'+encodeURIComponent(order.identifier)+'/cancel',{}));td.append(button);}if(order.last_error){const p=document.createElement('small');p.textContent=order.last_error;td.append(p);}tr.append(td);return tr;
    }));
    if(!data.orders.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.className='empty';td.textContent='실제 주문 내역이 없습니다.';tr.append(td);get('real-orders').append(tr);}
    if(typeof renderGateLogs==='function')renderGateLogs('real-decision-gate-logs',(data.decision_gate||{}).logs);
    if(typeof renderMovingAverage==='function')renderMovingAverage('real',data.moving_average);
    controls();
  }
  async function refresh(){if(busy)return;try{show(await api('status'));}catch(error){get('real-message').textContent=error.message;current=null;show(null);controls();}}
  async function perform(path,body){
    if(busy)return;busy=true;controls();get('real-message').textContent='처리 중입니다.';
    try{const data=await api(path,body);if(data.mode==='live')show(data);get('real-message').textContent=data.message||'실거래 상태를 갱신했습니다.';}
    catch(error){get('real-message').textContent=error.message;}finally{busy=false;await refresh();}
  }
  get('real-connect').addEventListener('click',()=>perform('connect',{}));get('real-test').addEventListener('click',()=>perform('test',{}));
  get('real-start').addEventListener('click',()=>perform('start',{acknowledge_real_orders:get('real-ack').checked}));
  get('real-stop').addEventListener('click',async()=>{try{show(await api('control/stop',{}));get('real-ack').checked=false;controls();get('real-message').textContent='새 실거래 주문을 정지했습니다. 기존 주문의 최종 상태도 확인하세요.';}catch(error){get('real-message').textContent=error.message;}});
  get('real-buy-toggle').addEventListener('click',()=>perform('control/'+(current?.buy_halted?'resume_buys':'pause_buys'),{}));
  get('real-ack').addEventListener('change',controls);form.addEventListener('input',()=>{dirty=true;controls();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;const payload=Object.fromEntries(new FormData(form));for(const name of ['short_window','long_window','dca_interval_hours','dca_offset_minutes'])payload[name]=Number(payload[name]);for(const name of ['max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])payload[name]=(Number(payload[name])/100).toFixed(8);
    busy=true;controls();try{const data=await api('settings',payload);dirty=false;show(data);get('real-settings-message').textContent='실거래 설정을 저장했습니다. 주문 테스트를 다시 실행하세요.';}catch(error){get('real-settings-message').textContent=error.message;}finally{busy=false;await refresh();}
  });
  gateForm.addEventListener('input',()=>{gateDirty=true;controls();get('real-decision-gate-message').textContent='저장하지 않은 변경 사항이 있습니다.';});
  get('real-decision-gate-reset').addEventListener('click',()=>{if(!current)return;gateDirty=false;fillGate(current);controls();get('real-decision-gate-message').textContent='현재 저장된 설정으로 되돌렸습니다.';});
  gateForm.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||locked())return;
    const payload=Object.fromEntries(new FormData(gateForm));
    payload.decision_gate_enabled=payload.decision_gate_enabled==='true';
    payload.decision_threshold_buy=(Number(payload.decision_threshold_buy)/100).toFixed(8);
    payload.decision_threshold_sell=(Number(payload.decision_threshold_sell)/100).toFixed(8);
    payload.decision_timeout_seconds=Number(payload.decision_timeout_seconds);
    if(!payload.openrouter_api_key)delete payload.openrouter_api_key;
    busy=true;controls();get('real-decision-gate-message').textContent='게이트 설정을 저장하고 있습니다.';
    try{const data=await api('decision-gate',payload);gateDirty=false;show(data);get('real-decision-gate-message').textContent='실거래 게이트 설정을 저장했습니다.';}
    catch(error){get('real-decision-gate-message').textContent=error.message;}finally{busy=false;await refresh();}
  });
  exchangeForm.addEventListener('input',()=>{exchangeDirty=true;controls();get('real-exchange-message').textContent='저장하지 않은 변경 사항이 있습니다.';});
  get('real-exchange-reset').addEventListener('click',()=>{if(!current)return;exchangeDirty=false;fillExchange(current);controls();get('real-exchange-message').textContent='현재 저장된 설정으로 되돌렸습니다.';});
  exchangeForm.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||locked())return;
    const payload=Object.fromEntries(new FormData(exchangeForm));
    payload.enabled=payload.enabled==='true';
    if(!payload.access_key)delete payload.access_key;
    if(!payload.secret_key)delete payload.secret_key;
    busy=true;controls();get('real-exchange-message').textContent='연동 설정을 저장하고 있습니다.';
    try{const data=await api('exchange',payload);exchangeDirty=false;show(data);get('real-exchange-message').textContent='업비트 연동 설정을 저장했습니다. 계좌 연결과 주문 테스트를 다시 실행하세요.';}
    catch(error){get('real-exchange-message').textContent=error.message;}finally{busy=false;await refresh();}
  });
  refresh();setInterval(refresh,5000);
})();
