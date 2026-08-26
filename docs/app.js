/* Foodie Map — static site. All data is pre-baked at build time; the only
   network calls made from the browser are the Google Maps JS bundle and, when
   you save a note, the GitHub Contents API. */

const S = {
  config: null,
  restaurants: [],
  notes: {},          // committed notes, as loaded from notes.json
  drafts: {},         // unsaved edits, mirrored to localStorage
  markers: new Map(),
  map: null,
  selected: null,
  filters: { q: '', minRating: 0, includeUnrated: true, cats: new Set() },
};

const DRAFT_KEY = 'foodiemap.drafts';
const TOKEN_KEY = 'foodiemap.ghtoken';
const $ = (id) => document.getElementById(id);

/* ---------- helpers ---------- */

/* Rating encoding. Three channels doing three different jobs:
     shape  — a star marks 愛店 (>= 4.6), a named category rather than "more"
     colour — 4.3 / 4.4 / 4.5 get three widely-separated steps of one hue, so
              the step IS the score (1:1, not a bucket) in the only band where
              telling two restaurants apart actually matters
     recede — <= 4.2 goes flat grey and stops competing for attention
   Steps validated for monotone lightness, adjacent gap, surface contrast and
   CVD separation in both themes. */
function ratingTier(r) {
  if (r == null) return 't-none';
  if (r >= 4.6) return 't-star';
  if (r >= 4.5) return 't45';
  if (r >= 4.4) return 't44';
  if (r >= 4.3) return 't43';
  return 't-low';
}
const TIER_VAR = {
  't-star': '--r-star', 't45': '--r45', 't44': '--r44',
  't43': '--r43', 't-low': '--r-low', 't-none': '--r-none',
};
const ratingClass = (r) => ratingTier(r);
const ratingColor = (r) =>
  getComputedStyle(document.body).getPropertyValue(TIER_VAR[ratingTier(r)]).trim() || '#9a958c';

// Five-point star, outer radius 10, centred on the anchor point.
const STAR_PATH =
  'M0.00,-10.00L2.59,-3.56L9.51,-3.09L4.18,1.36L5.88,8.09' +
  'L0.00,4.40L-5.88,8.09L-4.18,1.36L-9.51,-3.09L-2.59,-3.56Z';

function priceLabel(r) {
  if (r.price_min == null) return null;
  return r.price_min === r.price_max
    ? `人均 ${r.price_min} 元`
    : `人均 ${r.price_min}–${r.price_max} 元`;
}

const fmtDate = (iso) => (iso ? iso.slice(0, 10) : '');

function mapsUrl(r) {
  // place_id gives an exact match; the address is the fallback when geocoding
  // returned coordinates but no id.
  if (r.place_id) {
    return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
      r.name || '')}&query_place_id=${encodeURIComponent(r.place_id)}`;
  }
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
    r.address || r.name || '')}`;
}

const noteText = (id) => (id in S.drafts ? S.drafts[id] : (S.notes[id]?.text ?? ''));
const hasNote = (id) => noteText(id).trim().length > 0;

/* ---------- filtering ---------- */

function visible() {
  const { q, minRating, includeUnrated, cats } = S.filters;
  const needle = q.trim().toLowerCase();
  return S.restaurants.filter((r) => {
    if (r.rating == null) {
      if (!includeUnrated) return false;
    } else if (r.rating < minRating) return false;

    if (cats.size && !r.categories.some((c) => cats.has(c))) return false;

    if (needle) {
      const hay = `${r.name || ''} ${r.location_tag || ''} ${r.address || ''}`.toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });
}

function applyFilters() {
  const rows = visible();
  const shown = new Set(rows.map((r) => r.id));
  renderList(rows);

  let pinned = 0;
  for (const [id, marker] of S.markers) {
    const on = shown.has(id);
    marker.setMap(on ? S.map : null);
    if (on) pinned++;
  }

  const unlocated = rows.length - pinned;
  $('count').textContent =
    `${rows.length} 間餐廳` +
    (unlocated > 0 ? `（${unlocated} 間無座標，僅列表顯示）` : '');
}

/* ---------- list ---------- */

function renderList(rows) {
  const ul = $('list');
  ul.textContent = '';
  for (const r of rows) {
    const li = document.createElement('li');
    li.className = 'card' + (S.selected === r.id ? ' active' : '');
    li.dataset.id = r.id;

    const top = document.createElement('div');
    top.className = 'card-top';
    const h = document.createElement('h3');
    h.textContent = r.name || '(無店名)';
    const score = document.createElement('span');
    score.className = `score ${ratingClass(r.rating)}`;
    score.textContent = r.rating == null ? '—' : r.rating.toFixed(1);
    top.append(h, score);

    const meta = document.createElement('div');
    meta.className = 'card-meta';
    const bits = [r.categories.slice(0, 3).join('・'), priceLabel(r), `${r.visits}訪`]
      .filter(Boolean);
    meta.textContent = bits.join(' · ');
    if (hasNote(r.id)) {
      const dot = document.createElement('span');
      dot.className = 'has-note';
      dot.textContent = ' ✎';
      meta.append(dot);
    }

    li.append(top, meta);
    li.addEventListener('click', () => select(r.id, true));
    ul.append(li);
  }
}

/* ---------- detail panel ---------- */

function select(id, pan) {
  S.selected = id;
  const r = S.restaurants.find((x) => x.id === id);
  if (!r) return;
  const u = new URL(location.href);
  u.searchParams.set('r', id);
  history.replaceState(null, '', u);
  renderDetail(r);
  renderList(visible());
  if (pan && S.map && r.lat != null) {
    S.map.panTo({ lat: r.lat, lng: r.lng });
    if (S.map.getZoom() < 15) S.map.setZoom(16);
  }
}

function closeDetail() {
  S.selected = null;
  const u = new URL(location.href);
  u.searchParams.delete('r');
  history.replaceState(null, '', u);
  $('detail').hidden = true;
  renderList(visible());
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function renderDetail(r) {
  const d = $('detail');
  d.hidden = false;
  d.scrollTop = 0;
  d.textContent = '';

  const close = el('button', 'close', '×');
  close.setAttribute('aria-label', '關閉');
  close.addEventListener('click', closeDetail);
  d.append(close, el('h2', null, r.name || '(無店名)'));
  if (r.tagline) d.append(el('p', 'tagline', r.tagline));

  const stats = el('div', 'stats');
  const stat = (label, value) => {
    const s = el('div', 'stat');
    s.append(el('b', null, label), document.createTextNode(value));
    return s;
  };
  stats.append(
    stat('最新評分', r.rating == null ? '未評分' : `${r.rating.toFixed(1)} / 5`),
    stat('造訪次數', `${r.visits} 次`),
    stat('價位', priceLabel(r) || '未提供'),
    stat('最近一次', fmtDate(r.last_visit) || '—'),
  );
  d.append(stats);

  if (r.categories.length) {
    const chips = el('div', 'chips');
    r.categories.forEach((c) => chips.append(el('span', 'chip', c)));
    d.append(chips);
  }

  d.append(el('div', 'addr', r.address || '（尚未取得地址）'));
  const row = el('div', 'btn-row');
  const gm = el('a', 'primary', '在 Google 地圖開啟');
  gm.href = mapsUrl(r);
  gm.target = '_blank';
  gm.rel = 'noopener';
  row.append(gm);
  if (r.posts[0]?.url) {
    const ig = el('a', 'ghost', '最新貼文');
    ig.href = r.posts[0].url;
    ig.target = '_blank';
    ig.rel = 'noopener';
    row.append(ig);
  }
  d.append(row);

  /* --- personal note --- */
  d.append(el('div', 'section-title', '我的筆記'));
  const ta = el('textarea');
  ta.id = 'note';
  ta.value = noteText(r.id);
  ta.placeholder = '想點什麼、跟誰去、訂位電話…';
  d.append(ta);

  const nrow = el('div', 'btn-row');
  const save = el('button', 'primary', '儲存筆記');
  const status = el('span', null, '');
  status.id = 'note-status';
  nrow.append(save, status);
  d.append(nrow);

  ta.addEventListener('input', () => {
    S.drafts[r.id] = ta.value;
    localStorage.setItem(DRAFT_KEY, JSON.stringify(S.drafts));
    status.className = '';
    status.textContent = '尚未儲存';
  });
  save.addEventListener('click', () => saveNote(r.id, ta.value, save, status));

  if (S.notes[r.id]?.updated) {
    d.append(el('div', 'post-date', `上次儲存 ${fmtDate(S.notes[r.id].updated)}`));
  }

  /* --- posts --- */
  d.append(el('div', 'section-title', `貼文（${r.posts.length}）`));
  for (const p of r.posts) {
    const box = el('div', 'post');
    const head = el('div', 'post-head');
    const left = el('span', 'post-date', fmtDate(p.timestamp));
    const right = el('span', `score ${ratingClass(p.rating)}`,
      p.rating == null ? '' : `${p.rating.toFixed(1)}${p.visits ? ` · ${p.visits}訪` : ''}`);
    head.append(left, right);
    box.append(head);

    if (p.tagline) box.append(el('p', 'post-tagline', p.tagline));

    if (p.dishes?.length) {
      const ul = el('ul', 'dishes');
      for (const dish of p.dishes) {
        const li = el('li', dish.favourite ? 'fav' : null, dish.name);
        ul.append(li);
      }
      box.append(ul);
    }

    if (p.caption) {
      const det = el('details');
      det.append(el('summary', null, '完整貼文內容'));
      det.append(el('p', 'caption', p.caption));
      box.append(det);
    }

    if (p.url) {
      const a = el('a', null, 'Instagram 原文 ↗');
      a.href = p.url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.style.fontSize = '12.5px';
      box.append(a);
    }
    d.append(box);
  }
}

/* ---------- notes: commit to GitHub ---------- */

function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  return btoa(bin);
}

function b64decode(b64) {
  const bin = atob(b64.replace(/\n/g, ''));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

function askToken() {
  const { owner, name } = S.config.repo;
  $('token-repo').textContent = `${owner}/${name}`;
  $('token-modal').hidden = false;
  $('token-input').value = '';
  $('token-input').focus();
  return new Promise((resolve) => {
    const done = (val) => {
      $('token-modal').hidden = true;
      $('token-save').onclick = null;
      $('token-cancel').onclick = null;
      resolve(val);
    };
    $('token-save').onclick = () => {
      const v = $('token-input').value.trim();
      if (!v) return;
      localStorage.setItem(TOKEN_KEY, v);
      done(v);
    };
    $('token-cancel').onclick = () => done('');
  });
}

async function gh(path, token, options = {}) {
  const { owner, name } = S.config.repo;
  const res = await fetch(`https://api.github.com/repos/${owner}/${name}/contents/${path}`, {
    ...options,
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: `Bearer ${token}`,
      'X-GitHub-Api-Version': '2022-11-28',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  return res;
}

async function saveNote(id, text, btn, status) {
  let token = getToken();
  if (!token) token = await askToken();
  if (!token) return;

  const { notesPath, branch } = S.config.repo;
  btn.disabled = true;
  status.className = '';
  status.textContent = '儲存中…';

  try {
    // Re-read before writing: another device may have saved a note since this
    // page loaded, and a blind PUT would drop it.
    const cur = await gh(notesPath, token);
    let sha, remote = {};
    if (cur.status === 200) {
      const j = await cur.json();
      sha = j.sha;
      remote = JSON.parse(b64decode(j.content) || '{}');
    } else if (cur.status !== 404) {
      throw new Error(`讀取 notes.json 失敗（${cur.status}）`);
    }

    const trimmed = text.trim();
    if (trimmed) {
      remote[id] = { text: trimmed, updated: new Date().toISOString() };
    } else {
      delete remote[id];
    }

    const body = {
      message: `notes: update ${id}`,
      content: b64encode(JSON.stringify(remote, null, 2) + '\n'),
      branch,
      ...(sha ? { sha } : {}),
    };
    const put = await gh(notesPath, token, { method: 'PUT', body: JSON.stringify(body) });

    if (put.status === 401 || put.status === 403) {
      localStorage.removeItem(TOKEN_KEY);
      throw new Error('Token 無效或權限不足，已清除，請重新輸入');
    }
    if (put.status === 409) throw new Error('版本衝突，請再按一次儲存');
    if (!put.ok) throw new Error(`儲存失敗（${put.status}）`);

    S.notes = remote;
    delete S.drafts[id];
    localStorage.setItem(DRAFT_KEY, JSON.stringify(S.drafts));
    status.textContent = '已儲存到 GitHub';
    renderList(visible());
  } catch (err) {
    status.className = 'err';
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- map ---------- */

function markerIcon(r) {
  const tier = ratingTier(r.rating);
  const stroke = getComputedStyle(document.body).getPropertyValue('--bg').trim() || '#ffffff';
  if (tier === 't-star') {
    return {
      path: STAR_PATH,
      scale: 0.85,
      fillColor: ratingColor(r.rating),
      fillOpacity: 1,
      strokeColor: stroke,
      strokeWeight: 1.6,
    };
  }
  return {
    path: google.maps.SymbolPath.CIRCLE,
    // Low-rated pins are smaller as well as greyer — two ways of receding.
    scale: tier === 't-low' || tier === 't-none' ? 5 : 7,
    fillColor: ratingColor(r.rating),
    fillOpacity: tier === 't-low' || tier === 't-none' ? 0.8 : 0.95,
    strokeColor: stroke,
    strokeWeight: 1.8,
  };
}

/* A muted basemap. Google's default styling puts a coloured icon on every
   restaurant in Taipei, which is exactly what our own pins have to compete
   with — so desaturate the base, lighten it, and turn off the business POIs
   entirely. Inline `styles` only works on a map with no mapId, which is why we
   don't set one. */
function mapStyles(dark) {
  const base = dark
    ? [
        { elementType: 'geometry', stylers: [{ color: '#22222a' }] },
        { elementType: 'labels.text.fill', stylers: [{ color: '#7f7f8c' }] },
        { elementType: 'labels.text.stroke', stylers: [{ color: '#17171a' }] },
        { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#2c2c35' }] },
        { featureType: 'road.arterial', elementType: 'geometry', stylers: [{ color: '#32323d' }] },
        { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#1b2430' }] },
        { featureType: 'landscape.natural', elementType: 'geometry', stylers: [{ color: '#1f2620' }] },
      ]
    : [
        { elementType: 'geometry', stylers: [{ saturation: -70 }, { lightness: 35 }] },
        { elementType: 'labels.text.fill', stylers: [{ color: '#9c968c' }] },
        { elementType: 'labels.text.stroke', stylers: [{ color: '#ffffff' }, { weight: 3 }] },
        { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#ffffff' }] },
        { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#e8eef2' }] },
        { featureType: 'landscape', elementType: 'geometry', stylers: [{ color: '#f6f5f2' }] },
      ];

  return base.concat([
    // The main source of visual noise: Google's own restaurant/shop pins.
    { featureType: 'poi.business', stylers: [{ visibility: 'off' }] },
    { featureType: 'poi', elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
    { featureType: 'transit', elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
    { featureType: 'road', elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
  ]);
}

const LABEL_ZOOM = 15;  // below this, name labels on 600+ pins are unreadable

function markerLabel(r, zoom) {
  if (zoom < LABEL_ZOOM) return null;
  return {
    text: r.name || '',
    fontSize: '12px',
    fontWeight: '600',
    color: getComputedStyle(document.body).getPropertyValue('--text').trim() || '#1c1b19',
    className: 'pin-label',
  };
}

function initMap() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)');
  S.map = new google.maps.Map($('map'), {
    center: { lat: 25.038, lng: 121.547 },   // Taipei
    zoom: 13,
    mapTypeControl: false,
    streetViewControl: false,
    fullscreenControl: false,
    clickableIcons: false,
    styles: mapStyles(dark.matches),
  });
  dark.addEventListener('change', (e) => {
    S.map.setOptions({ styles: mapStyles(e.matches) });
    // Tier colours are theme tokens, so the markers have to be repainted too.
    for (const [id, m] of S.markers) {
      const r = S.restaurants.find((x) => x.id === id);
      if (r) m.setIcon(markerIcon(r));
    }
  });

  // Names appear only once you're zoomed in enough for them not to overlap.
  S.map.addListener('zoom_changed', () => {
    const z = S.map.getZoom();
    for (const [id, m] of S.markers) {
      const r = S.restaurants.find((x) => x.id === id);
      m.setLabel(markerLabel(r, z));
    }
  });

  for (const r of S.restaurants) {
    if (r.lat == null) continue;
    const m = new google.maps.Marker({
      position: { lat: r.lat, lng: r.lng },
      map: S.map,
      title: `${r.name}${r.rating != null ? ` · ${r.rating}` : ''}`,
      icon: markerIcon(r),
      label: markerLabel(r, 13),
    });
    m.addListener('click', () => select(r.id, true));
    S.markers.set(r.id, m);
  }
  applyFilters();

  if (S.selected) {
    const r = S.restaurants.find((x) => x.id === S.selected);
    if (r?.lat != null) { S.map.setCenter({ lat: r.lat, lng: r.lng }); S.map.setZoom(16); }
  }
}

function loadMaps(key) {
  return new Promise((resolve, reject) => {
    window.__initMap = () => resolve();
    const s = document.createElement('script');
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}` +
            `&callback=__initMap&loading=async&language=zh-TW&region=TW`;
    s.async = true;
    s.onerror = () => reject(new Error('Google Maps 載入失敗'));
    document.head.append(s);
  });
}

/* ---------- filter wiring ---------- */

function renderCategories(cats) {
  const box = $('categories');
  box.textContent = '';
  for (const c of cats) {
    const b = el('button', 'cat', c);
    b.type = 'button';
    b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', () => {
      if (S.filters.cats.has(c)) S.filters.cats.delete(c);
      else S.filters.cats.add(c);
      b.setAttribute('aria-pressed', String(S.filters.cats.has(c)));
      applyFilters();
    });
    box.append(b);
  }
}

function renderRatingLegend() {
  const box = $('rating-legend');
  if (!box) return;
  const cells = [
    ['t-low', '\u2264 4.2'], ['t43', '4.3'], ['t44', '4.4'],
    ['t45', '4.5'], ['t-star', '\u2265 4.6'],
  ];
  for (const [cls, label] of cells) {
    const lg = el('div', 'lg');
    const sw = el('div', 'sw' + (cls === 't-star' ? ' star' : ''));
    if (cls === 't-star') sw.textContent = '\u2605';
    else sw.style.background = `var(${TIER_VAR[cls]})`;
    lg.append(sw, el('div', 'lb', label));
    box.append(lg);
  }
}

function wireFilters() {
  $('search').addEventListener('input', (e) => {
    S.filters.q = e.target.value;
    applyFilters();
  });

  const slider = $('rating');
  const out = $('rating-out');
  const sync = () => {
    const v = Number(slider.value);
    S.filters.minRating = v;
    out.textContent = v === 0 ? '全部' : `${v.toFixed(1)}+`;
    applyFilters();
  };
  slider.addEventListener('input', sync);
  $('rating-reset').addEventListener('click', () => { slider.value = '0'; sync(); });

  $('include-unrated').addEventListener('change', (e) => {
    S.filters.includeUnrated = e.target.checked;
    applyFilters();
  });

  $('cat-reset').addEventListener('click', () => {
    S.filters.cats.clear();
    document.querySelectorAll('.cat').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    applyFilters();
  });

  $('token-forget').addEventListener('click', () => {
    localStorage.removeItem(TOKEN_KEY);
    $('token-modal').hidden = true;
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('token-modal').hidden) $('token-modal').hidden = true;
    else if (!$('detail').hidden) closeDetail();
  });
}

/* ---------- boot ---------- */

async function main() {
  const [config, data, notes] = await Promise.all([
    fetch('data/config.json').then((r) => r.json()),
    fetch('data/restaurants.json').then((r) => r.json()),
    fetch('data/notes.json').then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
  ]);

  S.config = config;
  S.restaurants = data.restaurants;
  S.notes = notes || {};
  try {
    S.drafts = JSON.parse(localStorage.getItem(DRAFT_KEY) || '{}');
  } catch { S.drafts = {}; }

  renderCategories(data.categories);
  renderRatingLegend();
  wireFilters();
  applyFilters();

  const deepLink = new URLSearchParams(location.search).get('r');
  if (deepLink && S.restaurants.some((r) => r.id === deepLink)) select(deepLink, false);

  try {
    await loadMaps(config.mapsBrowserKey);
    initMap();
  } catch (err) {
    $('map').hidden = true;
    const box = $('map-error');
    box.hidden = false;
    box.textContent = `${err.message}。請確認 Google Maps 金鑰的 referrer 限制包含目前網域。`;
  }
}

main();
