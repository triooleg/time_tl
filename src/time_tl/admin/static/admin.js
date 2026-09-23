(()=>{
  const textMetric=(name,m)=>{
    if(!m||m.value===null||m.value===undefined)return `${name}: Метрики недоступні`;
    const value=Array.isArray(m.value)?m.value.join(' / '):m.value;
    return `${name}: ${value} ${m.unit||''}`.trim();
  };
  const storageMetric=(name,m)=>{
    if(!m||!Array.isArray(m.value)||m.value.length<2)return `${name}: Метрики недоступні`;
    const used=Number(m.value[0]);
    const total=Number(m.value[1]);
    const reportedPercent=Number(m.value[2]);
    if(!Number.isFinite(used)||!Number.isFinite(total)||used<0||total<=0)return `${name}: Метрики недоступні`;
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
    if(!value)return 'дані недоступні';
    const seconds=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/1000));
    if(!Number.isFinite(seconds))return 'дані недоступні';
    return seconds<60?`${seconds} с`:`${Math.floor(seconds/60)} хв`;
  };
  const render=(data)=>{
    const aggregates=data.aggregates||{};
    const provider=data.provider||{};
    const overview=document.querySelectorAll('#overview article');
    setText(overview[0]?.querySelector('strong'),String(aggregates.users??'Дані недоступні'));
    setText(overview[0]?.querySelector('p'),`Профілі: ${aggregates.profiles??'дані недоступні'}`);
    const active=count(aggregates.alert_states,'ACTIVE');
    setText(overview[1]?.querySelector('strong'),String(active??'Дані недоступні'));
    setText(overview[2]?.querySelector('strong'),String(aggregates.outbox_pending??'Дані недоступні'));
    setText(overview[2]?.querySelector('p'),`Найстаріша: ${age(aggregates.outbox_oldest_at)}`);
    setText(overview[3]?.querySelector('strong'),`Доставлено: ${aggregates.outbox_delivered??'дані недоступні'}`);
    setText(overview[3]?.querySelector('p'),`Повтори: ${aggregates.outbox_retrying??'—'} · постійні помилки: ${aggregates.outbox_dead??'—'}`);

    const state=String(provider.state||'UNKNOWN');
    const providerHeading=document.querySelector('#provider p b');
    setText(providerHeading,state);
    setText(document.getElementById('aggregate'),state==='STALE'||state==='UNKNOWN'?'Потребує уваги':'Працює');
    const providerParagraphs=document.querySelectorAll('#provider p');
    const freshness=provider.freshness==='fresh'?'Свіжі дані':provider.freshness==='expired'?'Дані застаріли':'Стан не підтверджено';
    setText(providerParagraphs[0],`${state} · ${freshness}`);
    const providerDescription=document.createElement('span');
    providerDescription.textContent=state==='STALE'||state==='UNKNOWN'?' Нові відбої заблоковано.':'';
    providerParagraphs[0]?.append(providerDescription);

    const delivery=document.querySelector('#delivery p');
    setText(delivery,`Очікують: ${aggregates.outbox_pending??'—'} · повтори: ${aggregates.outbox_retrying??'—'} · доставлено: ${aggregates.outbox_delivered??'—'} · постійні помилки: ${aggregates.outbox_dead??'—'}`);
    const userParagraphs=document.querySelectorAll('#users p');
    setText(userParagraphs[0],`Користувачі: ${aggregates.users??'—'} · профілі: ${aggregates.profiles??'—'}. Тільки агрегати.`);
    const modeLabels={lessons:'уроки',lessons_only:'уроки',lessons_and_sirens:'уроки + тривоги',alerts_only:'лише тривоги',sirens_only:'лише тривоги',other:'інше'};
    const modeText=Object.entries(aggregates.modes||{}).map(([key,value])=>`${modeLabels[key]||'інше'}: ${value}`).join(' · ');
    let modeParagraph=userParagraphs[1];
    if(!modeParagraph){
      modeParagraph=document.createElement('p');
      document.getElementById('users')?.append(modeParagraph);
    }
    setText(modeParagraph,modeText?`Режими: ${modeText}`:'Режими: даних ще немає.');

    const cards=document.querySelectorAll('#metrics article');
    const host=data.host||{};
    setText(cards[0],textMetric('CPU',host.cpu));
    setText(cards[1],storageMetric('RAM',host.ram));
    setText(cards[2],storageMetric('Disk',host.disk));
    setText(cards[3],textMetric('Load',host.load));
    setText(document.getElementById('metrics-heartbeat'),host.source_status==='available'?'Heartbeat отримано':'Метрики недоступні');

    const byName=new Map((data.workers||[]).map(worker=>[worker.name,worker]));
    document.querySelectorAll('#server tbody tr').forEach(row=>{
      const name=row.cells[0]?.textContent;
      if(!name||name==='metrics collector')return;
      const worker=byName.get(name);
      const revision=worker?.applied_config_revision?` · конфіг v${worker.applied_config_revision}`:' · версію конфігурації не підтверджено';
      setText(row.cells[1],worker?.heartbeat_at?`${worker.status} · ${age(worker.heartbeat_at)}${revision}`:'Немає heartbeat');
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
      setText(updated,`Оновлено: ${new Date(data.generated_at).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'})}`);
      updated.classList.remove('critical');
    }catch(error){
      setText(updated,'Не вдалося оновити панель. Дані провайдера не змінювалися через цю дію. Можливо застаріло');
      updated.classList.add('critical');
    }finally{button.disabled=false;}
  }
  button?.addEventListener('click',refresh);
  document.addEventListener('visibilitychange',()=>{clearInterval(timer);timer=setInterval(refresh,document.hidden?60000:15000);});
  timer=setInterval(refresh,15000);
  refresh();
})();
