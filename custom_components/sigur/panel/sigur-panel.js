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
 * Events and "the structure changed" both arrive through the integration's own
 * `sigur/panel/subscribe` stream, which already leaves out whatever this user
 * is not meant to see - and which, unlike the bus, a non-admin may subscribe to.
 */

/**
 * Where the chosen preset is remembered: Home Assistant's per-user frontend
 * data, so it follows the user from the phone to the desktop.
 */
const USER_DATA_KEY = "sigur-panel";
/** Coalesce bursts of registry changes into one reload. */
const RELOAD_DEBOUNCE_MS = 400;
/**
 * How often a visible tile refetches its camera frame.
 *
 * Home Assistant only rotates the token inside `entity_picture` every five
 * minutes, so without this the picture on a tile can be that stale. Only tiles
 * actually on screen are refreshed: an installation with a hundred access
 * points would otherwise pull a hundred JPEGs from cameras on every tick.
 */
const CAMERA_REFRESH_MS = 30000;
/** How many events the feed shows. Older ones live in the recorder. */
const FEED_LIMIT = 60;
/**
 * How many events are kept behind the feed.
 *
 * More than are shown, so that narrowing the access point selection still
 * leaves something to look at instead of only what happened after the click.
 */
const FEED_BUFFER = 300;
/** Where the chosen access points are remembered, per browser. */
const SELECTION_STORAGE_KEY = "sigur-panel-selection";

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

/**
 * How a point may be passed. OIF does not report this - it knows only which
 * zone lies on each side - so it is declared by the user, and it decides which
 * one-shot pass buttons a tile offers.
 */
const DIRECTION_MODES = {
  both: "В обе стороны",
  in: "Только вход",
  out: "Только выход",
};

const offersDirection = (mode, direction) =>
  !mode || mode === "both" || mode === direction;

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

/** The key an access point goes by across servers: `entry_id:ap_id`. */
const apKey = (entryId, apId) => `${entryId}:${apId}`;

/**
 * The remembered selection: a set of access point keys, or null for "all".
 *
 * Storage can be missing or throw (private windows, blocked site data); the
 * panel then simply starts with everything shown.
 */
const loadSelection = () => {
  try {
    const raw = window.localStorage.getItem(SELECTION_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? new Set(parsed.map(String)) : null;
  } catch {
    return null;
  }
};

const saveSelection = (selection) => {
  try {
    if (selection) {
      window.localStorage.setItem(
        SELECTION_STORAGE_KEY,
        JSON.stringify([...selection]),
      );
    } else {
      window.localStorage.removeItem(SELECTION_STORAGE_KEY);
    }
  } catch {
    // Not remembered across reloads, which is all that is lost.
  }
};

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
    this._selection = loadSelection();
    this._shown = this._selection;
    this._presets = [];
    this._restricted = false;
    this._activePreset = null;
    this._basePreset = null;
    this._users = null;
    this._usersRequest = null;
    this._confirmDelete = false;
    this._pickerOpen = false;
    this._isAdmin = false;
    this._editing = null;
    this._subscriptions = [];
    this._reloadTimer = null;
    this._cameraTimer = null;
    this._visible = new Set();
    this._observer = null;
  }

  /** Home Assistant assigns this on every state change. */
  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this._isAdmin = hass.user?.is_admin ?? false;
    if (first) {
      this._loadUserData().then(() => this._load());
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
    clearTimeout(this._reloadTimer);
    clearInterval(this._cameraTimer);
    this._observer?.disconnect();
    for (const pending of this._subscriptions) {
      pending.then((off) => off()).catch(() => undefined);
    }
    this._subscriptions = [];
  }

  async _load() {
    try {
      const result = await this._hass.callWS({ type: "sigur/panel/data" });
      this._servers = result.servers ?? [];
      this._presets = result.presets ?? [];
      this._restricted = Boolean(result.restricted);
      this._error = null;
    } catch (err) {
      this._error = err?.message ?? String(err);
    }
    if (
      this._activePreset &&
      !this._presets.some((preset) => preset.id === this._activePreset)
    ) {
      // Deleted, or no longer assigned to this user.
      this._activePreset = null;
    }
    if (!this._presets.some((preset) => preset.id === this._basePreset)) {
      this._basePreset = null;
    }
    this._updateShown();
    this._loaded = true;
    this._render();
  }

  /** Read which preset this user had chosen, wherever they chose it. */
  async _loadUserData() {
    try {
      const result = await this._hass.callWS({
        type: "frontend/get_user_data",
        key: USER_DATA_KEY,
      });
      this._activePreset = result?.value?.preset ?? null;
    } catch {
      // Starts on "all", which is all that is lost.
    }
  }

  _saveUserData() {
    this._hass
      .callWS({
        type: "frontend/set_user_data",
        key: USER_DATA_KEY,
        value: { preset: this._activePreset },
      })
      .catch(() => undefined);
  }

  _subscribe() {
    this._subscriptions.push(
      this._hass.connection.subscribeMessage(
        (message) => {
          if (message.type === "changed") {
            // The structure is cached, so anything that invalidates it has to
            // say so: a renamed camera, an edited binding, a changed preset.
            this._scheduleReload();
            return;
          }
          const event = message.event;
          this._feed = [event, ...this._feed].slice(0, FEED_BUFFER);
          this._paintFeed();
          // Something happened at this door, which is exactly when its camera
          // is worth looking at. Refetch that frame now rather than waiting.
          const { server_entry_id: entry, access_point_id: ap } = event;
          if (entry != null && ap != null) {
            this._refreshCamera(`${entry}:${ap}`);
          }
        },
        { type: "sigur/panel/subscribe" },
      ),
    );

    // A renamed camera entity is the case that bites: the tile would go on
    // requesting a picture from an entity id that no longer resolves. This
    // event is one Home Assistant lets every user subscribe to.
    this._subscriptions.push(
      this._hass.connection.subscribeEvents(
        () => this._scheduleReload(),
        "entity_registry_updated",
      ),
    );
  }

  /** Refetch one tile's camera frame, bypassing the browser cache. */
  _refreshCamera(key) {
    const camera = this.shadowRoot
      .querySelector(`.tile[data-ap="${key}"]`)
      ?.querySelector(".camera");
    const base = camera?.dataset.picture;
    if (!base) return;
    const separator = base.includes("?") ? "&" : "?";
    camera.style.backgroundImage = `url("${base}${separator}_=${Date.now()}")`;
  }

  /** Refresh only the tiles the user can actually see. */
  _startCameraRefresh() {
    clearInterval(this._cameraTimer);
    this._observer?.disconnect();

    this._visible = new Set();
    this._observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const key = entry.target.dataset.ap;
          if (entry.isIntersecting) this._visible.add(key);
          else this._visible.delete(key);
        }
      },
      { root: null, rootMargin: "200px" },
    );
    for (const tile of this.shadowRoot.querySelectorAll(".tile")) {
      if (tile.querySelector(".camera")) this._observer.observe(tile);
    }

    this._cameraTimer = setInterval(() => {
      for (const key of this._visible) this._refreshCamera(key);
    }, CAMERA_REFRESH_MS);
  }

  _scheduleReload() {
    clearTimeout(this._reloadTimer);
    this._reloadTimer = setTimeout(() => this._load(), RELOAD_DEBOUNCE_MS);
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

  /** Whether the access point is among the chosen ones. */
  _isSelected(entryId, apId) {
    return !this._shown || this._shown.has(apKey(entryId, apId));
  }

  /** The preset being looked through, if any. */
  _preset() {
    return this._presets.find((preset) => preset.id === this._activePreset) ?? null;
  }

  /**
   * The preset the editor works on: the one looked through, or the one the
   * picker's hand-made choice was started from, so it can be saved back.
   */
  _editorPreset() {
    const id = this._activePreset ?? this._basePreset;
    return this._presets.find((preset) => preset.id === id) ?? null;
  }

  /**
   * The access points of a preset, as keys.
   *
   * Zones are resolved against the current structure, which is how a point
   * added to a zone later turns up in the preset by itself.
   */
  _presetKeys(preset) {
    const points = new Set(preset.access_points ?? []);
    const zones = new Set(preset.zones ?? []);
    const keys = new Set();
    for (const server of this._servers) {
      for (const ap of server.access_points) {
        const key = apKey(server.entry_id, ap.id);
        const zone = ap.zone_a != null ? apKey(server.entry_id, ap.zone_a) : null;
        if (points.has(key) || (zone && zones.has(zone))) keys.add(key);
      }
    }
    return keys;
  }

  /** Work out what is shown: the active preset, else the picker's choice. */
  _updateShown() {
    const preset = this._preset();
    this._shown = preset ? this._presetKeys(preset) : this._selection;
  }

  /** Look through a preset, or through everything with `null`. */
  _choosePreset(presetId) {
    this._activePreset = presetId;
    this._basePreset = null;
    if (!presetId) {
      this._selection = null;
      saveSelection(null);
    }
    this._confirmDelete = false;
    this._saveUserData();
    this._updateShown();
    this._render();
  }

  /**
   * Whether an event belongs to the chosen access points.
   *
   * An event with no access point - the server itself lost its link - is
   * shown as long as any point of that server is chosen.
   */
  _eventSelected(event) {
    if (!this._shown) return true;
    const entryId = event.server_entry_id;
    if (event.access_point_id != null) {
      return this._isSelected(entryId, event.access_point_id);
    }
    const server = this._servers.find((item) => item.entry_id === entryId);
    return (server?.access_points ?? []).some((ap) =>
      this._isSelected(entryId, ap.id),
    );
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
    this._paintPicker();
    this._paintPresetBar();
    this._paintStates();
    this._paintFeed();
    this._startCameraRefresh();
    if (this._pickerOpen) this._paintPresetUsers();
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
      return this._restricted
        ? `<div class="center muted">В назначенных вам пресетах нет ни одной точки доступа.</div>`
        : `<div class="center muted">Нет настроенных систем Sigur.</div>`;
    }
    return `
      <div class="layout">
        <main>
          ${this._presetBar()}
          <div class="toolbar">
            <input id="filter" type="search" placeholder="Поиск точки доступа"
                   value="${escapeHtml(this._filter)}" />
            <label class="toggle">
              <input id="problems" type="checkbox" ${this._onlyProblems ? "checked" : ""} />
              <span>Только проблемные</span>
            </label>
            <button id="picker-toggle" aria-expanded="${this._pickerOpen}"
                    title="Выбрать, какие точки доступа показывать">${escapeHtml(
                      this._selectionLabel(),
                    )}</button>
            <button id="reload" title="Перечитать список точек доступа">Обновить</button>
          </div>
          ${this._picker()}
          <p id="nothing-selected" class="center muted hidden">
            Не выбрано ни одной точки доступа.
          </p>
          ${this._servers.map((server) => this._server(server)).join("")}
          ${this._cameraOptions()}
        </main>
        <aside>
          <h2>События</h2>
          <div id="feed" class="feed"></div>
        </aside>
      </div>`;
  }

  /** Group a server's access points under the zone on their A side. */
  _zonesOf(server) {
    const zones = new Map();
    for (const ap of server.access_points) {
      const key = ap.zone_a_name || (ap.zone_a != null ? `Зона ${ap.zone_a}` : "Без зоны");
      if (!zones.has(key)) zones.set(key, []);
      zones.get(key).push(ap);
    }
    return zones;
  }

  _server(server) {
    const sections = [...this._zonesOf(server).entries()]
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
      <section class="server" data-entry="${escapeHtml(server.entry_id)}">
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

  _allKeys() {
    return this._servers.flatMap((server) =>
      server.access_points.map((ap) => apKey(server.entry_id, ap.id)),
    );
  }

  _currentKeys() {
    return new Set(this._shown ?? this._allKeys());
  }

  _selectionLabel() {
    if (!this._shown) return "Точки: все";
    const all = this._allKeys();
    const chosen = all.filter((key) => this._shown.has(key)).length;
    return `Точки: ${chosen} из ${all.length}`;
  }

  /**
   * One button per preset, plus "all".
   *
   * None of them is lit while the picker holds a hand-made choice. For a user
   * limited by presets, "all" means all of theirs - the server never sent the
   * rest.
   */
  _presetBar() {
    if (!this._presets.length) return "";
    const chips = [
      [null, this._restricted ? "Все мои" : "Все"],
      ...this._presets.map((preset) => [preset.id, preset.name]),
    ]
      .map(
        ([id, name]) =>
          `<button class="chip" data-preset="${escapeHtml(id ?? "")}">${escapeHtml(
            name,
          )}</button>`,
      )
      .join("");
    return `<nav class="presets" aria-label="Пресеты">${chips}</nav>`;
  }

  _paintPresetBar() {
    for (const chip of this.shadowRoot.querySelectorAll(".chip[data-preset]")) {
      const id = chip.dataset.preset || null;
      const active =
        id === null ? !this._activePreset && !this._selection : id === this._activePreset;
      chip.classList.toggle("active", active);
      chip.setAttribute("aria-pressed", String(active));
    }
  }

  /**
   * The current choice as a preset's members.
   *
   * A zone that is chosen whole is stored as the zone, so points added to it
   * later join the preset by themselves; anything else as single points.
   */
  _presetMembers() {
    const shown = this._shown ?? new Set(this._allKeys());
    const accessPoints = [];
    const zones = [];
    for (const server of this._servers) {
      const byZone = new Map();
      for (const ap of server.access_points) {
        const zone = ap.zone_a ?? `ap:${ap.id}`;
        if (!byZone.has(zone)) byZone.set(zone, []);
        byZone.get(zone).push(ap);
      }
      for (const [zone, points] of byZone) {
        const keys = points.map((ap) => apKey(server.entry_id, ap.id));
        if (typeof zone === "number" && keys.every((key) => shown.has(key))) {
          zones.push(apKey(server.entry_id, zone));
        } else {
          accessPoints.push(...keys.filter((key) => shown.has(key)));
        }
      }
    }
    return { access_points: accessPoints, zones };
  }

  /**
   * The preset editor at the foot of the picker, for administrators.
   *
   * It saves whatever the picker currently shows, so making a preset is: tick
   * the points, name it, choose who it is for.
   */
  _presetEditor() {
    if (!this._isAdmin) return "";
    const preset = this._editorPreset();
    const quoted = preset ? `«${escapeHtml(preset.name)}»` : "";
    return `
      <div class="preset-editor">
        <h4>Пресет</h4>
        <input id="preset-name" placeholder="Название пресета"
               value="${escapeHtml(preset?.name ?? "")}" />
        <div class="preset-users" id="preset-users">
          <span class="muted">Загрузка пользователей…</span>
        </div>
        <p class="hint">
          Пользователь, отмеченный хотя бы в одном пресете, видит в панели
          только точки своих пресетов и их события. Пресет без отмеченных
          пользователей — просто общий вид, он никого не ограничивает.
          Администраторы видят всё всегда. Зона, отмеченная целиком,
          сохраняется как зона: новые точки в ней попадут в пресет сами.
        </p>
        <div class="editor-actions">
          <button id="preset-save-new">Сохранить как новый</button>
          ${
            preset
              ? `<button id="preset-update" class="primary">Сохранить в ${quoted}</button>
                 <button id="preset-delete">${
                   this._confirmDelete ? "Точно удалить?" : `Удалить ${quoted}`
                 }</button>`
              : ""
          }
        </div>
      </div>`;
  }

  /** Fill the editor's user list; fetched once, when first needed. */
  async _paintPresetUsers() {
    const container = this.shadowRoot.getElementById("preset-users");
    if (!container) return;
    if (this._users === null) {
      // Opening the picker and a re-render can both ask; fetch only once.
      this._usersRequest ??= this._hass.callWS({ type: "config/auth/list" });
      try {
        const users = await this._usersRequest;
        // Administrators see everything whatever a preset says, and system
        // users never open a panel, so neither is worth offering.
        this._users = users
          .filter(
            (user) =>
              !user.system_generated &&
              user.is_active &&
              !(user.group_ids ?? []).includes("system-admin"),
          )
          .sort((a, b) => String(a.name).localeCompare(String(b.name)));
      } catch (err) {
        this._usersRequest = null;
        container.innerHTML = `<span class="error">${escapeHtml(
          err?.message ?? String(err),
        )}</span>`;
        return;
      }
    }
    // The picker may have been rebuilt while the list was on its way.
    const current = this.shadowRoot.getElementById("preset-users");
    if (current !== container) return;
    const assigned = new Set(this._editorPreset()?.users ?? []);
    container.innerHTML = this._users.length
      ? `<span class="muted">Назначить пользователям:</span>${this._users
          .map(
            (user) => `
          <label>
            <input type="checkbox" data-user="${escapeHtml(user.id)}"
                   ${assigned.has(user.id) ? "checked" : ""} />
            <span>${escapeHtml(user.name ?? user.id)}</span>
          </label>`,
          )
          .join("")}`
      : `<span class="muted">Пользователей без прав администратора нет — назначать некому.</span>`;
  }

  async _savePreset({ update }) {
    const root = this.shadowRoot;
    const name = root.getElementById("preset-name")?.value.trim() ?? "";
    if (!name) {
      this._toast("Дайте пресету название");
      return;
    }
    const users = [...root.querySelectorAll("input[data-user]:checked")].map(
      (box) => box.dataset.user,
    );
    const preset = update ? this._editorPreset() : null;
    let saved;
    try {
      saved = await this._hass.callWS({
        type: "sigur/presets/save",
        ...(preset ? { preset_id: preset.id } : {}),
        name,
        users: this._users === null && preset ? preset.users : users,
        ...this._presetMembers(),
      });
    } catch (err) {
      this._toast(err?.message ?? String(err));
      return;
    }
    this._activePreset = saved.id;
    this._basePreset = null;
    this._selection = null;
    saveSelection(null);
    this._saveUserData();
    this._toast(preset ? "Пресет сохранён" : "Пресет создан");
    await this._load();
  }

  async _deletePreset() {
    const preset = this._editorPreset();
    if (!preset) return;
    if (!this._confirmDelete) {
      this._confirmDelete = true;
      const button = this.shadowRoot.getElementById("preset-delete");
      if (button) button.textContent = "Точно удалить?";
      return;
    }
    this._confirmDelete = false;
    try {
      await this._hass.callWS({
        type: "sigur/presets/delete",
        preset_id: preset.id,
      });
    } catch (err) {
      this._toast(err?.message ?? String(err));
      return;
    }
    this._toast("Пресет удалён");
    this._choosePreset(null);
    await this._load();
  }

  /**
   * The access point picker: server, then zone, then point, each with a box.
   *
   * A zone's box ticks every point under it. The panel shows only what is
   * chosen here, the event feed included.
   */
  _picker() {
    const servers = this._servers
      .map((server) => {
        const zones = [...this._zonesOf(server).entries()]
          .map(
            ([zone, points]) => `
            <div class="pick-zone">
              <label class="pick-group">
                <input type="checkbox" data-pick-group="${escapeHtml(
                  points.map((ap) => apKey(server.entry_id, ap.id)).join(" "),
                )}" />
                <span>${escapeHtml(zone)}</span>
              </label>
              ${points
                .map(
                  (ap) => `
                <label class="pick-ap">
                  <input type="checkbox" data-pick="${escapeHtml(
                    apKey(server.entry_id, ap.id),
                  )}" />
                  <span>${escapeHtml(ap.name)}</span>
                </label>`,
                )
                .join("")}
            </div>`,
          )
          .join("");
        return `
          <div class="pick-server">
            ${this._servers.length > 1 ? `<h4>${escapeHtml(server.name)}</h4>` : ""}
            ${zones}
          </div>`;
      })
      .join("");
    return `
      <div id="picker" class="picker" ${this._pickerOpen ? "" : "hidden"}>
        <div class="picker-actions">
          <button data-pick-all>Все</button>
          <button data-pick-none>Ничего</button>
          <button data-pick-close class="primary">Готово</button>
        </div>
        <div class="picker-list">${servers}</div>
        ${this._presetEditor()}
      </div>`;
  }

  _togglePicker(open = !this._pickerOpen) {
    this._pickerOpen = open;
    this.shadowRoot.getElementById("picker")?.toggleAttribute("hidden", !open);
    this.shadowRoot
      .getElementById("picker-toggle")
      ?.setAttribute("aria-expanded", String(open));
    if (open) this._paintPresetUsers();
  }

  /**
   * Apply a hand-made selection: remember it, then repaint tiles and the feed.
   *
   * Editing the picker while looking through a preset starts from that
   * preset's points and leaves the preset itself untouched.
   */
  _setSelection(keys) {
    const all = this._allKeys();
    // Choosing every point is stored as "all", so that access points added
    // later show up instead of being quietly left out.
    const everything = all.every((key) => keys.has(key));
    this._selection = everything ? null : keys;
    saveSelection(this._selection);
    if (this._activePreset) {
      this._basePreset = this._activePreset;
      this._activePreset = null;
      this._saveUserData();
    }
    this._updateShown();
    this._paintPicker();
    this._paintPresetBar();
    this._paintStates();
    this._paintFeed();
  }

  /** Bring the picker's boxes and the toolbar label in step with the selection. */
  _paintPicker() {
    const root = this.shadowRoot;
    const chosen = (key) => !this._shown || this._shown.has(key);
    for (const box of root.querySelectorAll("input[data-pick]")) {
      box.checked = chosen(box.dataset.pick);
    }
    for (const box of root.querySelectorAll("input[data-pick-group]")) {
      const keys = box.dataset.pickGroup.split(" ");
      const count = keys.filter(chosen).length;
      box.checked = count === keys.length;
      box.indeterminate = count > 0 && count < keys.length;
    }
    const toggle = root.getElementById("picker-toggle");
    if (toggle) toggle.textContent = this._selectionLabel();
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

    const passes = [
      ["in", "Вход", ap.entities.allow_pass_in],
      ["out", "Выход", ap.entities.allow_pass_out],
    ]
      .filter(
        ([direction, , entity]) =>
          entity && offersDirection(ap.direction_mode, direction),
      )
      .map(
        ([, label, entity]) =>
          `<button class="pass-btn" data-press="${escapeHtml(entity)}">${label}</button>`,
      )
      .join("");

    const controls = server.control_enabled
      ? `
        <select class="mode" data-entity="${escapeHtml(ap.entities.mode ?? "")}"
                aria-label="Режим точки доступа">${modes}</select>
        ${passes ? `<div class="pass">${passes}</div>` : ""}`
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
        <label>
          <span>Направление прохода</span>
          <select class="direction-input">
            ${Object.entries(DIRECTION_MODES)
              .map(
                ([value, label]) =>
                  `<option value="${value}"${
                    (ap.direction_mode ?? "both") === value ? " selected" : ""
                  }>${escapeHtml(label)}</option>`,
              )
              .join("")}
          </select>
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
    const directionInput = tile?.querySelector(".direction-input");
    const camera = clear ? null : cameraInput?.value.trim() || null;
    const rtsp = clear ? null : rtspInput?.value.trim() || null;
    const direction = clear ? "both" : directionInput?.value || "both";

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
        direction_mode: direction,
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
      ap.direction_mode = direction;
    }
    if (clear && cameraInput && rtspInput) {
      cameraInput.value = "";
      rtspInput.value = "";
      if (directionInput) directionInput.value = "both";
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
    const reload = this.shadowRoot.getElementById("reload");
    if (reload) {
      reload.addEventListener("click", () => this._load());
    }
    const problems = this.shadowRoot.getElementById("problems");
    if (problems) {
      problems.addEventListener("change", (event) => {
        this._onlyProblems = event.target.checked;
        this._paintStates();
      });
    }
    this._bindPicker();
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

  _bindPicker() {
    const root = this.shadowRoot;
    for (const chip of root.querySelectorAll(".chip[data-preset]")) {
      chip.addEventListener("click", (event) =>
        this._choosePreset(event.currentTarget.dataset.preset || null),
      );
    }
    root
      .getElementById("preset-save-new")
      ?.addEventListener("click", () => this._savePreset({ update: false }));
    root
      .getElementById("preset-update")
      ?.addEventListener("click", () => this._savePreset({ update: true }));
    root
      .getElementById("preset-delete")
      ?.addEventListener("click", () => this._deletePreset());
    root
      .getElementById("picker-toggle")
      ?.addEventListener("click", () => this._togglePicker());
    root
      .querySelector("[data-pick-close]")
      ?.addEventListener("click", () => this._togglePicker(false));
    root
      .querySelector("[data-pick-all]")
      ?.addEventListener("click", () => this._setSelection(new Set(this._allKeys())));
    root
      .querySelector("[data-pick-none]")
      ?.addEventListener("click", () => this._setSelection(new Set()));
    for (const box of root.querySelectorAll("input[data-pick]")) {
      box.addEventListener("change", (event) => {
        const keys = this._currentKeys();
        if (event.target.checked) keys.add(event.target.dataset.pick);
        else keys.delete(event.target.dataset.pick);
        this._setSelection(keys);
      });
    }
    for (const box of root.querySelectorAll("input[data-pick-group]")) {
      box.addEventListener("change", (event) => {
        const keys = this._currentKeys();
        for (const key of event.target.dataset.pickGroup.split(" ")) {
          if (event.target.checked) keys.add(key);
          else keys.delete(key);
        }
        this._setSelection(keys);
      });
    }
    root.getElementById("picker")?.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this._togglePicker(false);
    });
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
          this._isSelected(server.entry_id, ap.id) &&
          this._matchesFilter(ap) &&
          (!this._onlyProblems || state.problem);
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
          const bound = this._state(camera.dataset.camera);
          const picture = bound?.attributes?.entity_picture;
          if (picture) {
            // Only repaint when the underlying URL really changed, so a state
            // update does not throw away a freshly refetched frame.
            if (camera.dataset.picture !== picture) {
              camera.dataset.picture = picture;
              camera.style.backgroundImage = `url("${picture}")`;
            }
            camera.classList.remove("missing");
            camera.textContent = "";
          } else {
            delete camera.dataset.picture;
            // The entity was renamed or removed. Say so rather than leaving
            // the previous frame on screen as if it were current.
            camera.style.backgroundImage = "";
            camera.classList.add("missing");
            camera.textContent = bound ? "нет кадра" : "камера недоступна";
          }
        }
      }
    }
    this._paintZoneVisibility();
    this._paintServerVisibility();
  }

  /**
   * Hide a server none of whose points are chosen.
   *
   * Only the selection counts here, not the search or the problems toggle: a
   * server whose doors are all fine should still show its connection badge.
   */
  _paintServerVisibility() {
    let anyChosen = false;
    for (const section of this.shadowRoot.querySelectorAll("section.server")) {
      const entryId = section.dataset.entry;
      const server = this._servers.find((item) => item.entry_id === entryId);
      const chosen = (server?.access_points ?? []).some((ap) =>
        this._isSelected(entryId, ap.id),
      );
      section.classList.toggle("hidden", !chosen);
      anyChosen ||= chosen;
    }
    this.shadowRoot
      .getElementById("nothing-selected")
      ?.classList.toggle("hidden", anyChosen);
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
    const events = this._feed
      .filter((event) => this._eventSelected(event))
      .slice(0, FEED_LIMIT);
    if (!events.length) {
      feed.innerHTML = this._feed.length
        ? `<p class="muted">По выбранным точкам доступа событий пока не было.</p>`
        : `<p class="muted">Пока ничего не произошло. События появятся здесь сразу, как только придут.</p>`;
      return;
    }
    feed.innerHTML = events
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

  .camera.missing {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    color: var(--secondary-text-color);
  }
  #reload,
  #picker-toggle {
    padding: 8px 14px;
    border-radius: 999px;
    border: 1px solid var(--divider-color);
    background: var(--card-background-color);
    color: inherit;
    font: inherit;
    cursor: pointer;
  }
  #reload:hover,
  #picker-toggle:hover { border-color: var(--primary-color); color: var(--primary-color); }
  #picker-toggle[aria-expanded="true"] { border-color: var(--primary-color); }

  .picker {
    margin: -4px 0 16px;
    padding: 12px 14px;
    border-radius: var(--ha-card-border-radius, 12px);
    background: var(--card-background-color);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
  }
  .picker[hidden] { display: none; }
  .picker-actions { display: flex; gap: 8px; margin-bottom: 10px; }
  .picker-actions button {
    padding: 6px 12px;
    border-radius: 999px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .picker-actions .primary {
    margin-left: auto;
    border-color: transparent;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  .picker-list {
    max-height: 50vh;
    overflow: auto;
    columns: 240px;
    column-gap: 20px;
  }
  .pick-server h4 { margin: 4px 0 6px; font-size: 14px; font-weight: 500; }
  .pick-zone { break-inside: avoid; margin-bottom: 10px; }
  .pick-zone label {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
    font-size: 13px;
    cursor: pointer;
  }
  .pick-group { font-weight: 500; }
  .pick-zone .pick-ap { padding-left: 22px; }
  .pick-zone label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .presets { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .chip {
    padding: 6px 14px;
    border-radius: 999px;
    border: 1px solid var(--divider-color);
    background: var(--card-background-color);
    color: inherit;
    font: inherit;
    font-size: 14px;
    cursor: pointer;
  }
  .chip:hover { border-color: var(--primary-color); }
  .chip.active {
    border-color: transparent;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }

  .preset-editor {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--divider-color);
  }
  .preset-editor h4 { margin: 0; font-size: 14px; font-weight: 500; }
  #preset-name {
    max-width: 360px;
    padding: 7px 9px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
    font-size: 13px;
  }
  #preset-name:focus { outline: 2px solid var(--primary-color); outline-offset: -1px; }
  .preset-users { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 13px; }
  .preset-users label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .preset-editor .hint { margin: 0; font-size: 11px; color: var(--secondary-text-color); }
  .preset-editor .editor-actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .preset-editor .editor-actions button {
    flex: 0 1 auto;
    padding: 7px 12px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .preset-editor .editor-actions .primary {
    border-color: transparent;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }

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
  .editor select {
    padding: 7px 9px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: inherit;
    font: inherit;
    font-size: 13px;
  }
  .editor input:focus,
  .editor select:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: -1px;
  }
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
