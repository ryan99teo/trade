(() => {
  const get = id => document.getElementById(id);
  const pct = value => value === null ? '미정' : Number(value).toFixed(2) + '%';
  const form = get('bt-form');
  let result = null, submitting = false;
  const yesterday = new Date(Date.now() + 9*3600000 - 86400000).toISOString().slice(0,10);
  const start = new Date(yesterday+'T00:00:00Z');start.setUTCDate(start.getUTCDate()-29);
  get('bt-start').value = start.toISOString().slice(0,10);get('bt-end').value = yesterday;
  get('bt-start').max = yesterday;get('bt-end').max = yesterday;

  function message(text, error=false) {
    get('bt-message').textContent=text;get('bt-message').classList.toggle('negative',error);
  }
  async function jsonFetch(url, options) {
    const response = await fetch(url,options), data=await response.json();
    if(!response.ok){const detail=data.detail;throw new Error(Array.isArray(detail)?detail.map(d=>d.loc.join('.')+': '+d.msg).join(' / '):(detail||'요청을 처리하지 못했습니다.'));}
    return data;
  }
  function plot(id, series, isPercent=false) {
    const canvas=get(id),width=canvas.clientWidth,height=210,ratio=devicePixelRatio||1;
    if(!width)return;
    canvas.width=width*ratio;canvas.height=height*ratio;
    const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
    const values=series.flatMap(s=>s.values),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.1,isPercent?.1:1);
    const left=width<500?65:95,right=12,top=12,bottom=32;
    const x=i=>left+i/Math.max(result.curve.length-1,1)*(width-left-right);
    const y=v=>top+(hi+pad-v)/(hi-lo+2*pad)*(height-top-bottom);
    ctx.font='10px sans-serif';
    for(let i=0;i<4;i++){
      const value=lo+(hi-lo)*i/3,py=y(value);ctx.strokeStyle='#243247';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(width-right,py);ctx.stroke();
      ctx.fillStyle='#8a99af';ctx.fillText(isPercent?value.toFixed(2)+'%':new Intl.NumberFormat('ko-KR',{notation:'compact',maximumFractionDigits:1}).format(value)+'원',0,py+3);
    }
    for(const s of series){ctx.beginPath();s.values.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.strokeStyle=s.color;ctx.lineWidth=2;ctx.stroke();}
    ctx.fillStyle='#8a99af';
    const dateLabel=t=>new Date(t).toLocaleDateString('ko-KR',{timeZone:'Asia/Seoul',month:'numeric',day:'numeric'});
    ctx.fillText(dateLabel(result.curve[0].time),left,height-7);
    const last=dateLabel(result.curve[result.curve.length-1].time);ctx.fillText(last,width-ctx.measureText(last).width-right,height-7);
  }
  function draw(){if(!result||get('backtest-view').hidden)return;plot('bt-chart',[{values:result.curve.map(p=>Number(p.equity)),color:'#e9b454'},{values:result.curve.map(p=>Number(p.benchmark)),color:'#67b9f2'}]);plot('bt-drawdown',[{values:result.curve.map(p=>-Number(p.drawdown_pct)),color:'#ff8b8b'}],true);}
  function show(data){
    result=data;const s=data.summary;get('bt-result').hidden=false;
    get('bt-return').textContent=pct(s.return_pct);color(get('bt-return'),s.return_pct);
    get('bt-pnl').textContent='총 손익 '+won(s.total_pnl);get('bt-mdd').textContent=pct(s.max_drawdown_pct);
    get('bt-win').textContent=pct(s.win_rate_pct);get('bt-trades').textContent=`매도 ${s.closed_trades}회 중 수익 ${s.winning_trades}회`;
    get('bt-benchmark').textContent=pct(s.benchmark_return_pct);color(get('bt-benchmark'),s.benchmark_return_pct);
    get('bt-excess').textContent='단순 보유 대비 '+Number(s.excess_return_pp).toFixed(2)+'%p';
    get('bt-period').textContent=`${data.request.start_date} ~ ${data.request.end_date} · 1시간봉 ${s.candle_count.toLocaleString()}개 · 누락 ${s.missing_candles}개 · ${strategyLabel(data.request)}`;
    const pairs=[['최종 평가 자산',won(s.final_equity)],['실현 / 평가 손익',won(s.realized_pnl)+' / '+won(s.unrealized_pnl)],['거래 수수료 합계',won(s.fees)],['전체 체결 / 매도 거래',s.fill_count+'건 / '+s.closed_trades+'회'],['종료 원화 / BTC',won(s.cash)+' / '+Number(s.btc).toFixed(8)+' BTC'],['단순 보유 최종 자산',won(s.benchmark_final_equity)],['단순 보유 최대 낙폭',pct(s.benchmark_max_drawdown_pct)],['신규 매수 중지 여부',s.buy_halted?'손실 한도 도달로 중지':'해당 없음']];
    pairs.push(['본 기간 / 준비 구간 누락',s.missing_candles+'개 / '+s.missing_warmup_candles+'개'],['실제 평가 구간',time(s.observed_start)+' ~ '+time(s.observed_end)]);
    get('bt-details').replaceChildren(...pairs.map(([key,value])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;div.append(dt,dd);return div;}));
    get('bt-notes').replaceChildren(...data.notes.map(note=>{const li=document.createElement('li');li.textContent=note;return li;}));
    get('bt-fill-count').textContent=`총 ${s.fill_count}건`;
    const rows=data.fills.slice(-100).reverse().map(f=>{const tr=document.createElement('tr');[time(f.time),f.side==='buy'?'매수':'매도',won(f.price),Number(f.quantity).toFixed(8),won(f.fee),won(f.realized_pnl)].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.append(td);});return tr;});
    if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=6;td.className='empty';td.textContent='선택한 기간에 체결이 없습니다.';tr.append(td);rows.push(tr);}get('bt-fills').replaceChildren(...rows);
    get('bt-csv').href='/api/backtests/'+data.id+'/trades.csv';get('bt-csv').hidden=false;
    get('bt-hover').textContent='그래프 위에 마우스를 올리면 해당 시점의 자산을 확인할 수 있습니다.';draw();
  }
  function showComparison(runs){
    const cfg=runs[0].request;get('bt-comparison').hidden=false;
    get('bt-comparison-period').textContent=`${cfg.start_date} ~ ${cfg.end_date} · 초기 자금 ${won(cfg.initial_cash)} · 1회 주문 ${won(cfg.order_krw)} · 최대 BTC ${Number(cfg.max_exposure)*100}% · 동일 비용과 위험 한도`;
    get('bt-comparison-rows').replaceChildren(...runs.map(run=>{
      const tr=document.createElement('tr'),s=run.summary;
      for(const value of [strategyLabel(run.request),pct(s.return_pct),pct(s.max_drawdown_pct),pct(s.win_rate_pct),s.fill_count+'건',won(s.fees)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
      const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.textContent='상세 보기';button.addEventListener('click',()=>{show(run);get('bt-history').value=run.id;});td.append(button);tr.append(td);return tr;
    }));
  }
  async function loadHistory(selectId){
    const data=await jsonFetch('/api/backtests'),select=get('bt-history');
    const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='실행 결과를 선택하세요';
    select.replaceChildren(placeholder,...data.runs.map(run=>{const option=document.createElement('option');option.value=run.id;option.textContent=`${run.request.start_date} ~ ${run.request.end_date} · ${strategyLabel(run.request)} · ${pct(run.summary.return_pct)} · ${time(run.created_at)}`;return option;}));
    if(selectId)select.value=selectId;
    if(data.running&&!submitting)message('다른 백테스트가 실행 중입니다. 완료 후 저장된 결과를 확인하세요.');
  }
  document.querySelectorAll('button[data-view]').forEach(button=>button.addEventListener('click',()=>{
    const view=button.dataset.view;
    for(const name of ['live','backtest','strategy','real']){get(name+'-view').hidden=name!==view;get(name+'-tab').setAttribute('aria-pressed',String(name===view));}
    if(view==='backtest'){loadHistory(result?.id).catch(e=>message(e.message,true));requestAnimationFrame(draw);}else if(view==='live')requestAnimationFrame(chart);
  }));
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(submitting)return;
    const compare=event.submitter?.value==='compare';
    submitting=true;get('bt-run').disabled=true;get('bt-compare').disabled=true;get('bt-run').textContent='수집 · 계산 중…';
    message('과거 캔들을 수집하고 계산하고 있습니다. 기간에 따라 수십 초 걸릴 수 있습니다.');
    const payload=Object.fromEntries(new FormData(form));
    for(const key of ['short_window','long_window','dca_interval_hours'])payload[key]=Number(payload[key]);
    for(const key of ['fee_rate','slippage','max_exposure','daily_loss_limit','target_exposure','rebalance_tolerance'])payload[key]=(Number(payload[key])/100).toFixed(8);
    try{const data=await jsonFetch('/api/backtests'+(compare?'/compare':''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const selected=compare?data.runs[0]:data;if(compare)showComparison(data.runs);else get('bt-comparison').hidden=true;show(selected);await loadHistory(selected.id);message((compare?'세 전략 비교 완료':'백테스트 완료')+' · 결과를 저장했습니다. 과거 성과는 미래 수익을 보장하지 않습니다.');}
    catch(e){message(e.message,true);}finally{submitting=false;get('bt-run').disabled=false;get('bt-compare').disabled=false;get('bt-run').textContent='백테스트 실행';}
  });
  get('bt-history').addEventListener('change',async event=>{if(!event.target.value)return;try{get('bt-comparison').hidden=true;show(await jsonFetch('/api/backtests/'+event.target.value));message('저장된 결과를 불러왔습니다.');}catch(e){message(e.message,true);}});
  get('bt-chart').addEventListener('pointermove',event=>{if(!result)return;const rect=event.currentTarget.getBoundingClientRect(),left=rect.width<500?65:95,index=Math.round(Math.max(0,Math.min(1,(event.clientX-rect.left-left)/(rect.width-left-12)))*(result.curve.length-1)),p=result.curve[index];get('bt-hover').textContent=time(p.time)+' · 전략 '+won(p.equity)+' · 단순 보유 '+won(p.benchmark);});
  window.addEventListener('resize',draw);
})();
