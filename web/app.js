const $ = id => document.getElementById(id);
const money = n => Number(n || 0).toLocaleString('ru-RU', {maximumFractionDigits: 2});
let selected = '', samples = [], polling = false;
const labels = {'m-cut':'Резка','m-handles':'Ручки','m-paint':'Покраска','m-assemble':'Сборка',sheet:'Лист',control:'Элемент управления',body:'Корпус',robot:'Робот'};
async function api(path) {
  const token = $('token').value;
  const response = await fetch(path, {headers: token ? {Authorization: `Bearer ${token}`} : {}});
  if (!response.ok) throw new Error(response.status === 401 ? 'Введите токен сервиса' : `Ошибка ${response.status}`);
  return response.json();
}
function textNode(tag, value, cls) { const el = document.createElement(tag); el.textContent = value; if(cls) el.className = cls; return el; }
function rows(id, values) {
  $(id).replaceChildren(...values.map(([name,value]) => {const el = textNode('div','','row');el.append(textNode('span',name),textNode('span',value,'muted'));return el;}));
}
function drawChart() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = $('chart'); svg.replaceChildren();
  if(samples.length < 2) return;
  const min = Math.min(0,...samples.map(p=>p[1])), max = Math.max(1,...samples.map(p=>p[1]));
  const first = samples[0][0], last = samples[samples.length-1][0];
  const line = document.createElementNS(ns,'polyline');
  line.setAttribute('points', samples.map(([t,c])=>`${10+980*(t-first)/Math.max(1,last-first)},${150-140*(c-min)/(max-min)}`).join(' '));
  line.setAttribute('fill','none');line.setAttribute('stroke','#bef584');line.setAttribute('stroke-width','2');svg.append(line);
}
async function poll() {
  if(polling) return; polling = true;
  try {
    const {runs} = await api('/api/runs');
    const old = selected;
    if(!runs.some(r=>r.runId===selected)) selected = runs.length ? runs[runs.length-1].runId : '';
    $('runs').replaceChildren(...(runs.length ? runs.map(r=>{const opt = textNode('option',r.runId);opt.value=r.runId;return opt;}) : [textNode('option','Пока нет прогонов')]));
    $('runs').value = selected;
    $('connection').textContent = 'Сервис доступен';
    $('empty').hidden = Boolean(selected); $('dashboard').hidden = !selected;
    if(!selected) return;
    if(old !== selected) samples = [];
    const run = await api('/api/runs/' + encodeURIComponent(selected));
    const {latest} = run, state = latest.state;
    $('status').textContent = run.status === 'finished' ? 'Смена завершена' : 'Смена идёт';
    if(!state) return;
    const cash = run.status === 'finished' && run.result.profit !== undefined ? run.result.profit : state.cash;
    $('cash').textContent = money(cash); $('tick').textContent = `${latest.t} / ${run.horizon}`;
    $('storage').textContent = `${state.storage.free} / ${state.storage.capacity}`; $('events').textContent = run.events;
    if(!samples.length || samples[samples.length-1][0] !== latest.t) samples.push([latest.t,cash]);
    else samples[samples.length-1][1] = cash;
    drawChart();
    rows('machines',state.machines.map(m=>[labels[m.id]||m.id,m.busyUntil===null?'Свободен':`Работает до такта ${m.busyUntil}`]));
    rows('buyers',state.buyers.map(b=>[b.id,b.cooldownUntil===null?'Готов к предложению':`Пауза до ${b.cooldownUntil}`]));
    $('items').replaceChildren(...state.storage.items.map(i=>textNode('div',`${labels[i.kind]||i.kind} · ${i.id}${i.props.color?' · '+i.props.color:''}${i.props.handles?' · с ручками':''}`,'item')));
    if(!state.storage.items.length) $('items').append(textNode('span','Склад пуст','muted'));
    $('log').replaceChildren(...run.log.slice(-20).reverse().map(entry=>{const row=document.createElement('tr');[entry.t,entry.type,JSON.stringify(entry.commands),JSON.stringify(entry.report),entry.decisionMs].forEach(value=>row.append(textNode('td',value)));return row;}));
  } catch(error) {$('connection').textContent = error.message;}
  finally {polling = false;}
}
$('runs').addEventListener('change',()=>{selected=$('runs').value;samples=[];poll();});
$('connect').addEventListener('click',poll);
poll(); setInterval(poll,1000);
