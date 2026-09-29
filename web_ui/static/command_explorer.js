/** Typed forms generated from the server's fixed A8 Mini command catalog. */
window.addEventListener('load', async () => {
    const searchInput = document.getElementById('explorer-search');
    const groupSelect = document.getElementById('explorer-group');
    const commandSelect = document.getElementById('explorer-command');
    const description = document.getElementById('explorer-description');
    const docBody = document.getElementById('explorer-doc-body');
    const form = document.getElementById('explorer-form');
    const fields = document.getElementById('explorer-fields');
    const sendButton = document.getElementById('explorer-send');
    const result = document.getElementById('explorer-result');
    const resultMeta = document.getElementById('explorer-result-meta');
    const resultFrames = document.getElementById('explorer-frames');
    const historyBody = document.getElementById('explorer-history');
    const historyEmpty = document.getElementById('history-empty');
    const repeatBox = document.getElementById('explorer-repeat');
    const repeatStart = document.getElementById('repeat-start');
    const repeatSummary = document.getElementById('repeat-summary');
    const feedback = document.getElementById('explorer-feedback');
    const ALL_GROUPS = '';
    const HISTORY_LIMIT = 50;
    let catalog = [];
    let history = [];
    let shownEntry = null;
    let repeatRun = null;

    function resolve(schema, root) {
        if (schema.$ref) return resolve(root.$defs[schema.$ref.split('/').pop()], root);
        if (schema.allOf) return resolve(schema.allOf[0], root);
        if (schema.anyOf) return resolve(schema.anyOf.find(item => item.type !== 'null'), root);
        return schema;
    }

    function field(name, raw, root, required, prefix = '') {
        const schema = resolve(raw, root);
        const path = prefix ? `${prefix}.${name}` : name;
        if (schema.type === 'object' && schema.properties) {
            const box = document.createElement('fieldset');
            const legend = document.createElement('legend');
            legend.textContent = name;
            box.append(legend);
            for (const [childName, childSchema] of Object.entries(schema.properties)) {
                box.append(field(childName, childSchema, root, (schema.required || []).includes(childName), path));
            }
            return box;
        }
        // Defaults may live on the wrapper (anyOf/allOf) rather than the resolved schema.
        const fallback = raw.default ?? schema.default;
        const minimum = schema.minimum ?? schema.exclusiveMinimum;
        const maximum = schema.maximum ?? schema.exclusiveMaximum;
        const wrapper = document.createElement('label');
        wrapper.className = 'explorer-field';
        const title = document.createElement('span');
        title.textContent = `${name}${required ? ' *' : ''}`;
        if (minimum != null || maximum != null) {
            const range = document.createElement('small');
            range.textContent = ` ${minimum ?? '−∞'} … ${maximum ?? '∞'}`;
            title.append(range);
        }
        wrapper.append(title);
        let input;
        if (schema.enum) {
            input = document.createElement('select');
            if (!required) input.append(new Option('(omit)', ''));
            for (let i = 0; i < schema.enum.length; i++) {
                const value = schema.enum[i];
                const label = schema['x-enumNames']?.[i] || String(value);
                input.append(new Option(`${label} (${value})`, JSON.stringify(value)));
            }
            if (fallback != null) input.dataset.default = JSON.stringify(fallback);
        } else {
            input = document.createElement('input');
            if (schema.type === 'boolean') {
                input.type = 'checkbox';
                input.dataset.default = String(fallback === true);
            } else if (schema.type === 'integer' || schema.type === 'number') {
                input.type = 'number';
                input.step = schema.type === 'integer' ? '1' : 'any';
                if (minimum != null) input.min = minimum;
                if (maximum != null) input.max = maximum;
            } else {
                input.type = 'text';
            }
            if (fallback != null && input.type !== 'checkbox') {
                input.dataset.default = String(fallback);
                input.placeholder = `default ${fallback}`;
            }
        }
        input.dataset.path = path;
        input.dataset.kind = schema.type || (schema.enum ? typeof schema.enum[0] : 'string');
        input.required = required && input.type !== 'checkbox';
        wrapper.append(input);
        return wrapper;
    }

    function resetDefaults() {
        for (const input of fields.querySelectorAll('[data-path]')) {
            const fallback = input.dataset.default;
            if (input.type === 'checkbox') input.checked = fallback === 'true';
            else if (input.tagName === 'SELECT') input.value = fallback ?? input.options[0]?.value ?? '';
            else input.value = fallback ?? '';
        }
    }

    function selected() { return catalog.find(item => item.name === commandSelect.value); }

    function showCommand() {
        const command = selected();
        fields.replaceChildren();
        sendButton.disabled = !command;
        if (!command) {
            description.textContent = 'No matching commands.';
            docBody.textContent = '';
            return;
        }
        const [summary, ...rest] = command.description.split('\n');
        description.textContent = summary || command.name;
        docBody.textContent = rest.join('\n').trim() || 'No further documentation.';
        for (const [name, schema] of Object.entries(command.schema.properties || {})) {
            fields.append(field(name, schema, command.schema, (command.schema.required || []).includes(name)));
        }
        if (!fields.children.length) {
            const none = document.createElement('p');
            none.className = 'control-hint';
            none.textContent = 'This command takes no arguments.';
            fields.append(none);
        }
        resetDefaults();
        repeatBox.disabled = !command.read_only || repeatRun !== null;
        repeatBox.title = command.read_only ? '' : 'Repeat is only available for read-only get_ commands.';
    }

    function showCommands() {
        const query = searchInput.value.trim().toLowerCase().replaceAll(' ', '_');
        const previous = commandSelect.value;
        const matches = catalog.filter(item =>
            (groupSelect.value === ALL_GROUPS || item.group === groupSelect.value) &&
            (!query || item.name.includes(query) || item.description.toLowerCase().includes(query)));
        commandSelect.replaceChildren();
        for (const group of [...new Set(matches.map(item => item.group))]) {
            const optgroup = document.createElement('optgroup');
            optgroup.label = group;
            for (const command of matches.filter(item => item.group === group)) {
                optgroup.append(new Option(command.name.replaceAll('_', ' '), command.name));
            }
            commandSelect.append(optgroup);
        }
        if (matches.some(item => item.name === previous)) commandSelect.value = previous;
        showCommand();
    }

    function collectArgs() {
        const args = {};
        for (const input of fields.querySelectorAll('[data-path]')) {
            if (input.type !== 'checkbox' && input.value === '') continue;
            const path = input.dataset.path.split('.');
            let target = args;
            for (const part of path.slice(0, -1)) target = target[part] ||= {};
            let value;
            if (input.type === 'checkbox') value = input.checked;
            else if (input.tagName === 'SELECT') value = JSON.parse(input.value);
            else if (input.dataset.kind === 'integer' || input.dataset.kind === 'number') value = Number(input.value);
            else value = input.value;
            target[path.at(-1)] = value;
        }
        return args;
    }

    function applyArgs(args) {
        resetDefaults();
        for (const input of fields.querySelectorAll('[data-path]')) {
            const value = input.dataset.path.split('.').reduce((node, key) => node?.[key], args);
            if (value === undefined) continue;
            if (input.type === 'checkbox') input.checked = Boolean(value);
            else if (input.tagName === 'SELECT') input.value = JSON.stringify(value);
            else input.value = value;
        }
    }

    async function run(command, args, confirmed) {
        const started = performance.now();
        const entry = {time: Date.now(), command: command.name, args, status: 'error', ms: null, result: null, error: null, frames: []};
        try {
            const response = await fetch(`/api/sdk/commands/${encodeURIComponent(command.name)}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({args, confirm: confirmed})
            });
            const payload = await response.json();
            const detail = payload.detail;
            if (!response.ok) {
                if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
                    Object.assign(entry, {status: detail.status || 'error', error: detail.error, ms: detail.elapsed_ms, frames: detail.frames || []});
                } else {
                    entry.error = Array.isArray(detail) ? detail.map(item => `${item.loc?.slice(1).join('.')}: ${item.msg}`).join('; ') : String(detail ?? `HTTP ${response.status}`);
                    entry.status = response.status === 422 ? 'invalid' : 'error';
                }
            } else {
                Object.assign(entry, {status: 'ok', result: payload.result, ms: payload.elapsed_ms, frames: payload.frames || []});
            }
        } catch (error) {
            entry.error = error.message;
        }
        entry.roundtrip_ms = Math.round(performance.now() - started);
        history.unshift(entry);
        history.length = Math.min(history.length, HISTORY_LIMIT);
        renderHistory();
        return entry;
    }

    function showEntry(entry) {
        shownEntry = entry;
        const when = new Date(entry.time).toLocaleTimeString();
        resultMeta.textContent = `${entry.command} · ${entry.status} · ${entry.ms ?? '—'} ms server / ${entry.roundtrip_ms} ms browser · ${when}`;
        resultMeta.className = `control-hint status-${entry.status}`;
        result.textContent = entry.status === 'ok'
            ? (JSON.stringify(entry.result, null, 2) ?? 'Command sent (no reply payload).')
            : `Command failed: ${entry.error}`;
        window.renderFrames(resultFrames, entry.frames);
        for (const row of historyBody.children) row.classList.toggle('selected', row.entry === entry);
    }

    function renderHistory() {
        historyEmpty.hidden = history.length > 0;
        historyBody.replaceChildren(...history.map(entry => {
            const row = document.createElement('tr');
            row.entry = entry;
            row.tabIndex = 0;
            row.classList.toggle('selected', entry === shownEntry);
            const cells = [new Date(entry.time).toLocaleTimeString([], {hour12: false}), entry.command, entry.status, entry.ms ?? '—'];
            for (const value of cells) {
                const cell = document.createElement('td');
                cell.textContent = value;
                row.append(cell);
            }
            row.cells[2].className = `status-${entry.status}`;
            const open = () => {
                const command = catalog.find(item => item.name === entry.command);
                if (command) {
                    searchInput.value = '';
                    groupSelect.value = ALL_GROUPS;
                    showCommands();
                    commandSelect.value = command.name;
                    showCommand();
                    applyArgs(entry.args);
                }
                showEntry(entry);
            };
            row.addEventListener('click', open);
            row.addEventListener('keydown', event => { if (event.key === 'Enter') open(); });
            return row;
        }));
    }

    form.addEventListener('submit', async event => {
        event.preventDefault();
        const command = selected();
        if (!command) return;
        const args = collectArgs();
        const confirmed = command.confirmation
            ? await window.confirmAction(command.confirmation, {title: command.name.replaceAll('_', ' '), danger: true,
                typeToConfirm: command.name === 'format_sd_card' ? 'FORMAT' : null})
            : false;
        if (command.confirmation && !confirmed) return;
        sendButton.disabled = true;
        result.textContent = 'Waiting for camera…';
        try {
            showEntry(await run(command, args, confirmed));
        } finally {
            sendButton.disabled = false;
        }
    });

    function percentile(sorted, fraction) {
        return sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] : null;
    }

    repeatStart.addEventListener('click', async () => {
        if (repeatRun) { repeatRun.cancelled = true; return; }
        const command = selected();
        if (!command?.read_only || !form.reportValidity()) return;
        const count = Math.max(1, Math.min(1000, Number(document.getElementById('repeat-count').value) || 1));
        const interval = Math.max(0, Number(document.getElementById('repeat-interval').value) || 0);
        const args = collectArgs();
        repeatRun = {cancelled: false};
        repeatStart.textContent = 'Stop';
        sendButton.disabled = true;
        const times = [];
        let failures = 0, done = 0;
        const summarize = () => {
            const sorted = [...times].sort((a, b) => a - b);
            repeatSummary.textContent = `${done}/${count} runs · ${failures} failed (${done ? Math.round(100 * failures / done) : 0}%) · ` +
                `p50 ${percentile(sorted, 0.5) ?? '—'} ms · p95 ${percentile(sorted, 0.95) ?? '—'} ms · max ${sorted.at(-1) ?? '—'} ms`;
        };
        try {
            for (let i = 0; i < count && !repeatRun.cancelled; i++) {
                const entry = await run(command, args, false);
                done++;
                if (entry.status === 'ok' && entry.ms != null) times.push(entry.ms);
                else failures++;
                showEntry(entry);
                summarize();
                if (i < count - 1 && interval) await new Promise(r => setTimeout(r, interval));
            }
        } finally {
            repeatSummary.textContent += repeatRun.cancelled ? ' · stopped' : ' · done';
            repeatRun = null;
            repeatStart.textContent = 'Start repeat';
            sendButton.disabled = false;
        }
    });

    document.getElementById('explorer-reset').addEventListener('click', resetDefaults);
    document.getElementById('explorer-copy').addEventListener('click', async () => {
        if (!shownEntry) return;
        try {
            await navigator.clipboard.writeText(JSON.stringify(shownEntry, null, 2));
            document.getElementById('explorer-copy').textContent = 'Copied';
            setTimeout(() => { document.getElementById('explorer-copy').textContent = 'Copy as JSON'; }, 1500);
        } catch {
            window.downloadJSON(`siyi-${shownEntry.command}.json`, shownEntry);
        }
    });
    document.getElementById('history-export').addEventListener('click', () => {
        window.downloadJSON(`siyi-command-history-${Date.now()}.json`, history);
    });
    document.getElementById('history-clear').addEventListener('click', () => {
        history = [];
        renderHistory();
    });

    searchInput.addEventListener('input', showCommands);
    groupSelect.addEventListener('change', showCommands);
    commandSelect.addEventListener('change', showCommand);
    window.addEventListener('siyi-telemetry', event => {
        const items = (event.detail.feedback || []).slice(-8).reverse();
        feedback.replaceChildren(...items.map(item => {
            const row = document.createElement('li');
            row.textContent = `${new Date(item.time * 1000).toLocaleTimeString()} · ${item.event}`;
            return row;
        }));
        if (!items.length) {
            const row = document.createElement('li');
            row.textContent = 'No feedback yet.';
            feedback.append(row);
        }
    });

    try {
        const response = await fetch('/api/sdk/commands', {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        catalog = await response.json();
        groupSelect.append(new Option('All groups', ALL_GROUPS));
        for (const group of [...new Set(catalog.map(item => item.group))]) {
            groupSelect.append(new Option(group, group));
        }
        showCommands();
        renderHistory();
    } catch (error) {
        result.textContent = `Command list unavailable: ${error.message}`;
    }
});
