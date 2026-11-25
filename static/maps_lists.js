// Listas de prospectos de Google Maps. Son independientes de las del Directorio.
const el = id => document.getElementById(id);
const root = el('maps-lists');
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const normalize = value => String(value ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const plural = (value, one, many) => `${value.toLocaleString('es-MX')} ${value === 1 ? one : many}`;
const statuses = ['Nuevo', 'Contactado', 'Interesado', 'Cliente', 'Descartado'];
const fields = ['nombre','categoria','calificacion','resenas','telefono','email','direccion','sitio_web','google_maps_url','linkedin_url','lider_nombre','lider_cargo','lider_fuente','lider_confianza','resumen_actividad'];
const selected = new Map();
let results = [], lists = [], activeList = null, database = null;
let writeQueue = Promise.resolve();

// Identifica un negocio para no repetirlo dentro de una lista.
function recordKey(record) {
  const link = record.google_maps_url || '';
  let place = link;
  try {
    const url = new URL(link);
    const id = url.searchParams.get('query_place_id');
    if (id) return `id:${id}`;
    if (url.pathname.includes('/maps/place/')) place = url.pathname;
  } catch { /* Sin enlace válido: se usan los datos del negocio. */ }
  return [record.nombre, place || `${record.direccion || ''}|${record.telefono || ''}`].map(normalize).join('|');
}
function webUrl(value) {
  if (!value) return '';
  try { const url = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
function externalLink(value, label) {
  const url = webUrl(value);
  return url ? `<a class="dir-contact-link" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : '';
}
function notify(message) {
  if (el('maps-list-dialog').open) { el('maps-list-error').textContent = message; el('maps-list-error').hidden = false; }
  el('maps-alert').textContent = message;
  el('maps-alert').hidden = false;
}
function guard(action) {
  return async event => {
    try { await action(event); }
    catch (error) { notify(error.message || 'No se pudo completar la operación. Intenta de nuevo.'); }
  };
}

// ── Almacenamiento ──────────────────────────────────────────
function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(`scrapsa-maps-lists-${root.dataset.owner}`, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('lists', {keyPath:'id'});
    request.onsuccess = () => { database = request.result; database.onversionchange = () => database.close(); resolve(); };
    request.onerror = () => reject(new Error('Este navegador no permite guardar listas. Puedes buscar y exportar resultados.'));
  });
}
function dbRequest(mode, action) {
  if (!database) return Promise.reject(new Error('No se pueden guardar listas en este navegador. Permite el almacenamiento local e intenta de nuevo.'));
  return new Promise((resolve, reject) => {
    const tx = database.transaction('lists', mode);
    const request = action(tx.objectStore('lists'));
    tx.oncomplete = () => resolve(request?.result);
    tx.onabort = tx.onerror = () => reject(new Error('No se pudo guardar la lista. Revisa el espacio disponible o exporta una copia.'));
  });
}
async function refreshLists() {
  lists = (await dbRequest('readonly', store => store.getAll())).sort((a, b) => b.created - a.created);
  el('maps-list-count').textContent = lists.length;
  if (!lists.some(list => list.id === activeList)) activeList = lists[0]?.id || null;
  renderLists();
}
function mutateList(listId, change) {
  const operation = writeQueue.catch(() => {}).then(async () => {
    const current = lists.find(list => list.id === listId);
    if (!current) return;
    const list = structuredClone(current);
    change(list);
    await dbRequest('readwrite', store => store.put(list));
    lists = lists.map(item => item.id === list.id ? list : item);
  });
  writeQueue = operation;
  return operation;
}
const newList = name => ({id: crypto.randomUUID(), name: name.slice(0, 80), created: Date.now(), items: []});

// ── Selección en los resultados ─────────────────────────────
function updateSelection() {
  el('maps-selection').hidden = !selected.size;
  el('maps-selected-count').textContent = selected.size.toLocaleString('es-MX');
  el('maps-selected-label').textContent = selected.size === 1 ? 'seleccionado' : 'seleccionados';
  const boxes = [...document.querySelectorAll('[data-map-index]')];
  // Al reiniciar una búsqueda quedan casillas anteriores sin resultado asociado.
  boxes.forEach(box => { const record = results[box.dataset.mapIndex]; box.checked = !!record && selected.has(recordKey(record)); });
  const all = el('maps-select-all');
  if (!all) return;
  const count = boxes.filter(box => box.checked).length;
  all.checked = !!boxes.length && count === boxes.length;
  all.indeterminate = count > 0 && count < boxes.length;
}
function toggle(box, checked) {
  const record = results[box.dataset.mapIndex];
  if (!record) return;
  checked ? selected.set(recordKey(record), record) : selected.delete(recordKey(record));
}

// ── Agregar a una lista ─────────────────────────────────────
function openAdd() {
  if (!database) throw new Error('Las listas requieren almacenamiento local habilitado en el navegador.');
  if (!selected.size) throw new Error('Selecciona al menos un negocio.');
  el('maps-list-error').hidden = true;
  el('maps-target-list').innerHTML = '<option value="">Crear una lista nueva</option>' + lists.map(list => `<option value="${escape(list.id)}">${escape(list.name)}</option>`).join('');
  el('maps-target-list').value = activeList || '';
  el('maps-new-list-name').value = '';
  el('maps-new-list-name').disabled = !!activeList;
  el('maps-add-description').textContent = `${plural(selected.size, 'negocio seleccionado', 'negocios seleccionados')}. Los negocios repetidos se conservan una sola vez.`;
  el('maps-list-dialog').showModal();
}
async function addToList(event) {
  event.preventDefault();
  const name = el('maps-new-list-name').value.trim();
  let list = lists.find(item => item.id === el('maps-target-list').value);
  if (!list && !name) { el('maps-new-list-name').focus(); throw new Error('Escribe un nombre para la nueva lista.'); }
  list = list ? structuredClone(list) : newList(name);
  const existing = new Set(list.items.map(item => item.key));
  const additions = [...selected].filter(([key]) => !existing.has(key)).map(([key, record]) => ({
    ...Object.fromEntries(fields.map(field => [field, record[field] ?? ''])), key, status: 'Nuevo', note: '',
  }));
  if (list.items.length + additions.length > 2000) throw new Error('Cada lista admite hasta 2,000 negocios. Crea otra lista para continuar.');
  list.items.push(...additions);
  await dbRequest('readwrite', store => store.put(list));
  activeList = list.id;
  await refreshLists();
  el('maps-list-dialog').close();
  selected.clear(); updateSelection();
  notify(`${plural(additions.length, 'negocio agregado', 'negocios agregados')} a «${list.name}».`);
}

// ── Vista de listas ─────────────────────────────────────────
function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}
function exportList(list) {
  const columns = [...fields, 'status', 'note'];
  const headers = ['Negocio','Categoría','Calificación','Reseñas','Teléfono','Email','Dirección','Sitio web','Google Maps','LinkedIn','Posible responsable','Cargo','Fuente del responsable','Confianza','Resumen de reseñas','Seguimiento','Nota'];
  const csv = '﻿' + [headers, ...list.items.map(row => columns.map(key => row[key]))].map(row => row.map(csvCell).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], {type:'text/csv;charset=utf-8'})), link = document.createElement('a');
  link.href = url; link.download = `Scrapsa_Maps_${list.name.replace(/[^\p{L}\p{N}-]/gu, '_').slice(0, 60)}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function renderLists() {
  el('maps-lists-nav').innerHTML = lists.map(list => `<button type="button" data-maps-list="${escape(list.id)}" class="${list.id === activeList ? 'is-active' : ''}" aria-pressed="${list.id === activeList}">${escape(list.name)}<span class="dir-sub">${plural(list.items.length, 'negocio', 'negocios')}</span></button>`).join('');
  const list = lists.find(item => item.id === activeList);
  if (!list) { el('maps-list-content').innerHTML = '<div class="dir-empty"><h3>Aún no tienes listas de Maps.</h3><p>Busca negocios en Maps, selecciónalos y pulsa «Agregar a lista».</p><button type="button" class="dir-btn dir-primary" data-maps-search>Buscar en Maps</button></div>'; return; }
  const filled = list.items.length > 0;
  el('maps-list-content').innerHTML = `<div class="dir-resultsbar"><div><strong>${escape(list.name)}</strong><small>${plural(list.items.length, 'negocio', 'negocios')} · Guardado en este navegador</small></div><div class="dir-actions">${filled ? '<button type="button" class="dir-btn" id="maps-list-export">Exportar CSV</button> ' : ''}<button type="button" class="dir-text-btn" id="maps-list-delete">Eliminar lista</button></div></div>` + (filled
    ? `<div class="dir-searchbar"><input type="search" class="dir-list-note" style="width:100%" id="maps-list-query" aria-label="Buscar en la lista" placeholder="Buscar negocio o nota…"><select id="maps-list-filter" aria-label="Filtrar seguimiento"><option value="">Todo el seguimiento</option>${statuses.map(s => `<option>${s}</option>`).join('')}</select></div><div class="dir-table-wrap"><table class="dir-table"><thead><tr><th>Negocio</th><th>Contacto</th><th>Seguimiento</th><th>Nota</th><th>Quitar</th></tr></thead><tbody id="maps-list-rows"></tbody></table></div>`
    : '<div class="dir-empty"><h3>Esta lista está vacía.</h3><p>Busca negocios en Maps, selecciónalos y pulsa «Agregar a lista».</p><button type="button" class="dir-btn dir-primary" data-maps-search>Buscar en Maps</button></div>');
  el('maps-list-delete').onclick = guard(async () => {
    if (!confirm(`¿Eliminar la lista «${list.name}» y sus notas?`)) return;
    await dbRequest('readwrite', store => store.delete(list.id)); await refreshLists();
  });
  if (!filled) return;
  el('maps-list-export').onclick = guard(async () => { await writeQueue; exportList(lists.find(item => item.id === list.id)); });
  el('maps-list-query').oninput = renderListRows;
  el('maps-list-filter').onchange = renderListRows;
  renderListRows();
}
function renderListRows() {
  const list = lists.find(item => item.id === activeList);
  if (!list) return;
  const q = normalize(el('maps-list-query').value), status = el('maps-list-filter').value;
  // Las filas se identifican por su posición en la lista guardada.
  const rows = list.items.map((row, index) => ({row, index})).filter(({row}) => (!status || row.status === status) && (!q || normalize([row.nombre, row.note, row.direccion, row.email, row.telefono, row.lider_nombre].join(' ')).includes(q)));
  el('maps-list-rows').innerHTML = rows.map(({row: r, index}) => `<tr>
    <td><span class="dir-company">${escape(r.nombre || 'Sin nombre')}</span><span class="dir-sub">${escape([r.categoria, r.direccion].filter(Boolean).join(' · '))}</span>${r.lider_nombre ? `<span class="dir-sub">Posible responsable: ${escape(r.lider_nombre)}${r.lider_cargo ? ` · ${escape(r.lider_cargo)}` : ''}</span>` : ''}</td>
    <td>${[escape(r.telefono), r.email ? `<a class="dir-contact-link" href="mailto:${encodeURIComponent(r.email)}">${escape(r.email)}</a>` : '', externalLink(r.sitio_web, 'Sitio web'), externalLink(r.google_maps_url, 'Google Maps'), externalLink(r.linkedin_url, 'LinkedIn')].filter(Boolean).join('<br>') || '<span class="dir-sub">Sin contacto</span>'}</td>
    <td><select class="dir-list-status" data-maps-status="${index}" aria-label="Seguimiento de ${escape(r.nombre)}">${statuses.map(s => `<option${s === r.status ? ' selected' : ''}>${s}</option>`).join('')}</select></td>
    <td><input class="dir-list-note" data-maps-note="${index}" aria-label="Nota de ${escape(r.nombre)}" value="${escape(r.note)}" maxlength="2000" placeholder="Agregar nota…"></td>
    <td><button type="button" class="dir-close" data-maps-remove="${index}" aria-label="Quitar ${escape(r.nombre)}">✕</button></td>
  </tr>`).join('') || '<tr><td colspan="5" class="dir-empty">No hay negocios para estos filtros.</td></tr>';
}
function switchView(view) {
  const search = view === 'search';
  el('legacy-workspace').classList.toggle('hidden', !search);
  el('maps-lists-view').hidden = search;
  el('maps-alert').hidden = true;
  for (const tab of ['search', 'lists']) { const active = tab === view; el(`maps-tab-${tab}`).classList.toggle('is-active', active); el(`maps-tab-${tab}`).setAttribute('aria-pressed', String(active)); }
  if (!search) renderLists();
}

// ── Eventos ─────────────────────────────────────────────────
document.addEventListener('scrapsa:mode', event => {
  const maps = event.detail === 'maps';
  root.hidden = !maps;
  selected.clear(); results = []; updateSelection();
  if (maps) switchView('search');
});
document.addEventListener('scrapsa:search', () => { selected.clear(); results = []; updateSelection(); el('maps-alert').hidden = true; });
document.addEventListener('scrapsa:maps-rendered', event => { results = event.detail; updateSelection(); });
document.addEventListener('change', event => {
  const input = event.target;
  if (input.matches('[data-map-index]')) { toggle(input, input.checked); updateSelection(); }
  if (input.id === 'maps-select-all') { document.querySelectorAll('[data-map-index]').forEach(box => toggle(box, input.checked)); updateSelection(); }
});
root.addEventListener('click', guard(async event => {
  if (event.target.closest('[data-maps-search]')) return switchView('search');
  const listButton = event.target.closest('[data-maps-list]'); if (listButton) { activeList = listButton.dataset.mapsList; renderLists(); }
  const remove = event.target.closest('[data-maps-remove]');
  if (remove) {
    const key = lists.find(list => list.id === activeList)?.items[remove.dataset.mapsRemove]?.key;
    await mutateList(activeList, list => { list.items = list.items.filter(row => row.key !== key); });
    await refreshLists();
  }
}));
root.addEventListener('change', guard(async event => {
  const input = event.target;
  if (!input.matches('[data-maps-status], [data-maps-note]')) return;
  const index = input.dataset.mapsStatus ?? input.dataset.mapsNote, value = input.value;
  const field = input.dataset.mapsStatus === undefined ? 'note' : 'status';
  const key = lists.find(list => list.id === activeList)?.items[index]?.key;
  await mutateList(activeList, list => {
    const record = list.items.find(row => row.key === key);
    if (record) record[field] = value;
  });
}));
el('maps-tab-search').onclick = () => switchView('search');
el('maps-tab-lists').onclick = () => switchView('lists');
el('maps-add-list').onclick = guard(openAdd);
el('maps-clear-selection').onclick = () => { selected.clear(); updateSelection(); };
el('maps-dialog-close').onclick = () => el('maps-list-dialog').close();
el('maps-list-form').onsubmit = guard(addToList);
el('maps-target-list').onchange = () => { el('maps-new-list-name').disabled = !!el('maps-target-list').value; };
el('maps-new-list').onclick = guard(async () => {
  if (!database) throw new Error('Habilita el almacenamiento local para crear listas.');
  const name = prompt('Nombre de la lista'); if (!name?.trim()) return;
  const list = newList(name.trim());
  await dbRequest('readwrite', store => store.put(list)); activeList = list.id; await refreshLists();
});

openDatabase().then(refreshLists).catch(error => notify(error.message));
