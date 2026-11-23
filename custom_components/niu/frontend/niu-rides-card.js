// niu-rides-card: rittenlog van de NIU-integratie, per dag gegroepeerd, elke rit klapt uit met kaart.
// Data komt uit de websocket-opdracht "niu/rides" van de integratie.

const CARD_VERSION = "1.0.0";

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class NiuRidesCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rides = [];
    this._total = 0;
    this._summary = null;
    this._backfilling = false;
    this._loading = false;
    this._error = null;
    this._open = new Set();
    this._lastStamp = null;
    this._loadedOnce = false;
  }

  static getStubConfig() {
    return { title: "Ritten" };
  }

  setConfig(config) {
    this._config = {
      title: "Ritten",
      limit: 15,
      compact: false,
      show_summary: true,
      ...config,
    };
    this._pageSize = Number(this._config.limit) || 15;
    if (this._hass) this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const entity = this._config?.entity;
    const stamp = entity && hass.states[entity] ? hass.states[entity].last_updated : null;
    if (first || !this._loadedOnce) {
      this._lastStamp = stamp;
      this._load(true);
    } else if (stamp && stamp !== this._lastStamp) {
      this._lastStamp = stamp;
      this._load(true);
    }
  }

  connectedCallback() {
    if (!this._config?.entity) {
      this._timer = setInterval(() => this._hass && this._load(true), 5 * 60 * 1000);
    }
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  getCardSize() {
    return this._config?.compact ? 4 : 10;
  }

  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }

  get _lang() {
    return this._hass?.locale?.language || this._hass?.language || "nl";
  }

  async _load(reset) {
    if (this._loading || !this._hass) return;
    this._loading = true;
    try {
      const offset = reset ? 0 : this._rides.length;
      const limit = reset ? Math.max(this._pageSize, this._rides.length || 0) : this._pageSize;
      const result = await this._hass.callWS({
        type: "niu/rides",
        offset,
        limit: Math.min(limit, 200),
        ...(this._config.entry_id ? { entry_id: this._config.entry_id } : {}),
      });
      this._rides = reset ? result.rides : this._rides.concat(result.rides);
      this._total = result.total;
      this._summary = result.summary;
      this._backfilling = result.backfilling;
      this._error = null;
      this._loadedOnce = true;
    } catch (err) {
      this._error = err?.message || String(err);
      this._loadedOnce = true;
    } finally {
      this._loading = false;
      this._render();
    }
  }

  // ------------------------------------------------------------ formatting

  _date(iso) {
    return iso ? new Date(iso) : null;
  }

  _time(date) {
    if (!date) return "–";
    return date.toLocaleTimeString(this._lang, { hour: "2-digit", minute: "2-digit" });
  }

  _dayKey(date) {
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  }

  _dayTitle(date) {
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (this._dayKey(date) === this._dayKey(today)) return "Vandaag";
    if (this._dayKey(date) === this._dayKey(yesterday)) return "Gisteren";
    const sameYear = date.getFullYear() === today.getFullYear();
    const text = date.toLocaleDateString(this._lang, {
      weekday: "long",
      day: "numeric",
      month: "long",
      ...(sameYear ? {} : { year: "numeric" }),
    });
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  _km(meters, digits = 1) {
    if (meters == null) return "–";
    return (meters / 1000).toLocaleString(this._lang, { minimumFractionDigits: digits, maximumFractionDigits: digits }) + " km";
  }

  _duration(seconds) {
    if (seconds == null) return "–";
    const minutes = Math.round(seconds / 60);
    if (minutes < 1) return `${Math.round(seconds)} s`;
    if (minutes < 60) return `${minutes} min`;
    return `${Math.floor(minutes / 60)} u ${minutes % 60} min`;
  }

  _speed(value) {
    if (value == null) return "–";
    return `${Math.round(value)} km/u`;
  }

  _place(name, point) {
    if (name) return esc(name);
    if (point) return `${point[0].toFixed(4)}, ${point[1].toFixed(4)}`;
    return "Onbekend";
  }

  _mapsLink(ride) {
    const s = ride.start_point;
    const e = ride.end_point;
    if (!s || !e) return null;
    return `https://www.google.com/maps/dir/?api=1&origin=${s[0]},${s[1]}&destination=${e[0]},${e[1]}&travelmode=bicycling`;
  }

  // -------------------------------------------------------------- rendering

  _summaryHtml() {
    const s = this._summary;
    if (!s || !this._config.show_summary) return "";
    const chip = (label, total) => `
      <div class="chip">
        <div class="chip-label">${label}</div>
        <div class="chip-value">${total.km.toLocaleString(this._lang, { maximumFractionDigits: 1 })} km</div>
        <div class="chip-sub">${total.rides} ${total.rides === 1 ? "rit" : "ritten"}</div>
      </div>`;
    return `<div class="summary">${chip("Vandaag", s.today)}${chip("Deze week", s.week)}${chip("Deze maand", s.month)}</div>`;
  }

  _rideHtml(ride) {
    const start = this._date(ride.start);
    const end = this._date(ride.end) || (start && ride.duration_s != null ? new Date(start.getTime() + ride.duration_s * 1000) : null);
    const open = this._open.has(ride.id);
    const meta = [this._duration(ride.duration_s), this._speed(ride.avg_speed)];
    if (ride.consumption != null) meta.push(`${Math.round(ride.consumption)}% accu`);
    const link = this._mapsLink(ride);
    const caption = {
      gps: "Gereden route (GPS)",
      thumbnail: "Route overgenomen uit NIU",
      niu: "Routekaart van NIU",
      markers: "Geen route beschikbaar · alleen start en eind",
    }[ride.map_source] || "";
    const detail = open
      ? `<div class="detail">
          ${ride.map
            ? `<img class="map ${ride.map_source === "niu" ? "square" : ""}" src="${esc(ride.map)}" alt="Kaart van de rit" loading="lazy">
               <div class="caption">${esc(caption)}</div>`
            : `<div class="nomap"><ha-icon icon="mdi:map-clock-outline"></ha-icon><span>Kaart wordt nog getekend…</span></div>`}
          <div class="grid">
            <div><span>Van</span>${this._place(ride.from, ride.start_point)}</div>
            <div><span>Naar</span>${this._place(ride.to, ride.end_point)}</div>
            <div><span>Vertrek</span>${this._time(start)}</div>
            <div><span>Aankomst</span>${this._time(end)}</div>
            <div><span>Afstand</span>${this._km(ride.distance_m, 2)}</div>
            <div><span>Rijtijd</span>${this._duration(ride.duration_s)}</div>
            <div><span>Gem. snelheid</span>${this._speed(ride.avg_speed)}</div>
            <div><span>Accuverbruik</span>${ride.consumption != null ? Math.round(ride.consumption) + "%" : "–"}</div>
          </div>
          ${link ? `<a class="maps" href="${link}" target="_blank" rel="noopener"><ha-icon icon="mdi:google-maps"></ha-icon>Open in Google Maps</a>` : ""}
        </div>`
      : "";
    return `
      <div class="ride ${open ? "open" : ""}">
        <button class="row" data-id="${esc(ride.id)}" aria-expanded="${open}">
          <div class="icon"><ha-icon icon="mdi:moped-electric"></ha-icon></div>
          <div class="main">
            <div class="top">
              <span class="time">${this._time(start)} – ${this._time(end)}</span>
              <span class="dist">${this._km(ride.distance_m)}</span>
            </div>
            <div class="route">${this._place(ride.from, ride.start_point)} <span class="arrow">→</span> ${this._place(ride.to, ride.end_point)}</div>
            <div class="meta">${meta.join(" · ")}</div>
          </div>
          <ha-icon class="chev" icon="${open ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
        </button>
        ${detail}
      </div>`;
  }

  _listHtml() {
    if (this._error) return `<div class="empty">Kon ritten niet laden: ${esc(this._error)}</div>`;
    if (!this._loadedOnce) return `<div class="empty">Ritten laden…</div>`;
    if (!this._rides.length) {
      return `<div class="empty">${this._backfilling ? "Ritgeschiedenis wordt ingelezen…" : "Nog geen ritten."}</div>`;
    }
    const groups = [];
    for (const ride of this._rides) {
      const date = this._date(ride.start);
      if (!date) continue;
      const key = this._dayKey(date);
      let group = groups[groups.length - 1];
      if (!group || group.key !== key) {
        group = { key, date, rides: [], meters: 0 };
        groups.push(group);
      }
      group.rides.push(ride);
      group.meters += ride.distance_m || 0;
    }
    return groups
      .map(
        (g) => `
        <div class="day">
          <div class="day-head"><span>${this._dayTitle(g.date)}</span><span>${this._km(g.meters)} · ${g.rides.length} ${g.rides.length === 1 ? "rit" : "ritten"}</span></div>
          ${g.rides.map((r) => this._rideHtml(r)).join("")}
        </div>`
      )
      .join("");
  }

  _footerHtml() {
    const parts = [];
    if (!this._config.compact && this._rides.length < this._total) {
      parts.push(`<button class="more" data-action="more">${this._loading ? "Laden…" : `Meer ritten (${this._total - this._rides.length})`}</button>`);
    }
    if (this._config.more_path) {
      parts.push(`<button class="more" data-action="navigate"><ha-icon icon="mdi:format-list-bulleted"></ha-icon>${esc(this._config.more_label || "Volledig rittenlog")}</button>`);
    }
    if (this._backfilling && this._rides.length) {
      parts.push(`<div class="hint">Oudere ritten worden nog ingelezen…</div>`);
    }
    return parts.length ? `<div class="footer">${parts.join("")}</div>` : "";
  }

  _render() {
    if (!this._config) return;
    const title = this._config.title
      ? `<div class="header"><ha-icon icon="${esc(this._config.icon || "mdi:map-marker-path")}"></ha-icon><span>${esc(this._config.title)}</span></div>`
      : "";
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        ${title}
        ${this._summaryHtml()}
        <div class="list">${this._listHtml()}</div>
        ${this._footerHtml()}
      </ha-card>`;
    this.shadowRoot.querySelectorAll("button.row").forEach((button) =>
      button.addEventListener("click", () => {
        const id = button.dataset.id;
        if (this._open.has(id)) this._open.delete(id);
        else this._open.add(id);
        this._render();
      })
    );
    this.shadowRoot.querySelector('[data-action="more"]')?.addEventListener("click", () => this._load(false));
    this.shadowRoot.querySelector('[data-action="navigate"]')?.addEventListener("click", () => {
      history.pushState(null, "", this._config.more_path);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
    });
  }
}

const STYLE = `
  ha-card { padding: 0 0 8px; overflow: hidden; }
  .header { display: flex; align-items: center; gap: 10px; padding: 16px 16px 8px; font-size: 1.15em; font-weight: 500; }
  .header ha-icon { color: var(--state-icon-color, var(--primary-color)); }
  .summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 4px 16px 8px; }
  .chip { background: var(--secondary-background-color); border-radius: 12px; padding: 8px 10px; }
  .chip-label { font-size: 0.75em; color: var(--secondary-text-color); }
  .chip-value { font-size: 1.1em; font-weight: 600; }
  .chip-sub { font-size: 0.75em; color: var(--secondary-text-color); }
  .day { padding: 0 8px; }
  .day-head { display: flex; justify-content: space-between; padding: 12px 8px 4px; font-size: 0.8em; font-weight: 600;
              color: var(--secondary-text-color); text-transform: none; }
  .ride { border-radius: 12px; }
  .ride.open { background: var(--secondary-background-color); margin: 2px 0 6px; }
  button.row { all: unset; box-sizing: border-box; width: 100%; display: flex; align-items: center; gap: 12px; padding: 8px; cursor: pointer;
               border-radius: 12px; }
  button.row:hover { background: var(--secondary-background-color); }
  button.row:focus-visible { outline: 2px solid var(--primary-color); }
  .icon { flex: none; width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center;
          background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.15); color: var(--primary-color); }
  .icon ha-icon { --mdc-icon-size: 20px; }
  .main { flex: 1; min-width: 0; }
  .top { display: flex; justify-content: space-between; gap: 8px; font-weight: 500; }
  .dist { font-variant-numeric: tabular-nums; }
  .route { font-size: 0.9em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .arrow { color: var(--secondary-text-color); }
  .meta { font-size: 0.8em; color: var(--secondary-text-color); }
  .chev { flex: none; color: var(--secondary-text-color); }
  .detail { padding: 0 8px 10px; }
  .map { display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: cover; border-radius: 10px; background: var(--card-background-color); }
  .map.square { aspect-ratio: 1; max-width: 400px; margin: 0 auto; object-fit: contain; }
  .caption { font-size: 0.72em; color: var(--secondary-text-color); padding: 4px 2px 0; }
  .nomap { display: flex; align-items: center; justify-content: center; gap: 8px; aspect-ratio: 16 / 10; border-radius: 10px;
           background: var(--card-background-color); color: var(--secondary-text-color); font-size: 0.9em; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; padding: 10px 2px 4px; font-size: 0.9em; }
  .grid span { display: block; font-size: 0.75em; color: var(--secondary-text-color); }
  .maps { display: inline-flex; align-items: center; gap: 6px; margin: 6px 2px 0; color: var(--primary-color); text-decoration: none; font-size: 0.9em; }
  .maps ha-icon { --mdc-icon-size: 18px; }
  .footer { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 8px 16px 4px; }
  button.more { all: unset; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 18px;
                color: var(--primary-color); background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.1); font-weight: 500; }
  button.more ha-icon { --mdc-icon-size: 18px; }
  .hint, .empty { color: var(--secondary-text-color); font-size: 0.85em; }
  .empty { padding: 16px; text-align: center; }
`;

if (!customElements.get("niu-rides-card")) {
  customElements.define("niu-rides-card", NiuRidesCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "niu-rides-card",
    name: "NIU rittenlog",
    description: "Ritten van je NIU-scooter per dag, met kaart per rit.",
  });
  console.info(`%c NIU-RIDES-CARD %c ${CARD_VERSION} `, "background:#e30613;color:#fff", "");
}
