/**
 * SIYI SDK Web UI Frontend Logic
 */

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
        this.theme = localStorage.getItem('theme') || 'dark';

        this.init();
    }

    async init() {
        console.log("SiyiApp: Initializing...");
        try {
            this.initTheme();
            this.liveViewEnabled = localStorage.getItem('liveViewEnabled') !== 'false';
            document.getElementById('config-live-view-toggle').checked = this.liveViewEnabled;
            
            console.log("SiyiApp: Binding events...");
            this.bindEvents();
            
            console.log("SiyiApp: Setting up joystick...");
            this.setupJoystick();
            
            console.log("SiyiApp: Connecting WebSocket...");
            this.connectWS();
            
            // Start proactive connection monitoring
            console.log("SiyiApp: Starting connection monitor...");
            setInterval(() => this.checkConnection(), 3000);
            setInterval(() => {
                if (performance.now() - this.lastStatusReceived > 2500) this.renderCameraState(null);
            }, 500);
            
            // Initial load
            this.renderConnection('checking');
            this.checkConnection();
            console.log("SiyiApp: Initialization complete.");
        } catch (e) {
            console.error("SiyiApp: Initialization crashed!", e);
        }
    }

    initTheme() {
        console.log(`initTheme: Applying '${this.theme}' mode`);
        this.applyTheme();
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
        localStorage.setItem('theme', this.theme);
    }

    toggleTheme() {
        this.theme = this.theme === 'dark' ? 'light' : 'dark';
        this.applyTheme();
    }

    async restoreUI() {
        if (!this.isCameraConnected) {
            console.log("restoreUI skipped: Camera not connected");
            return;
        }
        
        console.log("Restoring UI components...");
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
            'focus-near-btn', 'focus-far-btn', 'reboot-camera-btn', 'reboot-gimbal-btn', 'format-sd-btn']) {
            document.getElementById(id).disabled = !connected;
        }
        document.getElementById('config-res-select').disabled = !connected;
        document.getElementById('joystick-zone').setAttribute('aria-disabled', String(!connected));
        if (!connected) {
            this.stopMotion();
            this.renderCameraState(null);
            document.getElementById('video-stream').parentElement.style.display = 'flex';
            document.getElementById('video-stream').removeAttribute('src');
            document.getElementById('video-stream').hidden = true;
            document.getElementById('video-status').hidden = true;
            document.getElementById('media-grid').innerHTML = '<p class="media-message">Connect the camera to browse media.</p>';
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
        localStorage.setItem('liveViewEnabled', this.liveViewEnabled);
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
            if (await this.stopMotion(true)) this.notify('Stop commands sent for gimbal, zoom and focus.');
        };
        this.bindHold('zoom-in-btn', 'zoom', 1);
        this.bindHold('zoom-out-btn', 'zoom', -1);
        this.bindHold('focus-near-btn', 'focus', 1);
        this.bindHold('focus-far-btn', 'focus', -1);
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
    }

    async reboot(camera, gimbal) {
        const target = camera && gimbal ? "Camera & Gimbal" : (camera ? "Camera" : "Gimbal");
        const confirmed = confirm(`Are you sure you want to SOFT REBOOT the ${target}? This will temporarily interrupt connectivity.`);
        if (!confirmed) return;

        try {
            const res = await this.post('/api/system/reboot', {camera, gimbal});
            if (res?.status === 'ok') {
                alert(`${target} reboot command sent successfully.`);
                if (camera) {
                    this.isRebooting = true;
                    this.isCameraConnected = false;
                    this.renderConnection('rebooting');
                    this.updateLiveView(); // Stop stream locally
                }
            } else {
                throw new Error("Reboot command failed");
            }
        } catch (e) {
            console.error("Reboot failure", e);
            alert("Error: " + e.message);
        }
    }

    async formatSD() {
        const confirmed = confirm("WARNING: This will permanently erase ALL photos and videos on the SD card. This action cannot be undone. Are you sure you want to proceed?");
        if (!confirmed) return;

        try {
            const res = await this.post('/api/storage/format');
            if (res?.status === 'ok') {
                alert("SD card formatted successfully!");
                this.loadMedia(); // Refresh to show empty state
            } else {
                throw new Error("Format failed");
            }
        } catch (e) {
            console.error("Format failure", e);
            alert("Error: " + e.message);
        }
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
            if (options.method === 'POST' && /^\/api\/(gimbal|camera)\//.test(url)) {
                document.getElementById('latency-browser').textContent = `${Math.round(performance.now() - started)} ms`;
                const timing = /app;dur=([\d.]+)/.exec(resp.headers.get('Server-Timing') || '');
                document.getElementById('latency-server').textContent = timing ? `${Number(timing[1]).toFixed(1)} ms` : '—';
                const names = {
                    '/api/gimbal/rotate': 'Gimbal velocity', '/api/gimbal/center': 'Center',
                    '/api/gimbal/mode': 'Gimbal mode', '/api/camera/record': 'Recording toggle',
                    '/api/camera/photo': 'Photo', '/api/camera/zoom': 'Zoom velocity',
                    '/api/camera/focus': 'Focus velocity', '/api/camera/encoding': 'Encoding settings'
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

            if (resValue && this.isCameraConnected && !ipChanged) {
                const res = await this.request('/api/camera/encoding', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({resolution: resValue})
                });
                if (res?.status !== 'ok') {
                    throw new Error(res?.detail || "Failed to set encoding");
                }
            }

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
        document.getElementById('hdr-status').textContent = camera ? (camera.hdr ? 'On' : 'Off') : 'Unknown';
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
        document.getElementById('recording-badge').style.display = recording ? 'block' : 'none';
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
            this.queueMotion('focus', 0);
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

    connectWS() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${protocol}//${window.location.host}/ws/attitude`);
        
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
                this.renderCameraState(data);
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
        console.log("loadSystemInfo: Fetching info...");
        // Fetch encoding info
        const encData = await this.get('/api/camera/encoding');
        if (encData && !encData.detail) {
            const resEl = document.getElementById('info-resolution');
            const bitEl = document.getElementById('info-bitrate');
            const fpsEl = document.getElementById('info-fps');
            if (resEl) resEl.innerText = encData.resolution || "---";
            if (bitEl) bitEl.innerText = `${encData.bitrate_kbps} kbps`;
            if (fpsEl) fpsEl.innerText = encData.frame_rate || "---";
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

    async loadMedia() {
        const grid = document.getElementById('media-grid');
        if (!this.isCameraConnected) {
            grid.innerHTML = '<p class="media-message">Connect the camera to browse media.</p>';
            return;
        }
        try {
        grid.innerHTML = '<div class="media-thumb"><i class="fas fa-spinner fa-spin"></i></div>';
        document.getElementById('media-path-breadcrumb').innerText = this.currentMediaMode === 0 ? "/root/photo" : "/root/video";
        
        // Load directories
        const dirs = await this.get('/api/media/directories', {type: this.currentMediaMode});
        if (!this.isCameraConnected) return;
        if (!Array.isArray(dirs)) throw new Error('Invalid media response');
        if (!dirs || dirs.length === 0) {
            grid.innerHTML = '<div class="overlay-card" style="grid-column: 1/-1; text-align: center;">No media found</div>';
            return;
        }

        grid.innerHTML = '';
        for(const dir of dirs) {
            const el = document.createElement('div');
            el.className = 'media-card';
            el.innerHTML = `
                <div class="media-thumb"><i class="fas fa-folder fa-2x"></i></div>
                <div class="media-info">
                    <div class="media-name">${dir.name}</div>
                </div>
            `;
            el.onclick = () => this.openDirectory(dir.path);
            grid.appendChild(el);
        }
        } catch (e) {
            this.showMediaError(e);
        }
    }

    showMediaError(error) {
        const grid = document.getElementById('media-grid');
        grid.innerHTML = '';
        const message = document.createElement('p');
        message.className = 'media-message';
        message.innerText = `Unable to load media: ${error.message} Use Refresh to retry.`;
        grid.appendChild(message);
    }

    async openDirectory(path) {
        if (!this.isCameraConnected) return;
        try {
        this.currentPath = path;
        document.getElementById('media-path-breadcrumb').innerText = path;
        const grid = document.getElementById('media-grid');
        grid.innerHTML = '<div class="media-thumb"><i class="fas fa-spinner fa-spin"></i></div>';

        const files = await this.get('/api/media/files', {path, type: this.currentMediaMode});
        if (!this.isCameraConnected) return;
        if (!Array.isArray(files)) throw new Error('Invalid media response');
        grid.innerHTML = '';
        
        // Add back button
        const back = document.createElement('div');
        back.className = 'media-card';
        back.innerHTML = `
            <div class="media-thumb"><i class="fas fa-arrow-left fa-2x"></i></div>
            <div class="media-info"><div class="media-name">Back</div></div>
        `;
        back.onclick = () => this.loadMedia();
        grid.appendChild(back);

        if (!files) return;

        const icon = this.currentMediaMode === 0 ? "fa-file-image" : "fa-file-video";

        for(const file of files) {
            const el = document.createElement('div');
            el.className = 'media-card';
            el.innerHTML = `
                <div class="media-thumb"><i class="fas ${icon} fa-2x"></i></div>
                <div class="media-info">
                    <div class="media-name">${file.name}</div>
                    <a href="/api/media/download?url=${encodeURIComponent(file.url)}" target="_blank" class="btn btn-sm btn-primary" style="margin-top: 10px; width: 100%;">Download</a>
                </div>
            `;
            grid.appendChild(el);
        }
        } catch (e) {
            this.showMediaError(e);
        }
    }
}

window.onload = () => new SiyiApp();
