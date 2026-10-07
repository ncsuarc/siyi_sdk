/**
 * SIYI SDK Web UI Frontend Logic
 */

// Storage can be unavailable (private windows, blocked site data).
const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
};

window.downloadJSON = (filename, data) => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const link = Object.assign(document.createElement('a'), {href: url, download: filename});
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
};

/** Resolve true only if the user confirms; `typeToConfirm` requires typing a word first. */
window.confirmAction = (message, {title = 'Are you sure?', confirmLabel = 'Confirm', danger = false, typeToConfirm = null} = {}) => {
    const dialog = document.getElementById('confirm-dialog');
    const ok = document.getElementById('confirm-ok');
    const typeLabel = document.getElementById('confirm-type-label');
    const typeInput = document.getElementById('confirm-type-input');
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-message').textContent = message;
    ok.textContent = confirmLabel;
    ok.classList.toggle('btn-danger', danger);
    ok.classList.toggle('btn-primary', !danger);
    typeLabel.hidden = !typeToConfirm;
    typeInput.value = '';
    document.getElementById('confirm-type-prompt').textContent = typeToConfirm ? `Type ${typeToConfirm} to continue` : '';
    ok.disabled = Boolean(typeToConfirm);
    typeInput.oninput = () => { ok.disabled = typeInput.value.trim() !== typeToConfirm; };
    dialog.returnValue = 'cancel';
    dialog.showModal();
    (typeToConfirm ? typeInput : document.getElementById('confirm-cancel')).focus();
    return new Promise(resolve => {
        dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), {once: true});
    });
};

class SiyiApp {
    constructor() {
        this.ws = null;
        this.joystickActive = false;
        this.currentPath = "";
        this.currentMediaMode = 0; // 0: Images, 1: Videos
        this.liveViewEnabled = true;
        this.isCameraConnected = false;
        this.isRebooting = false;
        this.connectionCheckPending = false;
        this.cameraIp = '';
        this.requestTimeoutMs = 15000;
        this.modePending = false;
        this.recordPending = false;
        this.cameraState = null;
        this.lastStatusReceived = 0;
        this.motionQueues = new Map();
        this.stopHandlers = [];
        this.lastActions = new Map();
        this.look = {pending: false, latest: null, lastSent: 0, limitNotified: 0};
        this.zoomLevel = null;
        this.zoomMax = null;
        this.lockState = 'idle';
        this.lockReleasing = false;
        this.zoomTarget = {pending: false, latest: null};
        this.theme = store.get('theme') || 'dark';

        this.init();
    }

    async init() {
        try {
            this.applyTheme();
            this.liveViewEnabled = store.get('liveViewEnabled') !== 'false';
            document.getElementById('config-live-view-toggle').checked = this.liveViewEnabled;
            this.setupTabs();
            this.setupPlots();
            this.bindEvents();
            this.setupJoystick();
            this.setupPointer();
            this.setupKeyboard();
            this.loadPointingConfig();
            this.connectWS();

            setInterval(() => this.checkConnection(), 3000);
            setInterval(() => {
                if (performance.now() - this.lastStatusReceived > 2500) this.renderCameraState(null);
            }, 500);

            this.renderConnection('checking');
            this.checkConnection();
        } catch (e) {
            console.error("SiyiApp: Initialization crashed!", e);
        }
    }

    setupTabs() {
        const tabs = [...document.querySelectorAll('.workspace-tabs [role=tab]')];
        const select = name => {
            for (const tab of tabs) {
                const active = tab.dataset.tab === name;
                tab.setAttribute('aria-selected', String(active));
                tab.tabIndex = active ? 0 : -1;
                document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
            }
            store.set('workspaceTab', name);
            window.dispatchEvent(new CustomEvent('siyi-tab', {detail: name}));
        };
        for (const tab of tabs) {
            tab.addEventListener('click', () => select(tab.dataset.tab));
            tab.addEventListener('keydown', event => {
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
                event.preventDefault();
                const next = tabs[(tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
                next.focus();
                select(next.dataset.tab);
            });
        }
        const saved = store.get('workspaceTab');
        select(tabs.some(tab => tab.dataset.tab === saved) ? saved : 'control');
    }

    setupPlots() {
        this.attitudePlot = new RollingPlot(document.getElementById('attitude-plot'), [
            {key: 'yaw', color: '--plot-yaw'}, {key: 'pitch', color: '--plot-pitch'}, {key: 'roll', color: '--plot-roll'},
        ], {unit: '°'});
        this.rttPlot = new RollingPlot(document.getElementById('rtt-plot'), [
            {key: 'rtt', color: '--accent-color'},
        ], {windowMs: 60000, minSpan: 50});
        this.lastRttSample = null;
        const pause = document.getElementById('plot-pause');
        pause.onclick = () => {
            this.attitudePlot.paused = !this.attitudePlot.paused;
            pause.textContent = this.attitudePlot.paused ? 'Resume' : 'Pause';
            pause.classList.toggle('active', this.attitudePlot.paused);
        };
        window.addEventListener('siyi-tab', event => {
            if (event.detail === 'diagnostics') { this.attitudePlot.draw(); this.rttPlot.draw(); }
        });
    }

    setupKeyboard() {
        // Arrow keys hold-to-rotate at a fixed speed; releasing sends a stop.
        const speed = 50;
        const vectors = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1]};
        const held = new Set();
        let timer = null;
        const velocity = () => {
            let yaw = 0, pitch = 0;
            for (const key of held) { yaw += vectors[key][0]; pitch += vectors[key][1]; }
            return {yaw: yaw * speed, pitch: pitch * speed};
        };
        const release = () => {
            if (timer === null) return;
            held.clear();
            clearInterval(timer);
            timer = null;
            this.queueMotion('rotate', {yaw: 0, pitch: 0});
        };
        this.stopHandlers.push(release);
        const zoomHold = {'+': 1, '=': 1, '-': -1};
        let zoomKey = null;
        const releaseZoom = () => {
            if (zoomKey === null) return;
            zoomKey = null;
            clearInterval(this.zoomKeyTimer);
            this.queueMotion('zoom', 0);
        };
        this.stopHandlers.push(releaseZoom);
        const ignore = event => event.ctrlKey || event.metaKey || event.altKey ||
            document.getElementById('panel-control').hidden ||
            event.target.closest('input, select, textarea, button, dialog, [contenteditable]') ||
            document.getElementById('config-modal').classList.contains('active');

        document.addEventListener('keydown', event => {
            if (ignore(event) || !this.isCameraConnected) return;
            if (vectors[event.key]) {
                event.preventDefault();
                if (held.has(event.key)) return;
                held.add(event.key);
                this.queueMotion('rotate', velocity());
                if (timer === null) timer = setInterval(() => this.queueMotion('rotate', velocity()), 50);
            } else if (zoomHold[event.key] && zoomKey === null) {
                event.preventDefault();
                zoomKey = event.key;
                this.queueMotion('zoom', zoomHold[event.key]);
                this.zoomKeyTimer = setInterval(() => this.queueMotion('zoom', zoomHold[zoomKey]), 50);
            } else if (event.key === ' ') {
                event.preventDefault();
                document.getElementById('stop-btn').click();
            } else if (event.key.toLowerCase() === 'c' && !event.repeat) {
                document.getElementById('center-btn').click();
            }
        });
        document.addEventListener('keyup', event => {
            if (vectors[event.key] && held.delete(event.key)) {
                if (held.size) this.queueMotion('rotate', velocity());
                else release();
            } else if (event.key === zoomKey) {
                releaseZoom();
            }
        });
    }

    applyTheme() {
        if (this.theme === 'light') {
            document.body.classList.add('light-mode');
            const icon = document.querySelector('#theme-toggle-btn i');
            if (icon) {
                icon.className = 'fas fa-sun';
            }
        } else {
            document.body.classList.remove('light-mode');
            const icon = document.querySelector('#theme-toggle-btn i');
            if (icon) {
                icon.className = 'fas fa-moon';
            }
        }
        store.set('theme', this.theme);
        this.attitudePlot?.draw();
        this.rttPlot?.draw();
    }

    toggleTheme() {
        this.theme = this.theme === 'dark' ? 'light' : 'dark';
        this.applyTheme();
    }

    async restoreUI() {
        if (!this.isCameraConnected) {
            return;
        }
        
        try {
            await this.loadSystemInfo();
        } catch (e) {
            console.warn("Failed to load system info", e);
        }
        
        try {
            await this.loadMedia();
        } catch (e) {
            console.warn("Failed to load media", e);
        }
        
    }

    async checkConnection() {
        if (this.connectionCheckPending) return;
        this.connectionCheckPending = true;
        try {
            const info = await this.request('/api/config/ip', {}, {}, 5000);
            if (typeof info.connected !== 'boolean' || !info.ip) {
                throw new Error('Invalid connection status from server');
            }
            const wasConnected = this.isCameraConnected;
            const ipChanged = this.cameraIp !== info.ip;
            this.cameraIp = info.ip;
            document.getElementById('camera-ip-display').innerText = info.ip;
            document.getElementById('stream-backend-select').value = info.backend || 'auto';
            const ipInput = document.getElementById('config-ip-input');
            if (!ipInput.value || ipChanged) ipInput.value = info.ip;
            this.isCameraConnected = info.connected;
            if (info.connected) this.isRebooting = false;
            this.renderConnection(info.connected ? 'connected' : (this.isRebooting ? 'rebooting' : 'offline'));
            this.renderCameraState(info);
            if (!info.connected && !this.isRebooting && info.connection_error) {
                document.getElementById('overlay-msg').innerText += ` ${info.connection_error}`;
            }
            const videoStatus = document.getElementById('video-status');
            videoStatus.hidden = !info.connected || !this.liveViewEnabled || info.stream_ready === true;
            videoStatus.innerText = info.stream_error
                ? `Live video unavailable: ${info.stream_error}`
                : 'No live video received yet. Check the camera video stream or turn Live View off.';
            document.getElementById('video-stream').hidden = !info.stream_ready || !this.liveViewEnabled;
            if (info.connected && (!wasConnected || ipChanged)) {
                this.updateLiveView();
                this.restoreUI();
            }
        } catch (e) {
            this.isCameraConnected = false;
            this.renderConnection('server-offline');
        } finally {
            this.connectionCheckPending = false;
            document.getElementById('retry-connection-btn').disabled = false;
        }
    }

    renderConnection(status) {
        const connected = status === 'connected';
        const messages = {
            checking: ['CHECKING CONNECTION', 'Checking whether the camera is reachable...', 'CHECKING...'],
            offline: ['CAMERA OFFLINE', `Cannot reach the camera at ${this.cameraIp || 'the configured IP'}. Check camera power, the network cable/Wi-Fi, and the camera IP in Settings. Automatic retries are running.`, 'OFFLINE'],
            'server-offline': ['SERVER UNREACHABLE', 'The web server did not respond. Check that it is running, then retry. Automatic retries are running.', 'SERVER OFFLINE'],
            rebooting: ['CAMERA REBOOTING', 'The camera is restarting. If it stays offline, check its power and network connection.', 'REBOOTING...'],
            connected: ['', '', 'CONNECTED']
        };
        const [title, message, label] = messages[status];
        document.getElementById('offline-overlay').classList.toggle('active', !connected);
        document.getElementById('overlay-title').innerText = title;
        document.getElementById('overlay-msg').innerText = message;
        document.getElementById('connection-indicator').className = connected ? 'indicator online' : 'indicator';
        document.getElementById('camera-status').innerText = label;
        for (const id of ['photo-btn', 'record-btn', 'center-btn', 'lock-btn', 'follow-btn', 'fpv-btn', 'stop-btn', 'zoom-in-btn', 'zoom-out-btn',
            'reboot-camera-btn', 'reboot-gimbal-btn', 'format-sd-btn']) {
            document.getElementById(id).disabled = !connected;
        }
        document.getElementById('config-res-select').disabled = !connected;
        document.getElementById('config-rec-res-select').disabled = !connected;
        document.getElementById('joystick-zone').setAttribute('aria-disabled', String(!connected));
        if (!connected) {
            this.stopMotion();
            this.renderCameraState(null);
            document.getElementById('video-stream').parentElement.style.display = 'flex';
            document.getElementById('video-stream').removeAttribute('src');
            document.getElementById('video-stream').hidden = true;
            document.getElementById('video-status').hidden = true;
            this.mediaMessage('Connect the camera to browse media.');
        }
    }

    updateLiveView() {
        const stream = document.getElementById('video-stream');
        const container = stream.parentElement;
        if (this.liveViewEnabled && this.isCameraConnected) {
            container.style.display = 'flex';
            stream.src = '/api/stream/video';
        } else {
            container.style.display = this.isCameraConnected ? 'none' : 'flex';
            stream.removeAttribute('src');
        }
        document.getElementById('video-status').hidden = !this.liveViewEnabled || !this.isCameraConnected;
        this.request('/api/stream/toggle', {method: 'POST'}, {enabled: this.liveViewEnabled})
            .catch(error => {
                if (!this.liveViewEnabled || !this.isCameraConnected) return;
                const status = document.getElementById('video-status');
                status.hidden = false;
                status.innerText = `Live video unavailable: ${error.message}`;
            });
        store.set('liveViewEnabled', this.liveViewEnabled);
    }

    bindEvents() {
        // Modals
        document.getElementById('open-config-btn').onclick = () => document.getElementById('config-modal').classList.add('active');
        document.getElementById('offline-config-btn').onclick = () => document.getElementById('config-modal').classList.add('active');
        document.getElementById('retry-connection-btn').onclick = () => {
            document.getElementById('retry-connection-btn').disabled = true;
            this.checkConnection();
        };
        document.getElementById('close-config-btn').onclick = () => document.getElementById('config-modal').classList.remove('active');
        document.getElementById('save-config-btn').onclick = () => this.saveConfig();

        // Control Buttons
        document.getElementById('photo-btn').onclick = async () => {
            const result = await this.post('/api/camera/photo');
            if (result) this.notify('Photo command sent. Capture is not yet confirmed.');
        };
        document.getElementById('record-btn').onclick = () => this.toggleRecord();
        document.getElementById('center-btn').onclick = async () => {
            if (!await this.stopMotion(true)) return;
            const result = await this.post('/api/gimbal/center');
            if (result) this.notify('Center command acknowledged.');
        };
        for (const mode of ['LOCK', 'FOLLOW', 'FPV']) {
            document.getElementById(`${mode.toLowerCase()}-btn`).onclick = () => this.setGimbalMode(mode);
        }
        document.getElementById('stop-btn').onclick = async () => {
            if (await this.stopMotion(true)) this.notify('Stop commands sent for gimbal and zoom.');
        };
        this.bindHold('zoom-in-btn', 'zoom', 1);
        this.bindHold('zoom-out-btn', 'zoom', -1);
        window.addEventListener('blur', () => this.stopMotion());
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) this.stopMotion();
        });

        document.getElementById('refresh-media-btn').onclick = () => this.loadMedia();

        // Media Tabs
        document.getElementById('media-tab-img').onclick = () => this.setMediaMode(0);
        document.getElementById('media-tab-vid').onclick = () => this.setMediaMode(1);

        document.getElementById('format-sd-btn').onclick = () => this.formatSD();

        document.getElementById('reboot-camera-btn').onclick = () => this.reboot(true, false);
        document.getElementById('reboot-gimbal-btn').onclick = () => this.reboot(false, true);

        document.getElementById('theme-toggle-btn').onclick = () => this.toggleTheme();

        // Header Toggles
        document.getElementById('config-live-view-toggle').onchange = (e) => {
            this.liveViewEnabled = e.target.checked;
            this.updateLiveView();
        };
        document.getElementById('stream-backend-select').onchange = async (event) => {
            const select = event.target;
            select.disabled = true;
            try {
                const response = await this.request('/api/stream/backend', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({backend: select.value})
                });
                this.notify(`Video backend: ${response.backend}`);
                if (this.liveViewEnabled) this.updateLiveView();
            } catch (error) {
                this.notify(`Video backend failed: ${error.message}`, true);
            } finally {
                select.disabled = false;
            }
        };
    }

    async reboot(camera, gimbal) {
        const target = camera && gimbal ? "Camera & Gimbal" : (camera ? "Camera" : "Gimbal");
        const confirmed = await confirmAction(
            `Soft reboot the ${target}? Connectivity will be interrupted while it restarts.`,
            {title: `Reboot ${target}`, confirmLabel: 'Reboot', danger: true});
        if (!confirmed) return;

        const res = await this.post('/api/system/reboot', {camera, gimbal});
        if (!res) return;
        if (res.status !== 'ok') {
            this.notify(`${target} reboot failed.`, true);
            return;
        }
        this.notify(`${target} reboot command sent.`);
        if (camera) {
            this.isRebooting = true;
            this.isCameraConnected = false;
            this.renderConnection('rebooting');
            this.updateLiveView(); // Stop stream locally
        }
    }

    async formatSD() {
        const confirmed = await confirmAction(
            'This permanently erases ALL photos and videos on the SD card. It cannot be undone.',
            {title: 'Format SD card', confirmLabel: 'Format', danger: true, typeToConfirm: 'FORMAT'});
        if (!confirmed) return;

        const res = await this.post('/api/storage/format');
        if (!res) return;
        if (res.status === 'ok') {
            this.notify("The camera acknowledged the SD card format request.");
        } else if (res.status === 'unconfirmed') {
            this.notify("Format request sent, but the camera did not acknowledge it. Check the SD card before relying on the result.", true);
        } else {
            this.notify("SD card format failed.", true);
        }
        this.loadMedia();
    }

    setMediaMode(mode) {
        this.currentMediaMode = mode;
        document.getElementById('media-tab-img').classList.toggle('active', mode === 0);
        document.getElementById('media-tab-vid').classList.toggle('active', mode === 1);
        this.loadMedia();
    }

    async post(url, body = {}, params = {}) {
        try {
            return await this.request(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: body ? JSON.stringify(body) : null
            }, params);
        } catch (e) {
            this.notify(e.message, true);
            return null;
        }
    }

    async get(url, params = {}) {
        return this.request(url, {}, params);
    }

    async request(url, options = {}, params = {}, timeoutMs = this.requestTimeoutMs) {
        const queryParams = new URLSearchParams(params).toString();
        const fullUrl = queryParams ? `${url}?${queryParams}` : url;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), timeoutMs);
        const started = performance.now();
        try {
            const resp = await fetch(fullUrl, {...options, signal: controller.signal, cache: 'no-store'});
            const data = await resp.json();
            if (options.method === 'POST' && /^\/api\/(gimbal|camera|track)\//.test(url)) {
                document.getElementById('latency-browser').textContent = `${Math.round(performance.now() - started)} ms`;
                const timing = /app;dur=([\d.]+)/.exec(resp.headers.get('Server-Timing') || '');
                document.getElementById('latency-server').textContent = timing ? `${Number(timing[1]).toFixed(1)} ms` : '—';
                const names = {
                    '/api/gimbal/rotate': 'Gimbal velocity', '/api/gimbal/center': 'Center',
                    '/api/gimbal/mode': 'Gimbal mode', '/api/camera/record': 'Recording toggle',
                    '/api/camera/photo': 'Photo', '/api/camera/zoom': 'Zoom velocity',
                    '/api/camera/encoding': 'Encoding settings', '/api/gimbal/look': 'Point',
                    '/api/track/lock': 'Point lock', '/api/track/release': 'Release lock',
                    '/api/track/calibrate': 'Measure loop timing',
                    '/api/camera/zoom_to': 'Zoom to'
                };
                let suffix = '';
                if (url === '/api/gimbal/mode') suffix = ` · ${JSON.parse(options.body).mode}`;
                if (url === '/api/gimbal/rotate') {
                    const velocity = JSON.parse(options.body);
                    suffix = ` · yaw ${velocity.yaw}, pitch ${velocity.pitch}`;
                }
                if (params.direction != null) suffix = ` · ${params.direction === 0 ? 'stop' : params.direction}`;
                document.getElementById('latency-command').textContent = `Last command: ${names[url] || url}${suffix} · HTTP ${resp.status}`;
                const ms = value => value == null ? '—' : `${value} ms`;
                const command = data.command_timing || {};
                document.getElementById('latency-send').textContent = command.sdk_reply_ms != null
                    ? `${ms(command.sdk_reply_ms)} (reply)` : ms(command.send_ms);
                document.getElementById('latency-preflight').textContent = `${ms(command.queue_ms)} / ${ms(command.preflight_ms)}`;
            }
            if (!resp.ok) {
                const detail = Array.isArray(data.detail)
                    ? data.detail.map(item => item.msg).join('; ')
                    : data.detail;
                throw new Error(detail || `Request failed (${resp.status})`);
            }
            return data;
        } catch (e) {
            if (e.name === 'AbortError') throw new Error('Request timed out. Check the connection and retry.');
            throw e;
        } finally {
            clearTimeout(timeout);
        }
    }

    async saveConfig() {
        const ip = document.getElementById('config-ip-input').value.trim();
        const resValue = document.getElementById('config-res-select').value;
        const recResValue = document.getElementById('config-rec-res-select').value;
        const button = document.getElementById('save-config-btn');
        const message = document.getElementById('config-status');
        button.disabled = true;
        message.innerText = 'Saving settings...';
        
        try {
            const ipChanged = ip && ip !== this.cameraIp;
            if (ipChanged) {
                const res = await this.request('/api/config/ip', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip})
                });
                if (res.status !== 'ok') throw new Error(res.detail || 'Failed to save camera IP');
                this.cameraIp = ip;
                document.getElementById('camera-ip-display').innerText = ip;
                this.isCameraConnected = false;
                this.isRebooting = false;
                this.renderConnection('offline');
            }

            await this.savePointingConfig();

            for (const [stream, value] of [['recording', recResValue], ['main', resValue]]) {
                if (!value || !this.isCameraConnected || ipChanged) continue;
                const res = await this.request('/api/camera/encoding', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({resolution: value, stream})
                });
                if (res?.status !== 'ok') {
                    throw new Error(res?.detail || `Failed to set ${stream === 'main' ? 'live view' : 'recording'} encoding`);
                }
            }
            if (recResValue || resValue) this.loadSystemInfo();

            message.innerText = ipChanged || !this.isCameraConnected
                ? 'Camera IP saved. Waiting for the camera to respond. You can close Settings; connection retries continue automatically. Apply resolution changes after connecting.'
                : 'Settings saved successfully.';
            await this.checkConnection();
            if (this.isCameraConnected) {
                this.updateLiveView();
                this.restoreUI();
            }
        } catch (e) {
            message.innerText = 'Failed to save settings: ' + e.message;
        } finally {
            button.disabled = false;
        }
    }

    async toggleRecord() {
        if (this.recordPending) return;
        this.recordPending = true;
        document.getElementById('record-btn').disabled = true;
        this.notify('Sending recording command…');
        try {
            const result = await this.post('/api/camera/record');
            if (result) {
                this.renderCameraState(result);
                this.notify('Recording command sent; waiting for camera confirmation.');
            }
        } finally {
            this.recordPending = false;
            this.renderCameraState(this.cameraState);
        }
    }

    notify(message, error = false) {
        const el = document.getElementById('command-status');
        el.textContent = message;
        el.classList.toggle('error', error);
        el.hidden = false;
        clearTimeout(this.messageTimer);
        this.messageTimer = setTimeout(() => { el.hidden = true; }, error ? 8000 : 4500);
    }

    renderCameraState(data) {
        this.cameraState = data;
        if (data) this.lastStatusReceived = performance.now();
        const fresh = this.isCameraConnected && data?.status_fresh === true;
        const camera = fresh ? data.camera : null;
        const actions = data?.actions || {};
        const modePending = this.modePending || actions.mode?.status === 'pending';
        const recordPending = this.recordPending || actions.record?.status === 'pending';
        const mode = camera?.gimbal_mode;
        document.getElementById('gimbal-mode-status').textContent = mode || 'Unknown';
        const recordLabels = {RECORDING: 'Recording', NOT_RECORDING: 'Stopped', NO_TF_CARD: 'No SD card', DATA_LOSS: 'SD data loss'};
        document.getElementById('record-state-status').textContent = recordLabels[camera?.recording] || 'Unknown';
        document.getElementById('record-state-status').classList.toggle('recording', camera?.recording === 'RECORDING');
        const freshness = document.getElementById('state-freshness');
        freshness.textContent = fresh ? `Camera · ${(data.status_age_ms / 1000).toFixed(1)}s ago` : 'Unconfirmed / stale';
        freshness.title = data?.status_error || 'Active modes are read back from the camera.';
        freshness.classList.toggle('stale', !fresh);
        const descriptions = {
            LOCK: 'Lock: holds the pointing direction as the vehicle turns.',
            FOLLOW: 'Follow: follows vehicle yaw while stabilizing the view.',
            FPV: 'FPV: follows vehicle orientation, including roll.'
        };
        document.getElementById('mode-description').textContent = descriptions[mode] || 'Waiting for camera mode confirmation.';
        for (const value of ['LOCK', 'FOLLOW', 'FPV']) {
            const btn = document.getElementById(`${value.toLowerCase()}-btn`);
            btn.classList.toggle('active', mode === value);
            btn.setAttribute('aria-pressed', String(mode === value));
            btn.disabled = !this.isCameraConnected || modePending;
            btn.textContent = (value === 'FPV' ? value : value.charAt(0) + value.slice(1).toLowerCase()) +
                (actions.mode?.status === 'pending' && actions.mode.target === value ? ' …' : '');
        }
        const recording = camera?.recording === 'RECORDING';
        const btn = document.getElementById('record-btn');
        btn.textContent = recordPending ? 'Confirming…' : (recording ? 'Stop recording' : 'Start recording');
        btn.classList.toggle('btn-danger', recording);
        btn.disabled = !camera || recordPending || !['RECORDING', 'NOT_RECORDING'].includes(camera.recording);
        document.getElementById('recording-badge').hidden = !recording;
        if (data?.latency) {
            const metrics = data.latency;
            const ms = value => value == null ? '—' : `${value} ms`;
            document.getElementById('latency-camera').textContent = fresh ? ms(metrics.camera_rtt_ms) : 'Unavailable';
            document.getElementById('latency-percentiles').textContent = `${ms(metrics.camera_p50_ms)} / ${ms(metrics.camera_p95_ms)}`;
            const failureRate = metrics.queries ? (100 * metrics.timeouts / metrics.queries).toFixed(0) : '0';
            const failures = document.getElementById('latency-timeouts');
            failures.textContent = `${metrics.timeouts} / ${metrics.queries} (${failureRate}%)`;
            failures.classList.toggle('stale', metrics.timeouts > 0);
            const failure = document.getElementById('latency-failure');
            failure.hidden = !metrics.last_failure;
            failure.textContent = metrics.last_failure ? `Last failed query: ${metrics.last_failure}` : '';
            document.getElementById('latency-video').textContent = `${ms(metrics.jpeg_ms)} / ${ms(metrics.frame_age_ms)}`;
            document.getElementById('latency-attitude').textContent = ms(metrics.attitude_age_ms);
            document.getElementById('latency-samples').textContent = `${metrics.samples} successful samples · rolling window of 100 queries. Failed queries are excluded from p50 / p95. Movement is sent without ACK waits; sent does not mean confirmed.`;
        }
        const latest = Object.entries(actions).sort((a, b) => b[1].id - a[1].id)[0];
        const confirmation = document.getElementById('latency-confirmation');
        confirmation.textContent = latest
            ? `${latest[0]} · ${latest[1].status} · ${latest[1].confirmation_ms ?? latest[1].elapsed_ms} ms`
            : '—';
        for (const [kind, action] of Object.entries(actions)) {
            const previous = this.lastActions.get(kind);
            if (previous?.id === action.id && previous.status === 'pending' && action.status !== 'pending') {
                this.notify(action.status === 'confirmed'
                    ? `${action.target} confirmed by camera (${action.confirmation_ms} ms).`
                    : `${kind} change ${action.status}: ${action.error || 'No camera confirmation'}`,
                action.status !== 'confirmed');
            }
            this.lastActions.set(kind, {id: action.id, status: action.status});
        }
    }

    async setGimbalMode(mode) {
        if (this.modePending) return;
        this.modePending = true;
        this.renderCameraState(this.cameraState);
        this.notify(`Requesting ${mode.toLowerCase()} mode…`);
        try {
            if (!await this.stopMotion(true)) return;
            const result = await this.post('/api/gimbal/mode', {mode});
            if (result) {
                this.renderCameraState(result);
                this.notify(`${mode} sent; waiting for camera confirmation.`);
            }
        } finally {
            this.modePending = false;
            this.renderCameraState(this.cameraState);
        }
    }

    queueMotion(kind, value) {
        let queue = this.motionQueues.get(kind);
        if (!queue) {
            queue = {pending: false, latest: null};
            this.motionQueues.set(kind, queue);
        }
        // Replace unsent values, so releasing a control cannot sit behind a
        // backlog of obsolete velocities. One request at a time per control.
        queue.latest = value;
        if (!queue.pending) queue.flight = this.flushMotion(kind, queue);
        return queue.flight;
    }

    async flushMotion(kind, queue) {
        queue.pending = true;
        try {
            while (queue.latest !== null) {
                const value = queue.latest;
                queue.latest = null;
                const url = kind === 'rotate' ? '/api/gimbal/rotate' : `/api/camera/${kind}`;
                await this.request(url, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: kind === 'rotate' ? JSON.stringify(value) : null
                }, kind === 'rotate' ? {} : {direction: value}, 2000);
            }
            return true;
        } catch (e) {
            queue.latest = null;
            this.notify(`Control send failed: ${e.message}`, true);
            // The server stops held motion after 400ms without updates.
            this.stopMotion();
            return false;
        } finally {
            queue.pending = false;
            if (queue.latest !== null) queue.flight = this.flushMotion(kind, queue);
        }
    }

    async stopMotion(force = false) {
        for (const stop of this.stopHandlers) stop();
        if (force && this.isCameraConnected) {
            this.queueMotion('rotate', {yaw: 0, pitch: 0});
            this.queueMotion('zoom', 0);
        }
        const results = await Promise.all([...this.motionQueues.values()].map(queue => queue.flight));
        return results.every(result => result !== false);
    }

    bindHold(id, kind, direction) {
        const button = document.getElementById(id);
        let timer = null;
        const stop = () => {
            if (timer === null) return;
            clearInterval(timer);
            timer = null;
            this.queueMotion(kind, 0);
        };
        this.stopHandlers.push(stop);
        button.addEventListener('pointerdown', event => {
            if (!this.isCameraConnected || timer !== null || event.button !== 0) return;
            event.preventDefault();
            button.setPointerCapture(event.pointerId);
            this.queueMotion(kind, direction);
            timer = setInterval(() => this.queueMotion(kind, direction), 50);
        });
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) button.addEventListener(name, stop);
    }

    setupJoystick() {
        const zone = document.getElementById('joystick-zone');
        const handle = document.getElementById('joystick-handle');
        let moveInterval = null;
        let lastVel = {yaw: 0, pitch: 0};
        let pointerId = null;

        const handleMove = (e) => {
            if (!this.joystickActive) return;
            
            if (e.pointerId !== pointerId) return;
            const rect = zone.getBoundingClientRect();
            let dx = e.clientX - rect.left - rect.width / 2;
            let dy = e.clientY - rect.top - rect.height / 2;
            
            const dist = Math.min(60, Math.sqrt(dx*dx + dy*dy));
            const angle = Math.atan2(dy, dx);
            
            const posX = Math.cos(angle) * dist;
            const posY = Math.sin(angle) * dist;
            
            handle.style.left = `calc(50% + ${posX}px)`;
            handle.style.top = `calc(50% + ${posY}px)`;
            
            // Normalize velocity to -100 to 100
            lastVel = {
                yaw: Math.round((posX / 60) * 100),
                pitch: Math.round(-(posY / 60) * 100)
            };
            if (dist < 5) lastVel = {yaw: 0, pitch: 0};
        };

        const startMove = (e) => {
            if (!this.isCameraConnected || this.joystickActive || e.button !== 0) return;
            e.preventDefault();
            pointerId = e.pointerId;
            zone.setPointerCapture(pointerId);
            this.joystickActive = true;
            handleMove(e);
            this.queueMotion('rotate', lastVel);
            moveInterval = setInterval(() => this.queueMotion('rotate', lastVel), 50);
        };

        const stopMove = () => {
            if (!this.joystickActive) return;
            this.joystickActive = false;
            clearInterval(moveInterval);
            handle.style.left = '50%';
            handle.style.top = '50%';
            lastVel = {yaw: 0, pitch: 0};
            this.queueMotion('rotate', lastVel);
            const releasedPointer = pointerId;
            pointerId = null;
            if (zone.hasPointerCapture(releasedPointer)) zone.releasePointerCapture(releasedPointer);
        };

        this.stopHandlers.push(stopMove);
        zone.addEventListener('pointerdown', startMove);
        zone.addEventListener('pointermove', handleMove);
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) zone.addEventListener(name, stopMove);
    }

    setupPointer() {
        // Point-and-drag control on the video: the server turns screen
        // positions into absolute gimbal angles (see web_ui/pointing.py).
        const video = document.getElementById('video-stream');
        const marker = document.getElementById('aim-marker');
        const toggle = document.getElementById('pointer-control-toggle');
        toggle.checked = store.get('pointerControl') !== 'false';
        const sync = () => video.classList.toggle('pointer-aim', toggle.checked);
        toggle.addEventListener('change', () => { store.set('pointerControl', String(toggle.checked)); sync(); });
        sync();
        video.draggable = false;
        video.addEventListener('dragstart', event => event.preventDefault());

        const clickAction = document.getElementById('click-action-select');
        clickAction.value = store.get('clickAction') === 'lock' ? 'lock' : 'aim';
        clickAction.addEventListener('change', () => store.set('clickAction', clickAction.value));
        document.getElementById('release-lock-btn').addEventListener('click', () => this.releaseLock());
        this.initLockDebug();
        document.getElementById('calibrate-loop-btn').addEventListener('click', () => this.calibrateLoop());
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && this.lockState !== 'idle' &&
                !document.getElementById('config-modal').classList.contains('active')) {
                this.releaseLock();
            }
        });

        const enabled = () => toggle.checked && this.isCameraConnected;
        const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
        // The image keeps its aspect ratio, so its box is exactly the picture.
        const locate = event => {
            const rect = video.getBoundingClientRect();
            return {
                x: clamp((event.clientX - rect.left) / rect.width - 0.5, -0.5, 0.5),
                y: clamp((event.clientY - rect.top) / rect.height - 0.5, -0.5, 0.5),
                aspect: video.naturalWidth && video.naturalHeight
                    ? video.naturalWidth / video.naturalHeight : rect.width / rect.height,
                px: event.clientX - rect.left, py: event.clientY - rect.top,
            };
        };
        let gestures = 0;
        const newGesture = () => `${Date.now()}-${++gestures}`;
        const showMarker = point => {
            const rect = video.getBoundingClientRect();
            const box = video.parentElement.getBoundingClientRect();
            marker.hidden = true;
            marker.style.left = `${rect.left - box.left + point.px}px`;
            marker.style.top = `${rect.top - box.top + point.py}px`;
            void marker.offsetWidth; // restart the animation
            marker.hidden = false;
        };

        let drag = null;
        video.addEventListener('pointerdown', event => {
            if (!enabled() || event.button !== 0 || drag) return;
            event.preventDefault();
            video.setPointerCapture(event.pointerId);
            drag = {pointerId: event.pointerId, start: locate(event), moved: false, gesture: newGesture()};
        });
        video.addEventListener('pointermove', event => {
            if (!drag || event.pointerId !== drag.pointerId) return;
            const point = locate(event);
            if (!drag.moved && Math.hypot(point.px - drag.start.px, point.py - drag.start.py) < 5) return;
            drag.moved = true;
            video.classList.add('dragging');
            // Keep the scene point grabbed at the start under the cursor.
            this.queueLook({
                anchor_x: drag.start.x, anchor_y: drag.start.y, to_x: point.x, to_y: point.y,
                aspect: point.aspect, gesture: drag.gesture,
            });
        });
        const endDrag = event => {
            if (!drag || event.pointerId !== drag.pointerId) return;
            const finished = drag;
            drag = null;
            video.classList.remove('dragging');
            if (event.type === 'pointerup' && !finished.moved) {
                showMarker(finished.start);
                if (clickAction.value === 'lock') this.lockAt(finished.start);
                else this.queueLook({anchor_x: finished.start.x, anchor_y: finished.start.y, aspect: finished.start.aspect});
            }
        };
        for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) video.addEventListener(name, endDrag);

        let wheel = null;
        video.addEventListener('wheel', event => {
            if (!enabled()) return;
            event.preventDefault();
            const point = locate(event);
            const now = performance.now();
            // Wheel ticks within 400 ms form one gesture anchored where it began.
            if (!wheel || now - wheel.time > 400) {
                wheel = {gesture: newGesture(), anchor: point, zoom: this.zoomLevel ?? 1};
            }
            wheel.time = now;
            const steps = -event.deltaY / (event.deltaMode === 1 ? 3 : 100);
            wheel.zoom = clamp(wheel.zoom * 1.15 ** steps, 1, this.zoomMax ?? 6);
            if (this.lockState !== 'idle') {
                // Zoom in place; the lock keeps the spot centred and survives the scale change.
                this.queueZoom(Math.round(wheel.zoom * 10) / 10);
                return;
            }
            this.queueLook({
                anchor_x: wheel.anchor.x, anchor_y: wheel.anchor.y, to_x: point.x, to_y: point.y,
                aspect: point.aspect, zoom: Math.round(wheel.zoom * 10) / 10, gesture: wheel.gesture,
            });
        }, {passive: false});
    }

    async lockAt(point) {
        try {
            const lock = await this.request('/api/track/lock', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({x: point.x, y: point.y})
            }, {}, 3000);
            this.renderLock(lock);
            this.notify('Locked on the spot. The gimbal will keep it centred.');
        } catch (e) {
            this.notify(`Lock failed: ${e.message}`, true);
        }
    }

    async releaseLock() {
        this.lockReleasing = true;
        try {
            this.renderLock(await this.request('/api/track/release', {method: 'POST'}, {}, 3000));
            this.notify('Point lock released.');
        } catch (e) {
            this.notify(`Release failed: ${e.message}`, true);
        } finally {
            this.lockReleasing = false;
        }
    }

    renderLock(lock) {
        const state = lock?.state ?? 'idle';
        const was = this.lockState;
        this.lockState = state;
        document.getElementById('release-lock-btn').hidden = state === 'idle';
        const badge = document.getElementById('lock-badge');
        badge.hidden = state === 'idle';
        badge.classList.toggle('searching', state === 'searching');
        if (state !== 'idle') {
            const [yaw, pitch] = lock.error_deg ?? [0, 0];
            const mode = lock.compensated ? (lock.control === 'angle' ? 'angle' : 'speed') : 'uncompensated';
            badge.textContent = lock.control === 'firmware'
                ? `LOCK ${lock.score.toFixed(2)} · SIYI firmware (experimental)`
                : state === 'locked'
                ? `LOCK ${lock.score.toFixed(2)} · ${mode} · off by ${yaw >= 0 ? '+' : ''}${yaw.toFixed(1)}° / ${pitch >= 0 ? '+' : ''}${pitch.toFixed(1)}°`
                : 'LOCK SEARCHING · holding still';
        }
        // The server drops a lock it can't recover or that manual control replaced.
        if (was !== 'idle' && state === 'idle' && !this.lockReleasing) {
            this.notify(lock.reason || 'Point lock ended: the spot was lost or another control took over.', true);
        }
    }

    queueZoom(zoom) {
        // Latest target wins; one zoom request in flight.
        const queue = this.zoomTarget;
        queue.latest = zoom;
        if (queue.pending) return;
        queue.pending = true;
        (async () => {
            try {
                while (queue.latest !== null) {
                    const value = queue.latest;
                    queue.latest = null;
                    const result = await this.request('/api/camera/zoom_to', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({zoom: value})
                    }, {}, 2000);
                    this.zoomLevel = result.zoom;
                }
            } catch (e) {
                queue.latest = null;
                this.notify(`Zoom failed: ${e.message}`, true);
            } finally {
                queue.pending = false;
            }
        })();
    }

    queueLook(body) {
        // Latest target wins; at most one request in flight and about 20 per second.
        this.look.latest = body;
        if (!this.look.pending) this.flushLook();
    }

    async flushLook() {
        const look = this.look;
        look.pending = true;
        try {
            while (look.latest) {
                const wait = 50 - (performance.now() - look.lastSent);
                if (wait > 0) await new Promise(resolve => setTimeout(resolve, wait));
                const body = look.latest;
                look.latest = null;
                look.lastSent = performance.now();
                try {
                    const result = await this.request('/api/gimbal/look', {
                        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
                    }, {}, 2000);
                    if (result.zoom != null) this.zoomLevel = result.zoom;
                    if (result.zoom_max != null) this.zoomMax = result.zoom_max;
                    if (result.limited && performance.now() - look.limitNotified > 3000) {
                        look.limitNotified = performance.now();
                        this.notify('That point is past the gimbal travel limit; the camera stopped at the limit.');
                    }
                } catch (e) {
                    look.latest = null;
                    this.notify(`Aim failed: ${e.message}`, true);
                }
            }
        } finally {
            look.pending = false;
        }
    }

    pointingFields() {
        return {
            hfov: document.getElementById('pointing-hfov-input'),
            delay: document.getElementById('pointing-delay-input'),
            yaw: document.getElementById('pointing-invert-yaw'),
            pitch: document.getElementById('pointing-invert-pitch'),
            control: document.getElementById('lock-control-select'),
            response: document.getElementById('lock-response-input'),
            speed: document.getElementById('lock-speed-input'),
            model: document.getElementById('lock-model-select'),
        };
    }

    showPointingConfig(config) {
        const fields = this.pointingFields();
        fields.hfov.value = config.hfov_deg;
        fields.delay.value = config.video_delay_ms;
        fields.yaw.checked = config.yaw_sign === -1;
        fields.pitch.checked = config.pitch_sign === -1;
        this.pointingConfig = config;
        fields.control.value = config.lock_control ?? 'angle';
        fields.response.value = config.lock_response ?? 1;
        fields.speed.value = config.lock_max_speed ?? 100;
        fields.control.onchange = () => this.showLockControlOptions();
        this.showLockControlOptions();
        const status = document.getElementById('calibration-status');
        if (config.calibrated) {
            const rate = v => `${Math.abs(v).toFixed(2)}`;
            status.textContent = `Measured: ${rate(config.deg_per_unit_yaw)} / ${rate(config.deg_per_unit_pitch)} °/s per speed unit (yaw / pitch), ` +
                `command delay ${Math.round(config.command_delay_ms)} ms` +
                (config.motor_tau_ms ? ` (${Math.round(config.motor_tau_ms)} ms of it motor lag)` : '') +
                `, video delay ${Math.round(config.frame_delay_ms)} ms, ` +
                `field of view ${config.hfov_deg}°.`;
        }
        fields.model.value = config.lock_model ?? 'local';
    }

    /** The debug overlay is a display choice: it applies at once and never releases a lock. */
    async setLockDebug(enabled) {
        store.set('lockDebug', enabled ? '1' : '0');
        try {
            await this.request('/api/track/debug', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled})
            });
        } catch { /* the toggle is resent when the server is reachable again */ }
    }

    initLockDebug() {
        const toggle = document.getElementById('lock-debug-toggle');
        toggle.checked = store.get('lockDebug') === '1';
        toggle.addEventListener('change', () => this.setLockDebug(toggle.checked));
        if (toggle.checked) this.setLockDebug(true);
    }

    showLockControlOptions() {
        const fields = this.pointingFields();
        const firmware = fields.control.value === 'firmware';
        fields.speed.disabled = firmware;
        document.getElementById('firmware-lock-hint').hidden = !firmware;
    }

    async loadPointingConfig() {
        // The server forgets settings on restart, so this browser keeps a copy.
        try {
            const saved = JSON.parse(store.get('pointingConfig') || 'null');
            const send = body => this.request('/api/pointing/config', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
            });
            let config;
            if (!saved) {
                config = await this.request('/api/pointing/config');
            } else {
                try {
                    config = await send(saved);
                } catch (e) {
                    // A failed measurement can leave out-of-range values, and then the server
                    // rejects everything (lock steering included). Keep the choices, drop the measurement.
                    const {deg_per_unit_yaw, deg_per_unit_pitch, command_delay_ms, frame_delay_ms,
                        motor_tau_ms, calibrated, ...choices} = saved;
                    config = await send(choices);
                    store.set('pointingConfig', JSON.stringify(config));
                    this.notify(`Saved loop timing was invalid (${e.message}) and was cleared; your other ` +
                        'settings were kept. Measure loop timing again if you need it.', true);
                }
            }
            this.showPointingConfig(config);
        } catch (e) {
            console.warn('Pointing settings unavailable', e);
        }
    }

    async calibrateLoop() {
        const confirmed = await confirmAction(
            'The gimbal will turn about 20° right and back, then 20° up and back, over about 8 seconds. ' +
            'Point the camera at a textured, still scene and keep the aircraft steady.',
            {title: 'Measure loop timing', confirmLabel: 'Measure'});
        if (!confirmed) return;
        const button = document.getElementById('calibrate-loop-btn');
        const status = document.getElementById('calibration-status');
        button.disabled = true;
        status.textContent = 'Measuring… keep the camera steady.';
        try {
            const result = await this.request('/api/track/calibrate', {method: 'POST'}, {}, 30000);
            store.set('pointingConfig', JSON.stringify(result.config));
            this.showPointingConfig(result.config);
            if (result.notes.length) status.textContent += ` Warning: ${result.notes.join(' ')}`;
            this.notify('Loop timing measured and saved.');
        } catch (e) {
            status.textContent = `Measurement failed: ${e.message}`;
            this.notify(`Measurement failed: ${e.message}`, true);
        } finally {
            button.disabled = false;
        }
    }

    async savePointingConfig() {
        const fields = this.pointingFields();
        const config = await this.request('/api/pointing/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                // Keep measured values (turn rate, delays) that have no input of their own.
                ...(this.pointingConfig ?? {}),
                hfov_deg: Number(fields.hfov.value), video_delay_ms: Number(fields.delay.value),
                yaw_sign: fields.yaw.checked ? -1 : 1, pitch_sign: fields.pitch.checked ? -1 : 1,
                lock_control: fields.control.value, lock_response: Number(fields.response.value),
                lock_max_speed: Math.round(Number(fields.speed.value)), lock_model: fields.model.value,
            })
        });
        store.set('pointingConfig', JSON.stringify(config));
        this.showPointingConfig(config);
    }

    connectWS() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${protocol}//${window.location.host}/ws/attitude`);
        this.ws.onopen = () => {
            // A restarted server is back on defaults; send this browser's saved settings again.
            if (this.wsOpened) {
                this.loadPointingConfig();
                if (document.getElementById('lock-debug-toggle').checked) this.setLockDebug(true);
            }
            this.wsOpened = true;
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const yawEl = document.getElementById('att-yaw');
                const pitchEl = document.getElementById('att-pitch');
                const rollEl = document.getElementById('att-roll');
                const freshAttitude = this.isCameraConnected && data.latency?.attitude_age_ms != null && data.latency.attitude_age_ms < 1000;
                if (yawEl) yawEl.innerText = freshAttitude ? data.yaw.toFixed(1) : '—';
                if (pitchEl) pitchEl.innerText = freshAttitude ? data.pitch.toFixed(1) : '—';
                if (rollEl) rollEl.innerText = freshAttitude ? data.roll.toFixed(1) : '—';
                for (const axis of ['yaw', 'pitch', 'roll']) {
                    document.getElementById(`plot-${axis}`).textContent = freshAttitude ? `${data[axis].toFixed(1)}°` : '—';
                }
                if (freshAttitude) this.attitudePlot.push({yaw: data.yaw, pitch: data.pitch, roll: data.roll});
                // RTT arrives with every 10 Hz message but only changes about once a second.
                const rttKey = `${data.latency?.queries}:${data.latency?.camera_rtt_ms}`;
                if (data.latency?.queries && rttKey !== this.lastRttSample) {
                    this.lastRttSample = rttKey;
                    this.rttPlot.push({rtt: data.latency.camera_rtt_ms});
                }
                if (data.zoom != null) this.zoomLevel = data.zoom;
                if (data.zoom_max != null) this.zoomMax = data.zoom_max;
                this.renderLock(data.lock);
                this.renderCameraState(data);
                window.dispatchEvent(new CustomEvent('siyi-telemetry', {detail: data}));
            } catch (e) {
                console.error("WS Message Error", e);
            }
        };

        this.ws.onclose = () => {
            this.renderCameraState(null);
            setTimeout(() => this.connectWS(), 2000);
        };
    }

    async loadSystemInfo() {
        // Fetch encoding info
        const encData = await this.get('/api/camera/encoding');
        if (encData && !encData.detail) {
            const resEl = document.getElementById('info-resolution');
            const bitEl = document.getElementById('info-bitrate');
            const fpsEl = document.getElementById('info-fps');
            if (resEl) resEl.innerText = encData.resolution || "---";
            if (bitEl) bitEl.innerText = `${encData.bitrate_kbps} kbps`;
            if (fpsEl) fpsEl.innerText = encData.frame_rate || "---";
            const recEl = document.getElementById('info-rec-resolution');
            if (recEl) recEl.innerText = encData.recording?.resolution || "---";
        }

        // Fetch hardware and firmware info
        const sysInfo = await this.get('/api/system/info');
        if (sysInfo && !sysInfo.detail) {
            const camTypeEl = document.getElementById('info-camera-type');
            const camFwEl = document.getElementById('info-camera-fw');
            const gimFwEl = document.getElementById('info-gimbal-fw');
            if (camTypeEl) camTypeEl.innerText = sysInfo.camera_type || "---";
            if (camFwEl) camFwEl.innerText = sysInfo.camera_fw || "---";
            if (gimFwEl) gimFwEl.innerText = sysInfo.gimbal_fw || "---";
        }
    }

    mediaMessage(text) {
        const message = document.createElement('p');
        message.className = 'media-message';
        message.textContent = text;
        document.getElementById('media-grid').replaceChildren(message);
    }

    mediaCard(iconClass, name, onClick = null, downloadUrl = null) {
        const card = document.createElement(onClick ? 'button' : 'div');
        card.className = 'media-card';
        if (onClick) {
            card.type = 'button';
            card.onclick = onClick;
        }
        const thumb = document.createElement('div');
        thumb.className = 'media-thumb';
        const icon = document.createElement('i');
        icon.className = `fas ${iconClass} fa-2x`;
        thumb.append(icon);
        const info = document.createElement('div');
        info.className = 'media-info';
        const label = document.createElement('div');
        label.className = 'media-name';
        label.textContent = name;
        label.title = name;
        info.append(label);
        if (downloadUrl) {
            const link = document.createElement('a');
            link.className = 'btn btn-sm btn-primary btn-block media-download';
            link.href = `/api/media/download?url=${encodeURIComponent(downloadUrl)}`;
            link.target = '_blank';
            link.textContent = 'Download';
            info.append(link);
        }
        card.append(thumb, info);
        return card;
    }

    async loadMedia() {
        const grid = document.getElementById('media-grid');
        if (!this.isCameraConnected) {
            this.mediaMessage('Connect the camera to browse media.');
            return;
        }
        try {
            this.mediaMessage('Loading…');
            document.getElementById('media-path-breadcrumb').textContent = this.currentMediaMode === 0 ? "/root/photo" : "/root/video";
            const dirs = await this.get('/api/media/directories', {type: this.currentMediaMode});
            if (!this.isCameraConnected) return;
            if (!Array.isArray(dirs)) throw new Error('Invalid media response');
            if (dirs.length === 0) {
                this.mediaMessage('No media found.');
                return;
            }
            grid.replaceChildren(...dirs.map(dir => this.mediaCard('fa-folder', dir.name, () => this.openDirectory(dir.path))));
        } catch (e) {
            this.showMediaError(e);
        }
    }

    showMediaError(error) {
        this.mediaMessage(`Unable to load media: ${error.message} Use Refresh to retry.`);
    }

    async openDirectory(path) {
        if (!this.isCameraConnected) return;
        try {
            this.currentPath = path;
            document.getElementById('media-path-breadcrumb').textContent = path;
            this.mediaMessage('Loading…');
            const files = await this.get('/api/media/files', {path, type: this.currentMediaMode});
            if (!this.isCameraConnected) return;
            if (!Array.isArray(files)) throw new Error('Invalid media response');
            const icon = this.currentMediaMode === 0 ? "fa-file-image" : "fa-file-video";
            document.getElementById('media-grid').replaceChildren(
                this.mediaCard('fa-arrow-left', 'Back', () => this.loadMedia()),
                ...files.map(file => this.mediaCard(icon, file.name, null, file.url)));
        } catch (e) {
            this.showMediaError(e);
        }
    }
}

window.onload = () => new SiyiApp();
