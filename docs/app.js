/* Foodie Map — static site. All data is pre-baked at build time; the only
   network calls made from the browser are the Google Maps JS bundle and, when
   you save a note, the GitHub Contents API. */

const S = {
  config: null,
  restaurants: [],
  notes: {},          // committed notes, as loaded from notes.json
  drafts: {},         // unsaved edits, mirrored to localStorage
  markers: new Map(),
  byId: new Map(),       // id -> restaurant, so hot loops never scan the array
  onMap: new Map(),      // id -> currently attached, to avoid redundant setMap
  dirtyIcons: new Set(), // markers needing a repaint once they are on screen
  labelZoomOn: null,     // last known "are labels showing" state
  map: null,
  selected: null,
  accounts: [],       // per-reviewer scale, from restaurants.json
  source: null,       // the reviewer currently driving the map
  filters: { q: '', tiers: new Set(), cats: new Set(),
             priceLo: 0, priceHi: Infinity, meals: new Set(), showClosed: false,
             bothOnly: false },
};

/* Round numbers people actually think in, which also puts the fine-grained
   positions where the data is dense — 90% of restaurants sit under 3000, so one
   "3000+" position covers the tail. Index 0 is "no floor", the last is "no cap". */
const BUDGET_STOPS = [0, 200, 300, 400, 500, 600, 800, 1000, 1200, 1500, 2000, 2500, Infinity];
const BUDGET_MAX_I = BUDGET_STOPS.length - 1;

const DRAFT_KEY = 'foodiemap.drafts';
const TOKEN_KEY = 'foodiemap.ghtoken';
const SOURCE_KEY = 'foodiemap.source';
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
/* Tiers are per-reviewer. The two accounts do not share a scale — on every venue
   both have reviewed, jc_foodidi scored lower — so a single set of thresholds
   would park one reviewer's whole map in the bottom bands and put the top tier
   out of his reach. Cut points are frozen in config/rating_calibration.json and
   published per account; the ramp below just maps a tier index to a colour. */
const TIER_RAMP = ['--r-low', '--r43', '--r44', '--r45'];

function accountMeta(username) {
  const who = username || S.source;
  if (who === ALL) return S.combinedMeta;
  return S.accounts.find((a) => a.username === who) || null;
}

const ALL = '__all__';   // the combined view: every reviewer's restaurants at once

/* A restaurant's entry for one reviewer, or null if he never wrote about it.
   In the combined view there is no single reviewer, so the entry shown is the
   one that rates the place highest *on its own author's scale* — a place either
   of them counts among his favourites should read as a favourite. */
function reviewOf(r, username) {
  const who = username || S.source;
  if (who !== ALL) return (r.reviews || {})[who] || null;
  let best = null;
  for (const [acc, rev] of Object.entries(r.reviews || {})) {
    if (!best || normTier(rev, acc) > normTier(best.rev, best.acc)) best = { acc, rev };
  }
  return best ? best.rev : null;
}

// Which account supplies the entry currently on screen.
function reviewAccount(r) {
  if (S.source !== ALL) return (r.reviews || {})[S.source] ? S.source : null;
  let best = null;
  for (const [acc, rev] of Object.entries(r.reviews || {})) {
    if (!best || normTier(rev, acc) > normTier(best.rev, best.acc)) best = { acc, rev };
  }
  return best ? best.acc : null;
}

/* Reviewers may not have the same number of tiers — one has five, the other four,
   because a coarse rating lattice cannot always be split five ways. Comparing
   them means comparing position within each reviewer's own scale, rescaled to a
   common 0-4. The raw scores are never compared; only the ranks are. */
function normTier(rev, account) {
  if (!rev || rev.tier == null) return -1;
  const n = tierCount(account);
  if (n <= 1) return 0;
  return Math.round((rev.tier / (n - 1)) * (COMBINED_TIERS - 1));
}
const COMBINED_TIERS = 5;

function tierCount(username) {
  const who = username || S.source;
  if (who === ALL) return COMBINED_TIERS;
  const meta = S.accounts.find((a) => a.username === who);
  return meta ? (meta.cuts || []).length + 1 : 1;
}

/* Colour for tier `i` of `n`: the bottom always recedes to grey, the top is
   always the star, and any middle tiers walk up the blue ramp. */
function tierVar(i, n) {
  if (i == null) return '--r-none';
  if (i >= n - 1) return '--r-star';
  if (i === 0) return '--r-low';
  const mid = TIER_RAMP.slice(1);
  return mid[Math.min(mid.length - 1, i - 1)] || '--r44';
}

const cssVar = (name) =>
  getComputedStyle(document.body).getPropertyValue(name).trim() || '#9a958c';

// Tier of a restaurant under the reviewer currently selected. In the combined
// view it is the rescaled rank, so five levels of colour still mean one thing.
function tierOf(r, username) {
  const who = username || S.source;
  const rev = reviewOf(r, who);
  if (!rev || rev.tier == null) return null;
  if (who !== ALL) return rev.tier;
  const t = normTier(rev, reviewAccount(r));
  return t < 0 ? null : t;
}

const ratingClass = (r, username) => {
  const t = tierOf(r, username);
  const n = tierCount(username);
  if (t == null) return 't-none';
  return t >= n - 1 ? 't-star' : t === 0 ? 't-low' : `t-mid${t}`;
};
const ratingColor = (r, username) => cssVar(tierVar(tierOf(r, username), tierCount(username)));

// For a bare number — a post row, or the other reviewer's score in the panel.
function tierForRating(rating, username) {
  const meta = accountMeta(username);
  if (rating == null || !meta) return null;
  return (meta.cuts || []).reduce((t, c) => t + (rating >= c ? 1 : 0), 0);
}
function ratingClassFor(rating, username) {
  const t = tierForRating(rating, username);
  const n = tierCount(username);
  if (t == null) return 't-none';
  return t >= n - 1 ? 't-star' : t === 0 ? 't-low' : `t-mid${t}`;
}

// Five-point star, outer radius 10, centred on the anchor point.
const STAR_PATH =
  'M0.00,-10.00L2.59,-3.56L9.51,-3.09L4.18,1.36L5.88,8.09' +
  'L0.00,4.40L-5.88,8.09L-4.18,1.36L-9.51,-3.09L-2.59,-3.56Z';

// The Tainan roundup grades Michelin-style, with the rubric printed in the post.
// Kept separate from the 0-5 rating: two stars there is high praise, not a 2.0.
const GUIDE_STAR_TEXT = { 1: '值得駐足', 2: '值得繞道前往', 3: '值得專程造訪' };

function guideStarLabel(r) {
  if (!r.guide_stars) return null;
  return `${'★'.repeat(r.guide_stars)} ${GUIDE_STAR_TEXT[r.guide_stars] || ''}`.trim();
}

function mealLabel(r) {
  if (r.serves_lunch == null) return '營業時間不明';
  if (r.serves_lunch && r.serves_dinner) return '午餐・晚餐';
  if (r.serves_dinner) return '只做晚餐';
  if (r.serves_lunch) return '只做午餐';
  return '午晚餐皆無';
}

function priceLabel(rev) {
  if (!rev || rev.price_min == null) return null;
  return rev.price_min === rev.price_max
    ? `人均 ${rev.price_min} 元`
    : `人均 ${rev.price_min}–${rev.price_max} 元`;
}

const fmtDate = (iso) => (iso ? iso.slice(0, 10) : '');

/* One reviewer scores in tenths, the other in quarter steps. Rounding to one
   decimal turns his 3.75 into 3.8 and collapses two of his tiers into one, so
   the second decimal is kept whenever it carries information. */
const fmtRating = (v) =>
  v == null ? '' : (Math.round(v * 100) % 10 === 0 ? v.toFixed(1) : v.toFixed(2));

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
    // Switching reviewer switches the map to his recommendations. A restaurant
    // he never wrote about is not shown with someone else's score attached.
    const rev = reviewOf(r);
    if (!rev) return false;
    if (S.filters.bothOnly && Object.keys(r.reviews || {}).length < 2) return false;

    const tier = rev.tier == null ? 'none' : String(rev.tier);
    if (!tiers.has(tier)) return false;

    if (cats.size && !r.categories.some((c) => cats.has(c))) return false;

    // Somewhere that has closed for good is not a place you can eat, so it is out
    // of the way by default rather than deleted — the posts are still history.
    if (!S.filters.showClosed && r.business_status === 'CLOSED_PERMANENTLY') return false;

    // Meal filters are AND: picking both asks for somewhere open at both sittings.
    // Places whose hours Google does not know cannot satisfy either.
    for (const meal of S.filters.meals) {
      const flag = meal === 'lunch' ? r.serves_lunch : r.serves_dinner;
      if (flag !== true) return false;
    }

    // Each restaurant is matched on the midpoint of its 人均 range — its
    // representative price. Overlap was the obvious choice but tested badly: a
    // 1000–1500 place "overlaps" a 500–1000 filter because the endpoints touch,
    // so 42% of everything survived the filter. The midpoint has no such edge
    // artefact, and the full range is still shown on the card.
    const { priceLo, priceHi } = S.filters;
    if (priceLo > 0 || priceHi < Infinity) {
      // The price comes from the reviewer being shown; only one of them
      // publishes a 人均 range at all, so the combined view takes it wherever
      // it exists rather than dropping every restaurant the other one found.
      const priced = S.source === ALL
        ? Object.values(r.reviews || {}).find((v) => v.price_min != null)
        : (rev.price_min != null ? rev : null);
      if (!priced) return false;
      const mid = (priced.price_min + priced.price_max) / 2;
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

  /* Only the markers that actually changed state are touched. setMap on all 705
     costs ~270ms on a mid-range phone, and it was running on every keystroke in
     the search box; most filter changes flip a handful of pins. */
  for (const [id, marker] of S.markers) {
    const on = shown.has(id);
    if (on === S.onMap.get(id)) continue;
    if (on && S.dirtyIcons.has(id)) refreshMarker(id);
    marker.setMap(on ? S.map : null);
    S.onMap.set(id, on);
  }
  // A marker that stayed visible across a source switch still needs repainting.
  if (S.dirtyIcons.size) {
    for (const id of [...S.dirtyIcons]) if (S.onMap.get(id)) refreshMarker(id);
  }
  hideCard();
  renderStat(rows.length);
}

/* Repaint one marker for the current reviewer. Deferred until the marker is on
   screen: switching source used to setIcon on all 705 at once, which is 100ms of
   blocked main thread for pins the viewer cannot see. */
function refreshMarker(id) {
  const m = S.markers.get(id);
  const r = S.byId.get(id);
  if (!m || !r) return;
  m.setIcon(markerIcon(r));
  m.setLabel(markerLabel(r, S.map ? S.map.getZoom() : 13));
  S.dirtyIcons.delete(id);
}

/* ---------- header stat ---------- */

function renderStat(shownCount) {
  const meta = accountMeta();
  const total = meta ? meta.restaurant_count : S.restaurants.length;
  const box = $('stat');
  box.textContent = '';
  if (shownCount === total) {
    box.append(document.createTextNode(`目前有 ${total} 間餐廳`));
    return;
  }
  if (shownCount === 0) {
    // An empty map with no explanation reads as breakage. Name the cause and
    // offer the way out.
    box.append(el('span', 'empty', '沒有符合的餐廳'));
    const why = activeFilterNames();
    if (why.length) box.append(el('span', 'dim', `（${why.join('、')}）`));
    const reset = el('button', 'linkish', '清除所有篩選');
    reset.type = 'button';
    reset.addEventListener('click', resetAllFilters);
    box.append(reset);
    return;
  }
  box.append(document.createTextNode(`符合 ${shownCount} 間`));
  box.append(el('span', 'dim', ` / 共 ${total} 間`));
}

function activeFilterNames() {
  const f = S.filters;
  const names = [];
  if (f.q.trim()) names.push('搜尋');
  if (f.tiers.size < liveTiers().length) names.push('評分');
  if (f.priceLo > 0 || f.priceHi < Infinity) names.push('價位');
  if (f.meals.size) names.push('用餐時段');
  if (f.cats.size) names.push('分類');
  return names;
}

function resetAllFilters() {
  const f = S.filters;
  f.q = '';
  $('search').value = '';
  liveTiers().forEach((k) => f.tiers.add(k));
  syncTierButtons();
  f.cats.clear();
  document.querySelectorAll('.cat').forEach((b) => b.setAttribute('aria-pressed', 'false'));
  updateCatCount();
  f.meals.clear();
  document.querySelectorAll('.meal').forEach((b) => b.setAttribute('aria-pressed', 'false'));
  $('meal-note').textContent = '';
  $('budget-lo').value = '0';
  $('budget-hi').value = String(BUDGET_MAX_I);
  $('budget-lo').dispatchEvent(new Event('input'));
}

/* ---------- lazily-loaded post detail ---------- */

/* Full captions and dish lists are ~63% of the data and are only read when a
   detail panel opens, so they live in their own file and are fetched the first
   time one is needed rather than on first paint. */
let postsPromise = null;
function loadPosts() {
  if (!postsPromise) {
    postsPromise = fetch('data/posts.json')
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  return postsPromise;
}

async function hydratePosts(r) {
  if (r._hydrated) return true;
  const heavy = await loadPosts();
  for (const p of r.posts) Object.assign(p, heavy[p.id] || {});
  r._hydrated = true;
  return true;
}

/* ---------- detail panel ---------- */

function select(id, pan) {
  hideCard();
  S.selected = id;
  const r = S.byId.get(id) || S.restaurants.find((x) => x.id === id);
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
  // Draw with what is already loaded, then redraw once the captions and dish
  // lists arrive — the panel should never wait on a fetch to appear.
  if (!r._hydrated) {
    hydratePosts(r).then(() => {
      if (S.selected === r.id) renderDetail(r);
    });
  }
  const d = $('detail');
  d.hidden = false;
  d.scrollTop = 0;
  d.textContent = '';

  const close = el('button', 'close', '×');
  close.setAttribute('aria-label', '關閉');
  close.addEventListener('click', closeDetail);
  d.append(close, el('h2', null, r.name || '(無店名)'));
  if (r.business_status === 'CLOSED_PERMANENTLY') {
    d.append(el('p', 'closed-banner', 'Google 標示這家店已歇業。貼文保留下來作為紀錄。'));
  }
  if (r.from_roundup) {
    d.append(el('p', 'badge',
      '這家店來自合輯貼文，只有店名和一句推薦——評分、價位、菜色他沒有單獨寫。'));
  }
  if (r.aka && r.aka.length) {
    d.append(el('p', 'aka', `原名 ${r.aka.join('、')}`));
  }
  const rev = reviewOf(r) || {};
  if (rev.tagline) d.append(el('p', 'tagline', rev.tagline));

  const stats = el('div', 'stats');
  const stat = (label, value) => {
    const s = el('div', 'stat');
    s.append(el('b', null, label), document.createTextNode(value));
    return s;
  };
  stats.append(
    stat(reviewAccount(r) && Object.keys(r.reviews || {}).length > 1
         ? `評分 · @${(accountMeta(reviewAccount(r)) || {}).label || reviewAccount(r)}`
         : '最新評分',
         rev.rating == null ? '未評分'
         : `${fmtRating(rev.rating)} / 5${r.rating_derived ? '（由星級換算）' : ''}`),
    stat('造訪次數', rev.visits == null ? '—' : `${rev.visits} 次`),
    stat('價位', priceLabel(rev) || '—'),
    stat('最近一次', fmtDate(rev.last_visit) || '—'),
    stat('用餐時段', mealLabel(r)),
    ...(guideStarLabel(r) ? [stat('合輯評級', guideStarLabel(r))] : []),
    stat('每週公休', r.closed_days == null ? '—'
         : r.closed_days === 0 ? '無' : `${r.closed_days} 天`),
  );
  d.append(stats);

  if (r.categories.length) {
    const chips = el('div', 'chips');
    r.categories.forEach((c) => chips.append(el('span', 'chip', c)));
    d.append(chips);
  }

  d.append(el('div', 'addr', r.address || '（尚未取得地址）'));
  // One reviewer writes the phone number into his captions; Google's phone field
  // is an Enterprise-tier lookup we do not make, so this is the only source.
  const phone = r.posts.map((p) => (p.extras || {}).phone).find(Boolean)
    || Object.values(r.reviews || {}).map((v) => (v.extras || {}).phone).find(Boolean);
  if (phone) {
    const row = el('div', 'addr');
    const link = el('a', null, phone);
    link.href = `tel:${phone.replace(/[^\d+]/g, '')}`;
    row.append(link);
    d.append(row);
  }
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

  /* --- what the other reviewer thought --- */
  // Shown, never merged. Two reviewers disagreeing is information; averaging
  // their scores would destroy it, and their scales are not comparable anyway.
  const lead = reviewAccount(r);
  for (const acc of Object.keys(r.reviews || {})) {
    if (acc === lead) continue;
    const other = r.reviews[acc];
    const meta = accountMeta(acc);
    const box = el('div', 'other');
    const head = el('div', 'oh');
    head.append(el('span', 'who', `@${meta ? meta.label : acc} 的評分`));
    const sc = el('span', `score ${ratingClassFor(other.rating, acc)}`);
    sc.textContent = other.rating == null ? '未評分'
      : `${fmtRating(other.rating)}${other.visits != null ? ` · ${other.visits}訪` : ''}`;
    head.append(sc);
    box.append(head);
    if (other.tagline) box.append(el('p', null, other.tagline));
    d.append(box);
  }

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
    if ((r.accounts || []).length > 1 && p.source_account) {
      const meta = accountMeta(p.source_account);
      left.append(el('span', 'post-who', `　@${meta ? meta.label : p.source_account}`));
    }
    const right = el('span', `score ${ratingClassFor(p.rating, p.source_account)}`,
      p.rating == null ? '' : `${fmtRating(p.rating)}${p.visits ? ` · ${p.visits}訪` : ''}`);
    head.append(left, right);
    box.append(head);

    if (p.roundup) {
      box.append(el('div', 'post-roundup',
        `合輯：${p.roundup_title || ''}${p.visits ? ` · ${p.visits}訪` : ''}`));
    }
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
      // A roundup caption covers several restaurants, so say so before opening it.
      det.append(el('summary', null, p.roundup ? '完整合輯貼文（含其他餐廳）' : '完整貼文內容'));
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
  const rev = reviewOf(r) || {};
  const score = el('span', `hc-score ${ratingClass(r)}`);
  score.append(document.createTextNode(rev.rating == null ? '未評分' : fmtRating(rev.rating)));
  if (rev.visits != null) score.append(el('span', 'hc-visits', ` / ${rev.visits}訪`));
  top.append(score);
  box.append(top);

  // Whose score that is has to be said out loud once more than one reviewer is
  // on the map, otherwise the number reads as a property of the restaurant.
  const accounts = Object.keys(r.reviews || {});
  if (S.source === ALL || accounts.length > 1) {
    const line = el('div', 'hc-who');
    accounts.forEach((acc, i) => {
      if (i) line.append(document.createTextNode('　'));
      const meta = S.accounts.find((a) => a.username === acc);
      const v = r.reviews[acc];
      const chip = el('span', acc === reviewAccount(r) ? 'lead' : null,
        `@${meta ? meta.label : acc} ${v.rating == null ? '未評分' : fmtRating(v.rating)}`);
      line.append(chip);
    });
    box.append(line);
  }
  if (r.business_status === 'CLOSED_PERMANENTLY') {
    box.append(el('div', 'hc-closed', '已歇業'));
  }
  const gs = guideStarLabel(r);
  if (gs) box.append(el('div', 'hc-guide', gs + (r.rating_derived ? '（換算評分）' : '')));
  if (r.from_roundup && rev.rating == null && !gs) {
    box.append(el('div', 'hc-badge', '合輯提及 · 無評分'));
  }

  if (rev.tagline) box.append(el('p', 'hc-tagline', rev.tagline));

  const meta = el('div', 'hc-meta');
  const bits = [priceLabel(rev), fmtDate(rev.last_visit)].filter(Boolean);
  bits.forEach((b) => meta.append(el('span', null, b)));
  meta.append(el('span', 'hc-meals', mealLabel(r)));
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
  const t = tierOf(r);
  const n = tierCount();
  const tier = t == null ? 't-none' : (t >= n - 1 ? 't-star' : t === 0 ? 't-low' : 'mid');
  const stroke = getComputedStyle(document.body).getPropertyValue('--bg').trim() || '#ffffff';
  if (tier === 't-star') {
    return {
      path: STAR_PATH,
      // A 5-point star covers only 41% of its circumscribed circle, so matching
      // the circles on radius makes it read much smaller. 10.7 against a circle
      // radius of 6.5 matches them by area, plus a little so 愛店 stands out.
      scale: 1.07,
      fillColor: ratingColor(r),
      fillOpacity: 1,
      strokeColor: stroke,
      strokeWeight: 1.6,
    };
  }
  return {
    path: google.maps.SymbolPath.CIRCLE,
    // Low-rated pins are smaller as well as greyer — two ways of receding.
    scale: tier === 't-low' || tier === 't-none' ? 4.8 : 6.5,
    fillColor: ratingColor(r),
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
  const rev = reviewOf(r);
  // No numeric labels in the combined view. 4.4 and 3.75 side by side reads as
  // "one is better", when they are scores on two scales that do not compare —
  // which is the whole reason the tiers exist. Colour carries the rank instead.
  if (S.source === ALL) return null;
  if (zoom < LABEL_ZOOM || !rev || rev.rating == null) return null;
  // The bottom tier stays unlabelled: those pins are meant to recede, and
  // dropping their labels thins out collisions in the dense blocks. Judged by
  // tier rather than by an absolute number, because the reviewers' scales differ.
  const n = tierCount();
  if (rev.tier === 0) return null;
  const star = rev.tier != null && rev.tier >= n - 1;
  return {
    text: fmtRating(rev.rating),
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

  /* Labels appear only once you're zoomed in enough for them not to overlap.
     Relabelling only happens when that threshold is actually crossed — it used
     to run on every zoom step, and with a linear id lookup inside the loop. */
  S.map.addListener('zoom_changed', () => {
    const z = S.map.getZoom();
    const on = z >= LABEL_ZOOM && S.source !== ALL;
    if (on === S.labelZoomOn) return;
    S.labelZoomOn = on;
    for (const [id, m] of S.markers) {
      if (!S.onMap.get(id)) { S.dirtyIcons.add(id); continue; }
      const r = S.byId.get(id);
      if (r) m.setLabel(markerLabel(r, z));
    }
  });

  for (const r of S.restaurants) {
    if (r.lat == null) continue;
    const m = new google.maps.Marker({
      position: { lat: r.lat, lng: r.lng },
      map: null,     // attached by applyFilters; see below
      title: `${r.name}${reviewOf(r)?.rating != null ? ` · ${reviewOf(r).rating}` : ''}`,
      icon: markerIcon(r),
      label: markerLabel(r, 13),
      /* Renders the pins into one shared canvas instead of an element each.
         Google turns this off by itself for any marker carrying a label, so it
         applies in the combined view (no labels) and to the unlabelled bottom
         tier — which is most of them on a city-wide view. */
      optimized: true,
    });
    S.onMap.set(r.id, false);
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

/* The legend IS the rating filter, and it is rebuilt whenever the reviewer
   changes: the two accounts have different cut points and may not even have the
   same number of tiers, because a coarse rating lattice cannot always be split
   five ways. Labels come from the published per-account scale. */
const liveTiers = () => {
  const meta = accountMeta();
  const keys = meta ? meta.cuts.map((_, i) => String(i + 1)) : [];
  const all = ['0', ...keys];
  return all.filter((k) => S.restaurants.some(
    (r) => reviewOf(r) && String(reviewOf(r).tier) === k))
    .concat(S.restaurants.some((r) => reviewOf(r) && reviewOf(r).tier == null)
      ? ['none'] : []);
};

/* The combined view is presented as one more source. Its tier labels are ranks,
   not numbers: "4.4" is born2eat's fourth tier and jc's does not exist, so a
   shared axis can only honestly be labelled by position. */
function buildCombinedMeta() {
  const counts = [0, 0, 0, 0, 0];
  let rated = 0;
  for (const r of S.restaurants) {
    const acc = reviewAccountIn(r);
    if (!acc) continue;
    const t = normTier(r.reviews[acc], acc);
    if (t >= 0) { counts[t] += 1; rated += 1; }
  }
  return {
    username: ALL, label: '全部', url: null, primary: false,
    restaurant_count: S.restaurants.length, rated_count: rated,
    cuts: [1, 2, 3, 4],
    labels: ['較低', '普通', '不錯', '很好', '愛店'],
    top_label: '兩人任一的愛店',
    hints: ['層級是各自尺度上的名次，不是分數'],
    tier_counts: counts,
  };
}

// The same choice as reviewAccount, usable before S.source is set.
function reviewAccountIn(r) {
  let best = null;
  for (const [acc, rev] of Object.entries(r.reviews || {})) {
    if (!best || normTier(rev, acc) > normTier(best.rev, best.acc)) best = { acc, rev };
  }
  return best ? best.acc : null;
}

function renderSources() {
  const box = $('sources');
  if (!box) return;
  box.textContent = '';
  for (const a of [S.combinedMeta, ...S.accounts]) {
    const b = el('button', 'source');
    b.type = 'button';
    b.dataset.account = a.username;
    b.setAttribute('aria-pressed', String(a.username === S.source));
    b.append(document.createTextNode(a.label));
    b.append(el('span', 'n', `${a.restaurant_count} 家`));
    b.title = a.username === ALL
      ? `全部 ${a.restaurant_count} 家；每家以評價它的人自己的尺度上色`
      : `${a.label}：${a.restaurant_count} 家餐廳，${a.rated_count} 家有評分`;
    b.addEventListener('click', () => setSource(a.username));
    box.append(b);
  }
  renderByline();
}

function renderByline() {
  const box = $('byline');
  if (!box) return;
  box.textContent = 'from ';
  S.accounts.forEach((a, i) => {
    if (i) box.append(document.createTextNode('、'));
    const link = el('a', a.username === S.source ? 'active' : null, `@${a.label}`);
    link.href = a.url;
    link.target = '_blank';
    link.rel = 'noopener';
    box.append(link);
  });
}

/* Switching reviewer rebuilds everything the scale touches: his cut points, his
   tier labels and counts, and the pin colours. The two accounts do not share a
   scale, so nothing about the previous reviewer's view may survive the switch. */
function setSource(username) {
  if (username === S.source) return;
  S.source = username;
  try { localStorage.setItem(SOURCE_KEY, username); } catch { /* private mode */ }
  for (const b of document.querySelectorAll('.source')) {
    b.setAttribute('aria-pressed', String(b.dataset.account === username));
  }
  renderByline();
  renderRatingLegend();
  syncPriceAvailability();
  if (S.selected && !reviewOf(S.restaurants.find((x) => x.id === S.selected) || {})) {
    closeDetail();
  } else if (S.selected) {
    select(S.selected, false);
  }
  // Mark every pin as needing a repaint; applyFilters does the visible ones and
  // the rest are done lazily as they come into view.
  for (const id of S.markers.keys()) S.dirtyIcons.add(id);
  S.labelZoomOn = null;
  applyFilters();
}

/* One reviewer publishes a 人均 range and the other does not, so the price
   filter is switched off rather than silently matching nothing. */
function syncPriceAvailability() {
  const block = $('budget-lo') && $('budget-lo').closest('.filter');
  if (!block) return;
  const priced = S.restaurants.filter((r) => S.source === ALL
    ? Object.values(r.reviews || {}).some((v) => v.price_min != null)
    : (reviewOf(r) || {}).price_min != null).length;
  const off = priced === 0;
  block.classList.toggle('disabled', off);
  if (off) {
    $('budget-lo').value = '0';
    $('budget-hi').value = String(BUDGET_MAX_I);
    S.filters.priceLo = 0;
    S.filters.priceHi = Infinity;
    $('budget-out').textContent = '不適用';
    $('budget-note').textContent = `@${S.source} 不寫人均價位`;
  } else {
    $('budget-note').textContent = '';
  }
}

function renderRatingLegend() {
  const box = $('rating-legend');
  if (!box) return;
  box.textContent = '';
  const meta = accountMeta();
  const n = tierCount();

  const counts = {};
  for (const r of S.restaurants) {
    const rev = reviewOf(r);
    if (!rev) continue;
    const k = rev.tier == null ? 'none' : String(rev.tier);
    counts[k] = (counts[k] || 0) + 1;
  }

  const cells = [];
  for (let i = 0; i < n; i++) {
    const label = (meta && meta.labels && meta.labels[i]) ||
      (i === 0 ? '最低' : i === n - 1 ? '最高' : String(i));
    cells.push([String(i), label, i >= n - 1]);
  }
  cells.push(['none', '未評分', false]);

  S.filters.tiers = new Set();
  for (const [key, label, isTop] of cells) {
    // An empty tier would be a control that does nothing — leave it out.
    if (!counts[key]) continue;
    S.filters.tiers.add(key);

    const lg = el('button', 'lg');
    lg.type = 'button';
    lg.dataset.tier = key;
    const topNote = isTop && meta && meta.top_label ? ` ${meta.top_label}` : '';
    lg.title = `${label}${topNote}：${counts[key]} 家`;

    const sw = el('div', 'sw' + (isTop ? ' star' : ''));
    if (isTop) sw.textContent = '\u2605';
    else sw.style.background = `var(${key === 'none' ? '--r-none'
      : tierVar(Number(key), n)})`;

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
  // The scale's own vocabulary belongs to whoever wrote it — 4.3 願意再訪 is
  // born2eat's phrase and means nothing on the other reviewer's scale.
  const hints = $('rating-hints');
  if (hints) {
    hints.textContent = '';
    for (const h of (meta && meta.hints) || []) hints.append(el('span', null, h));
  }
  syncTierButtons();
}

function syncTierButtons() {
  for (const b of $('rating-legend').querySelectorAll('.lg')) {
    b.setAttribute('aria-pressed', String(S.filters.tiers.has(b.dataset.tier)));
  }
  const off = liveTiers().length - S.filters.tiers.size;
  $('rating-count').textContent = off > 0 ? `已篩掉 ${off} 級` : '';
}

function wireFilters() {
  // Typing is the one filter that fires continuously, so it settles first.
  let searchTimer = null;
  $('search').addEventListener('input', (e) => {
    S.filters.q = e.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFilters, 140);
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

    // Some restaurants carry no price at all. Say so rather than dropping them
    // silently — the count is read from the data, not hardcoded, because it
    // grows sharply once an account that publishes no 人均 range is added.
    const unpriced = S.restaurants.filter(
      (r) => reviewOf(r) && reviewOf(r).price_min == null).length;
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

  const mealNote = $('meal-note');
  const syncMeals = () => {
    for (const b of document.querySelectorAll('.meal')) {
      b.setAttribute('aria-pressed', String(S.filters.meals.has(b.dataset.meal)));
    }
    const unknown = S.restaurants.filter((r) => r.serves_lunch == null).length;
    mealNote.textContent =
      S.filters.meals.size && unknown ? `${unknown} 家 Google 沒有營業時間，不列入` : '';
    applyFilters();
  };
  for (const b of document.querySelectorAll('.meal')) {
    b.addEventListener('click', () => {
      const m = b.dataset.meal;
      if (S.filters.meals.has(m)) S.filters.meals.delete(m);
      else S.filters.meals.add(m);
      syncMeals();
    });
  }
  $('meal-reset').addEventListener('click', () => { S.filters.meals.clear(); syncMeals(); });
  const bothOnly = $('both-only');
  if (bothOnly) {
    bothOnly.addEventListener('change', (e) => {
      S.filters.bothOnly = e.target.checked;
      applyFilters();
    });
  }

  $('show-closed').addEventListener('change', (e) => {
    S.filters.showClosed = e.target.checked;
    applyFilters();
  });

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
  S.accounts = data.accounts || [];
  S.aliases = data.id_aliases || {};
  S.notes = notes || {};

  // Retired ids still resolve. A restaurant known only from a roundup gains a
  // real post and moves to a better id; the note filed under the old one has to
  // follow it rather than disappear.
  for (const [oldId, newId] of Object.entries(S.aliases)) {
    if (S.notes[oldId] && !S.notes[newId]) S.notes[newId] = S.notes[oldId];
  }

  for (const r of S.restaurants) S.byId.set(r.id, r);
  S.combinedMeta = buildCombinedMeta();

  const stored = (() => { try { return localStorage.getItem(SOURCE_KEY); } catch { return null; } })();
  const valid = [ALL, ...S.accounts.map((a) => a.username)];
  // Opens on the combined view: the map's job is to show everywhere worth eating,
  // and restricting it to one reviewer is a deliberate narrowing, not the default.
  S.source = valid.includes(stored) ? stored : ALL;

  try {
    S.drafts = JSON.parse(localStorage.getItem(DRAFT_KEY) || '{}');
  } catch { S.drafts = {}; }

  renderSources();
  renderRatingLegend();     // seeds S.filters.tiers, so it must run before filtering
  renderCategories(data.categories);
  wireFilters();
  syncPriceAvailability();
  applyFilters();

  const asked = new URLSearchParams(location.search).get('r');
  const deepLink = asked && !S.restaurants.some((r) => r.id === asked)
    ? S.aliases[asked] : asked;
  if (deepLink && S.restaurants.some((r) => r.id === deepLink)) {
    // A deep link may point at a restaurant the current reviewer never wrote
    // about; switch to one who did rather than opening an empty panel.
    const r = S.restaurants.find((x) => x.id === deepLink);
    if (!reviewOf(r)) setSource(ALL);
    select(deepLink, false);
  }

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
