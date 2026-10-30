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
        if (val === 'none') {
            if (postgresGroup) postgresGroup.style.display = 'none';
            if (sqliteGroup) sqliteGroup.style.display = 'none';
        } else if (val === 'postgresql') {
            if (postgresGroup) postgresGroup.style.display = 'block';
            if (sqliteGroup) sqliteGroup.style.display = 'none';
        } else {
            if (postgresGroup) postgresGroup.style.display = 'none';
            if (sqliteGroup) sqliteGroup.style.display = 'block';
        }
    }
    dbTypeSelect.addEventListener('change', updateDbFieldsVisibility);

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

            const startup = data.startup || {};
            const autoStartCheck = document.getElementById('AUTO_START_ON_STARTUP');
            const autoStartModeSelect = document.getElementById('AUTO_START_MODE');
            if (autoStartCheck) autoStartCheck.checked = startup.auto_start_on_startup ?? data.AUTO_START_ON_STARTUP ?? false;
            if (autoStartModeSelect) autoStartModeSelect.value = startup.auto_start_mode || data.AUTO_START_MODE || 'mockup';

            updateDbFieldsVisibility();
            appendLog('[SYSTEM] Loaded configuration from server.');
        } catch (err) {
            appendLog(`[ERROR] Failed to load configuration: ${err.message}`, 'err');
        }
    }

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
            database: {
                db_type: dbTypeSelect.value,
                db_name: dbNameInput ? dbNameInput.value.trim() : 'janome_jp4_data',
                table_name: dbTableInput ? dbTableInput.value.trim() : 'janome_jp4_telemetry',
                host: dbHostInput ? dbHostInput.value.trim() : 'localhost',
                port: dbPortInput ? parseInt(dbPortInput.value, 10) : 5432,
                user: dbUserInput ? dbUserInput.value.trim() : 'admin',
                password: dbPasswordInput ? dbPasswordInput.value.trim() : 'admin',
                sqlite_path: sqlitePathInput ? sqlitePathInput.value.trim() : 'janome_jp4_data.db',
                description: "Database storage for JANOME Electro Press JP4 telemetry data"
            },
            acquisition: {
                interval_time: parseFloat(acqInterval.value),
                max_retries: 3
            }
        };

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
        const payload = {
            database: {
                db_type: dbTypeSelect.value,
                db_name: dbNameInput ? dbNameInput.value.trim() : 'janome_jp4_data',
                host: dbHostInput ? dbHostInput.value.trim() : 'localhost',
                port: dbPortInput ? parseInt(dbPortInput.value, 10) : 5432,
                user: dbUserInput ? dbUserInput.value.trim() : 'admin',
                password: dbPasswordInput ? dbPasswordInput.value.trim() : 'admin',
                sqlite_path: sqlitePathInput ? sqlitePathInput.value.trim() : 'janome_jp4_data.db'
            }
        };
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

    // Log Console Helper
    function appendLog(lineText, type = 'sys') {
        const div = document.createElement('div');
        div.className = 'log-line';
        if (type === 'err' || lineText.includes('ERROR') || lineText.includes('failed')) {
            div.classList.add('err-line');
        } else if (type === 'stats' || lineText.includes('[STATS]') || lineText.includes('Success')) {
            div.classList.add('stats-line');
        } else {
            div.classList.add('sys-line');
        }
        div.textContent = lineText;
        terminalBody.appendChild(div);
        terminalBody.scrollTop = terminalBody.scrollHeight;
    }

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
                statusText.textContent = 'HARDWARE ACTIVE';
            } else {
                statusDot.className = 'pulse-dot mock';
                statusText.textContent = 'MOCK SIMULATION';
            }
            btnStartReal.disabled = true;
            btnStartMock.disabled = true;
            btnStop.disabled = false;
        } else {
            statusDot.className = 'pulse-dot offline';
            statusText.textContent = 'OFFLINE';
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
    });

    socket.on('log_update', (data) => {
        if (data && data.log) {
            appendLog(data.log);
        }
    });

    // Initial config load
    loadConfig();
});
