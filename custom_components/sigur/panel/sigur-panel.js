/**
 * Sigur sidebar panel.
 *
 * A plain custom element with no imports. Home Assistant does not expose bare
 * module specifiers to custom panels, and borrowing Lit off one of its internal
 * elements breaks on every frontend refactor, so this renders by hand and
 * styles itself entirely from Home Assistant's own CSS custom properties. That
 * way it follows the user's theme, light or dark, without knowing any colours.
 *
 * Structure comes from the `sigur/panel/data` websocket command; live values
 * come from `hass.states`, which the panel is handed on every state change.
 */

const EVENT_TYPE = "sigur_event";
/** How many events the feed keeps. Older ones live in the recorder. */
const FEED_LIMIT = 60;

const MODE_LABELS = {
  normal: "Нормальный",
  locked: "Заблокировано",
  unlocked: "Разблокировано",
};

const CATEGORY_LABELS = {
  pass_registered: "Проход",
  access_granted: "Доступ разрешён",
  access_denied: "Доступ запрещён",
  break_in: "Взлом",
  door_opened: "Дверь открыта",
  door_closed: "Дверь закрыта",
  door_held_open_start: "Удержание двери",
  door_held_open_end: "Удержание закончено",
  link_lost: "Связь потеряна",
  link_restored: "Связь восстановлена",
  mode_changed: "Смена режима",
  lock_fault: "Неисправность замка",
  power_mains: "Питание от сети",
  power_battery: "От аккумулятора",
  tamper: "Вскрытие корпуса",
  fire_alarm: "Пожарная тревога",
  waiting: "Ожидание",
  face: "Распознавание лица",
  temperature: "Температура",
  power_quality: "Напряжение",
  alarm_panel: "ОПС",
  gate: "Ворота",
  other: "Другое",
  unknown: "Неизвестное",
};

/** Categories worth colouring red in the feed. */
const ALARM_CATEGORIES = new Set([
  "break_in",
  "access_denied",
  "lock_fault",
  "fire_alarm",
  "tamper",
  "link_lost",
]);

const DIRECTION_LABELS = { in: "вход", out: "выход", unknown: "", none: "" };

const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );

const formatTime = (iso) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
};

class SigurPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._servers = [];
    this._feed = [];
    this._loaded = false;
    this._error = null;
    this._unsubscribe = null;
    this._filter = "";
    this._onlyProblems = false;
    this._isAdmin = false;
    this._editing = null;
  }

  /** Home Assistant assigns this on every state change. */
  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this._isAdmin = hass.user?.is_admin ?? false;
    if (first) {
      this._load();
      this._subscribe();
    } else if (this._loaded) {
      // Structure is unchanged; only the live values need repainting.
      this._paintStates();
    }
  }

  get hass() {
    return this._hass;
  }

  set narrow(value) {
    this._narrow = value;
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) {
      this._render();
    }
  }

  disconnectedCallback() {
    if (this._unsubscribe) {
      this._unsubscribe.then((off) => off()).catch(() => undefined);
      this._unsubscribe = null;
    }
  }

  async _load() {
    try {
      const result = await this._hass.callWS({ type: "sigur/panel/data" });
      this._servers = result.servers ?? [];
      this._error = null;
    } catch (err) {
      this._error = err?.message ?? String(err);
    }
    this._loaded = true;
    this._render();
  }

  _subscribe() {
    this._unsubscribe = this._hass.connection.subscribeEvents((event) => {
      this._feed = [event.data, ...this._feed].slice(0, FEED_LIMIT);
      this._paintFeed();
    }, EVENT_TYPE);
  }

  // --- state helpers -------------------------------------------------------

  _state(entityId) {
    return entityId ? this._hass.states[entityId] : undefined;
  }

  _stateValue(entityId) {
    return this._state(entityId)?.state;
  }

  /** Everything the tile needs to draw itself, read fresh from `hass`. */
  _tileState(ap) {
    const link = this._stateValue(ap.entities.connectivity);
    const door = this._stateValue(ap.entities.door);
    const mode = this._stateValue(ap.entities.mode);
    const online = link === "on";
    return {
      online,
      unknownLink: link === undefined || link === "unavailable",
      door,
      mode: mode && mode !== "unknown" && mode !== "unavailable" ? mode : null,
      problem: !online || door === "on",
    };
  }

  _matchesFilter(ap) {
    if (!this._filter) return true;
    const needle = this._filter.toLowerCase();
    return (
      ap.name.toLowerCase().includes(needle) || String(ap.id).includes(needle)
    );
  }

  // --- actions -------------------------------------------------------------

  async _setMode(entityId, option) {
    try {
      await this._hass.callService("select", "select_option", {
        entity_id: entityId,
        option,
      });
    } catch (err) {
      this._toast(err?.message ?? String(err));
    }
  }

  async _press(entityId) {
    try {
      await this._hass.callService("button", "press", { entity_id: entityId });
      this._toast("Проход разрешён");
    } catch (err) {
      this._toast(err?.message ?? String(err));
    }
  }

  _toast(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", {
        detail: { message },
        bubbles: true,
        composed: true,
      }),
    );
  }

  _openMore(entityId) {
    if (!entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // --- rendering -----------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `<style>${SigurPanel.styles}</style>${this._body()}`;
    this._bind();
    this._paintStates();
    this._paintFeed();
  }

  _body() {
    if (!this._loaded) {
      return `<div class="center muted">Загрузка…</div>`;
    }
    if (this._error) {
      return `<div class="center error">Не удалось получить данные: ${escapeHtml(
        this._error,
      )}</div>`;
    }
    if (!this._servers.length) {
      return `<div class="center muted">Нет настроенных систем Sigur.</div>`;
    }
    return `
      <div class="layout">
        <main>
          <div class="toolbar">
            <input id="filter" type="search" placeholder="Поиск точки доступа"
                   value="${escapeHtml(this._filter)}" />
            <label class="toggle">
              <input id="problems" type="checkbox" ${this._onlyProblems ? "checked" : ""} />
              <span>Только проблемные</span>
            </label>
          </div>
          ${this._servers.map((server) => this._server(server)).join("")}
          ${this._cameraOptions()}
        </main>
        <aside>
          <h2>События</h2>
          <div id="feed" class="feed"></div>
        </aside>
      </div>`;
  }

  _server(server) {
    const zones = new Map();
    for (const ap of server.access_points) {
      const key = ap.zone_a_name || (ap.zone_a != null ? `Зона ${ap.zone_a}` : "Без зоны");
      if (!zones.has(key)) zones.set(key, []);
      zones.get(key).push(ap);
    }
    const sections = [...zones.entries()]
      .map(
        ([zone, points]) => `
        <section class="zone" data-zone="${escapeHtml(zone)}">
          <h3>${escapeHtml(zone)}</h3>
          <div class="grid">
            ${points.map((ap) => this._tile(server, ap)).join("")}
          </div>
        </section>`,
      )
      .join("");

    return `
      <section class="server">
        <header>
          <h2>${escapeHtml(server.name)}</h2>
          <div class="badges">
            <span class="badge ${server.connected ? "ok" : "bad"}">
              ${server.connected ? "подключено" : "нет связи"}
            </span>
            <span class="badge muted">${server.access_points.length} точек</span>
            ${
              server.control_enabled
                ? ""
                : `<span class="badge warn" title="Включите «Разрешить управление точками доступа» в настройках интеграции">только чтение</span>`
            }
          </div>
        </header>
        ${sections}
      </section>`;
  }

  _tile(server, ap) {
    const camera = ap.camera_entity_id
      ? `<div class="camera" data-camera="${escapeHtml(ap.camera_entity_id)}"></div>`
      : "";
    const modes = ["normal", "locked", "unlocked"]
      .map(
        (value) =>
          `<option value="${value}">${escapeHtml(MODE_LABELS[value])}</option>`,
      )
      .join("");

    const controls = server.control_enabled
      ? `
        <select class="mode" data-entity="${escapeHtml(ap.entities.mode ?? "")}"
                aria-label="Режим точки доступа">${modes}</select>
        <div class="pass">
          ${
            ap.entities.allow_pass_in
              ? `<button class="pass-btn" data-press="${escapeHtml(ap.entities.allow_pass_in)}">Вход</button>`
              : ""
          }
          ${
            ap.entities.allow_pass_out
              ? `<button class="pass-btn" data-press="${escapeHtml(ap.entities.allow_pass_out)}">Выход</button>`
              : ""
          }
        </div>`
      : `<div class="mode-readonly"></div>`;

    const gear = this._isAdmin
      ? `<button class="gear" title="Привязать камеру"
                 data-edit="${server.entry_id}:${ap.id}">&#9881;</button>`
      : "";

    return `
      <article class="tile" data-ap="${server.entry_id}:${ap.id}"
               data-event-entity="${escapeHtml(ap.entities.event ?? "")}">
        ${camera}
        <div class="tile-head">
          <span class="dot"></span>
          <h4 title="${escapeHtml(ap.name)}">${escapeHtml(ap.name)}</h4>
          ${gear}
        </div>
        <div class="tile-state">
          <span class="link"></span>
          <span class="door"></span>
        </div>
        ${controls}
        ${this._isAdmin ? this._editor(server, ap) : ""}
      </article>`;
  }

  /**
   * The camera binding editor, collapsed until the gear is pressed.
   *
   * Both kinds are offered because access points differ: a camera entity is
   * what actually renders a picture, while a bare RTSP URL is kept for
   * automations and for a future camera platform. The hint says so, so nobody
   * fills in RTSP and waits for video that cannot appear.
   */
  _editor(server, ap) {
    return `
      <div class="editor" hidden>
        <label>
          <span>Камера</span>
          <input class="camera-input" list="sigur-cameras" placeholder="camera.…"
                 value="${escapeHtml(ap.camera_entity_id ?? "")}" />
        </label>
        <label>
          <span>RTSP</span>
          <input class="rtsp-input" placeholder="rtsp://…"
                 value="${escapeHtml(ap.rtsp_url ?? "")}" />
        </label>
        <p class="hint">
          Картинку на плитке даёт camera-сущность. RTSP хранится для
          автоматизаций: браузер не играет его напрямую.
        </p>
        <div class="editor-actions">
          <button class="save" data-save="${server.entry_id}:${ap.id}">Сохранить</button>
          <button class="clear" data-clear="${server.entry_id}:${ap.id}">Очистить</button>
        </div>
      </div>`;
  }

  /** A datalist of every camera entity, so the field can be completed. */
  _cameraOptions() {
    if (!this._isAdmin) return "";
    const options = Object.keys(this._hass.states)
      .filter((id) => id.startsWith("camera."))
      .sort()
      .map((id) => {
        const name = this._hass.states[id].attributes?.friendly_name ?? id;
        return `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`;
      })
      .join("");
    return `<datalist id="sigur-cameras">${options}</datalist>`;
  }

  _toggleEditor(key) {
    const tile = this.shadowRoot.querySelector(`.tile[data-ap="${key}"]`);
    const editor = tile?.querySelector(".editor");
    if (!editor) return;
    const opening = editor.hasAttribute("hidden");
    for (const other of this.shadowRoot.querySelectorAll(".editor")) {
      other.setAttribute("hidden", "");
    }
    if (opening) {
      editor.removeAttribute("hidden");
      editor.querySelector(".camera-input")?.focus();
    }
    this._editing = opening ? key : null;
  }

  async _saveBinding(key, { clear = false } = {}) {
    const [entryId, apId] = key.split(":");
    const tile = this.shadowRoot.querySelector(`.tile[data-ap="${key}"]`);
    const cameraInput = tile?.querySelector(".camera-input");
    const rtspInput = tile?.querySelector(".rtsp-input");
    const camera = clear ? null : cameraInput?.value.trim() || null;
    const rtsp = clear ? null : rtspInput?.value.trim() || null;

    if (camera && !camera.startsWith("camera.")) {
      this._toast("Идентификатор камеры должен начинаться с camera.");
      return;
    }

    try {
      await this._hass.callWS({
        type: "sigur/panel/set_binding",
        entry_id: entryId,
        access_point_id: Number(apId),
        camera_entity_id: camera,
        rtsp_url: rtsp,
      });
    } catch (err) {
      this._toast(err?.message ?? String(err));
      return;
    }

    // Keep the local copy in step so the tile repaints without a round trip.
    const server = this._servers.find((item) => item.entry_id === entryId);
    const ap = server?.access_points.find((item) => item.id === Number(apId));
    if (ap) {
      ap.camera_entity_id = camera;
      ap.rtsp_url = rtsp;
    }
    if (clear && cameraInput && rtspInput) {
      cameraInput.value = "";
      rtspInput.value = "";
    }
    this._toast(clear ? "Привязка удалена" : "Привязка сохранена");
    this._render();
  }

  _bind() {
    const filter = this.shadowRoot.getElementById("filter");
    if (filter) {
      filter.addEventListener("input", (event) => {
        this._filter = event.target.value;
        this._paintStates();
      });
    }
    const problems = this.shadowRoot.getElementById("problems");
    if (problems) {
      problems.addEventListener("change", (event) => {
        this._onlyProblems = event.target.checked;
        this._paintStates();
      });
    }
    for (const select of this.shadowRoot.querySelectorAll("select.mode")) {
      select.addEventListener("change", (event) =>
        this._setMode(event.target.dataset.entity, event.target.value),
      );
    }
    for (const button of this.shadowRoot.querySelectorAll("button.pass-btn")) {
      button.addEventListener("click", (event) =>
        this._press(event.currentTarget.dataset.press),
      );
    }
    for (const gear of this.shadowRoot.querySelectorAll("button.gear")) {
      gear.addEventListener("click", (event) =>
        this._toggleEditor(event.currentTarget.dataset.edit),
      );
    }
    for (const save of this.shadowRoot.querySelectorAll("button.save")) {
      save.addEventListener("click", (event) =>
        this._saveBinding(event.currentTarget.dataset.save),
      );
    }
    for (const clear of this.shadowRoot.querySelectorAll("button.clear")) {
      clear.addEventListener("click", (event) =>
        this._saveBinding(event.currentTarget.dataset.clear, { clear: true }),
      );
    }
    for (const head of this.shadowRoot.querySelectorAll(".tile-head h4")) {
      head.addEventListener("click", (event) =>
        this._openMore(event.target.closest(".tile").dataset.eventEntity),
      );
    }
  }

  /** Repaint the live parts of every tile without rebuilding the DOM. */
  _paintStates() {
    if (!this._loaded || this._error) return;
    for (const server of this._servers) {
      for (const ap of server.access_points) {
        const tile = this.shadowRoot.querySelector(
          `.tile[data-ap="${server.entry_id}:${ap.id}"]`,
        );
        if (!tile) continue;

        const state = this._tileState(ap);
        const visible =
          this._matchesFilter(ap) && (!this._onlyProblems || state.problem);
        tile.classList.toggle("hidden", !visible);
        tile.classList.toggle("offline", !state.online);
        tile.classList.toggle("alert", state.door === "on");

        tile.querySelector(".link").textContent = state.online
          ? "на связи"
          : "нет связи";
        const door = tile.querySelector(".door");
        door.textContent =
          state.door === "on"
            ? "открыта"
            : state.door === "off"
              ? "закрыта"
              : "нет датчика";
        door.className = `door ${state.door === "on" ? "open" : ""}`;

        const select = tile.querySelector("select.mode");
        if (select) {
          select.disabled = !state.mode;
          if (state.mode && select.value !== state.mode) {
            select.value = state.mode;
          }
        }
        const readonly = tile.querySelector(".mode-readonly");
        if (readonly) {
          readonly.textContent = state.mode
            ? MODE_LABELS[state.mode] ?? state.mode
            : "режим неизвестен";
        }

        const camera = tile.querySelector(".camera");
        if (camera) {
          const picture = this._state(camera.dataset.camera)?.attributes
            ?.entity_picture;
          if (picture) {
            camera.style.backgroundImage = `url("${picture}")`;
          }
        }
      }
    }
    this._paintZoneVisibility();
  }

  /** Hide a zone heading once every tile under it is filtered away. */
  _paintZoneVisibility() {
    for (const zone of this.shadowRoot.querySelectorAll("section.zone")) {
      const anyVisible = zone.querySelector(".tile:not(.hidden)") !== null;
      zone.classList.toggle("hidden", !anyVisible);
    }
  }

  _paintFeed() {
    const feed = this.shadowRoot.getElementById("feed");
    if (!feed) return;
    if (!this._feed.length) {
      feed.innerHTML = `<p class="muted">Пока ничего не произошло. События появятся здесь сразу, как только придут.</p>`;
      return;
    }
    feed.innerHTML = this._feed
      .map((event) => {
        const category = event.category ?? "unknown";
        const direction = DIRECTION_LABELS[event.direction] ?? "";
        const who = event.object_name
          ? `<span class="who">${escapeHtml(event.object_name)}</span>`
          : "";
        return `
          <div class="row ${ALARM_CATEGORIES.has(category) ? "alarm" : ""}">
            <span class="time">${escapeHtml(formatTime(event.occurred_at))}</span>
            <span class="what">
              <span class="ap">${escapeHtml(event.access_point_name ?? "—")}</span>
              <span class="cat">${escapeHtml(CATEGORY_LABELS[category] ?? category)}</span>
              ${who}
            </span>
            <span class="dir">${escapeHtml(direction)}</span>
          </div>`;
      })
      .join("");
  }
}

SigurPanel.styles = `
  :host {
    display: block;
    height: 100%;
    overflow: auto;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
  }
  .center { padding: 48px 24px; text-align: center; }
  .muted { color: var(--secondary-text-color); }
  .error { color: var(--error-color, #db4437); }
  .hidden { display: none !important; }

  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 340px;
    gap: 16px;
    padding: 16px;
    align-items: start;
  }
  @media (max-width: 1100px) {
    .layout { grid-template-columns: minmax(0, 1fr); }
    aside { order: -1; }
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    margin-bottom: 16px;
  }
  #filter {
    flex: 1 1 220px;
    min-width: 0;
    padding: 10px 14px;
    border-radius: 999px;
    border: 1px solid var(--divider-color);
    background: var(--card-background-color);
    color: inherit;
    font: inherit;
  }
  #filter:focus { outline: 2px solid var(--primary-color); outline-offset: -1px; }
  .toggle { display: flex; align-items: center; gap: 8px; cursor: pointer; }

  .server { margin-bottom: 28px; }
  .server > header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
  }
  h2 { font-size: 20px; font-weight: 500; margin: 0; }
  h3 {
    font-size: 13px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--secondary-text-color);
    margin: 20px 0 10px;
  }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; }
  .badge {
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 999px;
    background: var(--secondary-background-color);
    color: var(--secondary-text-color);
    white-space: nowrap;
  }
  .badge.ok { background: var(--success-color, #43a047); color: #fff; }
  .badge.bad { background: var(--error-color, #db4437); color: #fff; }
  .badge.warn { background: var(--warning-color, #ffa600); color: #000; }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }

  .tile {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
    border: 1px solid transparent;
    transition: border-color .15s ease;
  }
  .tile.offline { opacity: .55; }
  .tile.alert { border-color: var(--warning-color, #ffa600); }

  .camera {
    height: 96px;
    margin: -14px -14px 2px;
    border-radius: var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px) 0 0;
    background: var(--secondary-background-color) center/cover no-repeat;
  }

  .tile-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .tile-head h4 {
    margin: 0;
    font-size: 15px;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
  }
  .tile-head h4:hover { color: var(--primary-color); }
  .dot {
    flex: none;
    width: 9px; height: 9px;
    border-radius: 50%;
    background: var(--success-color, #43a047);
  }
  .tile.offline .dot { background: var(--disabled-text-color, #9e9e9e); }

  .tile-state {
    display: flex;
    gap: 10px;
    font-size: 13px;
    color: var(--secondary-text-color);
  }
  .door.open { color: var(--warning-color, #ffa600); font-weight: 500; }

  select.mode {
    width: 100%;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
  }
  select.mode:disabled { opacity: .5; }
  .mode-readonly { font-size: 13px; color: var(--secondary-text-color); }

  .pass { display: flex; gap: 8px; }
  .pass-btn {
    flex: 1;
    padding: 8px;
    border: none;
    border-radius: 8px;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
    font: inherit;
    cursor: pointer;
  }
  .pass-btn:hover { filter: brightness(1.08); }
  .pass-btn:active { filter: brightness(.92); }

  .gear {
    margin-left: auto;
    flex: none;
    border: none;
    background: none;
    color: var(--secondary-text-color);
    font-size: 16px;
    line-height: 1;
    padding: 2px 4px;
    cursor: pointer;
    border-radius: 6px;
  }
  .gear:hover { color: var(--primary-color); background: var(--secondary-background-color); }

  .editor {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 10px;
    margin-top: 2px;
    border-top: 1px solid var(--divider-color);
  }
  .editor[hidden] { display: none; }
  .editor label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
  .editor label span { color: var(--secondary-text-color); }
  .editor input {
    padding: 7px 9px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
    font-size: 13px;
    min-width: 0;
  }
  .editor input:focus { outline: 2px solid var(--primary-color); outline-offset: -1px; }
  .editor .hint { margin: 0; font-size: 11px; color: var(--secondary-text-color); }
  .editor-actions { display: flex; gap: 8px; }
  .editor-actions button {
    flex: 1;
    padding: 7px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .editor-actions .save {
    border-color: transparent;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  .editor-actions button:hover { filter: brightness(1.06); }

  aside {
    position: sticky;
    top: 16px;
    padding: 14px;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
    max-height: calc(100vh - 96px);
    display: flex;
    flex-direction: column;
  }
  aside h2 { margin-bottom: 10px; }
  .feed { overflow: auto; }
  .feed .row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 10px;
    align-items: baseline;
    padding: 7px 0;
    border-bottom: 1px solid var(--divider-color);
    font-size: 13px;
  }
  .feed .row:last-child { border-bottom: none; }
  .feed .time { color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .feed .ap { font-weight: 500; margin-right: 6px; }
  .feed .cat { color: var(--secondary-text-color); }
  .feed .who { display: block; color: var(--secondary-text-color); }
  .feed .dir { color: var(--secondary-text-color); white-space: nowrap; }
  .feed .row.alarm .cat { color: var(--error-color, #db4437); font-weight: 500; }
`;

// The module can be evaluated more than once in a single page session - a
// changed cache-busting version, or Home Assistant loading the panel again
// after a reconnect. Defining the element twice throws, and the panel then
// fails to load at all until a hard refresh.
if (!customElements.get("sigur-panel")) {
  customElements.define("sigur-panel", SigurPanel);
}
