/** Live TX/RX frame log fed by the server's transport tap. */
window.renderFrames = function renderFrames(container, frames) {
    container.replaceChildren();
    if (!frames.length) {
        const empty = document.createElement('p');
        empty.className = 'control-hint';
        empty.textContent = 'No frames.';
        container.append(empty);
        return;
    }
    const fragment = document.createDocumentFragment();
    for (const frame of frames) {
        const row = document.createElement('div');
        row.className = `frame-row frame-${frame.dir}`;
        const time = document.createElement('span');
        time.className = 'frame-time';
        const date = new Date(frame.t * 1000);
        time.textContent = `${date.toLocaleTimeString([], {hour12: false})}.${String(date.getMilliseconds()).padStart(3, '0')}`;
        const dir = document.createElement('span');
        dir.className = 'frame-dir';
        dir.textContent = frame.dir.toUpperCase();
        const cmd = document.createElement('span');
        cmd.className = 'frame-cmd';
        cmd.textContent = frame.error ? 'parse error'
            : `${frame.cmd} (0x${frame.cmd_id.toString(16).padStart(2, '0').toUpperCase()}) seq ${frame.seq}`;
        const hex = document.createElement('code');
        hex.className = 'frame-hex';
        if (frame.error) {
            hex.textContent = frame.error;
        } else {
            // Header is 8 bytes, CRC is the final 2; highlight the payload between.
            const bytes = frame.hex.split(' ');
            const header = document.createElement('span');
            header.className = 'hex-header';
            header.textContent = bytes.slice(0, 8).join(' ');
            const payload = document.createElement('span');
            payload.className = 'hex-payload';
            payload.textContent = bytes.slice(8, -2).join(' ');
            const crc = document.createElement('span');
            crc.className = 'hex-crc';
            crc.textContent = bytes.slice(-2).join(' ');
            hex.append(header, ' ', payload, payload.textContent ? ' ' : '', crc);
        }
        row.append(time, dir, cmd, hex);
        fragment.append(row);
    }
    container.append(fragment);
};

window.addEventListener('load', () => {
    const list = document.getElementById('log-frames');
    const status = document.getElementById('log-status');
    const dirFilter = document.getElementById('log-dir');
    const cmdFilter = document.getElementById('log-cmd');
    const hidePush = document.getElementById('log-hide-push');
    const pauseBtn = document.getElementById('log-pause');
    const maxFrames = 5000;  // about 90 s of the 50 Hz attitude stream
    let frames = [];
    let lastId = 0;
    let paused = false;
    let fetching = false;

    function visible() {
        const dir = dirFilter.value;
        const query = cmdFilter.value.trim().toLowerCase();
        return frames.filter(frame =>
            (!dir || frame.dir === dir) &&
            !(hidePush.checked && frame.push) &&
            (!query || (frame.cmd || '').toLowerCase().includes(query) ||
                (frame.cmd_id != null && `0x${frame.cmd_id.toString(16).padStart(2, '0')}` === query)));
    }

    function render() {
        const shown = visible();
        const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
        window.renderFrames(list, shown.slice(-300));
        if (atBottom) list.scrollTop = list.scrollHeight;
        status.textContent = `${shown.length} shown of ${frames.length} captured${paused ? ' · paused' : ''}.`;
    }

    async function fetchFrames() {
        if (paused || fetching) return;
        fetching = true;
        try {
            const response = await fetch(`/api/debug/frames?since=${lastId}`, {cache: 'no-store'});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            // The server restarts its counter after reconnecting to a new IP.
            if (data.last_id < lastId) { lastId = 0; return; }
            lastId = data.last_id;
            if (data.frames.length) {
                frames = frames.concat(data.frames).slice(-maxFrames);
                if (!document.getElementById('panel-protocol').hidden) render();
            }
        } catch (error) {
            status.textContent = `Protocol log unavailable: ${error.message}`;
        } finally {
            fetching = false;
        }
    }

    window.addEventListener('siyi-telemetry', event => {
        if (event.detail.frames_last_id !== lastId) fetchFrames();
    });
    window.addEventListener('siyi-tab', event => { if (event.detail === 'protocol') render(); });
    for (const el of [dirFilter, cmdFilter, hidePush]) el.addEventListener('input', render);
    pauseBtn.addEventListener('click', () => {
        paused = !paused;
        pauseBtn.textContent = paused ? 'Resume' : 'Pause';
        pauseBtn.classList.toggle('active', paused);
        if (!paused) fetchFrames();
        render();
    });
    document.getElementById('log-clear').addEventListener('click', () => { frames = []; render(); });
    document.getElementById('log-export').addEventListener('click', async () => {
        // The SDK frames plus the firmware tracking channel and lock events, on one clock.
        const fetchJSON = async url => {
            try {
                const response = await fetch(url, {cache: 'no-store'});
                return response.ok ? await response.json() : {error: `HTTP ${response.status}`};
            } catch (error) {
                return {error: error.message};
            }
        };
        const [tracking, settings] = await Promise.all([
            fetchJSON('/api/debug/tracking'), fetchJSON('/api/pointing/config'),
        ]);
        window.downloadJSON(`siyi-frames-${Date.now()}.json`, {
            exported_at: Date.now() / 1000, settings, frames, tracking,
        });
    });
});
