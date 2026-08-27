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
  filters: { q: '', tiers: new Set(), cats: new Set(),
             priceLo: 0, priceHi: Infinity },
};

/* Round numbers people actually think in, which also puts the fine-grained
   positions where the data is dense — 90% of restaurants sit under 3000, so one
   "3000+" position covers the tail. Index 0 is "no floor", the last is "no cap". */
const BUDGET_STOPS = [0, 200, 300, 400, 500, 600, 800, 1000, 1200, 1500, 2000, 2500, Infinity];
const BUDGET_MAX_I = BUDGET_STOPS.length - 1;

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
  const { q, tiers, cats } = S.filters;
  const needle = q.trim().toLowerCase();
  return S.restaurants.filter((r) => {
    if (!tiers.has(ratingTier(r.rating))) return false;

    if (cats.size && !r.categories.some((c) => cats.has(c))) return false;

    // Each restaurant is matched on the midpoint of its 人均 range — its
    // representative price. Overlap was the obvious choice but tested badly: a
    // 1000–1500 place "overlaps" a 500–1000 filter because the endpoints touch,
    // so 42% of everything survived the filter. The midpoint has no such edge
    // artefact, and the full range is still shown on the card.
    const { priceLo, priceHi } = S.filters;
    if (priceLo > 0 || priceHi < Infinity) {
      if (r.price_min == null) return false;   // no price to check against
      const mid = (r.price_min + r.price_max) / 2;
      if (mid < priceLo || mid > priceHi) return false;
    }

    if (needle) {
      const hay = `${r.name || ''} ${(r.aka || []).join(' ')} ${r.location_tag || ''} ${r.address || ''}`
        .toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });
}

function applyFilters() {
  const rows = visible();
  const shown = new Set(rows.map((r) => r.id));

  for (const [id, marker] of S.markers) {
    marker.setMap(shown.has(id) ? S.map : null);
  }
  hideCard();
  renderStat(rows.length);
}

/* ---------- header stat ---------- */

function renderStat(shownCount) {
  const total = S.restaurants.length;
  const box = $('stat');
  box.textContent = '';
  if (shownCount === total) {
    box.append(document.createTextNode(`目前有 ${total} 間餐廳`));
  } else {
    box.append(document.createTextNode(`符合 ${shownCount} 間`));
    box.append(el('span', 'dim', ` / 共 ${total} 間`));
  }
}

/* ---------- detail panel ---------- */

function select(id, pan) {
  hideCard();
  S.selected = id;
  const r = S.restaurants.find((x) => x.id === id);
  if (!r) return;
  const u = new URL(location.href);
  u.searchParams.set('r', id);
  history.replaceState(null, '', u);
  renderDetail(r);
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
  if (r.aka && r.aka.length) {
    d.append(el('p', 'aka', `原名 ${r.aka.join('、')}`));
  }
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
  } catch (err) {
    status.className = 'err';
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- hover card ---------- */

const CARD_W = 268, CARD_GAP = 16;

function showCard(r, ev) {
  const box = $('hovercard');
  box.textContent = '';

  const top = el('div', 'hc-top');
  top.append(el('h4', null, r.name || '(無店名)'));
  const score = el('span', `hc-score ${ratingClass(r.rating)}`);
  score.append(document.createTextNode(r.rating == null ? '未評分' : r.rating.toFixed(1)));
  score.append(el('span', 'hc-visits', ` / ${r.visits}訪`));
  top.append(score);
  box.append(top);

  if (r.tagline) box.append(el('p', 'hc-tagline', r.tagline));

  const meta = el('div', 'hc-meta');
  const bits = [priceLabel(r), fmtDate(r.last_visit)].filter(Boolean);
  bits.forEach((b) => meta.append(el('span', null, b)));
  box.append(meta);

  if (r.categories.length) {
    const chips = el('div', 'hc-chips');
    r.categories.slice(0, 4).forEach((c) => chips.append(el('span', 'hc-chip', c)));
    box.append(chips);
  }

  if (hasNote(r.id)) box.append(el('div', 'hc-note', '✎ 有筆記'));

  box.hidden = false;
  moveCard(ev);
}

function moveCard(ev) {
  const box = $('hovercard');
  if (box.hidden || !ev) return;
  const h = box.offsetHeight || 130;
  // Flip to the other side of the cursor near the right edge. When the detail
  // panel is open its left edge is the real boundary, not the window's.
  const panel = $('detail');
  const limit = panel.hidden
    ? window.innerWidth - 8
    : panel.getBoundingClientRect().left - 8;
  let x = ev.clientX + CARD_GAP;
  if (x + CARD_W > limit) x = ev.clientX - CARD_W - CARD_GAP;
  let y = ev.clientY + CARD_GAP;
  if (y + h > window.innerHeight - 8) y = ev.clientY - h - CARD_GAP;
  box.style.left = `${Math.max(8, x)}px`;
  box.style.top = `${Math.max(8, y)}px`;
}

function hideCard() {
  $('hovercard').hidden = true;
}

/* ---------- map ---------- */

function markerIcon(r) {
  const tier = ratingTier(r.rating);
  const stroke = getComputedStyle(document.body).getPropertyValue('--bg').trim() || '#ffffff';
  if (tier === 't-star') {
    return {
      path: STAR_PATH,
      // A 5-point star covers only 41% of its circumscribed circle, so matching
      // the circles on radius makes it read much smaller. 10.7 against a circle
      // radius of 6.5 matches them by area, plus a little so 愛店 stands out.
      scale: 1.07,
      fillColor: ratingColor(r.rating),
      fillOpacity: 1,
      strokeColor: stroke,
      strokeWeight: 1.6,
    };
  }
  return {
    path: google.maps.SymbolPath.CIRCLE,
    // Low-rated pins are smaller as well as greyer — two ways of receding.
    scale: tier === 't-low' || tier === 't-none' ? 4.8 : 6.5,
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

const LABEL_ZOOM = 14;  // ratings are short, so they can appear earlier than names did

function markerLabel(r, zoom) {
  // The rating, not the name — the name is already in the list and the tooltip,
  // whereas the exact score is the one thing the colour cannot fully carry.
  if (zoom < LABEL_ZOOM || r.rating == null) return null;
  // <= 4.2 stays unlabelled: those pins are meant to recede, and dropping their
  // labels thins out collisions in the dense blocks where it matters most.
  if (r.rating <= 4.2) return null;
  const star = r.rating >= 4.6;
  return {
    text: r.rating.toFixed(1),
    fontSize: '11.5px',
    fontWeight: '600',
    color: getComputedStyle(document.body).getPropertyValue('--text').trim() || '#1c1b19',
    className: star ? 'pin-label star' : 'pin-label',
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
    m.addListener('mouseover', (e) => {
      m.setZIndex(1000);          // lift the hovered pin above its neighbours
      showCard(r, e.domEvent);
    });
    m.addListener('mousemove', (e) => moveCard(e.domEvent));
    m.addListener('mouseout', () => {
      m.setZIndex(null);
      hideCard();
    });
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
      updateCatCount();
      applyFilters();
    });
    box.append(b);
  }
  $('cat-toggle').textContent = `展開全部分類（${cats.length}）`;
}

function updateCatCount() {
  const n = S.filters.cats.size;
  $('cat-count').textContent = n ? `已選 ${n}` : '';
}

const TIER_CELLS = [
  ['t-low', '\u2264 4.2'], ['t43', '4.3'], ['t44', '4.4'],
  ['t45', '4.5'], ['t-star', '\u2265 4.6'], ['t-none', '未評分'],
];

const liveTiers = () => TIER_CELLS.map(([k]) => k)
  .filter((k) => S.restaurants.some((r) => ratingTier(r.rating) === k));

function renderRatingLegend() {
  const box = $('rating-legend');
  if (!box) return;
  box.textContent = '';
  const counts = {};
  for (const r of S.restaurants) {
    const t = ratingTier(r.rating);
    counts[t] = (counts[t] || 0) + 1;
  }

  for (const [key, label] of TIER_CELLS) {
    // An empty tier would be a control that does nothing — leave it out.
    if (!counts[key]) continue;
    S.filters.tiers.add(key);

    const lg = el('button', 'lg');
    lg.type = 'button';
    lg.dataset.tier = key;
    lg.title = `${label}：${counts[key]} 家`;

    const sw = el('div', 'sw' + (key === 't-star' ? ' star' : ''));
    if (key === 't-star') sw.textContent = '\u2605';
    else sw.style.background = `var(${TIER_VAR[key]})`;

    lg.append(sw, el('span', 'lb', label), el('span', 'ct', String(counts[key])));
    lg.addEventListener('click', () => {
      if (S.filters.tiers.has(key)) S.filters.tiers.delete(key);
      else S.filters.tiers.add(key);
      // Turning the last tier off would empty the map with no way back but reset,
      // so the final selected tier stays on.
      if (S.filters.tiers.size === 0) S.filters.tiers.add(key);
      syncTierButtons();
      applyFilters();
    });
    box.append(lg);
  }
  syncTierButtons();
}

function syncTierButtons() {
  for (const b of $('rating-legend').querySelectorAll('.lg')) {
    b.setAttribute('aria-pressed', String(S.filters.tiers.has(b.dataset.tier)));
  }
  const off = liveTiers().length - S.filters.tiers.size;
  $('rating-count').textContent = off ? `已篩掉 ${off} 級` : '';
}

function wireFilters() {
  $('search').addEventListener('input', (e) => {
    S.filters.q = e.target.value;
    applyFilters();
  });

  $('rating-reset').addEventListener('click', () => {
    liveTiers().forEach((k) => S.filters.tiers.add(k));
    syncTierButtons();
    applyFilters();
  });

  const loEl = $('budget-lo');
  const hiEl = $('budget-hi');
  const budgetOut = $('budget-out');
  const budgetNote = $('budget-note');
  const budgetFill = $('budget-fill');

  const syncBudget = () => {
    let lo = Number(loEl.value);
    let hi = Number(hiEl.value);
    if (lo > hi) { [lo, hi] = [hi, lo]; loEl.value = String(lo); hiEl.value = String(hi); }

    S.filters.priceLo = BUDGET_STOPS[lo];
    S.filters.priceHi = BUDGET_STOPS[hi];

    const loOn = lo > 0;
    const hiOn = hi < BUDGET_MAX_I;
    budgetOut.textContent =
      !loOn && !hiOn ? '不限'
      : loOn && hiOn ? `${BUDGET_STOPS[lo]} – ${BUDGET_STOPS[hi]} 元`
      : loOn ? `${BUDGET_STOPS[lo]} 元以上`
      : `${BUDGET_STOPS[hi]} 元以內`;

    budgetFill.style.left = `${(lo / BUDGET_MAX_I) * 100}%`;
    budgetFill.style.right = `${100 - (hi / BUDGET_MAX_I) * 100}%`;

    // 6 restaurants carry no price. Say so rather than dropping them silently.
    const unpriced = S.restaurants.filter((r) => r.price_min == null).length;
    budgetNote.textContent =
      (loOn || hiOn) && unpriced ? `${unpriced} 家未標價位，設定價位後不列入` : '';
    applyFilters();
  };

  loEl.addEventListener('input', syncBudget);
  hiEl.addEventListener('input', syncBudget);
  $('budget-reset').addEventListener('click', () => {
    loEl.value = '0';
    hiEl.value = String(BUDGET_MAX_I);
    syncBudget();
  });
  syncBudget();

  $('cat-reset').addEventListener('click', () => {
    S.filters.cats.clear();
    document.querySelectorAll('.cat').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    updateCatCount();
    applyFilters();
  });

  const catBox = $('categories');
  const catToggle = $('cat-toggle');
  const total = () => document.querySelectorAll('.cat').length;
  catToggle.addEventListener('click', () => {
    const collapsed = catBox.classList.toggle('collapsed');
    catToggle.setAttribute('aria-expanded', String(!collapsed));
    catToggle.textContent = collapsed ? `展開全部分類（${total()}）` : '收合分類';
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

  renderRatingLegend();     // seeds S.filters.tiers, so it must run before filtering
  renderCategories(data.categories);
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
