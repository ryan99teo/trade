(() => {
  const form = document.getElementById('strategy-form');
  const fields = document.getElementById('strategy-fields');
  const save = document.getElementById('strategy-save');
  const message = document.getElementById('strategy-message');
  let current = null, dirty = false, saving = false, online = false;
  function notify(text, error=false) {message.textContent=text;message.classList.toggle('negative',error);}
  function fill() {
    if(!current)return;
    for(const name of ['strategy','dca_interval_hours','dca_offset_minutes','short_window','long_window','order_krw'])form.elements[name].value=current.settings[name];
    for(const name of ['max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])form.elements[name].value=Number((Number(current.settings[name])*100).toFixed(6));
    form.elements.order_krw.max=current.settings.initial_cash;
  }
  function controls() {
    fields.disabled=!online||!current||current.running||saving;
    save.disabled=fields.disabled||!dirty;
    document.getElementById('strategy-reset').disabled=saving||!dirty;
    document.getElementById('strategy-state').textContent=!online?'서버 연결 확인 필요':current?.running?'거래 실행 중 · 수정 잠김':'거래 정지 · 수정 가능';
  }
  document.addEventListener('trade-status',event=>{
    const initial=!current;current=event.detail;online=true;
    if(!dirty&&!saving)fill();controls();
    if(initial)notify(current.running?'실시간 화면에서 거래를 정지한 뒤 수정하세요.':'설정을 수정하고 저장하세요. 거래는 정지 상태로 유지됩니다.');
    const cfg=current.settings,pairs=[['초기 가상 자금',won(cfg.initial_cash)],['캔들 단위',cfg.candle_minutes+'분봉'],['수집 주기',cfg.poll_seconds+'초'],['가정 수수료 / 슬리피지',Number(cfg.fee_rate)*100+'% / '+Number(cfg.slippage)*100+'%']];
    document.getElementById('strategy-fixed').replaceChildren(...pairs.map(([key,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);return div;}));
    if(typeof renderGateLogs==='function')renderGateLogs('strategy-decision-gate-logs',(current.decision_gate||{}).logs);
  });
  document.addEventListener('trade-disconnected',()=>{online=false;controls();notify('서버에 연결할 수 없습니다. 연결 복구 후 저장하세요.',true);});
  form.addEventListener('input',()=>{dirty=true;controls();notify('저장하지 않은 변경 사항이 있습니다.');});
  document.getElementById('strategy-reset').addEventListener('click',()=>{dirty=false;fill();controls();notify('현재 저장된 설정으로 되돌렸습니다.');});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saving||!current||current.running||!online)return;
    if(Number(form.elements.short_window.value)>=Number(form.elements.long_window.value)){notify('단기 이동평균은 장기 이동평균보다 작아야 합니다.',true);return;}
    const payload=Object.fromEntries(new FormData(form));
    for(const name of ['short_window','long_window','dca_interval_hours','dca_offset_minutes'])payload[name]=Number(payload[name]);
    for(const name of ['max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])payload[name]=(Number(payload[name])/100).toFixed(8);
    saving=true;controls();notify('설정을 저장하고 있습니다.');
    try {
      const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)throw new Error(Array.isArray(data.detail)?data.detail.map(d=>d.msg).join(' / '):data.detail||'설정을 저장하지 못했습니다.');
      dirty=false;saving=false;render(data);notify('설정을 저장했습니다. 실시간 화면에서 거래 시작을 누르면 새 설정을 사용합니다.');
    } catch(error) {notify(error.message,true);} finally {saving=false;controls();}
  });
  if(window.currentTradeStatus)document.dispatchEvent(new CustomEvent('trade-status',{detail:window.currentTradeStatus}));
})();

(() => {
  const form=document.getElementById('decision-gate-form');
  const fields=document.getElementById('decision-gate-fields');
  const save=document.getElementById('decision-gate-save');
  const message=document.getElementById('decision-gate-message');
  const state=document.getElementById('decision-gate-state');
  let current=null,dirty=false,saving=false,online=false;
  function notify(text,error=false){message.textContent=text;message.classList.toggle('negative',error);}
  function fill(){
    if(!current)return;
    const cfg=current.settings,gate=current.decision_gate||{};
    form.elements.decision_gate_enabled.value=String(!!cfg.decision_gate_enabled);
    form.elements.decision_threshold_buy.value=Number((Number(cfg.decision_threshold_buy)*100).toFixed(6));
    form.elements.decision_threshold_sell.value=Number((Number(cfg.decision_threshold_sell)*100).toFixed(6));
    form.elements.decision_timeout_seconds.value=cfg.decision_timeout_seconds;
    form.elements.openrouter_api_key.value='';
    form.elements.openrouter_api_key.placeholder=cfg.openrouter_api_key_set?'저장됨 · 비우면 유지':'비우면 기존 키 유지';
    state.textContent=!online?'서버 연결 확인 필요':current.running?'거래 실행 중 · 수정 잠김':(gate.enabled?(gate.configured?'사용 중':'사용 · 키 필요'):'꺼짐 · 수정 가능');
  }
  function controls(){
    fields.disabled=!online||!current||current.running||saving;
    save.disabled=fields.disabled||!dirty;
    document.getElementById('decision-gate-reset').disabled=saving||!dirty;
  }
  document.addEventListener('trade-status',event=>{
    current=event.detail;online=true;
    if(!dirty&&!saving)fill();
    controls();
  });
  document.addEventListener('trade-disconnected',()=>{online=false;controls();state.textContent='서버 연결 확인 필요';notify('서버에 연결할 수 없습니다.',true);});
  form.addEventListener('input',()=>{dirty=true;controls();notify('저장하지 않은 변경 사항이 있습니다.');});
  document.getElementById('decision-gate-reset').addEventListener('click',()=>{dirty=false;fill();controls();notify('현재 저장된 설정으로 되돌렸습니다.');});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saving||!current||current.running||!online)return;
    const payload=Object.fromEntries(new FormData(form));
    payload.decision_gate_enabled=payload.decision_gate_enabled==='true';
    payload.decision_threshold_buy=(Number(payload.decision_threshold_buy)/100).toFixed(8);
    payload.decision_threshold_sell=(Number(payload.decision_threshold_sell)/100).toFixed(8);
    payload.decision_timeout_seconds=Number(payload.decision_timeout_seconds);
    if(!payload.openrouter_api_key)delete payload.openrouter_api_key;
    saving=true;controls();notify('게이트 설정을 저장하고 있습니다.');
    try{
      const response=await fetch('/api/decision-gate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)throw new Error(Array.isArray(data.detail)?data.detail.map(d=>d.msg).join(' / '):data.detail||'게이트 설정을 저장하지 못했습니다.');
      dirty=false;saving=false;render(data);notify('게이트 설정을 저장했습니다. 거래 시작 후 매매 신호에 적용됩니다.');
    }catch(error){notify(error.message,true);}finally{saving=false;controls();}
  });
  if(window.currentTradeStatus)document.dispatchEvent(new CustomEvent('trade-status',{detail:window.currentTradeStatus}));
})();

(() => {
  const button=document.getElementById('telegram-test'),state=document.getElementById('telegram-state'),message=document.getElementById('telegram-message');
  let sending=false;
  const names={fill:'모의 체결',risk:'손실 한도',error:'시세 수집 장애',recovery:'시세 수신 복구',test:'테스트 알림'};
  const statuses={pending:'전송 대기',sent:'전송 완료',failed:'전송 실패'};
  async function refreshTelegram(){
    try{
      const response=await fetch('/api/notifications');if(!response.ok)throw new Error('알림 상태를 조회할 수 없습니다.');
      const data=await response.json();state.textContent=data.enabled?'설정됨':'미설정';button.disabled=!data.enabled||sending;
      document.getElementById('telegram-guide').textContent=data.enabled?'테스트 알림을 보내 실제 수신 여부를 확인하세요. 전송 실패 시 자동으로 재시도합니다.':'서버 환경변수 TRADE_TELEGRAM_BOT_TOKEN과 TRADE_TELEGRAM_CHAT_ID를 설정하고 서버를 재시작하세요. 설정 방법은 README의 텔레그램 알림 항목에 있습니다.';
      const pairs=[['전송 대기',data.pending+'건'],['전송 완료',data.sent+'건'],['최종 실패',data.failed+'건']];
      document.getElementById('telegram-counts').replaceChildren(...pairs.map(([key,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);return div;}));
      const list=document.getElementById('telegram-history');
      if(!data.recent.length){empty(list,'아직 알림 내역이 없습니다.');return;}
      list.replaceChildren(...data.recent.map(row=>{const p=document.createElement('p');p.textContent=time(row.created_at)+' · '+names[row.kind]+' · '+statuses[row.status]+' · 시도 '+row.attempts+'회'+(row.last_error?' · '+row.last_error:'');return p;}));
    }catch(error){state.textContent='연결 확인 필요';button.disabled=true;message.textContent=error.message;}
  }
  button.addEventListener('click',async()=>{
    if(sending)return;sending=true;button.disabled=true;
    try{const response=await fetch('/api/notifications/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),data=await response.json();if(!response.ok)throw new Error(data.detail||'요청 실패');message.textContent='테스트 알림 #'+data.id+'을 요청했습니다. 전송 내역과 텔레그램 수신을 확인하세요.';}catch(error){message.textContent=error.message;}finally{sending=false;await refreshTelegram();}
  });
  refreshTelegram();setInterval(refreshTelegram,5000);
})();
