# Setup & Accounts

Which account owns each credential. **No secret values live in this file** — the repo is
public. Values are in gitignored `.env.local` (local runs) and GitHub Actions secrets (CI).

## Accounts

| Service | Account | Purpose |
|---|---|---|
| Apify | palatial account | Instagram Scraper actor — one run per account in `config/accounts.json` |
| Anthropic | personal (key reused from `blender_articulated_asset_generation/.env.local`) | Caption -> structured JSON extraction (Opus) |
| Google Cloud | datesmart account | Maps JavaScript API (browser) + Geocoding API (build) |
| GitHub | kaihungc1993 | Repo, Pages, Actions, notes-write PAT |

## Secret names

Set all four under **Settings -> Secrets and variables -> Actions**:

| Secret | Notes |
|---|---|
| `APIFY_TOKEN` | Apify -> Settings -> Integrations |
| `ANTHROPIC_API_KEY` | console.anthropic.com -> API Keys |
| `GOOGLE_GEOCODING_KEY` | Server key. API restriction: Geocoding API. Application restriction: None (Actions runner IPs are not static). |
| `GOOGLE_MAPS_BROWSER_KEY` | Injected into the page at build time. Public by nature — restrictions below are what protect it. |

The notes-write PAT is **not** a repo secret. It is pasted into the running webpage by the
user and kept in browser localStorage. Fine-grained, scoped to this repo only,
`Contents: Read and write`.

## Google Cloud key configuration

Two separate keys. Do not reuse one for both.

**Browser key** (Maps JavaScript API)
- Application restriction: Websites
  - `https://kaihungc1993.github.io/*`
  - `http://localhost:8000/*`  (local preview)
- API restriction: Maps JavaScript API only
- Quota: cap Maps JavaScript API at ~500/day

**Server key** (Geocoding + Places)
- Application restriction: None
- API restrictions: Geocoding API **and Places API (New)**
- Quota: cap Geocoding API at ~500/day

Places API (New) must be enabled on the project. It does two jobs here:

- **Text Search** resolves venue *names* to locations. The Geocoding API is built
  for addresses and silently returns the city centroid when it cannot match a
  business — 7 of the first 38 venues landed on the same point in central Taipei
  and the run reported no failures. Pro SKU, 5,000 free calls/month.
- **Place Details** supplies opening hours and business status. Those fields are
  **Enterprise** SKU, whose free allowance is **1,000 calls/month** — a tenth of
  the others. `hours.py` caches by place_id and refuses any run that would fetch
  more than 900 at once.

Referrer restrictions are spoofable, so the API restriction and the daily quota cap are the
controls that actually bound the blast radius of a leaked browser key.

## Cost expectations

- Apify: free plan, $5/mo credits. Monthly incremental crawl of ~30 posts is ~$0.05.
- Google Maps: Essentials tier, 10,000 free map loads/mo and 10,000 free geocodes/mo.
  Billing per map *initialization*; pan/zoom/markers are free. Billing account with a card
  is required even to use the free tier.
- Places: Text Search is Pro (5,000 free/mo), opening hours are Enterprise
  (1,000 free/mo). Both cached permanently, so the ~550-venue backfill was a
  one-time spend and monthly runs cost a handful of calls.
- Anthropic: Opus for extraction, run once per new post only (cached by post ID).

## Rotation

The Apify token was shared in plaintext during setup. Rotate it in
Apify -> Settings -> Integrations once the pipeline is confirmed working.
