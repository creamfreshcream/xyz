/* Hub front-end: talks to the JSON API, plays streams, renders live state. */

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('not authenticated');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* keep statusText */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

async function logout() {
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/login';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function formatTime(seconds) {
  if (!seconds || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/* ----------------------------------------------------------------- player */

let currentStationId = null;

async function play(stationId, stationName) {
  const audio = document.getElementById('audio');
  const player = document.getElementById('player');
  try {
    // A short-lived token keeps the stream URL usable by the <audio> element,
    // which cannot send an Authorization header.
    const { stream_url: url } = await api('/api/tokens', {
      method: 'POST',
      body: JSON.stringify({ station_id: stationId, ttl_hours: 12 }),
    });
    currentStationId = stationId;
    audio.src = url;
    await audio.play();
    player.hidden = false;
    document.getElementById('player-station').textContent = stationName;
    refreshPlayerTitle();
  } catch (err) {
    alert('Could not start playback: ' + err.message);
  }
}

function stopPlayback() {
  const audio = document.getElementById('audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  currentStationId = null;
  document.getElementById('player').hidden = true;
}

async function refreshPlayerTitle() {
  if (!currentStationId) return;
  try {
    const state = await api(`/api/stations/${currentStationId}/nowplaying`);
    const title = state.track ? `${state.track.artist} — ${state.track.name}` : 'Starting…';
    document.getElementById('player-title').textContent = title;
    const art = document.getElementById('player-art');
    if (state.track) {
      art.src = `/api/artwork/${state.track.id}`;
      art.hidden = false;
    }
  } catch (_) { /* transient */ }
}
setInterval(refreshPlayerTitle, 10000);

async function copyTuner(stationId, button) {
  try {
    const { stream_url: url } = await api('/api/tokens', {
      method: 'POST',
      body: JSON.stringify({ station_id: stationId }),
    });
    await navigator.clipboard.writeText(url);
    const original = button.textContent;
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = original; }, 1500);
  } catch (err) {
    prompt('Tuner URL (copy manually):', err.message);
  }
}

/* -------------------------------------------------------------------- hub */

function stationCard(station) {
  const state = station.now_playing || {};
  const track = state.track;
  const live = state.state === 'playing';
  const progress = track && state.duration_seconds
    ? Math.min(100, (state.elapsed_seconds / state.duration_seconds) * 100) : 0;

  return `
    <article class="card station-card">
      <div class="card-head">
        ${track
          ? `<img class="card-art" src="/api/artwork/${track.id}" alt="" onerror="this.style.visibility='hidden'">`
          : `<div class="card-art"></div>`}
        <div class="card-title">
          <h3><a href="/station/${encodeURIComponent(station.id)}">${escapeHtml(station.name)}</a></h3>
          <p class="card-desc">${escapeHtml(station.description || '')}</p>
        </div>
      </div>
      <div class="now">
        ${track
          ? `<span class="track">${escapeHtml(track.artist)} — ${escapeHtml(track.name)}</span>
             <span class="muted small">${formatTime(state.elapsed_seconds)} / ${formatTime(state.duration_seconds)}</span>
             <span class="progress"><span style="width:${progress}%"></span></span>`
          : `<span class="muted">${live ? 'Starting…' : 'Off air — press play to start the engine'}</span>`}
      </div>
      <div class="tags">
        <span class="pill ${live ? 'live ok' : ''}">${live ? 'On air' : escapeHtml(state.state || 'stopped')}</span>
        <span class="pill">${state.listeners || 0} listening</span>
        <span class="pill">${escapeHtml(station.stream.format.toUpperCase())} ${station.stream.bitrate}k</span>
        ${station.access.visibility === 'public'
          ? '<span class="pill warn">public</span>'
          : '<span class="pill">auth required</span>'}
      </div>
      <div class="card-actions">
        <button class="btn primary" onclick="play('${station.id}', '${escapeHtml(station.name).replace(/'/g, "\\'")}')">▶ Listen</button>
        <button class="btn" onclick="copyTuner('${station.id}', this)">Tuner URL</button>
        <a class="btn ghost" href="/station/${encodeURIComponent(station.id)}">Details</a>
      </div>
    </article>`;
}

async function loadStations() {
  const container = document.getElementById('stations');
  try {
    const stations = await api('/api/stations');
    container.innerHTML = stations.length
      ? stations.map(stationCard).join('')
      : '<p class="muted">No stations yet. Create one under “Stations”.</p>';
  } catch (err) {
    container.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
  }
}

async function loadHealth() {
  const pill = document.getElementById('health');
  if (!pill) return;
  try {
    const health = await api('/api/health');
    const jellyfin = health.jellyfin?.ok;
    const audiomuse = health.audiomuse?.ok;
    pill.className = 'pill ' + (jellyfin ? (audiomuse ? 'ok' : 'warn') : 'err');
    pill.textContent = jellyfin
      ? `Jellyfin ok · AudioMuse ${audiomuse ? 'ok' : 'off'}`
      : 'Jellyfin unreachable';
    pill.title = JSON.stringify(health, null, 2);
  } catch (err) {
    pill.className = 'pill err';
    pill.textContent = 'health unknown';
  }
}

function initHub() {
  loadStations();
  loadHealth();
  setInterval(loadStations, 15000);
}

/* --------------------------------------------------------- station detail */

function transitionLine(transition) {
  if (!transition || !transition.reason) return '<span class="muted">—</span>';
  const badges = [
    transition.beat_matched ? '<span class="pill ok">beat matched</span>' : '',
    transition.key_matched ? '<span class="pill ok">key matched</span>' : '',
    transition.bass_swap ? '<span class="pill">bass swap</span>' : '',
  ].join(' ');
  return `<div class="tags">
      <span class="pill">${transition.overlap_seconds}s ${escapeHtml(transition.curve)}</span>${badges}
    </div>
    <p class="muted small">${escapeHtml(transition.reason)}</p>`;
}

function renderStation(station, state, queue, isAdmin) {
  const track = state.track;
  const progress = track && state.duration_seconds
    ? Math.min(100, (state.elapsed_seconds / state.duration_seconds) * 100) : 0;
  const sources = station.sources.map((source) => {
    const values = source.genres || source.moods || source.artists || source.playlists || source.seeds
      || (source.search ? [source.search] : ['everything']);
    return `<span class="pill">${escapeHtml(source.kind)}: ${escapeHtml(values.join(', '))}</span>`;
  }).join(' ');

  return `
    <div class="detail-head">
      ${track ? `<img class="detail-art" src="/api/artwork/${track.id}" alt="">` : '<div class="detail-art"></div>'}
      <div class="grow">
        <h1>${escapeHtml(station.name)}</h1>
        <p class="muted">${escapeHtml(station.description || '')}</p>
        <div class="tags">${sources}</div>
        <div class="card-actions" style="margin-top:.8rem">
          <button class="btn primary" onclick="play('${station.id}', '${escapeHtml(station.name).replace(/'/g, "\\'")}')">▶ Listen</button>
          <button class="btn" onclick="copyTuner('${station.id}', this)">Tuner URL</button>
          ${isAdmin ? `<button class="btn" onclick="stationAction('${station.id}', 'skip')">Skip</button>
          <button class="btn" onclick="stationAction('${station.id}', 'start')">Start</button>
          <button class="btn" onclick="stationAction('${station.id}', 'stop')">Stop</button>
          <button class="btn" onclick="stationAction('${station.id}', 'refresh')">Refresh pool</button>` : ''}
        </div>
      </div>
    </div>

    <div class="columns">
      <section class="card">
        <h2 style="margin-top:0">On air</h2>
        ${track ? `
          <p class="track"><strong>${escapeHtml(track.artist)}</strong> — ${escapeHtml(track.name)}</p>
          <p class="muted small">${escapeHtml(track.album || '')} ${track.year ? '· ' + track.year : ''}</p>
          <span class="progress"><span style="width:${progress}%"></span></span>
          <p class="muted small">${formatTime(state.elapsed_seconds)} / ${formatTime(state.duration_seconds)} · ${state.listeners} listening</p>
          <dl class="kv">
            <dt>Tempo</dt><dd>${track.analysis.bpm ? track.analysis.bpm.toFixed(0) + ' BPM' : 'unknown'}</dd>
            <dt>Key</dt><dd>${escapeHtml(track.analysis.camelot || track.analysis.key || 'unknown')}</dd>
            <dt>Energy</dt><dd>${track.analysis.energy != null ? track.analysis.energy.toFixed(2) : 'unknown'}</dd>
            <dt>Analysis</dt><dd>${escapeHtml(track.analysis.source)}</dd>
          </dl>
          <h3 style="margin-top:1rem">Last transition</h3>
          ${transitionLine(state.transition)}
        ` : `<p class="muted">Not playing. ${escapeHtml(state.state)}</p>`}
      </section>

      <section class="card">
        <h2 style="margin-top:0">Up next</h2>
        <ol class="queue">
          ${(queue.queue || []).map((entry, index) => `
            <li>
              <span class="idx">${index + 1}</span>
              <span class="grow">
                <strong>${escapeHtml(entry.track.artist)}</strong> — ${escapeHtml(entry.track.name)}
                <br><span class="why">${escapeHtml(entry.reason)}</span>
              </span>
            </li>`).join('') || '<li class="muted">Queue is empty.</li>'}
        </ol>
        <p class="muted small" style="margin-top:.8rem">${queue.pool_size || 0} tracks in this station's pool.</p>
      </section>
    </div>`;
}

async function stationAction(stationId, action) {
  try {
    const result = await api(`/api/stations/${stationId}/${action}`, { method: 'POST' });
    if (action === 'refresh') alert(`Pool refreshed: ${result.tracks} tracks (${result.analysed} analysed).`);
  } catch (err) {
    alert(err.message);
  }
}

function initStation(stationId, role) {
  const container = document.getElementById('station-detail');
  const isAdmin = role === 'admin';

  async function refresh() {
    try {
      const [station, state, queue] = await Promise.all([
        api(`/api/stations/${stationId}`),
        api(`/api/stations/${stationId}/nowplaying`),
        api(`/api/stations/${stationId}/queue`),
      ]);
      container.innerHTML = renderStation(station, state, queue, isAdmin);
    } catch (err) {
      container.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
    }
  }

  refresh();
  // Live updates over SSE, with polling as a fallback.
  const events = new EventSource(`/api/stations/${stationId}/events`);
  events.onmessage = () => refresh();
  events.onerror = () => { events.close(); setInterval(refresh, 10000); };
}

/* ------------------------------------------------------------------ admin */

const VALUE_HINTS = {
  genre: { label: 'Genres', hint: 'Comma separated, e.g. Deep House, Nu Disco.', endpoint: '/api/library/genres' },
  mood: { label: 'Moods', hint: 'AudioMuse mood names, e.g. calm, warm.', endpoint: '/api/library/moods' },
  artist: { label: 'Artists', hint: 'Comma separated artist names.', endpoint: null },
  similar: { label: 'Seed tracks', hint: 'Item ids or "Artist - Title".', endpoint: null },
  library: { label: 'Search term (optional)', hint: 'Leave empty for the whole library.', endpoint: null },
};

async function fillHints(kind) {
  const list = document.getElementById('value-hints');
  const config = VALUE_HINTS[kind];
  list.innerHTML = '';
  if (!config.endpoint) return;
  try {
    const values = await api(config.endpoint);
    list.innerHTML = values.map((value) => `<option value="${escapeHtml(value)}">`).join('');
  } catch (_) { /* hints are optional */ }
}

async function initAdmin() {
  const kindSelect = document.getElementById('kind');
  const templateSelect = document.getElementById('template');
  const note = document.getElementById('template-note');

  const { templates } = await api('/api/templates');
  templateSelect.innerHTML = templates.map((t) => `<option value="${t.key}">${escapeHtml(t.label)}</option>`).join('');
  templateSelect.value = 'radio';

  function showTemplate() {
    const template = templates.find((t) => t.key === templateSelect.value);
    if (!template) return;
    const crossfade = template.crossfade;
    note.innerHTML = `
      <strong>${escapeHtml(template.label)}</strong>
      <p>${escapeHtml(template.description)}</p>
      <dl class="kv">
        <dt>Crossfade</dt><dd>${crossfade.default_seconds}s (${crossfade.min_seconds}–${crossfade.max_seconds}s), ${escapeHtml(crossfade.mode)}</dd>
        <dt>Curve</dt><dd>${escapeHtml(crossfade.curve)}</dd>
        <dt>Beat align</dt><dd>${crossfade.beat_align ? 'yes' : 'no'}</dd>
        <dt>Bass swap</dt><dd>${crossfade.bass_swap ? 'yes' : 'no'}</dd>
        <dt>Flow</dt><dd>${escapeHtml(template.rotation.flow)}</dd>
      </dl>`;
  }

  function showKind() {
    const kind = kindSelect.value;
    const config = VALUE_HINTS[kind];
    document.querySelector('#values-label').firstChild.textContent = config.label + ' ';
    document.getElementById('values-hint').textContent = config.hint;
    document.getElementById('similar-toggle').hidden = kind !== 'artist';
    document.getElementById('values').required = kind !== 'library';
    if (kind === 'mood') templateSelect.value = 'chill';
    if (kind === 'genre') templateSelect.value = 'radio';
    showTemplate();
    fillHints(kind);
  }

  kindSelect.addEventListener('change', showKind);
  templateSelect.addEventListener('change', showTemplate);
  showKind();

  document.getElementById('quick-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const statusEl = document.getElementById('quick-status');
    statusEl.textContent = 'Creating…';
    let overrides = {};
    const rawOverrides = document.getElementById('overrides').value.trim();
    if (rawOverrides) {
      try {
        overrides = JSON.parse(rawOverrides);
      } catch (err) {
        statusEl.textContent = 'Overrides are not valid JSON.';
        return;
      }
    }
    const values = document.getElementById('values').value
      .split(',').map((v) => v.trim()).filter(Boolean);
    try {
      const station = await api('/api/stations/quick', {
        method: 'POST',
        body: JSON.stringify({
          kind: kindSelect.value,
          name: document.getElementById('name').value,
          values,
          template: templateSelect.value,
          include_similar: document.getElementById('include-similar').checked,
          overrides,
        }),
      });
      statusEl.textContent = `Created “${station.name}”.`;
      event.target.reset();
      showKind();
      loadAdminList();
    } catch (err) {
      statusEl.textContent = err.message;
    }
  });

  loadAdminList();
}

async function loadAdminList() {
  const container = document.getElementById('admin-list');
  try {
    const stations = await api('/api/stations');
    container.innerHTML = stations.map((station) => `
      <div class="admin-row">
        <div class="grow">
          <strong><a href="/station/${encodeURIComponent(station.id)}">${escapeHtml(station.name)}</a></strong>
          <div class="sub">${escapeHtml(station.id)} · ${escapeHtml(station.sources.map((s) => s.kind).join(', '))}
            · ${station.crossfade.mode} crossfade · ${escapeHtml(station.access.visibility)}</div>
        </div>
        <span class="pill ${station.enabled ? 'ok' : ''}">${station.enabled ? 'enabled' : 'disabled'}</span>
        <button class="btn small" onclick="toggleStation('${station.id}', ${!station.enabled})">
          ${station.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn small danger" onclick="deleteStation('${station.id}')">Delete</button>
      </div>`).join('') || '<p class="muted">No stations yet.</p>';
  } catch (err) {
    container.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
  }
}

async function toggleStation(stationId, enabled) {
  await api(`/api/stations/${stationId}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
  loadAdminList();
}

async function deleteStation(stationId) {
  if (!confirm(`Delete station “${stationId}”? This cannot be undone.`)) return;
  await api(`/api/stations/${stationId}`, { method: 'DELETE' });
  loadAdminList();
}
