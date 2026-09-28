(()=>{
  const textMetric=(name,m)=>{
    if(!m||m.value===null||m.value===undefined)return `${name}: Метрики недоступны`;
    const value=Array.isArray(m.value)?m.value.join(' / '):m.value;
    return `${name}: ${value} ${m.unit||''}`.trim();
  };
  const storageMetric=(name,m)=>{
    if(!m||!Array.isArray(m.value)||m.value.length<2)return `${name}: Метрики недоступны`;
    const used=Number(m.value[0]);
    const total=Number(m.value[1]);
    const reportedPercent=Number(m.value[2]);
    if(!Number.isFinite(used)||!Number.isFinite(total)||used<0||total<=0)return `${name}: Метрики недоступны`;
    const percent=Number.isFinite(reportedPercent)&&reportedPercent>=0?reportedPercent:(used/total)*100;
    const gibibyte=1024**3;
    return `${name}: ${(used/gibibyte).toFixed(2)} / ${(total/gibibyte).toFixed(2)} ГБ (${percent.toFixed(1)}%)`;
  };
  if(typeof module!=='undefined'&&module.exports){module.exports={storageMetric};return;}
  const button=document.getElementById('refresh');
  const updated=document.getElementById('updated');
  let timer;
  const setText=(node,value)=>{if(node)node.textContent=value;};
  const count=(values,key)=>{
    if(!values||!Object.prototype.hasOwnProperty.call(values,key))return null;
    const value=Number(values[key]);
    return Number.isFinite(value)&&value>=0?value:null;
  };
  const age=(value)=>{
    if(!value)return 'данные недоступны';
    const seconds=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/1000));
    if(!Number.isFinite(seconds))return 'данные недоступны';
    return seconds<60?`${seconds} с`:`${Math.floor(seconds/60)} мин`;
  };
  const render=(data)=>{
    const aggregates=data.aggregates||{};
    const provider=data.provider||{};
    const overview=document.querySelectorAll('#overview article');
    setText(overview[0]?.querySelector('strong'),String(aggregates.users??'Данные недоступны'));
    setText(overview[0]?.querySelector('p'),`Профили: ${aggregates.profiles??'данные недоступны'}`);
    const active=count(aggregates.alert_states,'ACTIVE');
    setText(overview[1]?.querySelector('strong'),String(active??'Данные недоступны'));
    setText(overview[2]?.querySelector('strong'),String(aggregates.outbox_pending??'Данные недоступны'));
    setText(overview[2]?.querySelector('p'),`Самая старая: ${age(aggregates.outbox_oldest_at)}`);
    setText(overview[3]?.querySelector('strong'),`Доставлено: ${aggregates.outbox_delivered??'данные недоступны'}`);
    setText(overview[3]?.querySelector('p'),`Повторы: ${aggregates.outbox_retrying??'—'} · постоянные ошибки: ${aggregates.outbox_dead??'—'}`);

    const state=String(provider.state||'UNKNOWN');
    const providerHeading=document.querySelector('#provider p b');
    setText(providerHeading,state);
    setText(document.getElementById('aggregate'),state==='STALE'||state==='UNKNOWN'?'Требует внимания':'Работает');
    const providerParagraphs=document.querySelectorAll('#provider p');
    const freshness=provider.freshness==='fresh'?'Свежие данные':provider.freshness==='expired'?'Данные устарели':'Состояние не подтверждено';
    setText(providerParagraphs[0],`${state} · ${freshness}`);
    const providerDescription=document.createElement('span');
    providerDescription.textContent=state==='STALE'||state==='UNKNOWN'?' Новые отбои заблокированы.':'';
    providerParagraphs[0]?.append(providerDescription);

    const delivery=document.querySelector('#delivery p');
    setText(delivery,`Ожидают: ${aggregates.outbox_pending??'—'} · повторы: ${aggregates.outbox_retrying??'—'} · доставлено: ${aggregates.outbox_delivered??'—'} · постоянные ошибки: ${aggregates.outbox_dead??'—'}`);
    const userParagraphs=document.querySelectorAll('#users p');
    setText(userParagraphs[0],`Пользователи: ${aggregates.users??'—'} · профили: ${aggregates.profiles??'—'}. Только агрегаты.`);
    const modeLabels={lessons:'уроки',lessons_only:'уроки',lessons_and_sirens:'уроки + тревоги',alerts_only:'только тревоги',sirens_only:'только тревоги',other:'другое'};
    const modeText=Object.entries(aggregates.modes||{}).map(([key,value])=>`${modeLabels[key]||'другое'}: ${value}`).join(' · ');
    let modeParagraph=userParagraphs[1];
    if(!modeParagraph){
      modeParagraph=document.createElement('p');
      document.getElementById('users')?.append(modeParagraph);
    }
    setText(modeParagraph,modeText?`Режимы: ${modeText}`:'Режимы: данных ещё нет.');

    const cards=document.querySelectorAll('#metrics article');
    const host=data.host||{};
    setText(cards[0],textMetric('CPU',host.cpu));
    setText(cards[1],storageMetric('RAM',host.ram));
    setText(cards[2],storageMetric('Disk',host.disk));
    setText(cards[3],textMetric('Load',host.load));
    setText(document.getElementById('metrics-heartbeat'),host.source_status==='available'?'Сигнал активности (heartbeat) получен':'Метрики недоступны');

    const convergence=data.owner_configuration?.convergence;
    const convergenceState=convergence?.state==='converged'
      ?`Все воркеры применили ACTIVE v${convergence.active_revision}.`
      :convergence?.state==='partial'
        ?`Частичное применение ACTIVE v${convergence.active_revision}: ожидаем ${convergence.missing_or_mismatched_roles.join(', ')}.`
        :`Ожидаем применения ACTIVE v${convergence?.active_revision??'—'} всеми воркерами.`;
    setText(document.getElementById('owner-convergence'),convergenceState);

    const byName=new Map((data.workers||[]).map(worker=>[worker.name,worker]));
    const workerStatusLabels={
      healthy:'Исправен (healthy)',
      degraded:'Работает с отклонениями (degraded)',
      critical:'Критическое состояние (critical)',
      unknown:'Состояние не подтверждено (unknown)',
    };
    document.querySelectorAll('#server tbody tr').forEach(row=>{
      const name=row.cells[0]?.textContent;
      if(!name||name==='сборщик метрик')return;
      const worker=byName.get(name);
      const revision=worker?.applied_config_revision?` · конфигурация v${worker.applied_config_revision}`:' · версия конфигурации не подтверждена';
      const status=workerStatusLabels[worker?.status]||'Состояние не подтверждено (unknown)';
      setText(row.cells[1],worker?.heartbeat_at?`${status} · ${age(worker.heartbeat_at)}${revision}`:'Нет сигнала активности (heartbeat)');
    });
  };
  async function refresh(){
    if(!button)return;
    button.disabled=true;
    try{
      const response=await fetch('/api/dashboard',{credentials:'same-origin',headers:{Accept:'application/json'}});
      if(response.status===401){location.assign('/login?expired=1');return;}
      if(!response.ok)throw new Error('refresh failed');
      const data=await response.json();
      render(data);
      setText(updated,`Обновлено: ${new Date(data.generated_at).toLocaleString('ru-UA',{timeZone:'Europe/Kyiv'})}`);
      updated.classList.remove('critical');
    }catch(error){
      setText(updated,'Не удалось обновить панель. Данные провайдера не изменялись из-за этого действия. Возможно, данные устарели.');
      updated.classList.add('critical');
    }finally{button.disabled=false;}
  }
  button?.addEventListener('click',refresh);
  document.addEventListener('visibilitychange',()=>{clearInterval(timer);timer=setInterval(refresh,document.hidden?60000:15000);});
  timer=setInterval(refresh,15000);
  refresh();
})();
