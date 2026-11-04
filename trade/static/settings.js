(() => {
  const form = document.getElementById('strategy-form');
  const fields = document.getElementById('strategy-fields');
  const save = document.getElementById('strategy-save');
  const message = document.getElementById('strategy-message');
  let current = null, dirty = false, saving = false, online = false;
  function notify(text, error=false) {message.textContent=text;message.classList.toggle('negative',error);}
  function fill() {
    if(!current)return;
    for(const name of ['short_window','long_window','order_krw'])form.elements[name].value=current.settings[name];
    for(const name of ['max_exposure','daily_loss_limit'])form.elements[name].value=Number((Number(current.settings[name])*100).toFixed(6));
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
  });
  document.addEventListener('trade-disconnected',()=>{online=false;controls();notify('서버에 연결할 수 없습니다. 연결 복구 후 저장하세요.',true);});
  form.addEventListener('input',()=>{dirty=true;controls();notify('저장하지 않은 변경 사항이 있습니다.');});
  document.getElementById('strategy-reset').addEventListener('click',()=>{dirty=false;fill();controls();notify('현재 저장된 설정으로 되돌렸습니다.');});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(saving||!current||current.running||!online)return;
    if(Number(form.elements.short_window.value)>=Number(form.elements.long_window.value)){notify('단기 이동평균은 장기 이동평균보다 작아야 합니다.',true);return;}
    const payload=Object.fromEntries(new FormData(form));
    for(const name of ['short_window','long_window'])payload[name]=Number(payload[name]);
    for(const name of ['max_exposure','daily_loss_limit'])payload[name]=(Number(payload[name])/100).toFixed(8);
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
