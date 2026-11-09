'use strict';

const $ = id => document.getElementById(id);
const format = (value, digits = 2) => Number(value).toLocaleString('ru-RU', {maximumFractionDigits: digits});
const names = {sheet: 'Металлический лист', control: 'Элемент управления', body: 'Корпус', robot: 'Робот'};
const stages = {
  'm-cut': {name: 'Резка', description: 'Лист → корпус', icon: 'factory', duration: '2 такта'},
  'm-handles': {name: 'Установка ручек', description: 'Детали обретают форму', icon: 'settings', duration: '2 такта'},
  'm-paint': {name: 'Покраска', description: 'Три цвета. Больше возможностей.', icon: 'bolt', duration: '3 такта'},
  'm-assemble': {name: 'Сборка', description: 'Последний шаг перед продажей', icon: 'robot', duration: '3 такта'}
};
const eventNames = {supply: 'Поставка', machine_done: 'Готова деталь', sale_result: 'Ответ покупателя', idle: 'Ожидание'};
const opNames = {cut: 'Резка', 'add-handles': 'Установка ручек', 'paint-red': 'Красная покраска', 'paint-green': 'Зелёная покраска', 'paint-blue': 'Синяя покраска', assemble: 'Сборка'};
let selected = '', userSelected = false, polling = false, paused = false, currentRun = null;
let inventoryKind = 'all', chartRange = 'all', revision = '', runListKey = '', samples = [];
let plotPoints = [], toastTimer;

function node(tag, value = '', className = '') {
  const el = document.createElement(tag);
  el.textContent = value;
  if (className) el.className = className;
  return el;
}

function icon(name, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('aria-hidden', 'true');
  if (className) svg.setAttribute('class', className);
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

function svgElement(tag, attributes = {}, value) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, attribute] of Object.entries(attributes)) el.setAttribute(key, attribute);
  if (value !== undefined) el.textContent = value;
  return el;
}

function toast(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
}

async function api(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const token = $('token').value.trim();
    const response = await fetch(path, {
      headers: token ? {Authorization: 'Bearer ' + token} : {},
      signal: controller.signal,
      cache: 'no-store'
    });
    if (!response.ok) throw new Error(response.status === 401
      ? 'Сервис защищён токеном. Откройте «Подключение» и укажите токен.'
      : `Сервис вернул ошибку ${response.status}. Попробуйте обновить подключение.`);
    return await response.json();
  } finally { clearTimeout(timer); }
}

function emptyState(container, title, description = '', symbol = 'box') {
  const empty = node('div', '', 'empty-content');
  empty.append(icon(symbol), node('span', title));
  if (description) empty.append(node('small', description));
  container.replaceChildren(empty);
}

function renderMachines(machines = [], t = 0, finished = false) {
  const source = machines.length ? machines : Object.keys(stages).map(id => ({id, busyUntil: null}));
  $('machines').replaceChildren(...source.map((machine, index) => {
    const stage = stages[machine.id] || {name: machine.id, description: 'Операция производства', icon: 'factory'};
    const working = machine.busyUntil !== null;
    const card = node('article', '', 'machine-card' + (working ? ' working' : ''));
    const top = node('div', '', 'machine-top');
    const status = node('span', '', 'machine-state');
    status.append(node('i'), node('span', !machines.length ? 'Ожидание' : working ? (finished ? 'Осталась операция' : 'В работе') : 'Свободен'));
    top.append(node('span', String(index + 1).padStart(2, '0') + ' / ' + machine.id.replace('m-', '').toUpperCase(), 'machine-number'), status);
    const symbol = node('div', '', 'machine-icon'); symbol.append(icon(stage.icon));
    const bottom = node('div', '', 'machine-bottom');
    bottom.append(node('span', working ? 'Завершение' : 'Статус'), node('span', working ? `такт ${machine.busyUntil}` : machines.length ? 'Готов к запуску' : 'Нет прогона'));
    card.append(top, symbol, node('h3', stage.name), node('p', stage.description), bottom);
    return card;
  }));
}

function renderInventory() {
  const items = currentRun?.latest?.state?.storage.items || [];
  $('warehouse-count').textContent = `${items.length} на складе`;
  const visible = items.filter(item => inventoryKind === 'all' || inventoryKind === item.kind);
  if (!visible.length) {
    emptyState($('items'), items.length ? 'В этой категории пока пусто' : 'Здесь начинается производство',
      items.length ? 'Выберите другую категорию предметов.' : 'Принятые заготовки, корпуса и готовые роботы появятся здесь.');
    return;
  }
  const colors = {red: 'Красный', green: 'Зелёный', blue: 'Синий'};
  $('items').replaceChildren(...visible.map(item => {
    const row = node('div', '', 'inventory-row');
    const symbol = node('span', '', 'item-icon'); symbol.append(icon(item.kind === 'robot' ? 'robot' : 'box'));
    const label = node('div', '', 'item-name');
    label.append(node('span', names[item.kind] || item.kind), node('span', item.id, 'item-id'));
    const props = node('div', '', 'item-properties');
    if (item.props?.color) {
      const color = item.props.color;
      if (Object.hasOwnProperty.call(colors, color)) props.append(node('i', '', 'color-dot color-' + color));
      props.append(node('span', colors[color] || color));
    }
    if (item.props?.handles) props.append(node('span', '· с ручками'));
    row.append(symbol, label, props);
    return row;
  }));
}

function renderBuyers(buyers = [], t = 0, finished = false) {
  if (!buyers.length) {
    emptyState($('buyers'), 'Рынок ждёт первую смену', 'Покупатели появятся после запуска прогона.', 'users');
    return;
  }
  const ready = buyers.filter(buyer => buyer.cooldownUntil === null).length;
  $('buyers-count').textContent = `${ready} из ${buyers.length} доступны`;
  $('buyers').replaceChildren(...buyers.map(buyer => {
    const available = buyer.cooldownUntil === null;
    const row = node('div', '', 'buyer-row');
    const label = node('div', '', 'buyer-name');
    label.append(node('span', 'Покупатель ' + buyer.id.toUpperCase()), node('span', 'Предпочтения скрыты', 'buyer-detail'));
    row.append(node('span', buyer.id.toUpperCase(), 'buyer-avatar'), label,
      node('span', available ? 'Готов к сделке' : `Пауза · ${Math.max(0, buyer.cooldownUntil - t)} такт.`, 'buyer-status' + (available ? ' ready' : '')));
    return row;
  }));
}

function describeCommands(commands) {
  return commands.map(command => {
    if (command.cmd === 'accept') return 'Принять ' + command.itemId;
    if (command.cmd === 'start') return (opNames[command.opId] || command.opId) + ' · ' + command.machineId;
    if (command.cmd === 'offer') return `Предложить ${command.itemId} → ${command.buyerId}, от ${format(command.minPrice ?? 0)}`;
    if (command.cmd === 'discard') return 'Утилизировать ' + command.itemId;
    return command.cmd;
  }).join(' / ');
}

function renderLog() {
  const log = currentRun?.log || [];
  const filter = $('log-filter').value;
  const entries = log.filter(entry => filter === 'all' || entry.type === filter).slice(-20).reverse();
  $('log-count').textContent = entries.length ? `Показано ${entries.length} из ${log.length} событий` : 'Нет событий по этому фильтру';
  if (!entries.length) {
    const row = node('tr'), cell = node('td'); cell.colSpan = 5;
    emptyState(cell, currentRun ? 'Событий этого типа пока нет' : 'Первое решение ещё впереди', 'Здесь будет история работы стратегии.', 'log');
    row.append(cell); $('log').replaceChildren(row); return;
  }
  $('log').replaceChildren(...entries.map(entry => {
    const row = node('tr');
    const time = node('td', entry.t); time.append(node('small', '#' + entry.seq));
    const event = node('td');
    const known = Object.hasOwnProperty.call(eventNames, entry.type);
    event.append(node('span', eventNames[entry.type] || entry.type, 'event-badge' + (known ? ' event-' + entry.type : '')));
    const commands = node('td');
    if (entry.commands.length) {
      const details = node('details', '', 'command-summary');
      details.append(node('summary', describeCommands(entry.commands)), node('pre', JSON.stringify(entry.commands, null, 2)));
      commands.append(details);
    } else commands.append(node('span', 'Без новых команд', 'command-summary'));
    const report = node('td');
    const errors = (entry.report || []).filter(result => !result.ok);
    if (errors.length) report.append(node('span', `${errors.length} ошибок: ${errors.map(e => e.reason).join(', ')}`, 'report-error'));
    else if (entry.report?.length) {
      const ok = node('span', '', 'report-ok'); ok.append(icon('check'), node('span', `${entry.report.length} выполнено`)); report.append(ok);
    } else report.append(node('span', '—', 'command-summary'));
    row.append(time, event, commands, report, node('td', `${format(entry.decisionMs, 3)} мс`));
    return row;
  }));
}

function drawChart() {
  const svg = $('chart');
  svg.replaceChildren(); plotPoints = [];
  const history = currentRun?.history?.length ? currentRun.history : samples;
  const lastT = history.length ? history[history.length - 1].t : 0;
  const data = history.filter(point => chartRange === 'all' || point.t >= lastT - 100);
  $('chart-empty').hidden = data.length > 1;
  const left = 47, right = 745, top = 12, bottom = 191;
  const minCash = Math.min(0, ...data.map(point => point.cash));
  const maxCash = Math.max(1, ...data.map(point => point.cash));
  const spread = maxCash - minCash, low = minCash - spread * .08, high = maxCash + spread * .12;
  const first = data.length ? data[0].t : 0, last = data.length ? data[data.length - 1].t : 0;
  for (let i = 0; i <= 4; i++) {
    const y = top + (bottom - top) * i / 4;
    svg.append(svgElement('line', {x1: left, x2: right, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1, 'stroke-dasharray': '3 5'}));
    const value = high - (high - low) * i / 4;
    svg.append(svgElement('text', {x: left - 12, y: y + 3, fill: 'var(--muted)', 'font-size': 9, 'text-anchor': 'end'}, data.length > 1 ? format(value, 0) : '—'));
  }
  for (let i = 0; i <= 5; i++) {
    const x = left + (right - left) * i / 5;
    svg.append(svgElement('text', {x, y: 215, fill: 'var(--muted)', 'font-size': 9, 'text-anchor': 'middle'}, format(first + (last - first) * i / 5, 0)));
  }
  $('chart-span').textContent = `Такт ${first} → ${last}`;
  if (data.length < 2) return;
  const defs = svgElement('defs');
  const gradient = svgElement('linearGradient', {id: 'chart-fill', x1: 0, y1: 0, x2: 0, y2: 1});
  gradient.append(svgElement('stop', {offset: '0%', 'stop-color': '#a4c875', 'stop-opacity': '.25'}), svgElement('stop', {offset: '100%', 'stop-color': '#a4c875', 'stop-opacity': '.01'}));
  defs.append(gradient); svg.append(defs);
  plotPoints = data.map(point => ({...point, x: left + (right - left) * (point.t - first) / Math.max(1, last - first), y: bottom - (bottom - top) * (point.cash - low) / (high - low)}));
  const path = plotPoints.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
  svg.append(svgElement('path', {d: `${path} L${right},${bottom} L${left},${bottom} Z`, fill: 'url(#chart-fill)'}));
  svg.append(svgElement('path', {d: path, fill: 'none', stroke: '#88a854', 'stroke-width': 2.4, 'stroke-linejoin': 'round'}));
  const end = plotPoints[plotPoints.length - 1];
  svg.append(svgElement('circle', {cx: end.x, cy: end.y, r: 4, fill: '#88a854', stroke: 'var(--surface)', 'stroke-width': 2}));
  svg.setAttribute('aria-label', `Баланс с такта ${first} по ${last}: от ${format(data[0].cash)} до ${format(data[data.length - 1].cash)} единиц`);
}

function render(run) {
  currentRun = run;
  $('export').disabled = !run;
  const state = run?.latest?.state;
  const finished = run?.status === 'finished';
  const runStatus = $('run-status');
  runStatus.textContent = !run ? 'Ожидание' : finished ? 'Завершён' : 'В работе';
  runStatus.classList.toggle('live', Boolean(run && !finished));
  if (!state) {
    const defaults = {cash: '—', sold: '—', occupied: '—', latency: '—',
      'capacity-label': '/ — мест', 'free-label': 'С учётом резерва станков',
      'profit-tag': 'Ожидание', 'chart-current': '—', events: '0', 'nav-events': '0',
      'shift-label': run ? 'Ожидаем первое событие' : 'Готов к первой смене',
      'shift-percent': '0%', 'shift-ticks': 'Подключите сервис и запустите прогон',
      'busy-count': '—', 'refused-count': '—', 'invalid-count': '—',
      'buyers-count': '— доступны', 'sales-note': 'Данные появятся в ходе смены'};
    Object.entries(defaults).forEach(([id, value]) => { $(id).textContent = value; });
    $('profit-tag').classList.remove('negative');
    $('shift-progress').style.width = $('storage-progress').style.width = '0%';
    $('event-ring').style.strokeDashoffset = 408.407;
    renderMachines(); renderInventory(); renderBuyers(); renderLog(); drawChart();
    return;
  }
  const t = run.latest.t, horizon = run.horizon;
  const progress = finished ? 100 : Math.min(100, (t + 1) / (horizon + 1) * 100);
  const cash = finished && Number.isFinite(run.result.profit) ? run.result.profit : state.cash;
  const stats = run.stats || {};
  $('cash').textContent = format(cash);
  $('profit-tag').textContent = cash >= 0 ? 'В плюсе' : 'Инвестиции';
  $('profit-tag').classList.toggle('negative', cash < 0);
  $('chart-current').textContent = format(cash) + ' ед.';
  $('sold').textContent = Number.isFinite(run.result.sold ?? stats.sold) ? format(run.result.sold ?? stats.sold, 0) : '—';
  $('sales-note').textContent = finished ? 'итог завершённой смены' : 'подтверждённые продажи';
  $('occupied').textContent = state.storage.capacity - state.storage.free;
  $('capacity-label').textContent = '/ ' + state.storage.capacity + ' мест';
  $('free-label').textContent = `Свободно ${state.storage.free} · в резерве ${state.machines.filter(m => m.busyUntil !== null).length}`;
  const fill = (state.storage.capacity - state.storage.free) / Math.max(1, state.storage.capacity) * 100;
  $('storage-progress').style.width = fill + '%';
  $('storage-progress').parentElement.classList.toggle('full', fill > 85);
  const lastEntry = run.log[run.log.length - 1];
  $('latency').textContent = lastEntry ? format(lastEntry.decisionMs, 3) : '—';
  $('shift-label').textContent = finished ? 'Смена завершена' : 'Смена в процессе';
  $('shift-percent').textContent = format(progress, 0) + '%';
  $('shift-progress').style.width = progress + '%';
  $('shift-ticks').textContent = `Такт ${t} / ${horizon}`;
  $('events').textContent = format(run.events, 0);
  $('nav-events').textContent = run.events > 999 ? format(run.events / 1000, 1) + 'k' : run.events;
  $('event-ring').style.strokeDashoffset = 408.407 * (1 - progress / 100);
  $('busy-count').textContent = state.machines.filter(m => m.busyUntil !== null).length + ' / ' + state.machines.length;
  $('refused-count').textContent = format(run.result.refused ?? stats.refused ?? 0, 0);
  $('invalid-count').textContent = format(run.result.invalidCommands ?? stats.invalidCommands ?? 0, 0);
  if (!samples.length || samples[samples.length - 1].t !== t) samples.push({t, cash});
  else samples[samples.length - 1].cash = cash;
  renderMachines(state.machines, t, finished); renderInventory(); renderBuyers(state.buyers, t, finished); renderLog(); drawChart();
}

async function poll(force = false) {
  if (polling || (paused && !force)) return;
  polling = true;
  try {
    const {runs} = await api('/api/runs');
    if (!userSelected || !runs.some(run => run.runId === selected)) selected = runs.length ? runs[runs.length - 1].runId : '';
    const listKey = JSON.stringify(runs.map(run => run.runId));
    if (runListKey !== listKey) {
      runListKey = listKey;
      $('runs').replaceChildren(...(runs.length ? runs.map(run => {
        const option = node('option', run.runId); option.value = run.runId; return option;
      }) : [Object.assign(node('option', 'Нет активных прогонов'), {value: ''})]));
    }
    $('runs').value = selected;
    $('connection').className = 'connection online';
    $('connection-text').textContent = 'Сервис подключён';
    $('error-banner').hidden = true;
    $('updated').textContent = paused ? 'Обновление приостановлено' : 'Обновлено сейчас';
    if (!selected) { revision = ''; samples = []; render(null); return; }
    const requestedId = selected;
    const run = await api('/api/runs/' + encodeURIComponent(requestedId));
    if (selected !== requestedId) return;
    const nextRevision = `${selected}/${run.latest.seq}/${run.status}`;
    if (revision !== nextRevision) {
      if (currentRun?.runId !== run.runId) samples = [];
      revision = nextRevision;
      render(run);
    }
  } catch (error) {
    $('connection').className = 'connection offline';
    $('connection-text').textContent = 'Нет связи';
    $('updated').textContent = 'Данные не обновляются';
    $('error-banner').textContent = error.name === 'AbortError' ? 'Сервис долго отвечает. Подключение будет проверено автоматически.' : (error instanceof TypeError ? 'Нет связи с заводом. Проверьте, что сервер запущен.' : error.message);
    $('error-banner').hidden = false;
  } finally { polling = false; }
}

$('runs').addEventListener('change', () => { selected = $('runs').value; userSelected = true; revision = ''; samples = []; poll(true); });
$('pause').addEventListener('click', () => {
  paused = !paused;
  $('pause').replaceChildren(icon(paused ? 'play' : 'pause'));
  $('pause').setAttribute('aria-pressed', String(paused));
  $('pause').setAttribute('aria-label', paused ? 'Возобновить обновление панели' : 'Приостановить обновление панели');
  $('pause').title = $('pause').getAttribute('aria-label');
  $('updated').textContent = paused ? 'Обновление приостановлено' : 'Обновление…';
  if (!paused) poll();
});
document.querySelectorAll('[data-kind]').forEach(button => button.addEventListener('click', () => {
  inventoryKind = button.dataset.kind;
  document.querySelectorAll('[data-kind]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); });
  renderInventory();
}));
document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => {
  chartRange = button.dataset.range;
  document.querySelectorAll('[data-range]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); });
  drawChart();
}));
$('log-filter').addEventListener('change', renderLog);
['settings-open', 'settings-quick'].forEach(id => $(id).addEventListener('click', () => $('settings-dialog').showModal()));
$('guide-open').addEventListener('click', () => $('guide-dialog').showModal());
document.querySelectorAll('.dialog-close').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
  const rect = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
}));
$('settings-form').addEventListener('submit', event => { event.preventDefault(); $('settings-dialog').close(); revision = ''; poll(true); });
$('copy-command').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('demo-command').textContent); toast('Команда скопирована. Вставьте её в терминал проекта.'); }
  catch { toast('Выделите команду и скопируйте её вручную.'); }
});
$('export').addEventListener('click', () => {
  if (!currentRun) return;
  const blob = new Blob([JSON.stringify(currentRun, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = 'robomarkets-' + currentRun.runId + '.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('Снимок прогона и последние события экспортированы.');
});
try { if (localStorage.getItem('robomarkets-theme') === 'dark') document.documentElement.dataset.theme = 'dark'; } catch {}
$('theme-toggle').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme-toggle').setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
  try { localStorage.setItem('robomarkets-theme', dark ? 'dark' : 'light'); } catch {}
});
$('chart').addEventListener('pointermove', event => {
  if (!plotPoints.length) return;
  const bounds = $('chart').getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width * 760;
  const point = plotPoints.reduce((closest, next) => Math.abs(next.x - x) < Math.abs(closest.x - x) ? next : closest);
  const tooltip = $('chart-tooltip');
  tooltip.textContent = `Такт ${point.t} · ${format(point.cash)} ед.`;
  tooltip.style.left = Math.min(Math.max(0, event.clientX - bounds.left - 65), Math.max(0, bounds.width - 155)) + 'px';
  tooltip.style.top = Math.max(0, point.y / 225 * bounds.height - 40) + 'px'; tooltip.hidden = false;
});
$('chart').addEventListener('pointerleave', () => { $('chart-tooltip').hidden = true; });
const navigation = [...document.querySelectorAll('.nav-link')];
const observer = new IntersectionObserver(entries => {
  const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
  if (!visible.length) return;
  const id = visible[0].target.id;
  navigation.forEach(link => { const active = link.hash === '#' + id; link.classList.toggle('active', active); if (active) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current'); });
}, {rootMargin: '-8% 0px -55% 0px'});
document.querySelectorAll('.page-section[id]').forEach(section => observer.observe(section));
$('demo-command').textContent = `python3 -m scripts.demo --url ${location.origin} --delay 0.03`;
render(null);
poll();
setInterval(() => { if (!document.hidden) poll(); }, 1500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
