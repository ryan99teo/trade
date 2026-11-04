(() => {
  const get=id=>document.getElementById(id),form=get('real-settings');
  get('real-fields').append(document.querySelector('#strategy-fields .bt-fields').cloneNode(true));
  let current=null,busy=false,dirty=false;
  const states={prepared:'접수 준비',submitting:'접수 확인 중',unknown:'접수 불명 · 새 주문 중지',wait:'체결 대기',watch:'예약 대기',cancel_pending:'취소 확인 중',done:'체결 완료',cancel:'취소 완료',aborted:'미전송 폐기',rejected:'주문 거부'};
  const active=new Set(['prepared','submitting','unknown','wait','watch','cancel_pending']);
  async function api(path,body){const response=await fetch('/api/live/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw new Error(Array.isArray(data.detail)?data.detail.map(d=>d.msg).join(' / '):data.detail||'실거래 요청을 처리하지 못했습니다.');return data;}
  function controls(){
    const ready=current&&!busy;
    get('real-connect').disabled=!ready||!current.configured;get('real-test').disabled=!ready||!current.configured||current.running;
    get('real-start').disabled=!ready||!current.enabled||!current.configured||!current.connected||!!current.error||!current.quote_fresh||!current.tested_at||Date.now()-Date.parse(current.tested_at)>300000||current.running||!get('real-ack').checked||current.orders.some(o=>active.has(o.state));
    get('real-stop').disabled=!current;get('real-buy-toggle').disabled=!ready;
    get('real-fields').disabled=!ready||current.running||current.orders.some(o=>active.has(o.state));
    get('real-save').disabled=get('real-fields').disabled||!dirty;
  }
  function show(data){
    current=data;get('real-state').textContent=data.running?'실거래 실행 중':data.enabled?'실거래 정지':'실거래 비활성화';
    get('real-guide').textContent=!data.configured?'서버 환경변수 UPBIT_ACCESS_KEY · UPBIT_SECRET_KEY를 설정하세요. 실제 주문에는 TRADE_LIVE_ENABLED=true가 추가로 필요합니다.':data.error||(!data.enabled?'조회·주문 테스트만 가능합니다. 실제 주문은 서버 활성화 설정 후 시작할 수 있습니다.':'계좌 연결 확인 → 주문 테스트 → 실제 주문 동의 → 실거래 시작 순서로 실행하세요.');
    get('real-budget').textContent=won(data.budget);get('real-cap').textContent='1회 매수 상한 '+won(data.order_cap);
    get('real-cash').textContent=won(data.cash);get('real-btc').textContent=Number(data.btc).toFixed(8);get('real-pnl').textContent=won(data.total_pnl);color(get('real-pnl'),data.total_pnl);
    get('real-available').textContent=data.available_krw===null?'계좌 조회 전':'업비트 사용 가능 원화 '+won(data.available_krw);
    get('real-buy-toggle').textContent=data.buy_halted?'신규 매수 재개':'신규 매수 중지';
    if(!dirty){for(const name of ['strategy','dca_interval_hours','short_window','long_window','order_krw'])form.elements[name].value=data.settings[name];for(const name of ['max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])form.elements[name].value=Number(data.settings[name])*100;}
    form.elements.order_krw.max=data.order_cap;
    get('real-orders').replaceChildren(...data.orders.map(order=>{
      const tr=document.createElement('tr');for(const value of [time(order.created_at),order.side==='buy'?'매수':'매도',states[order.state]||order.state,Number(order.quantity).toFixed(8),won(order.funds),won(order.fee)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
      const td=document.createElement('td');if(active.has(order.state)){const button=document.createElement('button');button.textContent='취소 요청';button.disabled=busy;button.addEventListener('click',()=>perform('orders/'+encodeURIComponent(order.identifier)+'/cancel',{}));td.append(button);}if(order.last_error){const p=document.createElement('small');p.textContent=order.last_error;td.append(p);}tr.append(td);return tr;
    }));
    if(!data.orders.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.className='empty';td.textContent='실제 주문 내역이 없습니다.';tr.append(td);get('real-orders').append(tr);}controls();
  }
  async function refresh(){if(busy)return;try{show(await api('status'));}catch(error){get('real-message').textContent=error.message;current=null;controls();}}
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
    event.preventDefault();if(busy)return;const payload=Object.fromEntries(new FormData(form));for(const name of ['short_window','long_window','dca_interval_hours'])payload[name]=Number(payload[name]);for(const name of ['max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])payload[name]=(Number(payload[name])/100).toFixed(8);
    busy=true;controls();try{const data=await api('settings',payload);dirty=false;show(data);get('real-settings-message').textContent='실거래 설정을 저장했습니다. 주문 테스트를 다시 실행하세요.';}catch(error){get('real-settings-message').textContent=error.message;}finally{busy=false;await refresh();}
  });
  refresh();setInterval(refresh,5000);
})();
