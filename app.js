// ── POLITIEKE VERHOUDINGEN ────────────────────────────────────────────────────
const COALITIE_FRACTIES = new Set(['Lokaal Zaans', 'POV', 'VVD', 'D66', 'DENK', 'CDA']);
const ZETEL_MAP = {
  'Lokaal Zaans': 7, 'POV': 3, 'VVD': 3, 'D66': 3, 'DENK': 2, 'CDA': 1,
  'GROENLINKS – PvdA': 6, 'PVV': 3, 'Forum voor Democratie': 3,
  'Democratisch Zaanstad': 2, 'ROSA': 2, 'Partij voor de Dieren': 1,
  'SP': 1, 'ChristenUnie': 1
};

function parseFractieString(str) {
  if (!str) return [];
  return [...str.matchAll(/([^,(]+?)\s*\(\d+\)/g)].map(m => m[1].trim()).filter(Boolean);
}

function getStemSets(s) {
  return {
    voor: new Set(parseFractieString(s.fracties_voor)),
    tegen: new Set(parseFractieString(s.fracties_tegen)),
    onth: new Set(parseFractieString(s.fracties_onthouding)),
  };
}

function getAllFracties() {
  const all = new Set();
  stemmingen.forEach(s => {
    parseFractieString(s.fracties_voor).forEach(f => all.add(f));
    parseFractieString(s.fracties_tegen).forEach(f => all.add(f));
    parseFractieString(s.fracties_onthouding).forEach(f => all.add(f));
  });
  return [...all].sort((a, b) => {
    const aC = COALITIE_FRACTIES.has(a), bC = COALITIE_FRACTIES.has(b);
    if (aC !== bC) return aC ? -1 : 1;
    return (ZETEL_MAP[b] || 0) - (ZETEL_MAP[a] || 0);
  });
}

// ── STATE ─────────────────────────────────────────────────────────────────────
let vergaderingen = [], moties = [], camerasActief = [], camerasGeschiedenis = [], woningsluitingen = [], collegebrieven = [], stemmingen = [], uitval = [];
let huidigeClaims = [];
let _chartFractie = null;
let totaalTeller = {};
let percentageHistorie = {};

// ── INIT ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  const savedKey = localStorage.getItem('zr_gemini_key');
  if (savedKey) document.getElementById('geminiKey').value = savedKey;
  await Promise.all([loadVerg(), loadMoties(), loadCamerasEnSluitingen(), loadCollegebrieven(), loadStemmingen(), loadUitval()]);
  updateStats();
  renderOpgeslagenClaims();
  // NIEUW: cross-dataset visualisaties — pas renderen als alle bronnen binnen zijn
  renderActiviteitHeatmap();
  renderOnderwerpenChart();
  document.getElementById('headerMeta').textContent =
    vergaderingen[0]?.bijgewerkt ? 'bijgewerkt ' + vergaderingen[0].bijgewerkt : '';
});

// ── TABS ──────────────────────────────────────────────────────────────────────
function switchTab(el) {
  const name = el.dataset.tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
}

// ── LOAD DATA ─────────────────────────────────────────────────────────────────
async function loadVerg() {
  try {
    const r = await fetch('./data/vergaderingen.json');
    if (!r.ok) throw new Error(r.status);
    vergaderingen = await r.json();
    renderVerg(); renderOvVerg();
    // NIEUW #2 + #6
    renderVergDuurChart();
    renderSpreektijdRanglijst();
  } catch (e) {
    document.getElementById('vergList').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen vergaderingen — draai eerst de scraper.</div>'
        : `<div class="error-msg">Kon vergaderingen.json niet laden: ${e.message}</div>`;
  }
}

async function loadMoties() {
  try {
    const r = await fetch('./data/moties.json');
    if (!r.ok) throw new Error(r.status);
    moties = await r.json();
    renderMoties(); renderMotiesVisuals(); populatePartijFilter();
    // NIEUW #12
    renderMotieAmendementViz();
  } catch (e) {
    document.getElementById('motiesTable').innerHTML =
      `<tr><td colspan="5" class="${e.message === '404' ? 'empty' : 'error-msg'}">
        ${e.message === '404' ? 'Nog geen moties — draai eerst de scraper.' : 'Fout: ' + e.message}
      </td></tr>`;
  }
}

async function loadCamerasEnSluitingen() {
  try {
    const r = await fetch('./data/cameras_actief.json');
    if (!r.ok) throw new Error(r.status);
    camerasActief = await r.json();
  } catch (e) {
    camerasActief = [];
    document.getElementById('camActiefList').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen camera-data — draai eerst scrape_besluiten.py.</div>'
        : `<div class="error-msg">Fout bij laden cameras_actief.json: ${e.message}</div>`;
  }
  try {
    const r = await fetch('./data/cameras_geschiedenis.json');
    if (!r.ok) throw new Error(r.status);
    camerasGeschiedenis = await r.json();
  } catch (e) {
    camerasGeschiedenis = [];
  }
  try {
    const r = await fetch('./data/woningsluitingen.json');
    if (!r.ok) throw new Error(r.status);
    woningsluitingen = await r.json();
  } catch (e) {
    woningsluitingen = [];
    document.getElementById('bkList').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen woningsluitingen — draai eerst scrape_besluiten.py.</div>'
        : `<div class="error-msg">Fout: ${e.message}</div>`;
  }
  renderBekendmakingenDashboard();
  renderOvBk();
  renderBekendmakingenLijst();
  renderCamGeschiedenisLijst();
  // NIEUW #22 + #23
  renderCamVerlengingChart();
  renderWoningsluitingDuurChart();
}

async function loadRaadsvragen() {
  try {
    const r = await fetch('./data/raadsvragen.json');
    if (!r.ok) throw new Error(r.status);
    raadsvragen = await r.json();
    renderRaadsvragen(); populateRvFilter();
  } catch (e) {
    document.getElementById('rvList').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen raadsvragen — draai eerst de scraper.</div>'
        : `<div class="error-msg">Fout: ${e.message}</div>`;
  }
}

async function loadStemmingen() {
  try {
    const r = await fetch('./data/stemmingen.json');
    if (!r.ok) throw new Error(r.status);
    stemmingen = await r.json();
    renderStemmingen();
    renderStemStats();
    // NIEUW #10 + #11
    renderCoalitieTijdChart();
  } catch (e) {
    document.getElementById('stemList').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen stemmingen — draai eerst de scraper.</div>'
        : `<div class="error-msg">Fout: ${e.message}</div>`;
  }
}

async function loadUitval() {
  // 1. Teller + percentage-historie laden — renderUitval() heeft dit nodig
  try {
    const r = await fetch('./data/ebs_totaal_teller.json');
    if (r.ok) totaalTeller = await r.json();
  } catch (e) {
    totaalTeller = {};
  }
  try {
    const r = await fetch('./data/ebs_percentage_historie.json');
    if (r.ok) percentageHistorie = await r.json();
  } catch (e) {
    percentageHistorie = {};
  }

  // 2. Dan pas de uitvaldata laden, dedupliceren en renderen
  try {
    const r = await fetch('./data/ebs_uitval.json');
    if (!r.ok) throw new Error(r.status);
    const ruw = await r.json();
    // FIX: vangnet tegen historische duplicaten (ontstaan door overlappende
    // workflow-runs vóór de concurrency-fix in scrape_ebs.yml). Zelfde "id",
    // ander "bijgewerkt" — hou per id alleen de laatst bijgewerkte versie aan.
    const laatsteVersie = {};
    ruw.forEach(rit => {
      const bestaand = laatsteVersie[rit.id];
      if (!bestaand || rit.bijgewerkt > bestaand.bijgewerkt) laatsteVersie[rit.id] = rit;
    });
    uitval = Object.values(laatsteVersie);
    renderUitval();
  } catch (e) {
    document.getElementById('uvLijst').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen uitvaldata — draai eerst scrape_ebs.py.</div>'
        : `<div class="error-msg">Fout: ${e.message}</div>`;
  }
}

// ── EBS UITVAL ────────────────────────────────────────────────────────────────
function renderUitval() {
  const uitgevallen = uitval.filter(r => r.status === 'cancelled' || r.status === 'verkort');
  // ── STATS ─────────────────────────────────────────────────────────────────
  const datums = [...new Set(uitval.map(r => r.datum))].sort();

  let totaalRittenPeriode = 0;
  datums.forEach(d => {
    if (percentageHistorie[d]) totaalRittenPeriode += percentageHistorie[d].totaal;
    else if (totaalTeller[d]) totaalRittenPeriode += totaalTeller[d].totaal;
  });

  document.getElementById('uvTotaal').textContent = uitgevallen.length;
  const pct = totaalRittenPeriode ? Math.round(uitgevallen.length / totaalRittenPeriode * 100) : 0;
  document.getElementById('uvPct').textContent = pct + '%';
  document.getElementById('uvPeriode').textContent =
    datums.length ? datums[0] + ' t/m ' + datums[datums.length - 1] : 'geen data';

  const lijnTeller = {};
  uitgevallen.forEach(r => { lijnTeller[r.lijn] = (lijnTeller[r.lijn] || 0) + 1; });
  const topLijnEntry = Object.entries(lijnTeller).sort((a,b) => b[1]-a[1])[0];
  if (topLijnEntry) {
    document.getElementById('uvTopLijn').textContent = topLijnEntry[0];
    document.getElementById('uvTopLijnSub').textContent = topLijnEntry[1] + ' uitvallen';
  }

  const oorzaakTeller = {};
  uitgevallen.forEach(r => (r.oorzaak_categorieen || []).forEach(o => {
    oorzaakTeller[o] = (oorzaakTeller[o] || 0) + 1;
  }));
  const topOorzaakEntry = Object.entries(oorzaakTeller).sort((a,b) => b[1]-a[1])[0];
  if (topOorzaakEntry) {
    document.getElementById('uvTopOorzaak').textContent = topOorzaakEntry[0];
    document.getElementById('uvTopOorzaakSub').textContent = topOorzaakEntry[1] + 'x geregistreerd';
  }

  // ── UITVAL PER DAG (SVG) ──────────────────────────────────────────────────
  const dagMap = {};
  uitval.forEach(r => {
    if (!dagMap[r.datum]) dagMap[r.datum] = { totaal: 0, cancelled: 0, verkort: 0 };
    dagMap[r.datum].totaal++;
    if (r.status === 'cancelled') dagMap[r.datum].cancelled++;
    if (r.status === 'verkort')   dagMap[r.datum].verkort++;
  });
  const dagLijst = Object.entries(dagMap).sort((a,b) => a[0].localeCompare(b[0]));
  const dagEl = document.getElementById('uvDagChart');

  if (dagLijst.length < 1) {
    dagEl.innerHTML = '<div class="viz-empty">Onvoldoende data</div>';
  } else {
    const W = 680, H = 180;
    const PAD = { t: 10, r: 16, b: 36, l: 36 };
    const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;
    const maxC = Math.max(...dagLijst.map(([,d]) => d.cancelled + d.verkort), 1);
    const slot = pW / dagLijst.length;
    const barW = Math.max(6, Math.floor(slot * 0.6));

    const bars = dagLijst.map(([datum, d], i) => {
      const x    = PAD.l + i * slot + (slot - barW) / 2;
      const yB   = PAD.t + pH;
      const hCan = Math.round((d.cancelled / maxC) * pH);
      const hVer = Math.round((d.verkort   / maxC) * pH);
      const dd   = datum.slice(5); // MM-DD
      return `
        <rect x="${x}" y="${yB - hCan}" width="${barW}" height="${hCan}" fill="var(--stop)" opacity="0.82" rx="1">
          <title>${datum}: ${d.cancelled} cancelled</title></rect>
        <rect x="${x}" y="${yB - hCan - hVer}" width="${barW}" height="${hVer}" fill="var(--hold)" opacity="0.75">
          <title>${datum}: ${d.verkort} verkort</title></rect>
        <text x="${x+barW/2}" y="${H-4}" text-anchor="middle" font-size="9" fill="var(--muted)">${dd}</text>`;
    }).join('');

    const yTicks = [0, Math.ceil(maxC/2), maxC].map(v => {
      const y = PAD.t + pH - (v/maxC)*pH;
      return `<line x1="${PAD.l}" y1="${y}" x2="${PAD.l+pW}" y2="${y}" stroke="var(--rule)" stroke-width="0.5"/>
              <text x="${PAD.l-4}" y="${y+3}" text-anchor="end" font-size="9" fill="var(--muted)">${v}</text>`;
    }).join('');

    dagEl.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:${H}px;">
        ${yTicks}
        <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
        <line x1="${PAD.l}" y1="${PAD.t+pH}" x2="${PAD.l+pW}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
        ${bars}
      </svg>
      <div style="display:flex;gap:16px;padding:4px 0 12px;font-size:10px;color:var(--muted);">
        <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;background:var(--stop);opacity:.82;display:inline-block;"></span>Cancelled</span>
        <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;background:var(--hold);opacity:.75;display:inline-block;"></span>Verkort</span>
      </div>`;
  }

  // ── UITVAL PER DAGDEEL ────────────────────────────────────────────────────
  const dagdeelVolgorde = ['ochtendspits','dal','avondspits','avond','nacht','onbekend'];
  const dagdeelLabels   = { ochtendspits:'Ochtendspits (7–9)', dal:'Dal (9–16)', avondspits:'Avondspits (16–19)', avond:'Avond (19–24)', nacht:'Nacht (0–7)', onbekend:'Onbekend' };
  const dagdeelTeller = {};
  uitgevallen.forEach(r => { dagdeelTeller[r.dagdeel] = (dagdeelTeller[r.dagdeel] || 0) + 1; });
  const maxDd = Math.max(...Object.values(dagdeelTeller), 1);
  document.getElementById('uvDagdeelChart').innerHTML = dagdeelVolgorde
    .filter(d => dagdeelTeller[d] > 0)
    .map(d => `<div class="viz-bar-row">
      <div class="viz-bar-label" style="width:160px;">${dagdeelLabels[d]}</div>
      <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(dagdeelTeller[d]/maxDd*100)}%;background:var(--stop);"></div></div>
      <div class="viz-bar-pct">${dagdeelTeller[d]}</div>
    </div>`).join('') || '<div class="viz-empty">Geen data</div>';

  // ── UITVAL PER LIJN ───────────────────────────────────────────────────────
  const maxLijn = Math.max(...Object.values(lijnTeller), 1);
  const lijnLijst = Object.entries(lijnTeller).sort((a,b) => b[1]-a[1]);
  document.getElementById('uvLijnChart').innerHTML = lijnLijst
    .map(([lijn, n]) => {
      const rit = uitgevallen.find(r => r.lijn === lijn);
      const kleur = rit?.lijnkleur || 'var(--navy)';
      return `<div class="viz-bar-row">
        <div style="width:52px;flex-shrink:0;display:flex;align-items:center;">
          <span class="badge" style="background:${kleur};color:#fff;font-size:11px;font-weight:700;">${esc(lijn)}</span>
        </div>
        <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(n/maxLijn*100)}%;background:var(--stop);"></div></div>
        <div class="viz-bar-pct">${n}</div>
      </div>`;
    }).join('') || '<div class="viz-empty">Geen data</div>';

  // ── UITVAL PER HALTE ──────────────────────────────────────────────────────
  const halteTeller = {};
  uitgevallen.forEach(r => {
    const eersteHalte = (r.haltes || [])[0];
    if (eersteHalte) {
      halteTeller[eersteHalte.halte_naam] = (halteTeller[eersteHalte.halte_naam] || 0) + 1;
    }
  });
  const maxHalte = Math.max(...Object.values(halteTeller), 1);
  document.getElementById('uvHalteChart').innerHTML = Object.entries(halteTeller)
    .sort((a,b) => b[1]-a[1])
    .map(([naam, n]) => `<div class="viz-bar-row">
      <div class="viz-bar-label" style="width:200px;" title="${esc(naam)}">${esc(naam)}</div>
      <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(n/maxHalte*100)}%;background:var(--teal);"></div></div>
      <div class="viz-bar-pct">${n}</div>
    </div>`).join('') || '<div class="viz-empty">Geen data</div>';

  // ── UITVAL PER OORZAAK ────────────────────────────────────────────────────
  const maxOorzaak = Math.max(...Object.values(oorzaakTeller), 1);
  document.getElementById('uvOorzaakChart').innerHTML = Object.entries(oorzaakTeller)
    .sort((a,b) => b[1]-a[1])
    .map(([oorzaak, n]) => `<div class="viz-bar-row">
      <div class="viz-bar-label" style="width:200px;">${esc(oorzaak)}</div>
      <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(n/maxOorzaak*100)}%;background:var(--hold);"></div></div>
      <div class="viz-bar-pct">${n}</div>
    </div>`).join('') || '<div class="viz-empty">Geen data</div>';

  // ── FILTER-OPTIES VULLEN ──────────────────────────────────────────────────
  const selLijn = document.getElementById('filterUvLijn');
  selLijn.innerHTML = '<option value="">Alle lijnen</option>';
  [...new Set(uitgevallen.map(r => r.lijn).filter(Boolean))].sort((a,b)=>parseInt(a)-parseInt(b))
    .forEach(l => { const o = document.createElement('option'); o.value = l; o.textContent = 'Lijn ' + l; selLijn.appendChild(o); });

  renderUitvalLijst();
}

function renderUitvalLijst() {
  const statusF  = document.getElementById('filterUvStatus').value;
  const lijnF    = document.getElementById('filterUvLijn').value;
  const dagdeelF = document.getElementById('filterUvDagdeel').value;

  let f = uitval.filter(r => r.status === 'cancelled' || r.status === 'verkort');
  if (statusF)  f = f.filter(r => r.status === statusF);
  if (lijnF)    f = f.filter(r => r.lijn === lijnF);
  if (dagdeelF) f = f.filter(r => r.dagdeel === dagdeelF);

  document.getElementById('uvLijstCount').textContent = f.length + ' uitgevallen ritten';

  document.getElementById('uvLijst').innerHTML = f.length === 0
    ? '<div class="empty">Geen uitgevallen ritten gevonden.</div>'
    : f.map(r => {
        const eersteHalte  = (r.haltes || [])[0];
        const statusKleur  = r.status === 'cancelled' ? 'var(--stop)' : 'var(--hold)';
        const statusLabel  = r.status === 'cancelled' ? '✗ Cancelled' : '⚠ Verkort';
        const oorzaakStr   = (r.oorzaak_categorieen || []).join(', ') || '—';
        return `<div class="bk-item">
          <div class="bk-top">
            <div style="flex:1;">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-wrap:wrap;">
                <span style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:var(--muted);">${esc(r.datum)} ${esc(r.eerste_tijd || '')}</span>
                <span class="badge" style="background:${r.lijnkleur||'var(--navy)'};color:#fff;font-size:11px;font-weight:700;">${esc(r.lijn || '?')}</span>
                <span style="font-size:13px;font-weight:600;">${esc(eersteHalte?.bestemming || '—')}</span>
                <span style="font-size:12px;font-weight:700;color:${statusKleur};">${statusLabel}</span>
                <span class="badge badge-teal" style="font-size:10px;">${esc(r.dagdeel)}</span>
              </div>
              <div class="bk-meta" style="flex-wrap:wrap;gap:6px;">
                ${(r.haltes || []).map(h => `<span style="font-size:11px;color:var(--muted);">
                  ${esc(h.halte_naam)} ${esc(h.geplande_tijd)}${h.platform ? ' · '+esc(h.platform) : ''}
                </span>`).join('<span style="color:var(--rule)">·</span>')}
              </div>
              ${r.oorzaak_categorieen?.length ? `<div style="margin-top:4px;font-size:11px;color:var(--hold);">Oorzaak: ${esc(oorzaakStr)}</div>` : ''}
              ${r.terminus_alert ? `<div style="margin-top:3px;font-size:11px;color:var(--stop);">${esc(r.terminus_alert)}</div>` : ''}
              ${r.advies ? `<div style="margin-top:3px;font-size:11px;color:var(--muted);">${esc(r.advies)}</div>` : ''}
            </div>
          </div>
        </div>`;
      }).join('');
}

async function loadCollegebrieven() {
  try {
    const r = await fetch('./data/collegeberichten.json');
    if (!r.ok) throw new Error(r.status);
    collegebrieven = await r.json();
    renderCollegebrieven(); populateCbFilters(); renderOvCb(); renderCbStats();
    // NIEUW #16 + #17
    renderPortefeuillehouderDashboard();
    renderGeldChart();
  } catch (e) {
    document.getElementById('cbList').innerHTML =
      e.message === '404'
        ? '<div class="empty">Nog geen collegebrieven — draai eerst de scraper.</div>'
        : `<div class="error-msg">Fout: ${e.message}</div>`;
    document.getElementById('ovCb').innerHTML = '<div class="empty">Geen data</div>';
  }
}

// ── STATS ─────────────────────────────────────────────────────────────────────
function updateStats() {
  document.getElementById('statV').textContent = vergaderingen.length;
  const raadCount = vergaderingen.filter(v => v.type === 'Raadsvergadering').length;
  const commCount = vergaderingen.filter(v => v.type === 'Commissievergadering').length;
  document.getElementById('statVsub').textContent = raadCount + ' raad · ' + commCount + ' commissie';

  document.getElementById('statM').textContent = moties.length;
  const aang = moties.filter(m => m.status === 'aangenomen').length;
  const verw  = moties.filter(m => m.status === 'verworpen').length;
  document.getElementById('statMsub').textContent = aang + ' aangenomen · ' + verw + ' verworpen';

  const cameraNamenOoit = new Set([...camerasActief, ...camerasGeschiedenis].map(c => c.camera));
  document.getElementById('statB').textContent = camerasActief.length + woningsluitingen.length;
  document.getElementById('statBsub').textContent = camerasActief.length + ' camera actief (' + cameraNamenOoit.size + ' ooit) · ' + woningsluitingen.length + ' sluiting';

  document.getElementById('statC').textContent = collegebrieven.length;
  const totalClaims = collegebrieven.reduce((s, b) => s + (b.claims?.length || 0), 0);
  document.getElementById('statCsub').textContent = totalClaims + ' claims geïdentificeerd';

  if (vergaderingen.length > 0) {
    const v = vergaderingen[0];
    const d = new Date(v.datum);
    document.getElementById('statL').textContent = d.toLocaleDateString('nl-NL', { day: 'numeric', month: 'short' });
    document.getElementById('statLsub').textContent = v.type || '';
  }
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #1 — ACTIVITEITSHEATMAP (Overzicht-tab)
// GitHub-stijl dag-blokjes over de laatste ~53 weken, kleur = som van
// vergaderingen + moties + collegebrieven + stemmingen op die dag.
// ══════════════════════════════════════════════════════════════════════════
function renderActiviteitHeatmap() {
  const el = document.getElementById('activiteitHeatmap');
  if (!el) return;

  const telling = {};
  const tel = (datum) => { if (!datum) return; const d = datum.slice(0, 10); telling[d] = (telling[d] || 0) + 1; };
  vergaderingen.forEach(v => tel(v.datum));
  moties.forEach(m => tel(m.datum));
  collegebrieven.forEach(b => tel(b.datum));
  stemmingen.forEach(s => tel(s.datum));

  if (!Object.keys(telling).length) { el.innerHTML = '<div class="viz-empty">Nog geen data om activiteit te tonen</div>'; return; }

  const vandaag = new Date();
  const start = new Date(vandaag);
  start.setDate(start.getDate() - 370);
  // Uitlijnen op maandag zodat elke week-kolom netjes 7 dagen bevat
  const startDagIndex = (start.getDay() + 6) % 7; // 0 = maandag
  start.setDate(start.getDate() - startDagIndex);

  const dagen = [];
  for (let d = new Date(start); d <= vandaag; d.setDate(d.getDate() + 1)) {
    const iso = d.toISOString().slice(0, 10);
    dagen.push({ datum: iso, count: telling[iso] || 0 });
  }

  const maxC = Math.max(...dagen.map(d => d.count), 1);
  const kleur = c => {
    if (!c) return 'var(--rule)';
    const r = c / maxC;
    if (r > 0.75) return 'var(--navy)';
    if (r > 0.5)  return 'var(--teal)';
    if (r > 0.25) return '#5FA8B3';
    return '#BFE0E5';
  };

  const weken = [];
  for (let i = 0; i < dagen.length; i += 7) weken.push(dagen.slice(i, i + 7));

  const maandNamenKort = ['jan','feb','mrt','apr','mei','jun','jul','aug','sep','okt','nov','dec'];
  let vorigeMaand = -1;
  const maandLabelsHtml = weken.map(week => {
    const maand = new Date(week[0].datum + 'T00:00:00').getMonth();
    let label = '';
    if (maand !== vorigeMaand) { label = maandNamenKort[maand]; vorigeMaand = maand; }
    return `<div style="width:13px;font-size:9px;">${label}</div>`;
  }).join('');

  const flatCells = weken.map(week => week.map(dag =>
    `<div class="heatmap-cell" style="background:${kleur(dag.count)};" title="${dag.datum}: ${dag.count} activiteit${dag.count === 1 ? '' : 'en'}"></div>`
  ).join('')).join('');

  el.innerHTML = `
    <div class="heatmap-wrap">
      <div class="heatmap-month-labels">${maandLabelsHtml}</div>
      <div class="heatmap-grid">${flatCells}</div>
      <div class="heatmap-legend">minder
        <div class="heatmap-cell" style="background:var(--rule);"></div>
        <div class="heatmap-cell" style="background:#BFE0E5;"></div>
        <div class="heatmap-cell" style="background:#5FA8B3;"></div>
        <div class="heatmap-cell" style="background:var(--teal);"></div>
        <div class="heatmap-cell" style="background:var(--navy);"></div>
      meer</div>
    </div>`;
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #15 — ONDERWERPENRANGLIJST (Overzicht-tab)
// Simpele keyword-telling op titels van alle databronnen — werkt zonder AI.
// ══════════════════════════════════════════════════════════════════════════
const ONDERWERP_KEYWORDS = {
  'Wonen':                ['woning', 'wonen', 'huur', 'appartement', 'volkshuisvesting'],
  'Veiligheid':           ['veilig', 'ondermijning', 'crimine', 'politie', 'handhaving'],
  'Jeugd & onderwijs':    ['jeugd', 'onderwijs', 'school', 'kinderopvang', 'leerplicht'],
  'Verkeer & mobiliteit':  ['verkeer', 'fiets', 'mobiliteit', 'parkeren', 'weg', 'brug'],
  'Zorg':                 ['zorg', 'ggd', 'gezondheid', 'vaccinatie', 'wmo'],
  'Financiën':            ['begroting', 'financ', 'budget', 'subsidie', 'krediet'],
  'Milieu & duurzaamheid':['milieu', 'duurzaa', 'klimaat', 'energie', 'circulaire'],
  'Cultuur & erfgoed':    ['cultuur', 'erfgoed', 'monument', 'museum'],
  'Economie':             ['economie', 'onderneming', 'bedrijv', 'toerisme'],
  'Bestuur':              ['bestuur', 'college', 'portefeuille', 'verordening'],
};

function renderOnderwerpenChart() {
  const el = document.getElementById('onderwerpenChart');
  if (!el) return;

  const titels = [
    ...vergaderingen.map(v => v.titel),
    ...vergaderingen.flatMap(v => (v.agendapunten || []).map(a => a.titel)),
    ...moties.map(m => m.titel),
    ...collegebrieven.map(b => b.titel),
    ...stemmingen.map(s => s.titel),
  ].filter(Boolean).join(' \n ').toLowerCase();

  const tellingen = Object.entries(ONDERWERP_KEYWORDS).map(([naam, keywords]) => {
    const count = keywords.reduce((som, kw) => som + Math.max(0, titels.split(kw).length - 1), 0);
    return { naam, count };
  }).filter(t => t.count > 0).sort((a, b) => b.count - a.count);

  if (!tellingen.length) { el.innerHTML = '<div class="viz-empty">Geen onderwerpen herkend</div>'; return; }
  const maxC = tellingen[0].count;
  el.innerHTML = tellingen.map(t => `<div class="viz-bar-row">
    <div class="viz-bar-label" style="width:170px;">${esc(t.naam)}</div>
    <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(t.count/maxC*100)}%;background:var(--teal);"></div></div>
    <div class="viz-bar-pct">${t.count}</div>
  </div>`).join('');
}

// ── VERGADERINGEN ─────────────────────────────────────────────────────────────
function renderVerg() {
  const typeFilter = document.getElementById('filterVergType')?.value || '';
  const filtered   = typeFilter ? vergaderingen.filter(v => v.type === typeFilter) : vergaderingen;
  document.getElementById('vergCount').textContent = filtered.length + ' vergaderingen';
  if (!filtered.length) {
    document.getElementById('vergList').innerHTML = '<div class="empty">Geen vergaderingen gevonden.</div>';
    return;
  }
  document.getElementById('vergList').innerHTML = filtered.map(v => `
    <div class="meeting">
      <div class="meeting-row" onclick="toggleMeeting('${v.id}')">
        <div class="meeting-date-tag">${fmtDate(v.datum, 'short')}</div>
        <div class="meeting-info">
          <div class="meeting-title-text">${esc(v.titel)}</div>
          <div class="meeting-type-text">${esc(v.type || '')}</div>
        </div>
        <div class="meeting-badges">
          ${v.agendapunten?.length ? `<span class="badge badge-teal">${v.agendapunten.length} punten</span>` : ''}
          ${v.heeft_video ? '<span class="badge badge-go">Video</span>' : ''}
          ${v.datum > new Date().toISOString().slice(0,10) ? '<span class="badge badge-hold">Gepland</span>' : ''}
        </div>
        <div class="meeting-chevron" id="ch-${v.id}">›</div>
      </div>
      <div class="meeting-details" id="det-${v.id}">
        ${v.heeft_video ? `
          <div class="video-section">
            <div class="video-label">⬇ Video beschikbaar</div>
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
              <button class="btn-primary" style="width:auto;padding:8px 16px;"
                onclick="toggleVideoInstructie('${v.id}', event)">
                Toon download-instructie
              </button>
              <a href="${esc(v.url)}" target="_blank"
                style="font-size:12px;color:var(--teal);text-decoration:none;font-weight:500;">
                Open in iBabs →
              </a>
            </div>
            <div id="vinstr-${v.id}" style="display:none;">
              <div class="vinstr">
                <div class="vinstr-step"><div class="vinstr-nr">1</div><div class="vinstr-tekst">Open de vergaderpagina via de knop hierboven. Start de video zodat hij begint te spelen.</div></div>
                <div class="vinstr-step"><div class="vinstr-nr">2</div><div class="vinstr-tekst">Open <strong>DevTools</strong> met <strong>F12</strong> of <strong>⌥⌘J</strong>. Ga naar het tabblad <strong>Netwerk</strong>.</div></div>
                <div class="vinstr-step"><div class="vinstr-nr">3</div><div class="vinstr-tekst">Typ in het filterveld: <strong>m3u8</strong><br>Je ziet nu één of twee verzoeken. Negeer de kleine (~3 kB). Klik op de grote (<strong>~100–120 kB</strong>).</div></div>
                <div class="vinstr-step"><div class="vinstr-nr">4</div><div class="vinstr-tekst">Ga naar <strong>Headers</strong> → scroll naar <strong>Request URL</strong> → rechtsklik → <strong>Copy link address</strong>.</div></div>
                <div class="vinstr-step">
                  <div class="vinstr-nr">5</div>
                  <div class="vinstr-tekst">
                    Plak de gekopieerde URL hieronder — het yt-dlp commando verschijnt automatisch:
                    <div class="vinstr-url-wrap">
                      <input type="text" class="vinstr-url-input" id="vhls-${v.id}"
                        placeholder="https://...sdk-ssl.m3u8?Signature=..."
                        oninput="updateYtdlpCmd('${v.id}')">
                    </div>
                    <div id="vcmd-wrap-${v.id}" style="display:none;margin-top:6px;">
                      <div style="display:flex;align-items:center;gap:8px;">
                        <div class="vinstr-cmd" id="vcmd-${v.id}" style="flex:1;"></div>
                        <button class="btn-copy" onclick="copyCmd('${v.id}',event)">Kopieer</button>
                      </div>
                      <div class="vinstr-tip">⚠ Deze link verloopt na ~2 uur. Herhaal stap 3–4 als je een 403-fout krijgt.</div>
                    </div>
                  </div>
                </div>
                <div class="vinstr-step"><div class="vinstr-nr">6</div><div class="vinstr-tekst">Voer het commando uit in Terminal. Je krijgt een <strong>.ts bestand</strong> van honderden MB.<br>Sleep het audiogedeelte daarna naar de <strong>✦ Factcheck-tab</strong> om te transcriberen.</div></div>
              </div>
            </div>
          </div>
        ` : ''}
        ${(v.agendapunten || []).map(ap => `
          <div class="agenda-item">
            <span class="agenda-nr">${esc(ap.nummer || '')}</span>
            <span class="agenda-title-text">${esc(ap.titel || '')}</span>
            ${(ap.sprekers || []).length ? `
              <div class="speakers">
                ${ap.sprekers.map(s => `
                  <span class="speaker-tag">
                    ${s.tijd ? `<span class="speaker-time">${esc(s.tijd)}</span> ` : ''}${esc(s.naam)}
                  </span>`).join('')}
              </div>` : ''}
          </div>
        `).join('') || '<div class="agenda-item" style="color:var(--muted);font-size:12px;">Geen agendapunten beschikbaar</div>'}
      </div>
    </div>
  `).join('');
}

function toggleMeeting(id) {
  const det = document.getElementById('det-' + id);
  const ch  = document.getElementById('ch-'  + id);
  if (!det) return;
  const open = det.classList.toggle('open');
  ch.classList.toggle('open', open);
}

function toggleVideoInstructie(id, e) {
  e.stopPropagation();
  const el  = document.getElementById('vinstr-' + id);
  const btn = e.target;
  const open = el.style.display === 'none';
  el.style.display = open ? 'block' : 'none';
  btn.textContent  = open ? 'Verberg instructie' : 'Toon download-instructie';
}

function updateYtdlpCmd(id) {
  const url  = document.getElementById('vhls-' + id).value.trim();
  const wrap = document.getElementById('vcmd-wrap-' + id);
  const cmd  = document.getElementById('vcmd-' + id);
  if (url) {
    cmd.textContent = `yt-dlp "${url}" -o vergadering_${id}.ts`;
    wrap.style.display = 'block';
  } else {
    wrap.style.display = 'none';
  }
}

function copyCmd(id, e) {
  const tekst = document.getElementById('vcmd-' + id).textContent;
  navigator.clipboard.writeText(tekst)
    .then(() => { e.target.textContent = '✓'; setTimeout(() => e.target.textContent = 'Kopieer', 2000); });
}

function renderOvVerg() {
  document.getElementById('ovVerg').innerHTML =
    vergaderingen.slice(0, 5).map(v => `
      <div class="mini-item">
        <div class="mini-date">${fmtDate(v.datum, 'short')}</div>
        <div>
          <div class="mini-title">${esc(v.titel)}</div>
          <div class="mini-type">${esc(v.type || '')}</div>
        </div>
      </div>`).join('') || '<div class="empty">Geen data</div>';
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #2 — VERGADERDUUR DOOR DE TIJD (Vergaderingen-tab)
// eind − start per vergadering, kleur = type.
// ══════════════════════════════════════════════════════════════════════════
function renderVergDuurChart() {
  const el = document.getElementById('vergDuurChart');
  if (!el) return;

  const data = vergaderingen
    .filter(v => v.start && v.eind)
    .map(v => {
      const start = new Date(v.start), eind = new Date(v.eind);
      const minuten = Math.round((eind - start) / 60000);
      return { datum: v.datum, minuten, type: v.type };
    })
    .filter(d => d.minuten > 0 && d.minuten < 720) // sanity check: max 12 uur
    .sort((a, b) => a.datum.localeCompare(b.datum));

  if (data.length < 2) { el.innerHTML = '<div class="viz-empty">Onvoldoende data — start/eind ontbreken bij te veel vergaderingen</div>'; return; }

  const W = 680, H = 220, PAD = { t: 14, r: 16, b: 34, l: 40 };
  const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;
  const maxMin = Math.max(...data.map(d => d.minuten)) * 1.1;
  const slot = pW / data.length;
  const barW = Math.max(4, Math.min(22, slot * 0.6));

  const bars = data.map((d, i) => {
    const x = PAD.l + i * slot + (slot - barW) / 2;
    const h = Math.round((d.minuten / maxMin) * pH);
    const y = PAD.t + pH - h;
    const kleur = d.type === 'Raadsvergadering' ? 'var(--navy)' : d.type === 'Commissievergadering' ? 'var(--teal)' : 'var(--hold)';
    return `<rect x="${x}" y="${y}" width="${barW}" height="${h}" fill="${kleur}" opacity="0.85" rx="1">
      <title>${d.datum}: ${Math.floor(d.minuten/60)}u${String(d.minuten%60).padStart(2,'0')} — ${esc(d.type||'')}</title></rect>`;
  }).join('');

  const yTicks = [0, Math.round(maxMin/2), Math.round(maxMin)].map(v => {
    const y = PAD.t + pH - (v/maxMin)*pH;
    return `<line x1="${PAD.l}" y1="${y}" x2="${PAD.l+pW}" y2="${y}" stroke="var(--rule)" stroke-width="0.5"/>
            <text x="${PAD.l-6}" y="${y+3}" text-anchor="end" font-size="9" fill="var(--muted)">${Math.round(v/60)}u</text>`;
  }).join('');

  const stapX = Math.max(1, Math.ceil(data.length / 8));
  const xLabels = data.map((d, i) => {
    if (i % stapX !== 0) return '';
    const x = PAD.l + i * slot + slot / 2;
    return `<text x="${x}" y="${H-8}" text-anchor="middle" font-size="9" fill="var(--muted)">${fmtDate(d.datum,'short')}</text>`;
  }).join('');

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:${H}px;">
    ${yTicks}
    <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
    <line x1="${PAD.l}" y1="${PAD.t+pH}" x2="${PAD.l+pW}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
    ${bars}${xLabels}
  </svg>
  <div class="svg-chart-legend">
    <span><span class="dot" style="background:var(--navy);"></span>Raadsvergadering</span>
    <span><span class="dot" style="background:var(--teal);"></span>Commissievergadering</span>
    <span><span class="dot" style="background:var(--hold);"></span>Overig</span>
  </div>`;
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #6 — SPREEKTIJD-RANGLIJST (Vergaderingen-tab)
// LET OP: "tijd" in agendapunten[].sprekers[] kan een tijdstip zijn (bv.
// "20:14", moment waarop iemand begon) of een duur. We detecteren dit
// automatisch: bij een uur-waarde ≥6 gaan we uit van een tijdstip en
// berekenen we de duur als het verschil met de volgende spreker in
// hetzelfde agendapunt. Kan dat niet betrouwbaar (bv. alleen 1 spreker,
// of geen "tijd"-veld), dan vallen we terug op "aantal keer aan het
// woord" — dat wordt dan duidelijk in de UI vermeld.
// Koppeling raadslid → fractie gebeurt via de raadsleden-lijsten in
// stemmingen.json (beste-poging matching op achternaam, want de exacte
// schrijfwijze in agendapunten kan afwijken).
// ══════════════════════════════════════════════════════════════════════════
function bouwRaadslidFractieLookup() {
  const map = {};
  stemmingen.forEach(s => {
    [...(s.raadsleden_voor || []), ...(s.raadsleden_tegen || []), ...(s.raadsleden_onthouding || [])].forEach(r => {
      const achternaam = (r.naam.split(',')[0] || r.naam).trim().toLowerCase();
      if (achternaam) map[achternaam] = r.fractie;
    });
  });
  return map;
}

function zoekFractieVoorSpreker(naam, lookup) {
  if (!naam) return null;
  const naamLower = naam.toLowerCase().replace(/[.,]/g, '');
  const woorden = naamLower.split(/\s+/).filter(Boolean);
  for (const w of woorden) { if (lookup[w]) return lookup[w]; }
  for (const [achternaam, fractie] of Object.entries(lookup)) {
    if (achternaam && naamLower.includes(achternaam)) return fractie;
  }
  return null;
}

function parseSpreektijdWaarde(tijdStr) {
  if (!tijdStr) return null;
  const m = String(tijdStr).match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return null;
  const a = parseInt(m[1]), b = parseInt(m[2]);
  if (a > 23 || b > 59) return null;
  if (a >= 6) return { type: 'tijdstip', seconden: a * 3600 + b * 60 };
  return { type: 'duur', seconden: a * 60 + b };
}

function renderSpreektijdRanglijst() {
  const el = document.getElementById('spreektijdChart');
  if (!el) return;
  const groepering = document.getElementById('toggleSpreektijdGroepering')?.value || 'fractie';
  const lookup = bouwRaadslidFractieLookup();

  let voorbeeld = null;
  vergaderingen.forEach(v => (v.agendapunten || []).forEach(ap => (ap.sprekers || []).forEach(s => {
    if (voorbeeld === null) { const p = parseSpreektijdWaarde(s.tijd); if (p) voorbeeld = p.type; }
  })));

  if (voorbeeld === null) { el.innerHTML = '<div class="viz-empty">Geen sprekersdata beschikbaar in de agendapunten</div>'; return; }

  const teller = {};
  let eenheid;

  if (voorbeeld === 'tijdstip') {
    eenheid = 'sec';
    vergaderingen.forEach(v => (v.agendapunten || []).forEach(ap => {
      const sprekers = (ap.sprekers || [])
        .map(s => ({ naam: s.naam, parsed: parseSpreektijdWaarde(s.tijd) }))
        .filter(s => s.parsed);
      for (let i = 0; i < sprekers.length; i++) {
        const huidig = sprekers[i], volgende = sprekers[i + 1];
        let duur = volgende ? volgende.parsed.seconden - huidig.parsed.seconden : NaN;
        if (!(duur > 0 && duur <= 1800)) duur = 90; // fallback bij ontbrekende/onlogische volgende timestamp
        const fractie = zoekFractieVoorSpreker(huidig.naam, lookup);
        const key = groepering === 'fractie' ? (fractie || 'Onbekend') : huidig.naam;
        teller[key] = (teller[key] || 0) + duur;
      }
    }));
  } else {
    eenheid = 'keer';
    vergaderingen.forEach(v => (v.agendapunten || []).forEach(ap => (ap.sprekers || []).forEach(s => {
      const fractie = zoekFractieVoorSpreker(s.naam, lookup);
      const key = groepering === 'fractie' ? (fractie || 'Onbekend') : s.naam;
      teller[key] = (teller[key] || 0) + 1;
    })));
  }

  const rijen = Object.entries(teller).sort((a, b) => b[1] - a[1]).slice(0, 15);
  if (!rijen.length) { el.innerHTML = '<div class="viz-empty">Geen sprekersdata te koppelen aan fracties</div>'; return; }
  const maxV = rijen[0][1];
  el.innerHTML = rijen.map(([naam, v]) => {
    const label = eenheid === 'sec' ? `${Math.floor(v/60)}m ${v%60}s` : `${v}x`;
    return `<div class="viz-bar-row">
      <div class="viz-bar-label" style="width:180px;" title="${esc(naam)}">${esc(naam)}</div>
      <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(v/maxV*100)}%;background:var(--teal);"></div></div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);width:60px;text-align:right;flex-shrink:0;">${label}</div>
    </div>`;
  }).join('') + (eenheid === 'keer'
    ? '<div style="padding:8px 20px 0;font-size:10px;color:var(--muted);">⚠ Het "tijd"-veld leek geen spreekduur te zijn — geteld op aantal keer aan het woord in plaats van spreektijd.</div>'
    : '');
}

// ── MOTIES ────────────────────────────────────────────────────────────────────
function populatePartijFilter() {
  const sel = document.getElementById('filterPartij');
  const partijen = [...new Set(moties.map(m => m.partij).filter(Boolean))].sort();
  partijen.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; sel.appendChild(o); });
}

function renderMoties() {
  const partij = document.getElementById('filterPartij').value;
  const status = document.getElementById('filterStatus').value;
  const type   = document.getElementById('filterType').value;
  let f = moties.filter(m => m.status != null);
  if (partij) f = f.filter(m => m.partij === partij);
  if (status) f = f.filter(m => m.status === status);
  if (type)   f = f.filter(m => m.type === type);
  document.getElementById('motiesTable').innerHTML = f.length === 0
    ? '<tr><td colspan="5" class="empty">Geen moties gevonden.</td></tr>'
    : f.map(m => `
        <tr>
          <td><div class="motie-title">${esc(m.titel)}</div>${m.type ? `<div class="motie-desc">${esc(m.type)}</div>` : ''}</td>
          <td><span class="badge badge-teal">${esc(m.partij || '—')}</span></td>
          <td style="font-family:'JetBrains Mono',monospace;font-size:11px;white-space:nowrap;">${fmtDate(m.datum,'full')}</td>
          <td>${statusBadge(m.status)}</td>
          <td>${stemmingBar(m)}</td>
        </tr>`).join('');
}

function statusBadge(s) {
  const map = { aangenomen:['badge-go','Aangenomen ✓'], verworpen:['badge-stop','Verworpen ✗'], ingetrokken:['badge-hold','Ingetrokken'], aangehouden:['badge-hold','Aangehouden'] };
  const [cls, label] = map[s] || ['badge-teal', s || '—'];
  return `<span class="badge ${cls}">${label}</span>`;
}

function stemmingBar(m) {
  if (m.voor_pct == null && m.tegen_pct == null) return '<span style="color:var(--muted);font-size:11px;">—</span>';
  return `<div style="min-width:80px;">
    <div style="display:flex;height:6px;border-radius:2px;overflow:hidden;margin-bottom:3px;">
      <div style="width:${m.voor_pct}%;background:var(--go);"></div>
      <div style="width:${m.tegen_pct}%;background:var(--stop);"></div>
    </div>
    <div style="font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--muted);">${m.voor_pct}% voor · ${m.tegen_pct}% tegen</div>
  </div>`;
}

// NIEUW #13: scatter herbouwd als losstaande, herbruikbare functie zodat we
//'m kunnen filteren op type (Motie/Amendement) zonder renderMotiesVisuals
// opnieuw te draaien.
const PARTIJ_KORT_GLOBAL = {
  'GROENLINKS – PvdA': 'GL-PvdA', 'Groenlinks/PvdA': 'GL-PvdA',
  'Democratisch Zaanstad': 'DZ', 'Forum voor Democratie': 'FvD',
  'Forum voor democratie': 'FvD', 'Partij voor de Dieren': 'PvdD',
  'ChristenUnie': 'CU', 'Lokaal Zaans': 'LZ', 'Groep de Boer': 'GdB',
};
function kortGlobal(naam) { return PARTIJ_KORT_GLOBAL[naam] || naam; }

function berekenPartijStatsVoorType(typeFilter) {
  const stats = {};
  moties.forEach(m => {
    if (!m.partij) return;
    if (typeFilter && m.type !== typeFilter) return;
    if (!stats[m.partij]) stats[m.partij] = { totaal: 0, aangenomen: 0, verworpen: 0 };
    const s = stats[m.partij];
    s.totaal++;
    if (m.status === 'aangenomen') s.aangenomen++;
    if (m.status === 'verworpen')  s.verworpen++;
  });
  return Object.entries(stats).map(([naam, s]) => {
    const ms = s.aangenomen + s.verworpen;
    return { naam, ...s, pct: ms > 0 ? Math.round(s.aangenomen / ms * 100) : null };
  });
}

function bouwScatterSVG(typeFilter) {
  const scatter = berekenPartijStatsVoorType(typeFilter).filter(p => p.pct !== null && p.totaal >= 2);
  if (scatter.length < 3) return '<div class="viz-empty">Onvoldoende data voor deze weergave</div>';

  const W = 680, H = 320;
  const PAD = { t:28, r:24, b:44, l:40 };
  const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;
  const maxX = Math.max(...scatter.map(p => p.totaal)) * 1.12;
  const maxTot = Math.max(...scatter.map(p => p.totaal));
  const midX = PAD.l + pW / 2;
  const midY = PAD.t + pH / 2;
  const maxR = 17, minR = 5;

  const qBg = `
    <rect x="${PAD.l}" y="${PAD.t}" width="${pW/2}" height="${pH/2}" fill="#E6F4EC" opacity="0.25"/>
    <rect x="${midX}" y="${PAD.t}" width="${pW/2}" height="${pH/2}" fill="#E6F4EC" opacity="0.42"/>
    <rect x="${PAD.l}" y="${midY}" width="${pW/2}" height="${pH/2}" fill="#FAE9E9" opacity="0.15"/>
    <rect x="${midX}" y="${midY}" width="${pW/2}" height="${pH/2}" fill="#FAE9E9" opacity="0.25"/>
    <line x1="${midX}" y1="${PAD.t}" x2="${midX}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1" stroke-dasharray="4,3"/>
    <line x1="${PAD.l}" y1="${midY}" x2="${PAD.l+pW}" y2="${midY}" stroke="var(--rule)" stroke-width="1" stroke-dasharray="4,3"/>
    <text x="${PAD.l+6}" y="${PAD.t+13}" font-size="9" fill="var(--go)" font-weight="700" opacity="0.6">SELECTIEF · RAAK</text>
    <text x="${midX+6}" y="${PAD.t+13}" font-size="9" fill="var(--go)" font-weight="700" opacity="0.85">ACTIEF · EFFECTIEF</text>
    <text x="${PAD.l+6}" y="${PAD.t+pH-5}" font-size="9" fill="var(--stop)" font-weight="700" opacity="0.45">WEINIG · LAAG SUCCES</text>
    <text x="${midX+6}" y="${PAD.t+pH-5}" font-size="9" fill="var(--stop)" font-weight="700" opacity="0.6">VEEL · LAAG SUCCES</text>`;

  const yTicks = [0, 25, 50, 75, 100].map(v => {
    const y = PAD.t + pH - (v / 100) * pH;
    return `<line x1="${PAD.l}" y1="${y}" x2="${PAD.l+pW}" y2="${y}" stroke="var(--rule)" stroke-width="0.5"/>
            <text x="${PAD.l-5}" y="${y+3}" text-anchor="end" font-size="9" fill="var(--muted)">${v}%</text>`;
  }).join('');

  const xStep = Math.max(1, Math.ceil(maxX / 5 / 5) * 5);
  const xTicks = Array.from({length: 7}, (_, i) => i * xStep)
    .filter(v => v <= maxX)
    .map(v => {
      const x = PAD.l + (v / maxX) * pW;
      return `<line x1="${x}" y1="${PAD.t+pH}" x2="${x}" y2="${PAD.t+pH+4}" stroke="var(--rule)" stroke-width="1"/>
              <text x="${x}" y="${PAD.t+pH+14}" text-anchor="middle" font-size="9" fill="var(--muted)">${v}</text>`;
    }).join('');

  const axisLabels = `
    <text x="${PAD.l+pW/2}" y="${H-2}" text-anchor="middle" font-size="10" fill="var(--muted)">Aantal ingediend →</text>
    <text x="10" y="${PAD.t+pH/2}" text-anchor="middle" font-size="10" fill="var(--muted)" transform="rotate(-90,10,${PAD.t+pH/2})">Slagings% →</text>`;

  const sorted = [...scatter].sort((a, b) => b.totaal - a.totaal);
  const dotData = sorted.map(p => {
    const x = Math.round(PAD.l + (p.totaal / maxX) * pW);
    const y = Math.round(PAD.t + pH - (p.pct / 100) * pH);
    const r = Math.round(minR + (p.totaal / maxTot) * (maxR - minR));
    const k = kortGlobal(p.naam);
    const useLeft = x + r + 4 + k.length * 6.5 > W - 10;
    const lx = useLeft ? x - r - 4 : x + r + 4;
    const anchor = useLeft ? 'end' : 'start';
    return { p, x, y, r, k, lx, ly: y, anchor };
  });

  const geplaatst = [];
  dotData.forEach(d => {
    const lw = d.k.length * 6.5 + 8;
    const lh = 24;
    let ly = d.y;
    for (let poging = 0; poging < 16; poging++) {
      const botst = geplaatst.some(g => Math.abs(d.lx - g.lx) < lw && Math.abs(ly - g.ly) < lh);
      if (!botst) break;
      ly = d.y + (poging % 2 === 0 ? -1 : 1) * Math.ceil((poging + 1) / 2) * lh;
    }
    d.ly = ly;
    geplaatst.push({ lx: d.lx, ly });
  });

  const dots = dotData.map(({ p, x, y, r, k, lx, ly, anchor }) => {
    const isC  = COALITIE_FRACTIES.has(p.naam);
    const fill = isC ? '#006B7B' : '#0D1B2A';
    const op   = isC ? 0.82 : 0.62;
    const heeftLijn = Math.abs(ly - y) > 6;
    return `<g>
      <circle cx="${x}" cy="${y}" r="${r}" fill="${fill}" fill-opacity="${op}" stroke="${fill}" stroke-width="1.5" stroke-opacity="0.3">
        <title>${p.naam}: ${p.totaal} ingediend — ${p.pct}% aangenomen (${p.aangenomen} van ${p.aangenomen+p.verworpen} met uitslag)</title>
      </circle>
      ${heeftLijn ? `<line x1="${lx}" y1="${y}" x2="${lx}" y2="${ly}" stroke="var(--rule)" stroke-width="0.8" stroke-dasharray="2,2"/>` : ''}
      <text x="${lx}" y="${ly+3}" text-anchor="${anchor}" font-size="10" fill="var(--text)" font-weight="600">${esc(k)}</text>
      <text x="${lx}" y="${ly+13}" text-anchor="${anchor}" font-size="9" fill="var(--muted)">${p.pct}%</text>
    </g>`;
  }).join('');

  return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:${H}px;overflow:visible;">
    ${qBg}
    <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1.5"/>
    <line x1="${PAD.l}" y1="${PAD.t+pH}" x2="${PAD.l+pW}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1.5"/>
    ${yTicks}${xTicks}${axisLabels}${dots}
  </svg>
  <div class="svg-chart-legend">
    <span><span class="dot" style="background:#006B7B;opacity:.82;border-radius:50%;"></span>Coalitie</span>
    <span><span class="dot" style="background:#0D1B2A;opacity:.62;border-radius:50%;"></span>Oppositie</span>
    <span>· cirkelgrootte = aantal ingediend</span>
  </div>`;
}

function switchScatterType(val) {
  const container = document.getElementById('scatterContainer');
  if (container) container.innerHTML = bouwScatterSVG(val || null);
}

// ── MOTIES VISUALISATIES ──────────────────────────────────────────────────────
function renderMotiesVisuals() {
  const el = document.getElementById('motiesViz');
  if (!el || !moties.length) return;

  const maandNamen = ['Jan','Feb','Mrt','Apr','Mei','Jun','Jul','Aug','Sep','Okt','Nov','Dec'];

  const partijStats = {};
  moties.forEach(m => {
    if (!m.partij) return;
    if (!partijStats[m.partij]) partijStats[m.partij] = { totaal:0, aangenomen:0, verworpen:0, ingetrokken:0, aangehouden:0 };
    const s = partijStats[m.partij];
    s.totaal++;
    if (m.status === 'aangenomen')  s.aangenomen++;
    if (m.status === 'verworpen')   s.verworpen++;
    if (m.status === 'ingetrokken') s.ingetrokken++;
    if (m.status === 'aangehouden') s.aangehouden++;
  });
  const partijLijst = Object.entries(partijStats).map(([naam, s]) => {
    const ms = s.aangenomen + s.verworpen;
    return { naam, ...s, pct: ms > 0 ? Math.round(s.aangenomen / ms * 100) : null };
  });

  const indienerStats = {};
  moties.forEach(m => {
    if (!m.indiener) return;
    if (!indienerStats[m.indiener]) indienerStats[m.indiener] = { totaal:0, aangenomen:0, verworpen:0, partij: m.partij };
    indienerStats[m.indiener].totaal++;
    if (m.status === 'aangenomen') indienerStats[m.indiener].aangenomen++;
    if (m.status === 'verworpen')  indienerStats[m.indiener].verworpen++;
  });
  const topIndieners = Object.entries(indienerStats)
    .sort((a, b) => b[1].totaal - a[1].totaal).slice(0, 8)
    .map(([naam, s]) => {
      const ms = s.aangenomen + s.verworpen;
      return { naam, ...s, pct: ms > 0 ? Math.round(s.aangenomen / ms * 100) : null };
    });

  const nauwste = moties
    .filter(m => m.voor_pct != null && m.tegen_pct != null &&
                 (m.status === 'aangenomen' || m.status === 'verworpen'))
    .map(m => ({ ...m, marge: Math.abs(m.voor_pct - m.tegen_pct) }))
    .sort((a, b) => a.marge - b.marge)
    .slice(0, 8);

  const maandTrend = {};
  moties.forEach(m => {
    if (!m.datum) return;
    const k = m.datum.slice(0, 7);
    if (!maandTrend[k]) maandTrend[k] = { totaal:0, aangenomen:0, verworpen:0 };
    maandTrend[k].totaal++;
    if (m.status === 'aangenomen') maandTrend[k].aangenomen++;
    if (m.status === 'verworpen')  maandTrend[k].verworpen++;
  });
  const maandLijst = Object.entries(maandTrend).sort((a, b) => a[0].localeCompare(b[0]));

  const coSign = {};
  moties.forEach(m => {
    const fracties = (m.fracties || []).filter(Boolean);
    if (fracties.length < 2) return;
    for (let i = 0; i < fracties.length; i++)
      for (let j = i + 1; j < fracties.length; j++) {
        const key = [fracties[i], fracties[j]].sort().join('||');
        coSign[key] = (coSign[key] || 0) + 1;
      }
  });
  const topCoSign = Object.entries(coSign).sort((a, b) => b[1] - a[1]).slice(0, 8);

  const totaal      = moties.length;
  const aangenomen  = moties.filter(m => m.status === 'aangenomen').length;
  const metStatus   = moties.filter(m => m.status === 'aangenomen' || m.status === 'verworpen').length;
  const pctGlobaal  = metStatus ? Math.round(aangenomen / metStatus * 100) : 0;
  const meestActief = partijLijst.reduce((a, b) => b.totaal > a.totaal ? b : a, partijLijst[0] || { naam:'—', totaal:0 });

  // ── TREND SVG ─────────────────────────────────────────────────────────────
  function trendSVG() {
    if (maandLijst.length < 2) return '<div class="viz-empty">Onvoldoende data</div>';
    const W = 700, H = 160;
    const PAD = { t:12, r:16, b:28, l:36 };
    const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;
    const maxC = Math.max(...maandLijst.map(([,d]) => d.totaal), 1);
    const slot = pW / maandLijst.length;
    const barW = Math.max(8, Math.floor(slot * 0.65));

    const bars = maandLijst.map(([maand, d], i) => {
      const x   = PAD.l + i * slot + (slot - barW) / 2;
      const mn  = parseInt(maand.split('-')[1]);
      const yr  = maand.split('-')[0].slice(2);
      const yBase = PAD.t + pH;
      const hAng  = Math.round((d.aangenomen / maxC) * pH);
      const hVer  = Math.round((d.verworpen  / maxC) * pH);
      const hRest = Math.round((d.totaal     / maxC) * pH) - hAng - hVer;
      return `
        <rect x="${x}" y="${yBase - hAng}" width="${barW}" height="${hAng}" fill="var(--go)" opacity="0.82" rx="1">
          <title>${d.aangenomen} aangenomen</title></rect>
        <rect x="${x}" y="${yBase - hAng - hVer}" width="${barW}" height="${hVer}" fill="var(--stop)" opacity="0.75">
          <title>${d.verworpen} verworpen</title></rect>
        <rect x="${x}" y="${yBase - hAng - hVer - hRest}" width="${barW}" height="${hRest}" fill="var(--rule)" opacity="0.9">
          <title>${d.totaal - d.aangenomen - d.verworpen} aangehouden/ingetrokken</title></rect>
        <text x="${x+barW/2}" y="${H-4}" text-anchor="middle" font-size="9" fill="var(--muted)">${maandNamen[mn-1]}'${yr}</text>`;
    }).join('');

    const yTicks = [0, Math.ceil(maxC/2), maxC].map(v => {
      const y = PAD.t + pH - (v/maxC)*pH;
      return `<line x1="${PAD.l}" y1="${y}" x2="${PAD.l+pW}" y2="${y}" stroke="var(--rule)" stroke-width="0.5"/>
              <text x="${PAD.l-4}" y="${y+3}" text-anchor="end" font-size="9" fill="var(--muted)">${v}</text>`;
    }).join('');

    return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:${H}px;">
      ${yTicks}
      <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
      <line x1="${PAD.l}" y1="${PAD.t+pH}" x2="${PAD.l+pW}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
      ${bars}
    </svg>
    <div style="display:flex;gap:16px;padding:0 0 8px;font-size:10px;color:var(--muted);">
      <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;background:var(--go);opacity:.82;display:inline-block;"></span>Aangenomen</span>
      <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;background:var(--stop);opacity:.75;display:inline-block;"></span>Verworpen</span>
      <span style="display:flex;align-items:center;gap:4px;"><span style="width:8px;height:8px;background:var(--rule);display:inline-block;"></span>Aangehouden / ingetrokken</span>
    </div>`;
  }

  // ── DIVERGENDE BALK ───────────────────────────────────────────────────────
  function divergingHTML() {
    if (!nauwste.length) return '<div class="viz-empty">Geen stemmingsdata — voor_pct ontbreekt in moties.json</div>';

    const rows = nauwste.map(m => {
      const tp = m.tegen_pct || 0;
      const vp = m.voor_pct  || 0;
      const isAan  = m.status === 'aangenomen';
      const sKleur = isAan ? 'var(--go)' : 'var(--stop)';
      const sLabel = isAan ? '✓ Aangenomen' : '✗ Verworpen';
      const titel  = m.titel.replace(/^[A-Z0-9]+(?:\s+\([^)]+\))?\s+/i, '');
      return `
        <div style="display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--rule);">
          <div style="width:200px;flex-shrink:0;">
            <div style="font-size:11px;font-weight:600;line-height:1.35;color:var(--text);">${esc(titel)}</div>
            <div style="font-size:10px;color:var(--muted);margin-top:2px;">${fmtDate(m.datum,'short')} · marge ${m.marge}%</div>
          </div>
          <div style="flex:1;display:flex;align-items:center;height:22px;">
            <div style="flex:1;display:flex;justify-content:flex-end;height:100%;">
              <div style="width:${tp}%;height:100%;background:var(--stop);opacity:0.78;border-radius:2px 0 0 2px;display:flex;align-items:center;justify-content:flex-end;padding-right:3px;">
                ${tp > 18 ? `<span style="font-size:9px;color:white;font-weight:700;">${tp}%</span>` : ''}
              </div>
            </div>
            <div style="width:2px;height:26px;background:var(--ink2);flex-shrink:0;"></div>
            <div style="flex:1;height:100%;">
              <div style="width:${vp}%;height:100%;background:var(--go);opacity:0.82;border-radius:0 2px 2px 0;display:flex;align-items:center;padding-left:3px;">
                ${vp > 18 ? `<span style="font-size:9px;color:white;font-weight:700;">${vp}%</span>` : ''}
              </div>
            </div>
          </div>
          <div style="width:96px;flex-shrink:0;font-size:11px;font-weight:700;color:${sKleur};">${sLabel}</div>
        </div>`;
    }).join('');

    return `<div style="padding:0 20px 12px;">
      <div style="display:flex;gap:12px;padding-bottom:8px;border-bottom:2px solid var(--rule);margin-bottom:2px;">
        <div style="width:200px;flex-shrink:0;"></div>
        <div style="flex:1;display:flex;">
          <div style="flex:1;text-align:center;font-size:10px;font-weight:700;color:var(--stop);letter-spacing:.5px;">← TEGEN</div>
          <div style="flex:1;text-align:center;font-size:10px;font-weight:700;color:var(--go);letter-spacing:.5px;">VOOR →</div>
        </div>
        <div style="width:96px;flex-shrink:0;"></div>
      </div>
      ${rows}
    </div>`;
  }

  // ── HTML ──────────────────────────────────────────────────────────────────
  const maxInd = topIndieners[0]?.totaal || 1;
  const maxCS  = topCoSign[0]?.[1] || 1;

  el.innerHTML = `
    <div class="viz-stat-row" style="margin-bottom:16px;">
      <div class="viz-stat">
        <div class="viz-stat-label">Totaal ingediend</div>
        <div class="viz-stat-value">${totaal}</div>
        <div class="viz-stat-sub">moties &amp; amendementen</div>
      </div>
      <div class="viz-stat">
        <div class="viz-stat-label">Slagingspercentage</div>
        <div class="viz-stat-value" style="color:var(--go)">${pctGlobaal}%</div>
        <div class="viz-stat-sub">van ${metStatus} met uitslag</div>
      </div>
      <div class="viz-stat">
        <div class="viz-stat-label">Meest actief</div>
        <div class="viz-stat-value" style="font-size:22px;padding-top:6px;">${esc(meestActief.naam)}</div>
        <div class="viz-stat-sub">${meestActief.totaal} ingediend</div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <span class="card-title">Effectiviteit × Volume</span>
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:11px;color:var(--muted);">wie dient veel in én haalt het door de raad?</span>
          <select class="filter-select" style="font-size:11px;padding:4px 8px;" onchange="switchScatterType(this.value)">
            <option value="">Alle typen</option>
            <option value="Motie">Alleen moties</option>
            <option value="Amendement">Alleen amendementen</option>
          </select>
        </div>
      </div>
      <div style="padding:12px 20px 0;" id="scatterContainer">${bouwScatterSVG(null)}</div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div class="card-header"><span class="card-title">Moties &amp; amendementen per maand</span></div>
      <div style="padding:12px 20px 0;">${trendSVG()}</div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <span class="card-title">Nauwste stemmingen</span>
        <span style="font-size:11px;color:var(--muted);">gesorteerd op kleinste marge</span>
      </div>
      <div>${divergingHTML()}</div>
    </div>

    <div class="viz-row">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Meest actieve indieners</span>
          <span style="font-size:11px;color:var(--muted);">balk = volume · kleur = slagings%</span>
        </div>
        <div class="viz-bar-list">
          ${topIndieners.map(p => {
            const kleur = p.pct === null ? 'var(--rule)' : p.pct >= 60 ? 'var(--go)' : p.pct >= 35 ? 'var(--hold)' : 'var(--stop)';
            return `<div class="viz-bar-row">
              <div class="viz-bar-label" style="width:180px;" title="${esc(p.naam)}">${esc(p.naam)}</div>
              <div class="viz-bar-track">
                <div class="viz-bar-fill" style="width:${Math.round(p.totaal/maxInd*100)}%;background:${kleur};"></div>
              </div>
              <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);width:58px;text-align:right;flex-shrink:0;">${p.totaal} · ${p.pct !== null ? p.pct+'%' : '—'}</div>
            </div>`;
          }).join('')}
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <span class="card-title">Politieke samenwerking</span>
          <span style="font-size:11px;color:var(--muted);">meest voorkomende combinaties</span>
        </div>
        <div class="viz-bar-list">
          ${topCoSign.length === 0
            ? '<div class="viz-empty">Geen medeondertekenaardata — controleer het fracties-veld</div>'
            : topCoSign.map(([combo, count]) => {
                const [a, b] = combo.split('||');
                return `<div class="viz-bar-row">
                  <div style="width:158px;flex-shrink:0;display:flex;align-items:center;gap:4px;overflow:hidden;">
                    <span class="badge badge-teal" style="max-width:66px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a)}">${esc(a)}</span>
                    <span style="color:var(--muted);font-size:9px;flex-shrink:0;">+</span>
                    <span class="badge badge-navy" style="max-width:66px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(b||'')}">${esc(b||'')}</span>
                  </div>
                  <div class="viz-bar-track">
                    <div class="viz-bar-fill" style="width:${Math.round(count/maxCS*100)}%;background:var(--navy);"></div>
                  </div>
                  <div class="viz-bar-pct">${count}x</div>
                </div>`;
              }).join('')
          }
        </div>
      </div>
    </div>`;
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #12 — MOTIE VS AMENDEMENT SLAGINGSPERCENTAGE (Moties-tab)
// ══════════════════════════════════════════════════════════════════════════
function renderMotieAmendementViz() {
  const el = document.getElementById('motieAmendementViz');
  if (!el) return;
  if (!moties.length) { el.innerHTML = '<div class="viz-empty">Geen motiedata</div>'; return; }

  function slagingspercentage(type) {
    const subset = moties.filter(m => m.type === type && (m.status === 'aangenomen' || m.status === 'verworpen'));
    const aangenomen = subset.filter(m => m.status === 'aangenomen').length;
    return { totaal: subset.length, aangenomen, pct: subset.length ? Math.round(aangenomen/subset.length*100) : null };
  }
  const motieStat = slagingspercentage('Motie');
  const amendStat = slagingspercentage('Amendement');

  const totaalHtml = `<div class="type-compare-row">
    <div class="type-compare-card">
      <div class="type-compare-label">Moties</div>
      <div class="type-compare-value" style="color:var(--go);">${motieStat.pct ?? '—'}${motieStat.pct != null ? '%' : ''}</div>
      <div class="type-compare-sub">${motieStat.aangenomen} van ${motieStat.totaal} aangenomen</div>
    </div>
    <div class="type-compare-card">
      <div class="type-compare-label">Amendementen</div>
      <div class="type-compare-value" style="color:var(--teal);">${amendStat.pct ?? '—'}${amendStat.pct != null ? '%' : ''}</div>
      <div class="type-compare-sub">${amendStat.aangenomen} van ${amendStat.totaal} aangenomen</div>
    </div>
  </div>`;

  const fracties = [...new Set(moties.map(m => m.partij).filter(Boolean))];
  const perFractieRows = fracties.map(f => {
    const berekenPct = (type) => {
      const s = moties.filter(m => m.partij === f && m.type === type && (m.status === 'aangenomen' || m.status === 'verworpen'));
      const a = s.filter(m => m.status === 'aangenomen').length;
      return s.length ? Math.round(a / s.length * 100) : null;
    };
    return { f, mPct: berekenPct('Motie'), aPct: berekenPct('Amendement') };
  }).filter(r => r.mPct !== null || r.aPct !== null)
    .sort((a, b) => (b.mPct ?? -1) - (a.mPct ?? -1));

  const fractieHtml = perFractieRows.length ? `<div style="padding:0 20px 16px;overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead><tr>
        <th style="text-align:left;padding:8px 0;">Fractie</th>
        <th style="text-align:right;">Motie %</th>
        <th style="text-align:right;">Amendement %</th>
      </tr></thead>
      <tbody>${perFractieRows.map(r => `<tr>
        <td style="padding:6px 0;border-bottom:1px solid var(--rule);">${esc(r.f)}</td>
        <td style="text-align:right;border-bottom:1px solid var(--rule);color:var(--go);">${r.mPct != null ? r.mPct+'%' : '—'}</td>
        <td style="text-align:right;border-bottom:1px solid var(--rule);color:var(--teal);">${r.aPct != null ? r.aPct+'%' : '—'}</td>
      </tr>`).join('')}</tbody>
    </table>
  </div>` : '';

  el.innerHTML = totaalHtml + fractieHtml;
}

// ── CAMERA'S & WONINGSLUITINGEN ────────────────────────────────────────────────
const WIJK_MAP = {};
function getWijkUitAdres(adres) {
  if (!adres) return null;
  for (const [straat, wijk] of Object.entries(WIJK_MAP)) { if (adres.toLowerCase().includes(straat.toLowerCase())) return wijk; }
  return adres.split(' ')[0] || adres;
}
function telPerWijk(items) {
  const map = {};
  items.forEach(b => { if (!b.adres) return; const wijk = getWijkUitAdres(b.adres); if (!wijk) return; map[wijk] = (map[wijk] || 0) + 1; });
  return map;
}

function dagenTussen(start, eind) {
  if (!start || !eind) return 0;
  const a = new Date(start), b = new Date(eind);
  if (isNaN(a) || isNaN(b)) return 0;
  return Math.max(0, Math.round((b - a) / 86400000));
}

function renderBekendmakingenDashboard() {
  const vandaag = new Date().toISOString().slice(0, 10);

  const cameraNamenOoit = new Set([...camerasActief, ...camerasGeschiedenis].map(c => c.camera));
  document.getElementById('bkCamActiefTotaal').textContent  = camerasActief.length;
  document.getElementById('bkCamTotaalOoit').textContent    = cameraNamenOoit.size;
  document.getElementById('bkWoningTotaal').textContent     = woningsluitingen.length;

  const actiefLijst = [...camerasActief].sort((a, b) => a.eind.localeCompare(b.eind));
  document.getElementById('camActiefList').innerHTML = actiefLijst.length === 0
    ? '<div class="empty">Geen camera\'s op dit moment actief.</div>'
    : actiefLijst.map(c => {
        const dagenResterend = dagenTussen(vandaag, c.eind);
        const bijnaVerlopen = dagenResterend <= 14;
        return `<div class="bk-item">
          <div class="bk-top">
            <div>
              <div class="bk-title-link" style="cursor:default;">📷 ${esc(c.camera)}</div>
              <div class="bk-meta">
                <span class="bk-date">${fmtDate(c.start,'full')} t/m ${fmtDate(c.eind,'full')}</span>
                <span class="badge ${bijnaVerlopen ? 'badge-hold' : 'badge-go'}">${dagenResterend} dagen resterend</span>
                ${c.keer_verlengd > 0 ? `<span class="badge badge-teal">${c.keer_verlengd}× verlengd</span>` : ''}
              </div>
            </div>
          </div>
        </div>`;
      }).join('');

  const geschiedenisData = {};
  camerasGeschiedenis.forEach(c => {
    const dagen = (c.periodes || []).reduce((som, p) => som + dagenTussen(p.start, p.eind), 0);
    geschiedenisData[c.camera] = (geschiedenisData[c.camera] || 0) + (dagen || dagenTussen(c.start, c.eind));
  });
  tekenHorizontaleBalken('camGeschiedenisChart', geschiedenisData, 'dagen');

  const nu = new Date();
  const maanden = [];
  for (let i = 5; i >= 0; i--) { const d = new Date(nu.getFullYear(), nu.getMonth() - i, 1); maanden.push({ maand: d.toISOString().slice(0, 7), count: 0 }); }
  const tel = (items) => { const m = {}; maanden.forEach(x => m[x.maand] = 0); items.forEach(b => { if (b.datum) { const k = b.datum.slice(0, 7); if (k in m) m[k]++; } }); return maanden.map(x => ({ maand: x.maand, count: m[x.maand] })); };
  tekenLijnGrafiek('bkWoningChart', tel(woningsluitingen), 'Woningsluitingen');
  tekenHorizontaleBalken('bkWoningWijkChart', telPerWijk(woningsluitingen));
}

function tekenLijnGrafiek(containerId, data, label) {
  const container = document.getElementById(containerId);
  if (!container) return;
  if (!data || data.length < 2) { container.innerHTML = '<div class="viz-empty">Onvoldoende data</div>'; return; }
  const W = 500, H = 200, PAD = { t: 10, r: 16, b: 30, l: 40 };
  const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;
  const maxC = Math.max(...data.map(d => d.count), 1);
  const pts = data.map((d, i) => [PAD.l + (i / (data.length - 1)) * pW, PAD.t + (1 - d.count / maxC) * pH]);
  const line = pts.map(([x,y]) => `${x},${y}`).join(' ');
  const area = [`${PAD.l},${PAD.t+pH}`, ...pts.map(([x,y]) => `${x},${y}`), `${PAD.l+pW},${PAD.t+pH}`].join(' ');
  const maandNamen = ['Jan','Feb','Mrt','Apr','Mei','Jun','Jul','Aug','Sep','Okt','Nov','Dec'];
  const labels = data.map((d, i) => { const x = PAD.l + (i / (data.length - 1)) * pW; const m = parseInt(d.maand.split('-')[1]); return `<text x="${x}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#6B7280">${maandNamen[m-1]}</text>`; }).join('');
  const yTicks = [0, Math.ceil(maxC/2), maxC];
  const yLabels = yTicks.map(v => { const y = PAD.t + (1 - v / maxC) * pH; return `<text x="${PAD.l - 6}" y="${y + 4}" text-anchor="end" font-size="10" fill="#6B7280">${v}</text>`; }).join('');
  const dots = pts.map(([x,y], i) => `<circle cx="${x}" cy="${y}" r="3" fill="white" stroke="#006B7B" stroke-width="2"><title>${data[i].count} in ${data[i].maand}</title></circle>`).join('');
  container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;"><line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t+pH}" stroke="#D8D5CE" stroke-width="1"/><line x1="${PAD.l}" y1="${PAD.t+pH}" x2="${PAD.l+pW}" y2="${PAD.t+pH}" stroke="#D8D5CE" stroke-width="1"/>${yLabels}<polygon points="${area}" fill="#006B7B" fill-opacity="0.06"/><polyline points="${line}" fill="none" stroke="#006B7B" stroke-width="2.5" stroke-linejoin="round"/>${dots}${labels}</svg>`;
}
function tekenHorizontaleBalken(containerId, data, eenheid) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const entries = Object.entries(data).sort((a,b) => b[1]-a[1]);
  if (entries.length === 0) { container.innerHTML = '<div class="viz-empty">geen data</div>'; return; }
  const maxVal = entries[0][1];
  container.innerHTML = entries.map(([label, count]) => `<div class="bk-bar-row"><div class="bk-bar-label" title="${esc(label)}">${esc(label)}</div><div class="bk-bar-track"><div class="bk-bar-fill" style="width:${Math.round(count/maxVal*100)}%"></div></div><div class="bk-bar-count">${count}${eenheid ? '' : ''}</div></div>`).join('');
}

function renderOvBk() {
  const camItems = [...camerasActief, ...camerasGeschiedenis].map(c => ({
    datum: c.start, titel: `Cameratoezicht ${c.camera}`, link: (c.periodes?.[0]?.link) || '#', type: 'Camera',
  }));
  const woningItems = woningsluitingen.map(w => ({
    datum: w.datum, titel: w.titel, link: w.link || '#', type: 'Woningsluiting',
  }));
  const combined = [...camItems, ...woningItems]
    .filter(x => x.datum)
    .sort((a, b) => b.datum.localeCompare(a.datum))
    .slice(0, 5);

  document.getElementById('ovBk').innerHTML = combined.map(x => `
    <div class="mini-item">
      <div class="mini-date">${fmtDate(x.datum, 'short')}</div>
      <div>
        <div class="mini-title"><a href="${x.link}" target="_blank" style="color:inherit;text-decoration:none;">${esc(x.titel)}</a></div>
        <div class="mini-type">${esc(x.type)}</div>
      </div>
    </div>`).join('') || '<div class="empty">Geen data</div>';
}

function renderBekendmakingenLijst() {
  document.getElementById('bkCount').textContent = woningsluitingen.length + ' woningsluitingen';
  const lijst = document.getElementById('bkList');
  if (!woningsluitingen.length) { lijst.innerHTML = '<div class="empty">Geen woningsluitingen gevonden.</div>'; return; }
  lijst.innerHTML = woningsluitingen.map(w => `
    <div class="bk-item">
      <div class="bk-top"><div>
        <a class="bk-title-link" href="${w.link || '#'}" target="_blank">${esc(w.titel)}</a>
        <div class="bk-meta">
          <span class="badge badge-teal">Woningsluiting</span>
          <span class="bk-date">${fmtDate(w.datum,'full')}</span>
          ${w.adres ? `<span class="bk-date">📍 ${esc(w.adres)}</span>` : ''}
          ${w.eind_datum ? `<span class="bk-date">tot ${fmtDate(w.eind_datum,'full')} (${w.eind_datum_type === 'geschat_uit_artikeltekst' ? 'geschat' : 'officieel'})</span>` : ''}
        </div>
        ${w.excerpt ? `<div class="bk-desc">${esc(w.excerpt)}</div>` : ''}
      </div></div>
    </div>`).join('');
}

function renderCamGeschiedenisLijst() {
  const container = document.getElementById('camGeschiedenisLijst');
  const teller     = document.getElementById('camGeschiedenisCount');
  if (!container) return;
  teller.textContent = camerasGeschiedenis.length + ' verlopen camera-locaties';
  if (!camerasGeschiedenis.length) { container.innerHTML = '<div class="empty">Nog geen verlopen camera\'s.</div>'; return; }
  container.innerHTML = [...camerasGeschiedenis].sort((a,b) => b.eind.localeCompare(a.eind)).map(c => `
    <div class="bk-item">
      <div class="bk-top"><div>
        <div class="bk-title-link" style="cursor:default;">📷 ${esc(c.camera)}</div>
        <div class="bk-meta">
          <span class="badge badge-hold">Verlopen</span>
          <span class="bk-date">${fmtDate(c.start,'full')} t/m ${fmtDate(c.eind,'full')}</span>
          ${c.keer_verlengd > 0 ? `<span class="badge badge-teal">${c.keer_verlengd}× verlengd</span>` : ''}
        </div>
        ${(c.periodes || []).length > 1 ? `<div class="bk-desc">${c.periodes.length} periodes: ${c.periodes.map(p => `${p.start}–${p.eind}`).join(', ')}</div>` : ''}
      </div></div>
    </div>`).join('');
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #22 — CAMERA-VERLENGINGSRANGLIJST (Camera's & Sluitingen-tab)
// "Tijdelijk" wordt structureel: welke locaties worden het vaakst verlengd.
// ══════════════════════════════════════════════════════════════════════════
function renderCamVerlengingChart() {
  const el = document.getElementById('camVerlengingChart');
  if (!el) return;
  const map = {};
  [...camerasActief, ...camerasGeschiedenis].forEach(c => {
    map[c.camera] = Math.max(map[c.camera] || 0, c.keer_verlengd || 0);
  });
  const rijen = Object.entries(map).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  if (!rijen.length) { el.innerHTML = '<div class="viz-empty">Geen verlengingen geregistreerd</div>'; return; }
  const maxV = rijen[0][1];
  el.innerHTML = rijen.map(([camera, n]) => `<div class="bk-bar-row">
    <div class="bk-bar-label" title="${esc(camera)}">${esc(camera)}</div>
    <div class="bk-bar-track"><div class="bk-bar-fill" style="width:${Math.round(n/maxV*100)}%;"></div></div>
    <div class="bk-bar-count">${n}×</div>
  </div>`).join('');
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #23 — WONINGSLUITINGSDUUR-HISTOGRAM (Camera's & Sluitingen-tab)
// ══════════════════════════════════════════════════════════════════════════
function renderWoningsluitingDuurChart() {
  const el = document.getElementById('woningsluitingDuurChart');
  if (!el) return;
  const buckets = { '0–30 dagen': 0, '30–90 dagen': 0, '90+ dagen': 0, 'Onbekend (geen einddatum)': 0 };
  woningsluitingen.forEach(w => {
    if (!w.eind_datum) { buckets['Onbekend (geen einddatum)']++; return; }
    const dagen = dagenTussen(w.datum, w.eind_datum);
    if (dagen <= 30) buckets['0–30 dagen']++;
    else if (dagen <= 90) buckets['30–90 dagen']++;
    else buckets['90+ dagen']++;
  });
  const entries = Object.entries(buckets).filter(([, v]) => v > 0);
  if (!entries.length) { el.innerHTML = '<div class="viz-empty">Geen woningsluitingen beschikbaar</div>'; return; }
  const maxV = Math.max(...entries.map(([, v]) => v));
  el.innerHTML = entries.map(([label, n]) => `<div class="viz-bar-row">
    <div class="viz-bar-label" style="width:200px;">${esc(label)}</div>
    <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(n/maxV*100)}%;background:var(--hold);"></div></div>
    <div class="viz-bar-pct">${n}</div>
  </div>`).join('');
}

// ── RAADSVRAGEN ───────────────────────────────────────────────────────────────
function populateRvFilter() {
  const sel = document.getElementById('filterRvPartij');
  const fracties = [...new Set(raadsvragen.map(r => r.fractie).filter(Boolean))].sort();
  fracties.forEach(f => { const o = document.createElement('option'); o.value = f; o.textContent = f; sel.appendChild(o); });
  document.getElementById('rvCount').textContent = raadsvragen.length + ' raadsvragen';
}
function renderRaadsvragen() {
  const partijF = document.getElementById('filterRvPartij').value;
  const statusF = document.getElementById('filterRvStatus').value;
  let f = [...raadsvragen];
  if (partijF) f = f.filter(r => r.fractie === partijF);
  if (statusF) f = f.filter(r => r.status === statusF);
  document.getElementById('rvList').innerHTML = f.length === 0
    ? '<div class="empty">Geen raadsvragen gevonden.</div>'
    : f.map(r => `
        <div class="rv-item">
          <div class="rv-title">${esc(r.titel)}</div>
          <div class="rv-meta">
            ${r.fractie ? `<span class="badge badge-teal">${esc(r.fractie)}</span>` : ''}
            ${r.vraagsteller ? `<span style="font-size:11px;color:var(--teal);font-weight:600;">${esc(r.vraagsteller)}</span>` : ''}
            <span class="rv-date">${fmtDate(r.datum_ingediend, 'full')}</span>
            ${statusBadge(r.status)}
          </div>
          ${r.omschrijving ? `<div class="bk-desc" style="margin-top:6px;">${esc(r.omschrijving)}</div>` : ''}
        </div>`).join('');
}

// ── STEMMINGEN ────────────────────────────────────────────────────────────────
function renderStemStats() {
  const totaal   = stemmingen.length;
  const unaniem  = stemmingen.filter(s => s.voor_pct === 100).length;
  const verdeeld = stemmingen.filter(s => (s.tegen_pct || 0) > 0 || (s.onthouding_pct || 0) > 0).length;
  document.getElementById('stemTotaal').textContent   = totaal;
  document.getElementById('stemUnaniem').textContent  = unaniem;
  document.getElementById('stemVerdeeld').textContent = verdeeld;

  const metData = stemmingen.filter(s => s.fracties_voor || s.fracties_tegen);
  let coalitieBlokt = 0, coalitieWint = 0, coalitieSplitst = 0;

  metData.forEach(s => {
    const { voor, tegen } = getStemSets(s);
    const actief  = [...COALITIE_FRACTIES].filter(f => voor.has(f) || tegen.has(f));
    if (!actief.length) return;
    const cVoor  = actief.filter(f => voor.has(f)).length;
    const cTegen = actief.filter(f => tegen.has(f)).length;
    if (cVoor === actief.length || cTegen === actief.length) coalitieBlokt++;
    if (cVoor > cTegen) coalitieWint++;
    if (cVoor > 0 && cTegen > 0) coalitieSplitst++;
  });

  const pctBlokt = metData.length ? Math.round(coalitieBlokt / metData.length * 100) : 0;
  const pctWint  = metData.length ? Math.round(coalitieWint  / metData.length * 100) : 0;
  const coalitieZetels = [...COALITIE_FRACTIES].reduce((s, f) => s + (ZETEL_MAP[f] || 0), 0);
  const totaalZetels   = Object.values(ZETEL_MAP).reduce((s, v) => s + v, 0);
  const meerderheid    = coalitieZetels > totaalZetels / 2 ? 'meerderheid' : 'minderheid';

  document.getElementById('stemCoalitieStats').innerHTML = `
    <div class="coalitie-stat">
      <div class="coalitie-stat-label">Coalitie stemt als blok</div>
      <div class="coalitie-stat-value" style="color:var(--teal)">${pctBlokt}%</div>
      <div class="coalitie-stat-sub">${coalitieBlokt} van ${metData.length} stemmingen</div>
    </div>
    <div class="coalitie-stat">
      <div class="coalitie-stat-label">Coalitie aan winnende kant</div>
      <div class="coalitie-stat-value" style="color:var(--go)">${pctWint}%</div>
      <div class="coalitie-stat-sub">${coalitieWint} van ${metData.length} stemmingen</div>
    </div>
    <div class="coalitie-stat">
      <div class="coalitie-stat-label">Coalitie intern verdeeld</div>
      <div class="coalitie-stat-value" style="color:var(--stop)">${coalitieSplitst}</div>
      <div class="coalitie-stat-sub">stemmingen met scheuring</div>
    </div>
    <div class="coalitie-stat">
      <div class="coalitie-stat-label">Coalitie / Totaal</div>
      <div class="coalitie-stat-value" style="color:var(--navy)">${coalitieZetels}/${totaalZetels}</div>
      <div class="coalitie-stat-sub">${meerderheid}</div>
    </div>
  `;
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #10 + #11 — COALITIE VS. OPPOSITIE & POLARISATIE DOOR DE TIJD
// Per maand: % stemmingen waarin coalitie en oppositie aan dezelfde kant
// staan (i.p.v. tegenover elkaar), plus % unanieme stemmingen.
// ══════════════════════════════════════════════════════════════════════════
function renderCoalitieTijdChart() {
  const el = document.getElementById('coalitieTijdChart');
  if (!el) return;
  const metData = stemmingen.filter(s => s.fracties_voor || s.fracties_tegen);
  if (metData.length < 2) { el.innerHTML = '<div class="viz-empty">Onvoldoende stemmingsdata</div>'; return; }

  const alleFracties = getAllFracties();
  const maandMap = {};
  metData.forEach(s => {
    if (!s.datum) return;
    const maand = s.datum.slice(0, 7);
    if (!maandMap[maand]) maandMap[maand] = { totaal: 0, unaniem: 0, samen: 0, vergelijkbaar: 0 };
    const md = maandMap[maand];
    md.totaal++;
    if (s.voor_pct === 100) md.unaniem++;

    const { voor, tegen } = getStemSets(s);
    const cFracties = [...COALITIE_FRACTIES].filter(f => voor.has(f) || tegen.has(f));
    const oFracties = alleFracties.filter(f => !COALITIE_FRACTIES.has(f) && (voor.has(f) || tegen.has(f)));
    if (!cFracties.length || !oFracties.length) return;
    md.vergelijkbaar++;
    const cVoor = cFracties.filter(f => voor.has(f)).length;
    const oVoor = oFracties.filter(f => voor.has(f)).length;
    const cStandpunt = cVoor >= cFracties.length - cVoor ? 'voor' : 'tegen';
    const oStandpunt = oVoor >= oFracties.length - oVoor ? 'voor' : 'tegen';
    if (cStandpunt === oStandpunt) md.samen++;
  });

  const maandLijst = Object.entries(maandMap).sort((a, b) => a[0].localeCompare(b[0]));
  if (maandLijst.length < 2) { el.innerHTML = '<div class="viz-empty">Onvoldoende maanden met data</div>'; return; }

  const maandNamenKort = ['Jan','Feb','Mrt','Apr','Mei','Jun','Jul','Aug','Sep','Okt','Nov','Dec'];
  const W = 680, H = 220, PAD = { t: 16, r: 16, b: 30, l: 36 };
  const pW = W - PAD.l - PAD.r, pH = H - PAD.t - PAD.b;

  const puntenVoor = getter => maandLijst.map(([maand, d], i) => {
    const x = PAD.l + (i / (maandLijst.length - 1)) * pW;
    const noemer = getter === 'samen' ? d.vergelijkbaar : d.totaal;
    const pct = noemer ? (d[getter] / noemer) * 100 : 0;
    const y = PAD.t + pH - (pct / 100) * pH;
    return [x, y, Math.round(pct)];
  });
  const samenPts   = puntenVoor('samen');
  const unaniemPts = puntenVoor('unaniem');

  const lijn = (arr, kleur) =>
    `<polyline points="${arr.map(([x,y]) => `${x},${y}`).join(' ')}" fill="none" stroke="${kleur}" stroke-width="2.5" stroke-linejoin="round"/>` +
    arr.map(([x, y, pct], i) => `<circle cx="${x}" cy="${y}" r="3" fill="white" stroke="${kleur}" stroke-width="2"><title>${maandLijst[i][0]}: ${pct}%</title></circle>`).join('');

  const yTicks = [0, 25, 50, 75, 100].map(v => {
    const y = PAD.t + pH - (v/100)*pH;
    return `<line x1="${PAD.l}" y1="${y}" x2="${PAD.l+pW}" y2="${y}" stroke="var(--rule)" stroke-width="0.5"/>
            <text x="${PAD.l-5}" y="${y+3}" text-anchor="end" font-size="9" fill="var(--muted)">${v}%</text>`;
  }).join('');
  const xLabels = maandLijst.map(([maand], i) => {
    const x = PAD.l + (i / (maandLijst.length - 1)) * pW;
    const mn = parseInt(maand.split('-')[1]);
    return `<text x="${x}" y="${H-8}" text-anchor="middle" font-size="9" fill="var(--muted)">${maandNamenKort[mn-1]}</text>`;
  }).join('');

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:${H}px;">
    ${yTicks}
    <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
    <line x1="${PAD.l}" y1="${PAD.t+pH}" x2="${PAD.l+pW}" y2="${PAD.t+pH}" stroke="var(--rule)" stroke-width="1"/>
    ${lijn(samenPts, 'var(--teal)')}
    ${lijn(unaniemPts, 'var(--go)')}
    ${xLabels}
  </svg>
  <div class="svg-chart-legend">
    <span><span class="dot" style="background:var(--teal);"></span>% coalitie &amp; oppositie stemmen samen (zelfde kant)</span>
    <span><span class="dot" style="background:var(--go);"></span>% unanieme stemmingen</span>
  </div>`;
}

function renderStemmingen() {
  document.getElementById('stemCount').textContent = stemmingen.length + ' stemmingen';

  const metData     = stemmingen.filter(s => s.fracties_voor || s.fracties_tegen);
  const allFracties = getAllFracties();

  // ── HEATMAP ──────────────────────────────────────────────────────────────
  const heatmapEl = document.getElementById('stemHeatmap');
  if (metData.length && allFracties.length) {
    const heatData = metData
      .filter(s => (s.tegen_pct || 0) > 0 || (s.onthouding_pct || 0) > 0)
      .slice(0, 25);

    if (!heatData.length) {
      heatmapEl.innerHTML = '<div class="viz-empty">Geen verdeelde stemmingen beschikbaar</div>';
    } else {
      const colHeaders = heatData.map((s, i) => {
        const kort = s.titel.replace(/^RV\s+|^OV\d+:\s*/i, '').slice(0, 24);
        return `<th style="padding:0;width:28px;min-width:28px;vertical-align:bottom;">
          <div onclick="heatmapDetail(${i})" title="${esc(s.titel)}"
            style="width:28px;height:110px;display:flex;align-items:flex-end;justify-content:center;padding-bottom:4px;cursor:pointer;">
            <div style="writing-mode:vertical-rl;transform:rotate(180deg);font-size:10px;color:var(--muted);
              white-space:nowrap;overflow:hidden;max-height:106px;text-overflow:ellipsis;">
              ${esc(kort)}
            </div>
          </div>
        </th>`;
      }).join('');

      const rows = allFracties.map(f => {
        const isC  = COALITIE_FRACTIES.has(f);
        const cells = heatData.map((s, i) => {
          const { voor, tegen, onth } = getStemSets(s);
          const kleur = voor.has(f) ? 'var(--go)' : tegen.has(f) ? 'var(--stop)' : onth.has(f) ? 'var(--hold)' : 'var(--rule)';
          const label = voor.has(f) ? 'voor' : tegen.has(f) ? 'tegen' : onth.has(f) ? 'onthouding' : 'geen data';
          return `<td onclick="heatmapDetail(${i})" title="${esc(f)}: ${label}"
            style="width:28px;height:28px;min-width:28px;background:${kleur};border:2px solid white;cursor:pointer;">
          </td>`;
        }).join('');
        return `<tr style="${isC ? 'background:var(--teal-bg);' : ''}">
          <td style="text-align:right;padding:2px 10px 2px 0;white-space:nowrap;font-size:11px;
            min-width:160px;${isC ? 'color:var(--teal);font-weight:700;' : 'color:var(--text);'}">
            ${isC ? '◆ ' : ''}${esc(f)}
          </td>
          ${cells}
        </tr>`;
      }).join('');

      heatmapEl.innerHTML = `
        <div style="overflow-x:auto;padding:12px 20px 0;">
          <table style="border-collapse:separate;border-spacing:0;">
            <thead><tr><th style="min-width:160px;"></th>${colHeaders}</tr></thead>
            <tbody>${rows}</tbody>
          </table>
          <div style="margin-top:8px;padding-bottom:10px;font-size:10px;color:var(--muted);">
            ◆ coalitie · alleen verdeelde stemmingen · klik op een cel of kolomlabel voor detail
          </div>
        </div>
        <div id="heatmapDetailPanel" style="display:none;border-top:1px solid var(--rule);padding:14px 20px;background:var(--paper);">
          <div id="heatmapDetailContent"></div>
        </div>
      `;

      window._heatData = heatData;
    }
  } else {
    heatmapEl.innerHTML = '<div class="viz-empty">Geen stemmingsdata beschikbaar</div>';
  }

  // ── LOYALITEIT ────────────────────────────────────────────────────────────
  renderStemLoyaliteit();

  // ── ALLIANTIES ────────────────────────────────────────────────────────────
  renderStemAllianties();

  // ── MEEST VERDEELD ────────────────────────────────────────────────────────
  renderMeestVerdeeld();

  // ── STEMMEN PER FRACTIE ───────────────────────────────────────────────────
  renderStemmenPerFractie();

  // ── AANWEZIGHEID PER STEMDAG ──────────────────────────────────────────────
  renderAanwezigheid();

  // ── FRACTIELOYALITEIT ──────────────────────────────────────────────────────
  renderFractieloyaliteit();

  // ── NIEUW #8: MEEST ONAFHANKELIJKE RAADSLEDEN ────────────────────────────
  renderOnafhankelijkeRaadsledenChart();

  // ── ALLE STEMMINGEN LIJST ─────────────────────────────────────────────────
  renderStemLijst();
}

function renderStemLoyaliteit() {
  const el = document.getElementById('stemLoyaliteit');
  if (!el) return;
  el.innerHTML = '';
  const metData = stemmingen.filter(s => s.fracties_voor || s.fracties_tegen);
  if (!metData.length) { el.innerHTML = '<div class="viz-empty">Geen stemmingsdata</div>'; return; }
  const fractieMap = {};
  const allFracties = getAllFracties();
  allFracties.forEach(f => { fractieMap[f] = { mee: 0, totaal: 0 }; });
  metData.forEach(s => {
    const { voor, tegen } = getStemSets(s);
    const winnaar = (s.voor_pct || 0) >= 50 ? 'voor' : 'tegen';
    allFracties.forEach(f => {
      if (voor.has(f) || tegen.has(f)) {
        fractieMap[f].totaal++;
        if ((winnaar === 'voor' && voor.has(f)) || (winnaar === 'tegen' && tegen.has(f))) fractieMap[f].mee++;
      }
    });
  });
  const rijen = Object.entries(fractieMap)
    .filter(([, v]) => v.totaal > 0)
    .sort((a, b) => (b[1].mee / b[1].totaal) - (a[1].mee / a[1].totaal));
  let html = `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">
    <thead><tr><th style="text-align:left;">Fractie</th><th style="text-align:right;">Mee</th><th style="text-align:right;">Totaal</th><th style="text-align:right;">%</th></tr></thead><tbody>`;
  rijen.forEach(([fractie, v]) => {
    const pct = Math.round(v.mee / v.totaal * 100);
    html += `<tr><td>${esc(fractie)}</td><td style="text-align:right;">${v.mee}</td><td style="text-align:right;">${v.totaal}</td><td style="text-align:right;">${pct}%</td></tr>`;
  });
  html += '</tbody></table></div>';
  html += '<div style="margin-top:8px;font-size:10px;color:var(--muted);">Een fractie stemde "mee met de meerderheid" als ze vóór stemde bij een aangenomen voorstel, of tegen bij een verworpen voorstel.</div>';
  el.style.padding = '12px 16px';
  el.innerHTML = html;
}

function renderStemAllianties() {
  const el = document.getElementById('stemAllianties');
  if (!el) return;
  el.innerHTML = '';
  const metData = stemmingen.filter(s => s.fracties_voor || s.fracties_tegen);
  if (!metData.length) { el.innerHTML = '<div class="viz-empty">Geen stemmingsdata</div>'; return; }
  const allFracties = getAllFracties();
  const coVote = {}, coTotal = {};
  metData.forEach(s => {
    const { voor, tegen } = getStemSets(s);
    const deelnemers = [...new Set([...voor, ...tegen])];
    for (let i = 0; i < deelnemers.length; i++) {
      for (let j = i + 1; j < deelnemers.length; j++) {
        const a = deelnemers[i], b = deelnemers[j];
        const key = [a, b].sort().join('||');
        coTotal[key] = (coTotal[key] || 0) + 1;
        if ((voor.has(a) && voor.has(b)) || (tegen.has(a) && tegen.has(b))) coVote[key] = (coVote[key] || 0) + 1;
      }
    }
  });
  let html = '<div style="overflow-x:auto;"><table style="border-collapse:collapse;font-size:11px;"><tr><th style="min-width:120px;"></th>';
  allFracties.forEach(f => { html += `<th style="padding:4px;font-size:10px;max-height:100px;" title="${esc(f)}"><div style="writing-mode:vertical-rl;transform:rotate(180deg);overflow:hidden;max-height:96px;text-overflow:ellipsis;">${esc(f)}</div></th>`; });
  html += '</tr>';
  allFracties.forEach(fRij => {
    html += `<tr><td style="text-align:right;padding-right:8px;font-weight:600;white-space:nowrap;">${esc(fRij)}</td>`;
    allFracties.forEach(fKol => {
      if (fRij === fKol) { html += '<td style="background:#f0f0f0;"></td>'; return; }
      const key = [fRij, fKol].sort().join('||');
      const samen = coVote[key] || 0, tot = coTotal[key] || 0;
      const pct = tot ? Math.round(samen / tot * 100) : 0;
      const kleur = `hsl(200, ${Math.round(pct * 0.8)}%, ${70 - pct * 0.3}%)`;
      html += `<td style="text-align:center;padding:6px;background:${kleur};cursor:pointer;" onclick="toonAlliantieDetail('${esc(fRij)}','${esc(fKol)}')" title="${esc(fRij)} &amp; ${esc(fKol)}: ${samen}/${tot} (${pct}%)">${samen}<br><span style="font-size:9px;">${pct}%</span></td>`;
    });
    html += '</tr>';
  });
  html += '</table></div><div style="margin-top:8px;font-size:10px;color:var(--muted);">Klik op een cel om de bijbehorende stemmingen te zien.</div>';
  el.innerHTML = html;
  window._alliantieData = { coVote, coTotal, metData };
}

function renderMeestVerdeeld() {
  const verdeeld = stemmingen
    .filter(s => (s.tegen_pct || 0) > 0 || (s.onthouding_pct || 0) > 0)
    .map(s => ({ ...s, marge: Math.abs((s.voor_pct || 0) - (s.tegen_pct || 0)) }))
    .sort((a, b) => a.marge - b.marge)
    .slice(0, 6);

  document.getElementById('stemVerdeeldLijst').innerHTML = verdeeld.length === 0
    ? '<div class="viz-empty">Geen verdeelde stemmingen</div>'
    : `<div style="padding:14px 20px;">
        <div style="display:flex;font-size:10px;color:var(--muted);margin-bottom:10px;padding-left:148px;">
          <span style="flex:1;text-align:center;">← Tegen &nbsp;|&nbsp; Voor →</span>
        </div>` +
      verdeeld.map(s => {
        const vp = s.voor_pct || 0, tp = s.tegen_pct || 0;
        const kort = s.titel.replace(/^(RV|OV\d+):\s*/i,'').slice(0, 36);
        return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
          <div style="width:140px;font-size:11px;color:var(--muted);text-align:right;flex-shrink:0;line-height:1.3;overflow:hidden;" title="${esc(s.titel)}">${esc(kort)}</div>
          <div style="display:flex;align-items:center;width:200px;flex-shrink:0;">
            <div style="display:flex;justify-content:flex-end;width:100px;">
              <div style="width:${tp}px;height:20px;background:var(--stop);border-radius:3px 0 0 3px;display:flex;align-items:center;padding-left:4px;">
                ${tp > 14 ? `<span style="font-size:10px;color:white;font-family:'JetBrains Mono',monospace;">${tp}%</span>` : ''}
              </div>
            </div>
            <div style="width:2px;height:26px;background:var(--ink2);flex-shrink:0;"></div>
            <div style="width:100px;">
              <div style="width:${vp}px;height:20px;background:var(--go);border-radius:0 3px 3px 0;display:flex;align-items:center;justify-content:flex-end;padding-right:4px;">
                ${vp > 14 ? `<span style="font-size:10px;color:white;font-family:'JetBrains Mono',monospace;">${vp}%</span>` : ''}
              </div>
            </div>
          </div>
          <div style="font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--muted);white-space:nowrap;">${fmtDate(s.datum,'short')} · ${s.marge}%</div>
        </div>`;
      }).join('') + '</div>';
}

function renderStemmenPerFractie() {
  const fractieTeller = {};
  const heeftRaadsleden = stemmingen.some(s => (s.raadsleden_voor || []).length > 0);
  if (heeftRaadsleden) {
    stemmingen.forEach(s => {
      (s.raadsleden_voor  || []).forEach(r => { if (!fractieTeller[r.fractie]) fractieTeller[r.fractie] = { voor: 0, tegen: 0 }; fractieTeller[r.fractie].voor++;  });
      (s.raadsleden_tegen || []).forEach(r => { if (!fractieTeller[r.fractie]) fractieTeller[r.fractie] = { voor: 0, tegen: 0 }; fractieTeller[r.fractie].tegen++; });
    });
  } else {
    stemmingen.forEach(s => {
      parseFractieString(s.fracties_voor ).forEach(f => { if (!fractieTeller[f]) fractieTeller[f] = { voor: 0, tegen: 0 }; fractieTeller[f].voor++;  });
      parseFractieString(s.fracties_tegen).forEach(f => { if (!fractieTeller[f]) fractieTeller[f] = { voor: 0, tegen: 0 }; fractieTeller[f].tegen++; });
    });
  }
  const sortedFracties = Object.entries(fractieTeller).sort((a, b) => (b[1].voor + b[1].tegen) - (a[1].voor + a[1].tegen));
  const fLabels    = sortedFracties.map(([naam]) => naam);
  const fVoorVals  = sortedFracties.map(([, v]) => v.voor);
  const fTegenVals = sortedFracties.map(([, v]) => v.tegen);
  const fBarH      = Math.max(300, fLabels.length * 40 + 60);
  const fractieLijstEl = document.getElementById('stemFractieLijst');
  fractieLijstEl.innerHTML = `
    <div style="position:relative;height:${fBarH}px;padding:8px 12px;">
      <canvas id="fractieCanvas" role="img" aria-label="Voor en tegen stemmen per fractie">Stemmen per fractie.</canvas>
    </div>
    <div style="display:flex;gap:16px;padding:4px 20px 12px;font-size:11px;color:var(--muted);">
      <span style="display:flex;align-items:center;gap:4px;"><span style="width:10px;height:10px;background:#177A3C;border-radius:2px;display:inline-block;"></span>Voor</span>
      <span style="display:flex;align-items:center;gap:4px;"><span style="width:10px;height:10px;background:#B92B27;border-radius:2px;display:inline-block;"></span>Tegen</span>
    </div>`;
  if (_chartFractie) { _chartFractie.destroy(); _chartFractie = null; }
  _chartFractie = new Chart(document.getElementById('fractieCanvas'), {
    type: 'bar',
    data: {
      labels: fLabels,
      datasets: [
        { label: 'Voor',  data: fVoorVals,  backgroundColor: '#177A3C', borderRadius: 3, barThickness: 10 },
        { label: 'Tegen', data: fTegenVals, backgroundColor: '#B92B27', borderRadius: 3, barThickness: 10 }
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: '#D8D5CE' }, ticks: { color: '#6B7280', font: { size: 10 } } },
        y: { grid: { display: false }, ticks: { color: '#374151', font: { size: 11 } } }
      }
    }
  });
}

async function renderAanwezigheid() {
  const container = document.getElementById('stemAanwezigheid');
  if (!container) return;
  try {
    const resp = await fetch('./data/raadsleden_presentie.json');
    if (!resp.ok) throw new Error('Bestand niet gevonden');
    const data = await resp.json();
    if (!data.length) { container.innerHTML = '<div class="viz-empty">Nog geen presentiedata.</div>'; return; }
    data.sort((a, b) => (b.afwezig - a.afwezig) || a.naam.localeCompare(b.naam));
    let html = `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead><tr><th style="text-align:left;">Raadslid</th><th style="text-align:left;">Fractie</th><th style="text-align:right;">Aanwezig</th><th style="text-align:right;">Afwezig</th><th style="text-align:right;">Totaal</th></tr></thead><tbody>`;
    data.forEach(r => {
      const totaal = r.aanwezig + r.afwezig;
      html += `<tr><td>${esc(r.naam)}</td><td>${esc(r.fractie || '')}</td><td style="text-align:right;">${r.aanwezig}</td><td style="text-align:right;">${r.afwezig}</td><td style="text-align:right;">${totaal}</td></tr>`;
    });
    html += '</tbody></table></div><div style="margin-top:6px;font-size:10px;color:var(--muted);">Aanwezigheid per stemdag (deelname aan stemmingen). ⚠ Dit is een momentopname over de hele periode — een tijdlijn per raadslid vergt een uitbreiding van de scraper (raadsleden_presentie.json bevat nu geen datums per stemdag).</div>';
    container.innerHTML = html;
    container.classList.remove('raadslid-placeholder');
  } catch (e) {
    container.innerHTML = '<div class="raadslid-placeholder"><strong>Presentiedata nog niet beschikbaar</strong>Zorg dat de scraper raadsleden_presentie.json genereert.</div>';
  }
}

// NIEUW: gedeelde berekening voor zowel de fractieloyaliteit-tabel als de
// "meest onafhankelijke raadsleden"-ranglijst (#8), zodat de logica maar
// op één plek staat.
function berekenFractieloyaliteitData() {
  if (!stemmingen.some(s => (s.raadsleden_voor || []).length > 0)) return [];
  const allFracties = getAllFracties();
  const ledenMap = {};
  stemmingen.forEach(s => {
    const { voor: fVoor, tegen: fTegen } = getStemSets(s);
    const fractieStandpunt = {};
    allFracties.forEach(f => {
      if (fVoor.has(f)) fractieStandpunt[f] = 'voor';
      else if (fTegen.has(f)) fractieStandpunt[f] = 'tegen';
    });
    const aanwezigen = new Set([
      ...(s.raadsleden_voor || []).map(r => r.naam),
      ...(s.raadsleden_tegen || []).map(r => r.naam),
      ...(s.raadsleden_onthouding || []).map(r => r.naam)
    ]);
    aanwezigen.forEach(naam => {
      const raadslid = (s.raadsleden_voor || []).find(r => r.naam === naam) ||
                       (s.raadsleden_tegen || []).find(r => r.naam === naam) ||
                       (s.raadsleden_onthouding || []).find(r => r.naam === naam);
      if (!raadslid) return;
      if (!ledenMap[naam]) ledenMap[naam] = { naam, fractie: raadslid.fractie, conform: 0, afwijkend: 0, details: [] };
      const fp = fractieStandpunt[raadslid.fractie];
      if (!fp) return;
      const isVoor  = (s.raadsleden_voor || []).find(r => r.naam === naam);
      const isTegen = (s.raadsleden_tegen || []).find(r => r.naam === naam);
      if (!isVoor && !isTegen) return; // onthouding overslaan voor loyaliteitsberekening
      const eigenStem = isVoor ? 'voor' : 'tegen';
      if (eigenStem === fp) ledenMap[naam].conform++;
      else { ledenMap[naam].afwijkend++; ledenMap[naam].details.push({ stemming: s.titel, datum: s.datum, eigen: eigenStem, fractie: fp }); }
    });
  });
  return Object.values(ledenMap).filter(l => (l.conform + l.afwijkend) > 0).sort((a, b) => {
    const pctA = a.conform / (a.conform + a.afwijkend);
    const pctB = b.conform / (b.conform + b.afwijkend);
    return pctA - pctB;
  });
}

function renderFractieloyaliteit() {
  const container = document.getElementById('stemLoyaliteitNieuw');
  if (!container) return;
  const lijst = berekenFractieloyaliteitData();
  if (!lijst.length) {
    container.innerHTML = '<div class="raadslid-placeholder"><strong>Geen raadsledendata</strong>Zorg dat de scraper raadsleden per stemming vastlegt.</div>';
    return;
  }
  let html = '<div style="padding:8px 16px 12px;">';
  lijst.forEach(l => {
    const totaal = l.conform + l.afwijkend;
    const pct = Math.round(l.conform / totaal * 100);
    const kleur = pct >= 90 ? '#177A3C' : pct >= 70 ? '#B86A00' : '#B92B27';
    html += `<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--rule);">
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:600;">${esc(l.naam)} <span style="font-size:11px;color:var(--muted);">(${esc(l.fractie)})</span></div>
        <div style="font-size:11px;color:${kleur};margin-top:2px;">${l.conform}/${totaal} conform fractie (${pct}%)</div>
        ${l.afwijkend > 0 ? `<div style="font-size:11px;color:var(--muted);">${l.afwijkend}x afwijkend</div>` : ''}
      </div>
      <div style="width:120px;height:8px;background:var(--paper);border-radius:4px;overflow:hidden;flex-shrink:0;">
        <div style="width:${pct}%;height:100%;background:${kleur};border-radius:4px;"></div>
      </div>
      <details style="flex-shrink:0;font-size:11px;color:var(--teal);cursor:pointer;">
        <summary>details</summary>
        <ul style="margin:4px 0;padding-left:16px;">
          ${l.details.map(d => `<li>${d.datum}: ${d.stemming} – stemde <strong>${d.eigen}</strong>, fractie <strong>${d.fractie}</strong></li>`).join('')}
        </ul>
      </details>
    </div>`;
  });
  html += '</div>';
  container.innerHTML = html;
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #8 — MEEST ONAFHANKELIJKE RAADSLEDEN (Stemmingen-tab)
// Top-5 op afwijkingspercentage t.o.v. de eigen fractie, minimaal 3
// stemmingen om ruis van eenmalige uitschieters te voorkomen.
// ══════════════════════════════════════════════════════════════════════════
function renderOnafhankelijkeRaadsledenChart() {
  const el = document.getElementById('onafhankelijkeRaadsledenChart');
  if (!el) return;
  const lijst = berekenFractieloyaliteitData().filter(l => (l.conform + l.afwijkend) >= 3);
  if (!lijst.length) { el.innerHTML = '<div class="viz-empty">Onvoldoende data (minimaal 3 stemmingen per raadslid nodig)</div>'; return; }
  const top5 = lijst.slice(0, 5); // al oplopend gesorteerd op conform% → eerste = meest afwijkend
  el.innerHTML = top5.map((l, i) => {
    const totaal = l.conform + l.afwijkend;
    const afwijkPct = Math.round(l.afwijkend / totaal * 100);
    return `<div class="viz-bar-row">
      <div class="rank-badge ${i === 0 ? 'rank-1' : ''}">${i+1}</div>
      <div class="viz-bar-label" style="width:190px;">${esc(l.naam)} <span style="color:var(--muted);font-weight:400;">(${esc(l.fractie)})</span></div>
      <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${afwijkPct}%;background:var(--hold);"></div></div>
      <div class="viz-bar-pct">${afwijkPct}%</div>
    </div>`;
  }).join('');
}

function renderStemLijst() {
  document.getElementById('stemList').innerHTML = stemmingen.map(s => {
    const { voor, tegen } = getStemSets(s);
    const coVoor   = [...COALITIE_FRACTIES].filter(f => voor.has(f)).length;
    const coTegen  = [...COALITIE_FRACTIES].filter(f => tegen.has(f)).length;
    const coSplit  = coVoor > 0 && coTegen > 0;
    return `
      <div class="meeting">
        <div class="meeting-row" onclick="toggleStem('${s.id}')">
          <div class="meeting-date-tag">${fmtDate(s.datum, 'short')}</div>
          <div class="meeting-info">
            <div class="meeting-title-text">${esc(s.titel)}</div>
            <div class="meeting-type-text">${esc(s.uitslag_tekst || s.uitslag || '')}</div>
          </div>
          <div class="meeting-badges">
            ${s.voor_pct === 100 ? '<span class="badge badge-go">Unaniem</span>' : ''}
            ${(s.tegen_pct||0) > 0 ? `<span class="badge badge-stop">${s.tegen_pct}% tegen</span>` : ''}
            ${(s.onthouding_pct||0) > 0 ? `<span class="badge badge-hold">${s.onthouding_pct}% onth.</span>` : ''}
            ${coSplit ? '<span class="badge badge-hold">Coalitie split</span>' : ''}
          </div>
          <div class="meeting-chevron" id="stemch-${s.id}">›</div>
        </div>
        <div class="meeting-details" id="stemdet-${s.id}">
          <div style="padding:12px 20px;">
            ${s.voor_pct != null ? `
              <div style="display:flex;height:8px;border-radius:3px;overflow:hidden;margin-bottom:10px;">
                <div style="width:${s.voor_pct||0}%;background:var(--go);"></div>
                <div style="width:${s.tegen_pct||0}%;background:var(--stop);"></div>
                <div style="width:${s.onthouding_pct||0}%;background:var(--hold);"></div>
              </div>` : ''}
            ${s.fracties_voor       ? `<div style="font-size:11px;color:var(--go);margin-bottom:4px;"><strong>Voor:</strong> ${esc(s.fracties_voor)}</div>` : ''}
            ${s.fracties_tegen      ? `<div style="font-size:11px;color:var(--stop);margin-bottom:4px;"><strong>Tegen:</strong> ${esc(s.fracties_tegen)}</div>` : ''}
            ${s.fracties_onthouding ? `<div style="font-size:11px;color:var(--hold);margin-bottom:8px;"><strong>Onthouding:</strong> ${esc(s.fracties_onthouding)}</div>` : ''}
            ${coSplit ? `<div style="font-size:11px;background:var(--hold-bg);color:var(--hold);padding:5px 8px;margin-bottom:8px;">⚠ Coalitie intern verdeeld — ${coVoor} voor, ${coTegen} tegen</div>` : ''}
            ${(s.raadsleden_tegen||[]).length > 0 ? `
              <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--stop);margin-bottom:6px;">Tegen gestemd</div>
              <div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px;">
                ${(s.raadsleden_tegen||[]).map(r => `<span class="badge badge-stop">${esc(r.naam)} <span style="opacity:.7">${esc(r.fractie)}</span></span>`).join('')}
              </div>` : ''}
            ${(s.raadsleden_onthouding||[]).length > 0 ? `
              <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--hold);margin-bottom:6px;">Onthouding</div>
              <div style="display:flex;flex-wrap:wrap;gap:5px;">
                ${(s.raadsleden_onthouding||[]).map(r => `<span class="badge badge-hold">${esc(r.naam)} <span style="opacity:.7">${esc(r.fractie)}</span></span>`).join('')}
              </div>` : ''}
            <a href="${esc(s.url)}" target="_blank" style="font-size:12px;color:var(--teal);text-decoration:none;font-weight:500;display:inline-block;margin-top:10px;">Open in iBabs →</a>
          </div>
        </div>
      </div>`;
  }).join('');
}

function toggleStem(id) {
  const det = document.getElementById('stemdet-' + id);
  const ch  = document.getElementById('stemch-'  + id);
  if (!det) return;
  const open = det.classList.toggle('open');
  ch.classList.toggle('open', open);
}

function heatmapDetail(i) {
  const s = (window._heatData || [])[i];
  if (!s) return;
  const panel   = document.getElementById('heatmapDetailPanel');
  const content = document.getElementById('heatmapDetailContent');
  if (!panel || !content) return;
  const { voor, tegen, onth } = getStemSets(s);
  content.innerHTML = `
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;">
      <div style="flex:1;min-width:200px;">
        <div style="font-size:13px;font-weight:700;margin-bottom:4px;">${esc(s.titel)}</div>
        <div style="font-size:11px;color:var(--muted);margin-bottom:10px;">${fmtDate(s.datum, 'full')}</div>
        ${s.voor_pct != null ? `
          <div style="display:flex;height:24px;border-radius:2px;overflow:hidden;margin-bottom:8px;max-width:360px;">
            <div style="width:${s.voor_pct||0}%;background:var(--go);"></div>
            <div style="width:${s.tegen_pct||0}%;background:var(--stop);"></div>
            <div style="width:${s.onthouding_pct||0}%;background:var(--hold);"></div>
          </div>` : ''}
        ${s.fracties_voor       ? `<div style="font-size:11px;color:var(--go);margin-bottom:3px;"><strong>Voor:</strong> ${esc(s.fracties_voor)}</div>` : ''}
        ${s.fracties_tegen      ? `<div style="font-size:11px;color:var(--stop);margin-bottom:3px;"><strong>Tegen:</strong> ${esc(s.fracties_tegen)}</div>` : ''}
        ${s.fracties_onthouding ? `<div style="font-size:11px;color:var(--hold);"><strong>Onthouding:</strong> ${esc(s.fracties_onthouding)}</div>` : ''}
      </div>
      <button onclick="document.getElementById('heatmapDetailPanel').style.display='none'"
        style="background:none;border:none;font-size:18px;color:var(--muted);cursor:pointer;flex-shrink:0;">✕</button>
    </div>
  `;
  panel.style.display = 'block';
}

function toonAlliantieDetail(fA, fB) {
  if (!window._alliantieData) return;
  const { metData } = window._alliantieData;
  const relevant = metData.filter(s => {
    const { voor, tegen } = getStemSets(s);
    return (voor.has(fA) || tegen.has(fA)) && (voor.has(fB) || tegen.has(fB));
  });
  let msg = `Stemmingen waarin ${fA} en ${fB} allebei aanwezig waren:\n\n`;
  relevant.forEach(s => {
    const { voor, tegen } = getStemSets(s);
    const gelijk = (voor.has(fA) && voor.has(fB)) || (tegen.has(fA) && tegen.has(fB));
    msg += `- ${s.datum}: ${s.titel} (${gelijk ? 'samen gestemd' : 'verschillend gestemd'})\n`;
  });
  alert(msg);
}

// ── COLLEGEBRIEVEN ────────────────────────────────────────────────────────────
function renderCbStats() {
  const totaalClaims = collegebrieven.reduce((s, b) => s + (b.claims?.length || 0), 0);
  const hoogClaims   = collegebrieven.reduce((s, b) => s + (b.claims || []).filter(c => c.prioriteit === 'HOOG').length, 0);
  document.getElementById('cbTotaal').textContent = collegebrieven.length;
  document.getElementById('cbClaims').textContent = totaalClaims;
  document.getElementById('cbHoog').textContent   = hoogClaims;
}

function populateCbFilters() {
  const selPh = document.getElementById('filterCbPh');
  const phLijst = [...new Set(collegebrieven.map(b => b.portefeuillehouder).filter(Boolean).flatMap(p => p.split(', ')))].sort();
  phLijst.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; selPh.appendChild(o); });
  document.getElementById('cbCount').textContent = collegebrieven.length + ' brieven';
}

function renderCollegebrieven() {
  const typeF   = document.getElementById('filterCbType').value;
  const phF     = document.getElementById('filterCbPh').value;
  const claimsF = document.getElementById('filterCbClaims').value;
  let f = [...collegebrieven];
  if (typeF)   f = f.filter(b => b.type === typeF);
  if (phF)     f = f.filter(b => (b.portefeuillehouder || '').includes(phF));
  if (claimsF === 'claims') f = f.filter(b => (b.claims?.length || 0) > 0);
  if (claimsF === 'hoog')   f = f.filter(b => (b.claims || []).some(c => c.prioriteit === 'HOOG'));
  document.getElementById('cbCount').textContent = f.length + ' brieven';
  if (!f.length) { document.getElementById('cbList').innerHTML = '<div class="empty">Geen collegebrieven gevonden.</div>'; return; }
  document.getElementById('cbList').innerHTML = f.map(b => {
    const claims = b.claims || [];
    const hoogAantal = claims.filter(c => c.prioriteit === 'HOOG').length;
    return `
      <div class="cb-item">
        <div class="cb-row" onclick="toggleCb('${b.id}')">
          <div class="cb-date-tag">${fmtDate(b.datum, 'short')}</div>
          <div class="cb-info">
            <div class="cb-title">${esc(b.titel)}</div>
            <div class="cb-meta">${esc(b.type || '')}${b.portefeuillehouder ? ' · ' + esc(b.portefeuillehouder) : ''}</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center;">
            ${claims.length ? `<span class="badge badge-teal">${claims.length} claims</span>` : ''}
            ${hoogAantal    ? `<span class="badge badge-stop">${hoogAantal} HOOG</span>` : ''}
          </div>
          <div class="cb-chevron" id="cbch-${b.id}">›</div>
        </div>
        <div class="cb-details" id="cbdet-${b.id}">
          <div class="cb-links">
            <a href="${esc(b.url || '#')}" target="_blank" style="font-size:12px;color:var(--teal);text-decoration:none;font-weight:500;">Open in iBabs →</a>
            ${b.pdf_url ? `<a href="${esc(b.pdf_url)}" target="_blank" style="font-size:12px;color:var(--teal);text-decoration:none;font-weight:500;">PDF downloaden →</a>` : ''}
          </div>
          ${claims.length === 0
            ? `<div class="cb-geen-claims">Nog geen claims geïdentificeerd voor deze brief.</div>`
            : claims.map(c => {
                const prioClass = (c.prioriteit || 'LAAG').toLowerCase();
                return `
                  <div class="cb-claim-item">
                    <div class="cb-claim-header">
                      <span class="badge badge-${prioClass === 'hoog' ? 'stop' : prioClass === 'middel' ? 'hold' : 'teal'}">${c.prioriteit || 'LAAG'}</span>
                      ${c.score != null ? `<span class="cb-claim-score">${c.score}/100</span>` : ''}
                    </div>
                    <div class="cb-claim-text">"${esc(c.claim)}"</div>
                    <div class="cb-claim-verificatie">${esc(c.verificatie || '')}</div>
                    ${c.kruischeck ? `<div class="cb-claim-kruischeck">${esc(c.kruischeck)}</div>` : ''}
                  </div>`;
              }).join('')
          }
        </div>
      </div>`;
  }).join('');
}

function toggleCb(id) {
  const det = document.getElementById('cbdet-' + id);
  const ch  = document.getElementById('cbch-'  + id);
  if (!det) return;
  const open = det.classList.toggle('open');
  ch.classList.toggle('open', open);
}

function renderOvCb() {
  const metClaims = collegebrieven.filter(b => (b.claims?.length || 0) > 0).slice(0, 5);
  if (!metClaims.length) { document.getElementById('ovCb').innerHTML = '<div class="empty">Geen brieven met claims beschikbaar.</div>'; return; }
  document.getElementById('ovCb').innerHTML = metClaims.map(b => {
    const topClaim = (b.claims || []).sort((a, b) => (b.score || 0) - (a.score || 0))[0];
    return `
      <div class="mini-item">
        <div class="mini-date">${fmtDate(b.datum, 'short')}</div>
        <div>
          <div class="mini-title">${esc(b.titel)}</div>
          ${topClaim ? `<div class="mini-type">"${esc(topClaim.claim.slice(0, 80))}${topClaim.claim.length > 80 ? '…' : ''}"</div>` : ''}
        </div>
      </div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #16 — PORTEFEUILLEHOUDER-DASHBOARD (Collegebrieven-tab)
// ══════════════════════════════════════════════════════════════════════════
function renderPortefeuillehouderDashboard() {
  const el = document.getElementById('portefeuillehouderDashboard');
  if (!el) return;
  if (!collegebrieven.length) { el.innerHTML = '<div class="viz-empty">Geen collegebrieven</div>'; return; }

  const map = {};
  collegebrieven.forEach(b => {
    const namen = (b.portefeuillehouder || 'Onbekend').split(', ').map(n => n.trim()).filter(Boolean);
    const lengte = (b.tekst || '').split(/\s+/).filter(Boolean).length;
    const hoogClaims = (b.claims || []).filter(c => c.prioriteit === 'HOOG').length;
    namen.forEach(naam => {
      if (!map[naam]) map[naam] = { brieven: 0, hoogTotaal: 0, lengteTotaal: 0 };
      map[naam].brieven++;
      map[naam].hoogTotaal += hoogClaims;
      map[naam].lengteTotaal += lengte;
    });
  });

  const rijen = Object.entries(map).sort((a, b) => b[1].brieven - a[1].brieven);
  el.innerHTML = `<div class="ph-dash-grid">` + rijen.map(([naam, d]) => `
    <div class="ph-dash-card">
      <div class="ph-dash-name">${esc(naam)}</div>
      <div class="ph-dash-row"><span>Brieven</span><strong>${d.brieven}</strong></div>
      <div class="ph-dash-row"><span>Gem. HOOG-claims</span><strong>${(d.hoogTotaal / d.brieven).toFixed(1)}</strong></div>
      <div class="ph-dash-row"><span>Gem. brieflengte</span><strong>${Math.round(d.lengteTotaal / d.brieven)} woorden</strong></div>
    </div>`).join('') + `</div>`;
}

// ══════════════════════════════════════════════════════════════════════════
// NIEUW #17 — GELD GENOEMD DOOR HET COLLEGE (Collegebrieven-tab)
// €-bedragen geëxtraheerd uit claims[].claim, per portefeuillehouder.
// ══════════════════════════════════════════════════════════════════════════
function parseEuroBedrag(str) {
  const m = String(str || '').match(/€\s?([\d.,]+)\s?(mln|miljoen|mld|miljard)?/i);
  if (!m) return null;
  let getal = m[1];
  const eenheid = (m[2] || '').toLowerCase();
  if (eenheid) {
    getal = parseFloat(getal.replace(/\./g, '').replace(',', '.'));
    getal *= eenheid.startsWith('mld') ? 1_000_000_000 : 1_000_000;
  } else {
    getal = parseFloat(getal.replace(/\./g, '').replace(',', '.'));
  }
  return isNaN(getal) ? null : getal;
}

function fmtEuroKort(v) {
  if (v >= 1_000_000) return '€' + (v / 1_000_000).toFixed(1).replace('.', ',') + ' mln';
  return '€' + Math.round(v).toLocaleString('nl-NL');
}

function renderGeldChart() {
  const el = document.getElementById('geldChart');
  if (!el) return;
  const perPh = {};
  collegebrieven.forEach(b => {
    const namen = (b.portefeuillehouder || 'Onbekend').split(', ').map(n => n.trim()).filter(Boolean);
    (b.claims || []).forEach(c => {
      const bedrag = parseEuroBedrag(c.claim);
      if (bedrag == null) return;
      namen.forEach(naam => { perPh[naam] = (perPh[naam] || 0) + bedrag; });
    });
  });
  const rijen = Object.entries(perPh).sort((a, b) => b[1] - a[1]);
  if (!rijen.length) { el.innerHTML = '<div class="viz-empty">Geen €-bedragen herkend in claims</div>'; return; }
  const maxV = rijen[0][1];
  el.innerHTML = rijen.map(([naam, bedrag]) => `<div class="viz-bar-row">
    <div class="viz-bar-label" style="width:170px;">${esc(naam)}</div>
    <div class="viz-bar-track"><div class="viz-bar-fill" style="width:${Math.round(bedrag/maxV*100)}%;background:var(--go);"></div></div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);width:80px;text-align:right;flex-shrink:0;">${fmtEuroKort(bedrag)}</div>
  </div>`).join('') + '<div style="padding:8px 20px 0;font-size:10px;color:var(--muted);">Bedragen automatisch herkend via regex op claim-teksten — controleer bij twijfel de brontekst.</div>';
}

// ── FACTCHECK ─────────────────────────────────────────────────────────────────
function audioBestandGekozen() {
  const file = document.getElementById('audioFile').files[0];
  const section = document.getElementById('audioSection');
  const btn = document.getElementById('transcribeBtn');
  const status = document.getElementById('audioStatusMsg');
  if (!file) return;
  if (file.size > 15 * 1024 * 1024) {
    status.textContent = '⚠ Bestand te groot (max 15 MB). Gebruik Google AI Studio voor grote bestanden.';
    status.style.display = 'block';
    section.classList.remove('has-file');
    btn.style.display = 'none';
    return;
  }
  section.classList.add('has-file');
  status.textContent = `✓ ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
  status.style.display = 'block';
  btn.style.display = 'block';
}

async function transcribeerAudio() {
  const file   = document.getElementById('audioFile').files[0];
  const apiKey = document.getElementById('geminiKey').value.trim();
  const btn    = document.getElementById('transcribeBtn');
  const status = document.getElementById('audioStatusMsg');
  if (!apiKey) { alert('Voer eerst een Gemini API key in.'); return; }
  if (!file)   return;
  btn.disabled = true; btn.textContent = 'Transcriberen...';
  status.textContent = 'Audio wordt verwerkt door Gemini…';
  try {
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload  = () => resolve(reader.result.split(',')[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=${apiKey}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          contents: [{ parts: [
            { inline_data: { mime_type: file.type, data: base64 } },
            { text: 'Transcribeer deze audio volledig en nauwkeurig naar het Nederlands. Geef alleen de transcriptie terug, geen uitleg of samenvatting.' }
          ]}],
          generationConfig: { temperature: 0, maxOutputTokens: 8192 }
        })
      }
    );
    const data = await res.json();
    if (data.error) throw new Error(data.error.message);
    document.getElementById('fcText').value = data.candidates[0].content.parts[0].text;
    status.textContent = '✓ Transcriptie klaar — controleer de tekst en klik op Analyseer';
    btn.textContent = '↻ Opnieuw transcriberen';
  } catch(e) {
    status.textContent = `Fout bij transcriberen: ${e.message}`;
    btn.textContent = 'Transcribeer audio →';
  }
  btn.disabled = false;
}

// NIEUW #28: bouwEigenDataContext uitgebreid met een apart, makkelijk te
// matchen blok met €-bedragen per portefeuillehouder (zelfde extractie als
// renderGeldChart), zodat Gemini exacte-bedrag-matches kan maken i.p.v.
// alleen losse tekstuele gelijkenis.
function bouwEigenDataContext() {
  const delen = [];
  if (moties.length) {
    const recent = moties.slice(0, 20).map(m => `- ${m.datum || '?'}: "${m.titel}" (${m.partij || '?'}, ${m.status || 'onbekend'})`).join('\n');
    delen.push(`RECENTE MOTIES & STEMMINGEN:\n${recent}`);
  }
  if (camerasActief.length || woningsluitingen.length) {
    const camLijst = camerasActief.slice(0, 10).map(c => `- cameratoezicht: ${c.camera} (${c.start} t/m ${c.eind})`).join('\n');
    const woningLijst = woningsluitingen.slice(0, 10).map(w => `- ${w.datum || '?'}: woningsluiting ${w.titel}`).join('\n');
    delen.push(`RECENTE CAMERA'S & WONINGSLUITINGEN:\n${camLijst}\n${woningLijst}`);
  }
  if (stemmingen.length) {
    const recent = stemmingen.slice(0, 10).map(s => {
      const uitslag = s.voor_pct != null ? `${s.voor_pct}% voor, ${s.tegen_pct || 0}% tegen` : (s.uitslag_tekst || s.uitslag || 'onbekend');
      return `- ${s.datum || '?'}: "${s.titel}" — ${uitslag}`;
    }).join('\n');
    delen.push(`RECENTE STEMMINGEN:\n${recent}`);
  }
  const cbClaims = collegebrieven.flatMap(b => (b.claims || []).map(c => `- ${b.datum}: "${c.claim}" (${b.titel}, ${b.portefeuillehouder || 'onbekend'})`)).slice(0, 15).join('\n');
  if (cbClaims) delen.push(`CLAIMS UIT COLLEGEBRIEVEN (met datum, brieftitel en portefeuillehouder):\n${cbClaims}`);

  // NIEUW: geldbedragen expliciet apart, zodat exacte-bedrag-matching makkelijker is
  const perPhBedrag = {};
  collegebrieven.forEach(b => {
    (b.claims || []).forEach(c => {
      const bedrag = parseEuroBedrag(c.claim);
      if (bedrag == null) return;
      const key = b.portefeuillehouder || 'Onbekend';
      perPhBedrag[key] = (perPhBedrag[key] || 0) + bedrag;
    });
  });
  const geldLijst = Object.entries(perPhBedrag).map(([ph, bedrag]) => `- ${ph}: ${fmtEuroKort(bedrag)}`).join('\n');
  if (geldLijst) delen.push(`EERDER GENOEMDE €-BEDRAGEN PER PORTEFEUILLEHOUDER:\n${geldLijst}`);

  const storedClaims = JSON.parse(localStorage.getItem('zr_claims') || '[]');
  if (storedClaims.length) {
    const recent = storedClaims.slice(0, 10).map(c => `- ${c.datum}: "${c.claim}" (bron: ${c.bron_titel || '?'})`).join('\n');
    delen.push(`EERDER GEÏDENTIFICEERDE CLAIMS:\n${recent}`);
  }
  return delen.join('\n\n');
}

async function analyseerFactcheck() {
  const apiKey  = document.getElementById('geminiKey').value.trim();
  const context = document.getElementById('fcContext').value.trim();
  const tekst   = document.getElementById('fcText').value.trim();
  const btn     = document.getElementById('fcBtn');
  const status  = document.getElementById('fcStatus');
  const lijst   = document.getElementById('claimsList');
  const saveBtn = document.getElementById('fcSaveBtn');
  if (!apiKey) { alert('Voer eerst een Gemini API key in.'); return; }
  if (tekst.length < 100) { alert('Voer minimaal 100 tekens transcriptie in.'); return; }
  btn.disabled = true; btn.textContent = 'Analyseren...';
  saveBtn.style.display = 'none';
  status.textContent = 'Gemini is aan het werk…';
  lijst.innerHTML = '<div class="factcheck-empty">Claims worden geïdentificeerd en gecheckt tegen eigen data...</div>';
  const eigenData = bouwEigenDataContext();
  // NIEUW #28: expliciete matching-criteria i.p.v. vage "vind je een relatie"-instructie
  const prompt = `Je bent een factcheck-assistent voor een journalist die gemeenteraadsvergaderingen van Zaanstad analyseert.
${context ? 'Context: ' + context : ''}

Analyseer de transcriptie en identificeer alle feitelijke claims die verifieerbaar zijn.
Denk aan: getallen, percentages, datums, tijdlijnen, beloftes van het college, budgetten, aantallen woningen, criminaliteitscijfers.

${eigenData ? `Vergelijk elke claim met de onderstaande eigen data. Gebruik hierbij deze prioriteitsvolgorde voor een kruischeck:
1. Exact dezelfde €-bedragen of aantallen (sterkste match — noem het exacte bedrag/aantal en de bron expliciet).
2. Dezelfde portefeuillehouder of hetzelfde onderwerp binnen een vergelijkbare periode (datum dicht bij elkaar).
3. Tegenstrijdige cijfers over hetzelfde onderwerp (meld dit als "tegenspraak" met beide waarden genoemd).
Noem in de kruischeck altijd expliciet WELK stuk eigen data overeenkomt (bijv. brieftitel, motienummer of datum) zodat de journalist het direct kan terugvinden. Als er geen duidelijke relatie is op basis van bovenstaande criteria, laat kruischeck leeg — verzin geen zwakke verbanden.\n\n${eigenData}` : ''}

Geef voor elke claim:
- De exacte claim
- Wie het zei (als duidelijk)
- Hoe een journalist dit kan controleren
- Prioriteit: HOOG / MIDDEL / LAAG
- Score: 0-100 (checkwaardigheid)
- Kruischeck: korte opmerking als er een relatie is met eigen data (anders leeg laten)

Maximaal 10 claims, HOOG eerst.

Antwoord ALLEEN met een JSON-array, geen markdown:
[{"claim":"...","spreker":"...","verificatie":"...","prioriteit":"HOOG","score":85,"kruischeck":"..."}]

Transcriptie:
${tekst.slice(0, 9000)}`;

  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=${apiKey}`,
      { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ contents:[{parts:[{text:prompt}]}], generationConfig:{temperature:0.2,maxOutputTokens:2048} }) }
    );
    const data = await res.json();
    if (data.error) throw new Error(data.error.message);
    const raw   = data.candidates[0].content.parts[0].text;
    const match = raw.match(/\[[\s\S]*\]/);
    if (!match) throw new Error('Onverwacht antwoordformaat van Gemini.');
    huidigeClaims = JSON.parse(match[0]);
    status.textContent = huidigeClaims.length + ' claims gevonden';
    lijst.innerHTML = huidigeClaims.map((c, i) => {
      const prioClass = (c.prioriteit || 'MIDDEL').toLowerCase();
      const kruisClass = c.kruischeck ? (c.kruischeck.toLowerCase().includes('tegenspr') ? 'weersproken' : 'bevestigd') : '';
      return `
        <div class="claim-item">
          <div class="claim-num ${prioClass}">${i+1} — ${c.prioriteit || 'MIDDEL'}${c.score != null ? ` · ${c.score}/100` : ''}</div>
          <div class="claim-text">"${esc(c.claim)}"</div>
          ${c.spreker ? `<div class="claim-speaker">— ${esc(c.spreker)}</div>` : ''}
          <div class="claim-check">${esc(c.verificatie)}</div>
          ${c.kruischeck ? `<div class="claim-kruischeck ${kruisClass}">⟳ ${esc(c.kruischeck)}</div>` : ''}
        </div>`;
    }).join('');
    saveBtn.style.display = 'block';
  } catch (e) {
    status.textContent = 'Fout';
    lijst.innerHTML = `<div class="error-msg">Fout: ${esc(e.message)}<br>
      Controleer je API key op <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--stop)">aistudio.google.com</a>.</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Analyseer op verifieerbare claims →';
  }
}

function slaClaimsOp() {
  if (!huidigeClaims.length) return;
  const context  = document.getElementById('fcContext').value.trim() || 'onbekend';
  const bestaand = JSON.parse(localStorage.getItem('zr_claims') || '[]');
  const nieuw    = huidigeClaims.map(c => ({ ...c, bron: 'vergadering', bron_titel: context, datum: new Date().toISOString().slice(0, 10), opgeslagen: new Date().toISOString() }));
  localStorage.setItem('zr_claims', JSON.stringify([...nieuw, ...bestaand].slice(0, 200)));
  document.getElementById('fcSaveBtn').textContent = `✓ ${nieuw.length} claims opgeslagen`;
  setTimeout(() => document.getElementById('fcSaveBtn').textContent = '↓ Sla claims op voor kruisverificatie', 2500);
  renderOpgeslagenClaims();
}

function renderOpgeslagenClaims() {
  const claims = JSON.parse(localStorage.getItem('zr_claims') || '[]');
  const el = document.getElementById('savedClaimsList');
  if (!claims.length) { el.innerHTML = '<div class="empty" style="padding:16px;font-size:12px;">Nog geen claims opgeslagen.</div>'; return; }
  el.innerHTML = claims.slice(0, 15).map(c => `
    <div class="saved-claim-item">
      <div class="saved-claim-text">"${esc(c.claim.slice(0, 90))}${c.claim.length > 90 ? '…' : ''}"</div>
      <div class="saved-claim-meta">${esc(c.bron_titel)} · ${c.datum} · <span class="badge badge-${(c.prioriteit||'LAAG').toLowerCase() === 'hoog' ? 'stop' : 'teal'}" style="font-size:9px;">${c.prioriteit||'?'}</span></div>
    </div>`).join('') +
    (claims.length > 15 ? `<div style="padding:10px 20px;font-size:11px;color:var(--muted);">+ ${claims.length - 15} meer opgeslagen</div>` : '');
}

function clearOpgeslagenClaims() {
  if (!confirm('Weet je zeker dat je alle opgeslagen claims wilt verwijderen?')) return;
  localStorage.removeItem('zr_claims');
  renderOpgeslagenClaims();
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function fmtDate(s, mode) {
  if (!s) return '—';
  try {
    const d = new Date(s + 'T00:00:00');
    return mode === 'short'
      ? d.toLocaleDateString('nl-NL', { day:'numeric', month:'short' })
      : d.toLocaleDateString('nl-NL', { day:'numeric', month:'short', year:'numeric' });
  } catch { return s; }
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
