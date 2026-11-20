const el = id => document.getElementById(id);
const root = el('directory-workspace');
const number = value => Number(value).toLocaleString('es-MX');
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const selected = new Map();
const statuses = ['Nuevo', 'Contactado', 'Interesado', 'Cliente', 'Descartado'];
let rows = [], page = 1, total = 0, pages = 1, score = 1, metadata = null;
let controller = null, municipalityController = null, timer = null, lists = [], activeList = null, database = null;
let detailRecord = null, pendingRecords = [], loading = false;
let listWriteQueue = Promise.resolve();

function notify(message) {
  if (el('dir-list-dialog').open) { el('dir-list-error').textContent = message; el('dir-list-error').hidden = false; }
  el('dir-alert').textContent = message;
  el('dir-alert').hidden = false;
}
function guard(action) {
  return async event => {
    try { await action(event); }
    catch (error) { if (error.name !== 'AbortError') notify(error.message || 'No se pudo completar la operación. Intenta de nuevo.'); }
  };
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) throw new Error('Tu sesión terminó. Vuelve a iniciar sesión para consultar el directorio.');
  let body;
  try { body = await response.json(); } catch { throw new Error('No se pudo conectar con el directorio. Intenta de nuevo.'); }
  if (!response.ok) throw new Error(body.error || 'No se pudo completar la consulta.');
  return body;
}
function option(value, label) { return `<option value="${escape(value)}">${escape(label)}</option>`; }
function rating(value) {
  const n = Math.max(1, Math.min(5, Number(value) || 1));
  return `<span class="dir-rating" role="img" aria-label="Calidad ${n} de 5">${[1,2,3,4,5].map(i => `<i class="${i <= n ? 'on' : ''}"></i>`).join('')}</span><span class="dir-quality-value">${n} de 5</span>`;
}
function phoneLink(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  return digits ? `<a class="dir-contact-link" href="tel:${digits.length === 10 ? '+52' : '+'}${digits}">${escape(phone)}</a>` : '';
}
function emailLink(email) {
  return email ? `<a class="dir-contact-link" href="mailto:${encodeURIComponent(email)}" title="${escape(email)}">${escape(email)}</a>` : '';
}
function websiteUrl(value) {
  try { const url = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function setScore(value) {
  score = value;
  el('dir-score').querySelectorAll('button').forEach(button => {
    const active = Number(button.dataset.score) === value;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  el('dir-best').setAttribute('aria-pressed', String(score === 4 && el('dir-phone').checked));
}
function parameters() {
  const params = new URLSearchParams();
  const fields = {'state':'dir-state','municipality':'dir-municipality','sector':'dir-sector','size':'dir-size',
    'q':'dir-query','scope':'dir-scope','postal_code':'dir-postal','sort':'dir-sort'};
  for (const [key, id] of Object.entries(fields)) if (el(id).value.trim()) params.set(key, el(id).value.trim());
  for (const key of ['phone','email','website']) if (el(`dir-${key}`).checked) params.set(key, '1');
  params.set('score', score);
  const activity = el('dir-activity').value.trim();
  if (activity) {
    const code = activity.match(/^\d{6}/)?.[0];
    const match = metadata?.activities.find(item => item[0] === code || normalize(item[1]) === normalize(activity));
    if (match) { params.set('scian', match[0]); el('dir-activity').setCustomValidity(''); }
    else { el('dir-activity').setCustomValidity('Elige una actividad de las sugerencias o escribe su código SCIAN de seis dígitos.'); el('dir-activity').reportValidity(); throw new Error('Selecciona una actividad SCIAN válida para aplicar el filtro.'); }
  } else el('dir-activity').setCustomValidity('');
  if (params.has('postal_code') && !/^\d{1,5}$/.test(params.get('postal_code'))) throw new Error('El código postal debe contener entre uno y cinco números.');
  return params;
}
function updateSelection() {
  el('dir-selection').hidden = !selected.size;
  el('dir-selected-count').textContent = number(selected.size);
  const count = rows.filter(row => selected.has(row.id)).length;
  el('dir-select-page').checked = !!rows.length && count === rows.length;
  el('dir-select-page').indeterminate = count > 0 && count < rows.length;
  root.querySelectorAll('[data-select]').forEach(input => { input.checked = selected.has(Number(input.dataset.select)); });
}
function renderRows() {
  el('dir-rows').innerHTML = rows.length ? rows.map(record => `<tr>
    <td class="dir-check"><input type="checkbox" data-select="${record.id}" aria-label="Seleccionar ${escape(record.name)}" ${selected.has(record.id) ? 'checked' : ''}></td>
    <td><button type="button" class="dir-company" data-detail="${record.id}">${escape(record.name)}</button><span class="dir-sub">${escape(record.activity)}</span></td>
    <td>${escape(record.sector)}<br><span class="dir-badge">${escape(record.size)}</span></td>
    <td>${escape(record.municipality)}<span class="dir-sub">${escape(record.state_name)}</span></td>
    <td>${phoneLink(record.phone)}${emailLink(record.email)}${!record.phone && !record.email ? '<span class="dir-sub">Sin contacto registrado</span>' : ''}</td>
    <td>${rating(record.score)}</td><td><button type="button" class="dir-open" data-detail="${record.id}" aria-label="Ver ficha de ${escape(record.name)}">Ver ficha</button></td>
  </tr>`).join('') : '<tr><td colspan="7" class="dir-empty"><strong>No encontramos empresas con estos filtros.</strong><br>Prueba con otra actividad o amplía la zona de búsqueda.</td></tr>';
  el('dir-total').textContent = number(total);
  el('dir-result-context').textContent = el('dir-state').value ? `en ${metadata?.states.find(s => s.st === el('dir-state').value)?.name || 'el estado seleccionado'}` : 'en todo México';
  el('dir-page').value = page; el('dir-page').max = pages;
  el('dir-pages').textContent = `de ${number(pages)}`;
  el('dir-range').textContent = total ? `${number((page - 1) * 50 + 1)}–${number(Math.min(page * 50, total))} de ${number(total)} empresas` : '0 empresas';
  updateSelection();
}
async function search(newPage = 1) {
  const params = parameters();
  params.set('page', newPage);
  controller?.abort(); controller = new AbortController();
  const current = controller;
  loading = true;
  el('dir-alert').hidden = true;
  el('dir-table-region').setAttribute('aria-busy', 'true');
  el('dir-prev').disabled = true; el('dir-next').disabled = true;
  try {
    const data = await api(`/api/directory/search?${params}`, {signal: current.signal});
    rows = data.results; page = data.page; total = data.total; pages = data.pages;
    renderRows();
  } finally {
    if (current === controller) {
      loading = false;
      el('dir-table-region').setAttribute('aria-busy', 'false');
      el('dir-prev').disabled = page <= 1; el('dir-next').disabled = page >= pages;
    }
  }
}
const schedule = () => { clearTimeout(timer); timer = setTimeout(guard(() => search()), 350); };
async function updateMunicipalities() {
  municipalityController?.abort(); municipalityController = new AbortController();
  const current = municipalityController;
  el('dir-municipality').disabled = true;
  el('dir-municipality').innerHTML = option('', el('dir-state').value ? 'Cargando municipios…' : 'Selecciona un estado');
  if (!el('dir-state').value) return;
  const data = await api(`/api/directory/meta?state=${encodeURIComponent(el('dir-state').value)}`, {signal: current.signal});
  if (current !== municipalityController) return;
  el('dir-municipality').innerHTML = option('', 'Todos los municipios') + data.municipalities.map(m => option(m.name, `${m.name} (${number(m.count)})`)).join('');
  el('dir-municipality').disabled = false;
}
async function detail(id) {
  detailRecord = rows.find(row => row.id === id) || lists.flatMap(list => list.items).find(row => row.id === id) || await api(`/api/directory/business/${id}`);
  const r = detailRecord;
  const web = r.website && websiteUrl(r.website);
  const phone = String(r.phone || '').replace(/\D/g, '');
  const coordinates = r.latitude !== null && r.longitude !== null && Number.isFinite(Number(r.latitude)) && Number.isFinite(Number(r.longitude));
  const map = coordinates ? `https://www.google.com/maps?q=${encodeURIComponent(`${r.latitude},${r.longitude}`)}` : '';
  const fields = [['Razón social', escape(r.legal_name || '—')],['Actividad', `${escape(r.scian)} · ${escape(r.activity)}`],['Sector', escape(r.sector)],['Tamaño', `${escape(r.size)} · ${escape(r.staff)}`],['Dirección',escape(r.address || '—')],['Ubicación',escape(`${r.municipality}, ${r.state_name}`)],['Código postal',escape(r.postal_code || '—')],['Teléfono', phoneLink(r.phone) || '—'],['Email',emailLink(r.email) || '—'],['Sitio web',web ? `<a href="${escape(web)}" target="_blank" rel="noopener noreferrer">${escape(r.website)}</a>` : '—'],['Registro DENUE',escape(r.registered || '—')]];
  el('dir-detail-content').innerHTML = `<h2>${escape(r.name)}</h2><p>${escape(r.activity)}</p>${rating(r.score)}<div class="dir-detail-actions">${phone.length === 10 ? `<a class="dir-btn" href="https://wa.me/52${phone}" target="_blank" rel="noopener noreferrer">WhatsApp</a>` : ''}${map ? `<a class="dir-btn" href="${map}" target="_blank" rel="noopener noreferrer">Google Maps</a>` : ''}<button type="button" class="dir-btn dir-primary" id="dir-detail-add">Agregar a lista</button></div><dl>${fields.map(([label,value]) => `<dt>${label}</dt><dd>${value}</dd>`).join('')}</dl><p>Fuente: DENUE / INEGI. Los datos corresponden al registro del establecimiento y pueden haber cambiado.</p>`;
  el('dir-detail-add').addEventListener('click', guard(() => { el('dir-detail').close(); openAdd([r]); }));
  el('dir-detail').showModal();
}
function saveFile(blob, filename) {
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}
function exportLocal(records, name = 'seleccion') {
  const fields = ['id','name','legal_name','sector','size','staff','state_name','municipality','postal_code','address','phone','email','website','scian','activity','score','registered','status','note'];
  const headers = ['ID','Empresa','Razón social','Sector','Tamaño','Personal','Estado','Municipio','Código postal','Dirección','Teléfono','Email','Sitio web','SCIAN','Actividad','Calidad','Registro DENUE','Seguimiento','Nota'];
  const csv = '\ufeff' + [headers, ...records.map(row => fields.map(key => row[key]))].map(row => row.map(csvCell).join(',')).join('\r\n');
  saveFile(new Blob([csv], {type:'text/csv;charset=utf-8'}), `ScrappSA_${name.replace(/[^\p{L}\p{N}-]/gu, '_').slice(0,60)}.csv`);
}
async function exportResults(format = 'csv') {
  if (loading) throw new Error('Espera a que termine la búsqueda para exportar.');
  if (!total) throw new Error('No hay empresas que exportar.');
  if (total > (metadata?.export_limit || 50000)) throw new Error('Acota tu búsqueda a un máximo de 50,000 empresas para exportar.');
  const params = parameters(); params.set('format', format);
  notify('Preparando la exportación…');
  const response = await fetch(`/api/directory/export?${params}`);
  if (!response.ok) { const data = await response.json(); throw new Error(data.error || 'No se pudo exportar.'); }
  saveFile(await response.blob(), `ScrappSA_directorio.${format}`);
  notify('Exportación lista.');
}
function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(`scrappsa-directory-${root.dataset.owner}`, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('lists', {keyPath:'id'});
    request.onsuccess = () => { database = request.result; database.onversionchange = () => database.close(); resolve(); };
    request.onerror = () => reject(new Error('Este navegador no permite guardar listas. Puedes consultar y exportar empresas.'));
    request.onblocked = () => reject(new Error('Cierra otras pestañas de ScrappSA para habilitar las listas.'));
  });
}
function dbRequest(mode, action) {
  if (!database) return Promise.reject(new Error('No se pueden guardar listas en este navegador. Permite el almacenamiento local e intenta de nuevo.'));
  return new Promise((resolve,reject) => {
    const tx = database.transaction('lists', mode);
    const request = action(tx.objectStore('lists'));
    tx.oncomplete = () => resolve(request?.result);
    tx.onabort = tx.onerror = () => reject(new Error('No se pudo guardar la lista. Revisa el espacio disponible o exporta una copia.'));
  });
}
async function refreshLists() {
  lists = (await dbRequest('readonly', store => store.getAll())).sort((a,b) => b.created - a.created);
  el('dir-list-count').textContent = lists.length;
  if (!lists.some(list => list.id === activeList)) activeList = lists[0]?.id || null;
  renderLists();
}
function mutateList(listId, change) {
  const operation = listWriteQueue.catch(() => {}).then(async () => {
    const current = lists.find(list => list.id === listId);
    if (!current) return;
    const list = structuredClone(current);
    change(list);
    await dbRequest('readwrite', store => store.put(list));
    lists = lists.map(item => item.id === list.id ? list : item);
  });
  listWriteQueue = operation;
  return operation;
}
function openAdd(records) {
  if (!database) throw new Error('Las listas requieren almacenamiento local habilitado en el navegador.');
  if (!records.length) throw new Error('Selecciona al menos una empresa.');
  pendingRecords = records;
  el('dir-list-error').hidden = true;
  el('dir-target-list').innerHTML = option('', 'Crear una lista nueva') + lists.map(list => option(list.id, list.name)).join('');
  el('dir-new-list-name').value = ''; el('dir-new-list-name').disabled = false;
  el('dir-add-description').textContent = `${number(records.length)} empresas seleccionadas. Las empresas repetidas se conservan una sola vez.`;
  el('dir-list-dialog').showModal();
}
async function addToList(event) {
  event.preventDefault();
  const name = el('dir-new-list-name').value.trim();
  let list = lists.find(list => list.id === el('dir-target-list').value);
  if (!list && !name) { el('dir-new-list-name').focus(); throw new Error('Escribe un nombre para la nueva lista.'); }
  list = list ? structuredClone(list) : {id: crypto.randomUUID(), name, created:Date.now(), items:[]};
  const existing = new Set(list.items.map(item => item.id));
  const additions = pendingRecords.filter(row => !existing.has(row.id)).map(row => ({...row, status:'Nuevo', note:''}));
  if (list.items.length + additions.length > 2000) throw new Error('Cada lista admite hasta 2,000 empresas. Crea otra lista para continuar.');
  list.items.push(...additions);
  await dbRequest('readwrite', store => store.put(list));
  activeList = list.id;
  await refreshLists();
  el('dir-list-dialog').close();
  notify(`${number(additions.length)} empresas agregadas a «${list.name}».`);
}
function renderLists() {
  el('dir-lists-nav').innerHTML = lists.map(list => `<button type="button" data-list="${escape(list.id)}" class="${list.id === activeList ? 'is-active' : ''}" aria-pressed="${list.id === activeList}">${escape(list.name)}<span class="dir-sub">${number(list.items.length)} empresas</span></button>`).join('');
  const list = lists.find(list => list.id === activeList);
  if (!list) { el('dir-list-content').innerHTML = '<div class="dir-empty"><h3>Tus oportunidades, en un solo lugar.</h3><p>Selecciona empresas en el directorio y agrégalas a una lista.<br>Aquí podrás darles seguimiento y guardar notas.</p></div>'; return; }
  el('dir-list-content').innerHTML = `<div class="dir-resultsbar"><div><strong>${escape(list.name)}</strong><small>${number(list.items.length)} empresas · Guardado en este navegador</small></div><div class="dir-actions"><button type="button" class="dir-btn" id="dir-list-export">Exportar CSV</button><button type="button" class="dir-text-btn" id="dir-list-delete">Eliminar lista</button></div></div><div class="dir-searchbar"><input type="search" class="dir-list-note" style="width:100%" id="dir-list-query" aria-label="Buscar en la lista" placeholder="Buscar empresa o nota…"><select id="dir-list-filter" aria-label="Filtrar seguimiento">${option('','Todo el seguimiento') + statuses.map(s => option(s,s)).join('')}</select></div><div class="dir-table-wrap"><table class="dir-table"><thead><tr><th>Empresa</th><th>Contacto</th><th>Seguimiento</th><th>Nota</th><th>Quitar</th></tr></thead><tbody id="dir-list-rows"></tbody></table></div>`;
  el('dir-list-export').onclick = guard(async () => { await listWriteQueue; const saved = lists.find(item => item.id === list.id); exportLocal(saved.items, saved.name); });
  el('dir-list-delete').onclick = guard(async () => {
    if (!confirm(`¿Eliminar la lista «${list.name}» y sus notas?`)) return;
    await dbRequest('readwrite', store => store.delete(list.id)); await refreshLists();
  });
  el('dir-list-query').oninput = renderListRows;
  el('dir-list-filter').onchange = renderListRows;
  renderListRows();
}
function renderListRows() {
  const list = lists.find(list => list.id === activeList);
  if (!list) return;
  const q = normalize(el('dir-list-query').value), status = el('dir-list-filter').value;
  const records = list.items.filter(row => (!status || row.status === status) && (!q || normalize([row.name,row.note,row.municipality,row.email,row.phone].join(' ')).includes(q)));
  el('dir-list-rows').innerHTML = records.map(r => `<tr><td><button type="button" class="dir-company" data-detail="${r.id}">${escape(r.name)}</button><span class="dir-sub">${escape(r.municipality)}, ${escape(r.state_name)}</span></td><td>${phoneLink(r.phone)}${emailLink(r.email)}</td><td><select class="dir-list-status" data-status="${r.id}" aria-label="Seguimiento de ${escape(r.name)}">${statuses.map(s => `<option${s === r.status ? ' selected' : ''}>${s}</option>`).join('')}</select></td><td><input class="dir-list-note" data-note="${r.id}" aria-label="Nota de ${escape(r.name)}" value="${escape(r.note)}" maxlength="2000" placeholder="Agregar nota…"></td><td><button type="button" class="dir-close" data-remove="${r.id}" aria-label="Quitar ${escape(r.name)}">✕</button></td></tr>`).join('') || '<tr><td colspan="5" class="dir-empty">No hay empresas para estos filtros.</td></tr>';
}
function switchView(view) {
  const search = view === 'search';
  el('dir-search-view').hidden = !search; el('dir-lists-view').hidden = search;
  for (const tab of ['search','lists']) { const active = tab === view; el(`dir-tab-${tab}`).classList.toggle('is-active',active); el(`dir-tab-${tab}`).setAttribute('aria-pressed',String(active)); }
  if (!search) renderLists();
}

el('dir-search-form').addEventListener('submit', guard(event => { event.preventDefault(); clearTimeout(timer); return search(); }));
el('dir-query').addEventListener('input', schedule);
el('dir-postal').addEventListener('input', schedule);
el('dir-activity').addEventListener('input', () => el('dir-activity').setCustomValidity(''));
el('dir-activity').addEventListener('change', guard(() => search()));
for (const id of ['municipality','sector','size','phone','email','website','sort','scope']) el(`dir-${id}`).addEventListener('change', guard(() => { el('dir-best').setAttribute('aria-pressed', String(score === 4 && el('dir-phone').checked)); return search(); }));
el('dir-state').addEventListener('change', guard(async () => { const promise = updateMunicipalities(); await Promise.all([promise, search()]); }));
el('dir-score').addEventListener('click', guard(event => { const button = event.target.closest('[data-score]'); if (!button) return; setScore(Number(button.dataset.score)); return search(); }));
el('dir-best').onclick = guard(() => { setScore(4); el('dir-phone').checked = true; el('dir-best').setAttribute('aria-pressed','true'); return search(); });
root.querySelectorAll('[data-query]').forEach(button => button.onclick = guard(() => { el('dir-query').value = button.dataset.query; return search(); }));
el('dir-reset').onclick = guard(async () => {
  clearTimeout(timer); municipalityController?.abort();
  for (const id of ['query','state','sector','size','postal','activity']) el(`dir-${id}`).value = '';
  el('dir-activity').setCustomValidity('');
  el('dir-scope').value = 'all'; el('dir-sort').value = 'score';
  for (const id of ['phone','email','website']) el(`dir-${id}`).checked = false;
  setScore(1); await updateMunicipalities(); await search();
});
el('dir-prev').onclick = guard(() => search(page - 1));
el('dir-next').onclick = guard(() => search(page + 1));
el('dir-page').onchange = guard(() => search(Math.max(1, Math.min(pages, Number(el('dir-page').value) || 1))));
el('dir-select-page').onchange = guard(event => {
  if (event.target.checked && selected.size + rows.filter(row => !selected.has(row.id)).length > 2000) { updateSelection(); throw new Error('Puedes seleccionar hasta 2,000 empresas a la vez.'); }
  rows.forEach(row => event.target.checked ? selected.set(row.id,row) : selected.delete(row.id)); updateSelection();
});
root.addEventListener('click', guard(async event => {
  const detailButton = event.target.closest('[data-detail]');
  if (detailButton) await detail(Number(detailButton.dataset.detail));
  const close = event.target.closest('[data-close]'); if (close) el(close.dataset.close).close();
  const listButton = event.target.closest('[data-list]'); if (listButton) { activeList = listButton.dataset.list; renderLists(); }
  const remove = event.target.closest('[data-remove]');
  if (remove) {
    const id = Number(remove.dataset.remove);
    await mutateList(activeList, list => { list.items = list.items.filter(row => row.id !== id); });
    await refreshLists();
  }
}));
root.addEventListener('change', guard(async event => {
  const input = event.target;
  if (input.matches('[data-select]')) {
    const id = Number(input.dataset.select);
    if (input.checked && selected.size >= 2000) { input.checked = false; throw new Error('Puedes seleccionar hasta 2,000 empresas a la vez.'); }
    input.checked ? selected.set(id, rows.find(row => row.id === id)) : selected.delete(id); updateSelection();
  }
  if (input.matches('[data-status], [data-note]')) {
    const id = Number(input.dataset.status || input.dataset.note), value = input.value;
    const field = input.dataset.status ? 'status' : 'note';
    await mutateList(activeList, list => {
      const record = list.items.find(row => row.id === id);
      if (record) record[field] = value;
    });
  }
}));
el('dir-clear-selection').onclick = () => { selected.clear(); updateSelection(); };
el('dir-add-list').onclick = guard(() => openAdd([...selected.values()]));
el('dir-export-selected').onclick = () => exportLocal([...selected.values()]);
el('dir-export').onclick = guard(() => exportResults());
el('dir-export-json').onclick = guard(() => exportResults('json'));
el('dir-tab-search').onclick = () => switchView('search');
el('dir-tab-lists').onclick = () => switchView('lists');
el('dir-list-form').onsubmit = guard(addToList);
el('dir-target-list').onchange = () => { el('dir-new-list-name').disabled = !!el('dir-target-list').value; };
el('dir-new-list').onclick = guard(async () => {
  if (!database) throw new Error('Habilita el almacenamiento local para crear listas.');
  const name = prompt('Nombre de la lista'); if (!name?.trim()) return;
  const list = {id:crypto.randomUUID(),name:name.trim().slice(0,80),created:Date.now(),items:[]};
  await dbRequest('readwrite', store => store.put(list)); activeList = list.id; await refreshLists();
});

async function initialize() {
  metadata = await api('/api/directory/meta');
  el('dir-catalogue-count').textContent = `${number(metadata.total)} empresas · ${metadata.states.length} estados`;
  el('dir-state').innerHTML = option('', 'Todo México') + metadata.states.map(s => option(s.st, `${s.name} (${number(s.n)})`)).join('');
  el('dir-sector').innerHTML = option('', 'Todos los sectores') + metadata.sectors.map(s => option(s,s)).join('');
  el('dir-activities').innerHTML = metadata.activities.map(([code,name]) => option(`${code} · ${name}`, name)).join('');
  await search();
}
openDatabase().then(refreshLists).catch(error => notify(error.message));
initialize().catch(error => {
  el('dir-table-region').setAttribute('aria-busy','false');
  el('dir-catalogue-count').textContent = 'Catálogo no disponible';
  el('dir-rows').innerHTML = '<tr><td colspan="7" class="dir-empty">No se pudo cargar el directorio. Intenta de nuevo con el botón Buscar.</td></tr>';
  notify(error.message);
});
