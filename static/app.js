// app.js
// Janome JP4 Control Panel Frontend Logic

document.addEventListener('DOMContentLoaded', () => {
    const socket = io();

    // DOM Elements - Status & Header
    const realtimeClock = document.getElementById('realtime-clock');
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');

    // DOM Elements - Telemetry Cards
    const elPolled = document.getElementById('telemetry-polled');
    const elEncoder = document.getElementById('telemetry-encoder');
    const elTeaching = document.getElementById('telemetry-teaching');
    const elLoad = document.getElementById('telemetry-load');
    const elDb = document.getElementById('telemetry-db');
    const MAX_LOG_LINES = 1000;

    // DOM Elements - Form Fields
    const configForm = document.getElementById('config-form');
    const serialPortInput = document.getElementById('SERIAL_PORT');
    const btnScanSerial = document.getElementById('btn-scan-serial');
    const scannedPortsMenu = document.getElementById('scanned-ports-menu');
    const serialBaudrate = document.getElementById('SERIAL_BAUDRATE');
    const serialTimeout = document.getElementById('SERIAL_TIMEOUT');
    const acqInterval = document.getElementById('ACQ_INTERVAL');

    const dbTypeSelect = document.getElementById('DB_TYPE');
    const dbNameInput = document.getElementById('DB_NAME');
    const dbHostInput = document.getElementById('DB_HOST');
    const dbPortInput = document.getElementById('DB_PORT');
    const dbUserInput = document.getElementById('DB_USER');
    const dbPasswordInput = document.getElementById('DB_PASSWORD');
    const dbTableInput = document.getElementById('DB_TABLE');

    const sqlitePathInput = document.getElementById('SQLITE_PATH');

    const influxFields = {
        url: document.getElementById('INFLUX_URL'),
        version: document.getElementById('INFLUX_VERSION'),
        token: document.getElementById('INFLUX_TOKEN'),
        org: document.getElementById('INFLUX_ORG'),
        bucket: document.getElementById('INFLUX_BUCKET'),
        user: document.getElementById('INFLUX_USER'),
        password: document.getElementById('INFLUX_PASSWORD'),
        measurement: document.getElementById('INFLUX_MEASUREMENT'),
        retention: document.getElementById('INFLUX_RETENTION_DAYS')
    };
    const influxGroup = document.getElementById('influx-config-group');

    const postgresGroup = document.getElementById('postgres-config-group');
    const sqliteGroup = document.getElementById('sqlite-config-group');

    // Action & Test Buttons
    const btnTestSerial = document.getElementById('btn-test-serial');
    const btnTestDb = document.getElementById('btn-test-db');
    const btnStartReal = document.getElementById('btn-start-real');
    const btnStartMock = document.getElementById('btn-start-mock');
    const btnStop = document.getElementById('btn-stop');
    const btnClearLog = document.getElementById('btn-clear-log');
    const terminalBody = document.getElementById('terminal-body');

    // Realtime Clock Update
    function updateClock() {
        const now = new Date();
        const hrs = String(now.getHours()).padStart(2, '0');
        const mins = String(now.getMinutes()).padStart(2, '0');
        const secs = String(now.getSeconds()).padStart(2, '0');
        realtimeClock.textContent = `${hrs}:${mins}:${secs}`;
    }
    setInterval(updateClock, 1000);
    updateClock();

    // Toggle Postgres vs SQLite Form Fields
    function updateDbFieldsVisibility() {
        const val = dbTypeSelect.value;
        postgresGroup.style.display = val === 'postgresql' ? 'block' : 'none';
        sqliteGroup.style.display = val === 'sqlite' ? 'block' : 'none';
        influxGroup.style.display = val === 'influxdb' ? 'block' : 'none';
        // InfluxDB 1.x logs in with user/password; 2.x and 3.x use a token and an organization
        const v1 = influxFields.version.value === '1';
        influxGroup.querySelectorAll('.influx-v1').forEach(el => { el.style.display = v1 ? '' : 'none'; });
        influxGroup.querySelectorAll('.influx-v2').forEach(el => { el.style.display = v1 ? 'none' : ''; });
    }
    dbTypeSelect.addEventListener('change', updateDbFieldsVisibility);
    influxFields.version.addEventListener('change', updateDbFieldsVisibility);

    // Database part of the settings, as the form shows it (used by Save and by Test connection)
    function collectDbConfig() {
        return {
            db_type: dbTypeSelect.value,
            db_name: dbNameInput.value.trim(),
            table_name: dbTableInput.value.trim() || 'janome_jp4_telemetry',
            host: dbHostInput.value.trim(),
            port: parseInt(dbPortInput.value, 10),
            user: dbUserInput.value.trim(),
            password: dbPasswordInput.value.trim(),
            sqlite_path: sqlitePathInput.value.trim(),
            influx_url: influxFields.url.value.trim(),
            influx_version: influxFields.version.value,
            influx_token: influxFields.token.value.trim(),
            influx_org: influxFields.org.value.trim(),
            influx_bucket: influxFields.bucket.value.trim(),
            influx_user: influxFields.user.value.trim(),
            influx_password: influxFields.password.value,
            influx_measurement: influxFields.measurement.value.trim(),
            influx_retention_days: parseInt(influxFields.retention.value, 10) || 0
        };
    }

    // Load Configuration from API
    async function loadConfig() {
        try {
            const res = await fetch('/api/config');
            if (!res.ok) throw new Error('Failed to fetch config');
            const data = await res.json();

            const serial = data.serial || {};
            if (serialPortInput) serialPortInput.value = serial.port || '';
            if (serialBaudrate) serialBaudrate.value = serial.baudrate || 9600;
            if (serialTimeout) serialTimeout.value = serial.timeout || 2.0;

            const acq = data.acquisition || {};
            if (acqInterval) acqInterval.value = acq.interval_time || 1.0;

            const db = data.database || {};
            if (dbTypeSelect) dbTypeSelect.value = db.db_type || 'none';
            if (dbNameInput) dbNameInput.value = db.db_name || 'janome_jp4_data';
            if (dbHostInput) dbHostInput.value = db.host || 'localhost';
            if (dbPortInput) dbPortInput.value = db.port || 5432;
            if (dbUserInput) dbUserInput.value = db.user || 'admin';
            if (dbPasswordInput) dbPasswordInput.value = db.password || 'admin';
            if (dbTableInput) dbTableInput.value = db.table_name || 'janome_jp4_telemetry';
            if (sqlitePathInput) sqlitePathInput.value = db.sqlite_path || 'janome_jp4_data.db';
            influxFields.url.value = db.influx_url || '';
            influxFields.version.value = String(db.influx_version || '2').startsWith('1') ? '1' : '2';
            influxFields.token.value = db.influx_token || '';
            influxFields.org.value = db.influx_org || '';
            influxFields.bucket.value = db.influx_bucket || db.influx_database || '';
            influxFields.user.value = db.influx_user || '';
            influxFields.password.value = db.influx_password || '';
            influxFields.measurement.value = db.influx_measurement || '';
            influxFields.retention.value = db.influx_retention_days || 0;

            const startup = data.startup || {};
            const autoStartCheck = document.getElementById('AUTO_START_ON_STARTUP');
            const autoStartModeSelect = document.getElementById('AUTO_START_MODE');
            if (autoStartCheck) autoStartCheck.checked = startup.auto_start_on_startup ?? data.AUTO_START_ON_STARTUP ?? false;
            if (autoStartModeSelect) autoStartModeSelect.value = startup.auto_start_mode || data.AUTO_START_MODE || 'mockup';

            const csvCfg = data.csv_export || {};
            const csvEnabled = document.getElementById('CSV_ENABLED');
            const csvMode = document.getElementById('CSV_MODE');
            const csvFolder = document.getElementById('CSV_FOLDER');
            if (csvEnabled) csvEnabled.checked = csvCfg.enabled ?? true;
            if (csvMode) csvMode.value = csvCfg.mode === 'day' ? 'day' : 'run';
            if (csvFolder) csvFolder.value = csvCfg.folder || 'exports';
            const csvDayStart = document.getElementById('CSV_DAY_START');
            if (csvDayStart) csvDayStart.value = /^\d{1,2}:\d{2}/.test(csvCfg.day_start || '') ? csvCfg.day_start.slice(0, 5).padStart(5, '0') : '00:00';
            updateCsvDayStart();
            const csvEndLoad = document.getElementById('CSV_END_LOAD_SETTING');
            if (csvEndLoad) csvEndLoad.value = csvCfg.end_load_setting_kgf ?? 250;

            updateDbFieldsVisibility();
            appendLog('[SYSTEM] Loaded configuration from server.');
        } catch (err) {
            appendLog(`[ERROR] Failed to load configuration: ${err.message}`, 'err');
        }
    }

    // The day-start time only matters when a new file is made for every day
    function updateCsvDayStart() {
        const mode = document.getElementById('CSV_MODE');
        const start = document.getElementById('CSV_DAY_START');
        if (mode && start) start.disabled = mode.value !== 'day';
    }
    const csvModeEl = document.getElementById('CSV_MODE');
    if (csvModeEl) csvModeEl.addEventListener('change', updateCsvDayStart);

    // Folder browser for the CSV folder: the panel lists this computer's folders (see /api/browse)
    (function setupFolderBrowser() {
        const folderInput = document.getElementById('CSV_FOLDER');
        const openBtn = document.getElementById('btn-browse-csv');
        const panel = document.getElementById('folder-browser');
        if (!folderInput || !openBtn || !panel) return;
        const roots = document.getElementById('fb-roots');
        const upBtn = document.getElementById('fb-up');
        const newBtn = document.getElementById('fb-new');
        const pathEl = document.getElementById('fb-path');
        const listEl = document.getElementById('fb-list');
        const errorEl = document.getElementById('fb-error');
        let current = '';
        let parent = '';
        let homePath = '';

        async function load(path) {
            errorEl.textContent = '';
            try {
                const res = await fetch('/api/browse?path=' + encodeURIComponent(path || ''));
                const data = await res.json();
                if (data.status !== 'success') {
                    errorEl.textContent = data.message || 'This folder cannot be opened.';
                    return;
                }
                current = data.path;
                parent = data.parent;
                homePath = data.home || '';
                pathEl.textContent = data.path;
                upBtn.disabled = !data.parent;
                roots.innerHTML = '';
                // The first entry is the user's home folder (where browsing starts), then the drives
                const places = (homePath ? [{ value: homePath, label: 'Home' }] : [])
                    .concat(data.roots.map(r => ({ value: r, label: r })));
                places.forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.value;
                    opt.textContent = p.label;
                    roots.appendChild(opt);
                });
                const inHome = homePath && data.path.toLowerCase().startsWith(homePath.toLowerCase());
                const here = inHome ? homePath : data.roots.find(r => data.path.toLowerCase().startsWith(r.toLowerCase()));
                if (here) roots.value = here;
                roots.classList.toggle('hidden', places.length < 2);
                listEl.innerHTML = '';
                if (!data.folders.length) {
                    const empty = document.createElement('div');
                    empty.className = 'fb-empty';
                    empty.textContent = 'No sub-folders here.';
                    listEl.appendChild(empty);
                }
                data.folders.forEach(f => {
                    const row = document.createElement('div');
                    row.className = 'device-item';
                    row.textContent = f.name;  // textContent: a folder name is never treated as HTML
                    row.addEventListener('click', () => load(f.path));
                    listEl.appendChild(row);
                });
            } catch (err) {
                errorEl.textContent = `Cannot list folders: ${err.message}`;
            }
        }

        openBtn.addEventListener('click', () => {
            if (panel.classList.contains('hidden')) {
                panel.classList.remove('hidden');
                // Start at the folder already chosen if it is a full path, otherwise at the home folder
                const typed = folderInput.value.trim();
                const full = /^([A-Za-z]:[\\/]|\\\\|\/|~)/.test(typed);
                load(full ? typed : '');
            } else {
                panel.classList.add('hidden');
            }
        });
        roots.addEventListener('change', () => load(roots.value));
        upBtn.addEventListener('click', () => { if (parent) load(parent); });
        newBtn.addEventListener('click', async () => {
            const name = window.prompt('Name of the new folder (created inside the folder shown)');
            if (!name) return;
            errorEl.textContent = '';
            try {
                const res = await fetch('/api/browse/mkdir', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ parent: current, name })
                });
                const data = await res.json();
                if (data.status === 'success') load(data.path);
                else errorEl.textContent = data.message || 'Cannot create the folder.';
            } catch (err) {
                errorEl.textContent = `Cannot create the folder: ${err.message}`;
            }
        });
        document.getElementById('fb-select').addEventListener('click', () => {
            if (current) folderInput.value = current;
            panel.classList.add('hidden');
        });
        document.getElementById('fb-cancel').addEventListener('click', () => panel.classList.add('hidden'));
    })();

    // Save Configuration to API
    configForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const autoStartCheck = document.getElementById('AUTO_START_ON_STARTUP');
        const autoStartModeSelect = document.getElementById('AUTO_START_MODE');
        const payload = {
            startup: {
                auto_start_on_startup: autoStartCheck ? autoStartCheck.checked : false,
                auto_start_mode: autoStartModeSelect ? autoStartModeSelect.value : 'mockup'
            },
            AUTO_START_ON_STARTUP: autoStartCheck ? autoStartCheck.checked : false,
            AUTO_START_MODE: autoStartModeSelect ? autoStartModeSelect.value : 'mockup',
            serial: {
                port: serialPortInput.value.trim(),
                baudrate: parseInt(serialBaudrate.value, 10),
                timeout: parseFloat(serialTimeout.value)
            },
            database: Object.assign(collectDbConfig(), {
                description: "Database storage for JANOME Electro Press JP4 telemetry data"
            }),
            acquisition: {
                interval_time: parseFloat(acqInterval.value)
            },
        };
        // the CSV section is missing if the page is an older cached copy; then leave the saved setting alone
        const csvEnabledBox = document.getElementById('CSV_ENABLED');
        const csvModeSelect = document.getElementById('CSV_MODE');
        const csvFolderInput = document.getElementById('CSV_FOLDER');
        if (csvEnabledBox && csvModeSelect && csvFolderInput) {
            payload.csv_export = {
                enabled: csvEnabledBox.checked,
                mode: csvModeSelect.value,
                // Windows "Copy as path" puts the path in quotes: they are not part of the folder name
                folder: csvFolderInput.value.trim().replace(/^["']+|["']+$/g, '').trim() || 'exports'
            };
            const csvDayStartInput = document.getElementById('CSV_DAY_START');
            if (csvDayStartInput) payload.csv_export.day_start = csvDayStartInput.value || '00:00';
            const csvEndLoadInput = document.getElementById('CSV_END_LOAD_SETTING');
            if (csvEndLoadInput && csvEndLoadInput.value !== '') payload.csv_export.end_load_setting_kgf = parseFloat(csvEndLoadInput.value);
        }

        try {
            const res = await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.status === 'success') {
                appendLog('[SYSTEM] Configuration saved successfully to config.json.');
                alert('Configuration saved successfully!');
            } else {
                alert(`Error saving config: ${data.message}`);
            }
        } catch (err) {
            appendLog(`[ERROR] Failed to save configuration: ${err.message}`, 'err');
        }
    });

    // Scan Serial Ports
    btnScanSerial.addEventListener('click', async () => {
        try {
            scannedPortsMenu.innerHTML = '<div class="device-item"><span class="dev-name">Scanning hardware ports...</span></div>';
            scannedPortsMenu.classList.remove('hidden');

            const res = await fetch('/api/scan_serial');
            const data = await res.json();

            if (data.status === 'success' && data.devices.length > 0) {
                scannedPortsMenu.innerHTML = '';
                data.devices.forEach(dev => {
                    const div = document.createElement('div');
                    div.className = 'device-item';
                    div.innerHTML = `
                        <span class="dev-name">${dev.name}</span>
                        <span class="dev-meta">${dev.type} ${dev.vendor ? '• ' + dev.vendor : ''}</span>
                    `;
                    div.addEventListener('click', () => {
                        serialPortInput.value = dev.port || dev.id;
                        scannedPortsMenu.classList.add('hidden');
                        appendLog(`[SYSTEM] Selected serial port: ${serialPortInput.value}`);
                    });
                    scannedPortsMenu.appendChild(div);
                });
            } else {
                scannedPortsMenu.innerHTML = '<div class="device-item"><span class="dev-name">No active ports found</span></div>';
            }
        } catch (err) {
            appendLog(`[ERROR] Port scan failed: ${err.message}`, 'err');
            scannedPortsMenu.classList.add('hidden');
        }
    });

    // Hide scan menu when clicking outside
    document.addEventListener('click', (e) => {
        if (!btnScanSerial.contains(e.target) && !scannedPortsMenu.contains(e.target)) {
            scannedPortsMenu.classList.add('hidden');
        }
    });

    // Test Serial Connection
    btnTestSerial.addEventListener('click', async () => {
        const payload = {
            serial: {
                port: serialPortInput.value.trim(),
                baudrate: parseInt(serialBaudrate.value, 10)
            }
        };
        try {
            appendLog(`[SYSTEM] Testing connection on ${payload.serial.port}...`);
            const res = await fetch('/api/test_serial', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                appendLog(`[SUCCESS] ${data.message}`, 'stats');
                alert(data.message);
            } else {
                appendLog(`[ERROR] ${data.message}`, 'err');
                alert(data.message);
            }
        } catch (err) {
            appendLog(`[ERROR] Serial test request failed: ${err.message}`, 'err');
        }
    });

    // Test Database Connection
    btnTestDb.addEventListener('click', async () => {
        const payload = { database: collectDbConfig() };
        try {
            appendLog('[SYSTEM] Testing database connectivity...');
            const res = await fetch('/api/test_db', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                appendLog(`[SUCCESS] ${data.message}`, 'stats');
                alert(data.message);
            } else {
                appendLog(`[ERROR] ${data.message}`, 'err');
                alert(data.message);
            }
        } catch (err) {
            appendLog(`[ERROR] Database test failed: ${err.message}`, 'err');
        }
    });

    // Start Ingestion Actions
    btnStartReal.addEventListener('click', () => {
        appendLog('[SYSTEM] Requesting start REAL Janome JP4 hardware ingestion stream...');
        socket.emit('start_janome', { mode: 'real' });
    });

    btnStartMock.addEventListener('click', () => {
        appendLog('[SYSTEM] Requesting start MOCK Janome JP4 synthetic simulation...');
        socket.emit('start_janome', { mode: 'mockup' });
    });

    btnStop.addEventListener('click', () => {
        appendLog('[SYSTEM] Requesting stop Janome JP4 ingestion process...');
        socket.emit('stop_janome');
    });

    btnClearLog.addEventListener('click', () => {
        terminalBody.innerHTML = '';
    });

    // Database readout under the live readings
    function showDbState(state, pending) {
        const waiting = parseInt(pending, 10) || 0;
        if (state === 'down') {
            elDb.textContent = `Unreachable - ${waiting} waiting on disk`;
            elDb.className = 'db-state db-down';
        } else if (state === 'connected') {
            elDb.textContent = waiting > 1 ? `Connected - sending ${waiting}` : 'Connected';
            elDb.className = 'db-state db-ok';
        } else {
            elDb.textContent = 'Local file only';
            elDb.className = 'db-state db-none';
        }
    }

    // Log Console Helper
    function appendLog(lineText, type = 'sys') {
        const div = document.createElement('div');
        div.className = 'log-line';
        if (type === 'err' || lineText.includes('ERROR') || lineText.includes('WARNING') || lineText.includes('failed')) {
            div.classList.add('err-line');
        } else if (type === 'stats' || lineText.includes('[STATS]') || lineText.includes('Success')) {
            div.classList.add('stats-line');
        } else {
            div.classList.add('sys-line');
        }
        div.textContent = lineText;
        terminalBody.appendChild(div);
        // Keep the page light during long runs: only the most recent lines stay in the view
        while (terminalBody.childElementCount > MAX_LOG_LINES) {
            terminalBody.removeChild(terminalBody.firstChild);
        }
        terminalBody.scrollTop = terminalBody.scrollHeight;
    }

    // ---------- Run summary (statistics + samples of the whole run, Start to Stop) ----------
    const RUN_SIGNALS = [
        { key: 'encoder_position_mm', label: 'Actual position', unit: 'mm', dp: 4 },
        { key: 'teaching_position_mm', label: 'Programmed position', unit: 'mm', dp: 4 },
        { key: 'load_n', label: 'Load', unit: 'N', dp: 1 }
    ];
    const SAMPLE_ROWS_SHOWN = 2000;

    const elRunState = document.getElementById('run-state');
    const elRunMeta = document.getElementById('run-meta');
    const elStatsBody = document.getElementById('stats-body');
    const elSamplesDetails = document.getElementById('samples-details');
    const elSamplesCount = document.getElementById('samples-count');
    const elSamplesBody = document.getElementById('samples-body');
    const elSamplesNote = document.getElementById('samples-note');
    const btnRefreshSamples = document.getElementById('btn-refresh-samples');
    let lastRunState = 'idle';

    function formatClock(iso) {
        const d = new Date(iso);
        const p = (n, w = 2) => String(n).padStart(w, '0');
        return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
    }

    function formatDuration(seconds) {
        const s = Math.max(0, Math.round(seconds));
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const sec = s % 60;
        const mm = String(m).padStart(2, '0');
        const ss = String(sec).padStart(2, '0');
        return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    }

    function renderRun(run) {
        if (!run) return;

        const badges = {
            idle: ['badge badge-idle', 'No run yet'],
            running: [run.mode === 'real' ? 'badge badge-running' : 'badge badge-sim',
                      run.mode === 'real' ? 'Running' : 'Running (simulation)'],
            finished: ['badge badge-finished', run.mode === 'real' || !run.mode ? 'Finished' : 'Finished (simulation)']
        };
        const [badgeClass, badgeText] = badges[run.state] || badges.idle;
        elRunState.className = badgeClass;
        elRunState.textContent = badgeText;

        if (run.state === 'idle') {
            elRunMeta.textContent = 'Start a run to see statistics here. They cover the whole run, from Start to Stop, and stay on screen after you stop.';
        } else {
            const parts = [
                `Started ${new Date(run.started_at).toLocaleTimeString([], { hour12: false })}`,
                `${run.state === 'running' ? 'Running for' : 'Lasted'} ${formatDuration(run.duration_s)}`,
                `${run.count.toLocaleString()} readings`
            ];
            elRunMeta.textContent = parts.join('  ·  ');
            if (run.truncated) {
                elRunMeta.textContent += `  ·  statistics cover all readings; the table keeps the latest ${run.stored.toLocaleString()}`;
            }
        }

        elStatsBody.innerHTML = '';
        RUN_SIGNALS.forEach(sig => {
            const st = run.stats ? run.stats[sig.key] : null;
            const tr = document.createElement('tr');
            const cell = (text, cls) => {
                const td = document.createElement('td');
                td.textContent = text;
                if (cls) td.className = cls;
                return td;
            };
            const nameTd = document.createElement('td');
            nameTd.innerHTML = `${sig.label}<span class="unit">${sig.unit}</span>`;
            tr.appendChild(nameTd);
            ['min', 'max', 'mean', 'std', 'last'].forEach(k => {
                tr.appendChild(cell(st ? st[k].toFixed(sig.dp) : '–'));
            });
            elStatsBody.appendChild(tr);
        });

        elSamplesCount.textContent = run.count ? `(${run.count.toLocaleString()})` : '';
        if (run.cycles) showCycleTotals(run.cycles);

        // Refresh the sample table once when a run ends so it shows the final data
        if (lastRunState === 'running' && run.state !== 'running' && elSamplesDetails.open) {
            loadSamples();
        }
        lastRunState = run.state;
    }

    async function loadSamples() {
        try {
            const res = await fetch(`/api/run/samples?limit=${SAMPLE_ROWS_SHOWN}`);
            const data = await res.json();
            const rows = data.samples || [];
            const frag = document.createDocumentFragment();
            // Newest first, so the latest readings are visible without scrolling
            for (let i = rows.length - 1; i >= 0; i--) {
                const [iso, enc, teach, load] = rows[i];
                const tr = document.createElement('tr');
                [formatClock(iso), enc.toFixed(4), teach.toFixed(4), load.toFixed(1)].forEach(text => {
                    const td = document.createElement('td');
                    td.textContent = text;
                    tr.appendChild(td);
                });
                frag.appendChild(tr);
            }
            elSamplesBody.innerHTML = '';
            elSamplesBody.appendChild(frag);

            if (!rows.length) {
                elSamplesNote.textContent = 'No readings yet.';
            } else if (data.count > rows.length) {
                elSamplesNote.textContent = `Showing the latest ${rows.length.toLocaleString()} of ${data.count.toLocaleString()} readings. Download the CSV for all of them.`;
            } else {
                elSamplesNote.textContent = `Showing all ${rows.length.toLocaleString()} readings, newest first.`;
            }
        } catch (err) {
            elSamplesNote.textContent = `Could not load readings: ${err.message}`;
        }
    }

    // ---------- Press link (is the press answering?) ----------
    const elLink = document.getElementById('link-state');
    const LINK_TEXT = {
        up: ['Connected', 'db-ok'],
        no_response: ['Not answering - retrying', 'db-down'],
        port_unavailable: ['Serial port unavailable - retrying', 'db-down'],
        connecting: ['Connecting...', 'db-none'],
        idle: ['Not running', 'db-none']
    };

    function showLink(link) {
        const [text, cls] = LINK_TEXT[link.state] || [link.state, 'db-none'];
        elLink.textContent = text;
        elLink.className = `db-state ${cls}`;
        elLink.title = link.detail || '';
    }

    // ---------- Finished press cycles (OK / NG) ----------
    const elCyclesBody = document.getElementById('cycles-body');
    const elCyclesNote = document.getElementById('cycles-note');
    const elCyclesOk = document.getElementById('cycles-ok');
    const elCyclesNg = document.getElementById('cycles-ng');
    const MAX_CYCLE_ROWS = 200;

    function cycleRow(c) {
        const tr = document.createElement('tr');
        if (!c.ok) tr.className = 'cycle-ng';
        const result = c.ok ? 'OK' : `NG - ${c.result_text}`;
        [formatClock(c.time), result, c.end_position_mm.toFixed(4), c.end_load_n.toFixed(1), c.work_time_s.toFixed(2)].forEach(text => {
            const td = document.createElement('td');
            td.textContent = text;
            tr.appendChild(td);
        });
        return tr;
    }

    function addCycleRow(cycle) {
        elCyclesBody.insertBefore(cycleRow(cycle), elCyclesBody.firstChild);
        while (elCyclesBody.childElementCount > MAX_CYCLE_ROWS) elCyclesBody.removeChild(elCyclesBody.lastChild);
        elCyclesNote.textContent = `Showing the latest ${elCyclesBody.childElementCount} cycle(s), newest first.`;
    }

    function showCycleTotals(totals) {
        elCyclesOk.textContent = `OK ${totals.ok}`;
        elCyclesNg.textContent = `NG ${totals.ng}`;
        if (!totals.total) {
            elCyclesBody.innerHTML = '';
            elCyclesNote.textContent = 'No cycles yet.';
        }
    }

    async function loadCycles() {
        try {
            const res = await fetch('/api/cycles?limit=' + MAX_CYCLE_ROWS);
            const data = await res.json();
            elCyclesBody.innerHTML = '';
            data.cycles.forEach(c => elCyclesBody.insertBefore(cycleRow(c), elCyclesBody.firstChild));
            showCycleTotals(data.totals);
            if (data.cycles.length) elCyclesNote.textContent = `Showing the latest ${data.cycles.length} cycle(s), newest first.`;
        } catch (err) {
            elCyclesNote.textContent = `Could not load cycles: ${err.message}`;
        }
    }

    // ---------- Event (error) log ----------
    // The card is missing if the page is an older cached copy; everything here is null-safe.
    const elEventsBody = document.getElementById('events-body');
    const elEventsNote = document.getElementById('events-note');
    const elEventsProblems = document.getElementById('events-problems');
    const MAX_EVENT_ROWS = 200;

    function parseEventLine(line) {
        // time | LEVEL | CASE | CODE | message | details
        const parts = line.split(' | ');
        if (parts.length < 5) return null;
        return { time: parts[0].trim(), level: parts[1].trim(), case: parts[2].trim(), code: parts[3].trim(),
                 message: parts[4].trim(), details: (parts[5] || '').trim() };
    }

    function eventRow(ev) {
        const tr = document.createElement('tr');
        const slug = (ev.case || 'info').toLowerCase().replace(/[^a-z0-9]+/g, '-');
        tr.className = `event-${ev.level.toLowerCase()} case-${slug}`;
        const cells = [ev.time.slice(5), ev.case || ev.level, ev.code, ev.details ? `${ev.message} (${ev.details})` : ev.message];
        cells.forEach(text => {
            const td = document.createElement('td');
            td.textContent = text;
            tr.appendChild(td);
        });
        return tr;
    }

    function addEventRow(ev) {
        if (!elEventsBody) return;
        if (elEventsProblems && elEventsProblems.checked && ev.level === 'INFO') return;
        elEventsBody.insertBefore(eventRow(ev), elEventsBody.firstChild);
        while (elEventsBody.childElementCount > MAX_EVENT_ROWS) elEventsBody.removeChild(elEventsBody.lastChild);
        elEventsNote.textContent = `Showing the latest ${elEventsBody.childElementCount} event(s), newest first.`;
    }

    async function loadEvents() {
        if (!elEventsBody) return;
        try {
            const level = elEventsProblems && elEventsProblems.checked ? '&level=WARNING' : '';
            const res = await fetch('/api/events?limit=' + MAX_EVENT_ROWS + level);
            const data = await res.json();
            elEventsBody.innerHTML = '';
            data.events.forEach(ev => elEventsBody.insertBefore(eventRow(ev), elEventsBody.firstChild));
            elEventsNote.textContent = data.events.length
                ? `Showing the latest ${data.events.length} event(s), newest first.` : 'No events yet.';
        } catch (err) {
            elEventsNote.textContent = `Could not load events: ${err.message}`;
        }
    }

    if (elEventsProblems) elEventsProblems.addEventListener('change', loadEvents);
    socket.on('event_update', (data) => {
        const ev = data && data.line ? parseEventLine(data.line) : null;
        if (ev) addEventRow(ev);
    });

    elSamplesDetails.addEventListener('toggle', () => { if (elSamplesDetails.open) loadSamples(); });
    btnRefreshSamples.addEventListener('click', loadSamples);
    // While a run is going and the table is open, keep it current
    setInterval(() => {
        if (elSamplesDetails.open && lastRunState === 'running') loadSamples();
    }, 3000);

    // Socket.IO Handlers
    socket.on('connect', () => {
        appendLog('[SYSTEM] Connected to Janome JP4 Socket.IO server on Port 8085.');
    });

    socket.on('status_change', (data) => {
        const isRunning = data.is_running;
        const mode = data.mode || 'mockup';

        if (isRunning) {
            if (mode === 'real') {
                statusDot.className = 'pulse-dot online';
                statusText.textContent = 'Running (reading the press)';
            } else {
                statusDot.className = 'pulse-dot mock';
                statusText.textContent = 'Running (simulation)';
            }
            btnStartReal.disabled = true;
            btnStartMock.disabled = true;
            btnStop.disabled = false;
        } else {
            statusDot.className = 'pulse-dot offline';
            statusText.textContent = 'Stopped';
            btnStartReal.disabled = false;
            btnStartMock.disabled = false;
            btnStop.disabled = true;
        }
    });

    socket.on('stats_update', (stats) => {
        if (!stats) return;
        if (stats.polled) elPolled.textContent = stats.polled;
        if (stats.encoder_position_mm) elEncoder.textContent = stats.encoder_position_mm;
        if (stats.teaching_position_mm) elTeaching.textContent = stats.teaching_position_mm;
        if (stats.load_n) elLoad.textContent = stats.load_n;
        if (stats.db_state) showDbState(stats.db_state, stats.db_pending);
    });

    socket.on('run_update', renderRun);
    socket.on('link_update', showLink);
    socket.on('cycle_update', addCycleRow);

    socket.on('log_update', (data) => {
        if (data && data.log) {
            appendLog(data.log);
        }
    });

    // Initial config load
    loadConfig();
    loadCycles();
    loadEvents();
});
