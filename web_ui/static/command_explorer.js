/** Typed forms generated from the server's fixed A8 Mini command catalog. */
window.addEventListener('load', async () => {
    const groupSelect = document.getElementById('explorer-group');
    const commandSelect = document.getElementById('explorer-command');
    const description = document.getElementById('explorer-description');
    const form = document.getElementById('explorer-form');
    const fields = document.getElementById('explorer-fields');
    const result = document.getElementById('explorer-result');
    const feedback = document.getElementById('explorer-feedback');
    const telemetry = document.getElementById('explorer-telemetry');
    let catalog = [];

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
        const wrapper = document.createElement('label');
        wrapper.className = 'explorer-field';
        const title = document.createElement('span');
        title.textContent = `${name}${required ? ' *' : ''}`;
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
        } else {
            input = document.createElement('input');
            if (schema.type === 'boolean') {
                input.type = 'checkbox';
                input.checked = schema.default === true;
            } else if (schema.type === 'integer' || schema.type === 'number') {
                input.type = 'number';
                input.step = schema.type === 'integer' ? '1' : 'any';
                if (schema.default != null) input.value = schema.default;
            } else {
                input.type = 'text';
                if (schema.default != null) input.value = schema.default;
            }
        }
        input.dataset.path = path;
        input.dataset.kind = schema.type || (schema.enum ? typeof schema.enum[0] : 'string');
        input.required = required && input.type !== 'checkbox';
        wrapper.append(input);
        return wrapper;
    }

    function selected() { return catalog.find(item => item.name === commandSelect.value); }

    function showCommand() {
        const command = selected();
        fields.replaceChildren();
        if (!command) return;
        description.textContent = command.description.split('\n')[0];
        for (const [name, schema] of Object.entries(command.schema.properties || {})) {
            fields.append(field(name, schema, command.schema, (command.schema.required || []).includes(name)));
        }
        result.textContent = 'Ready.';
    }

    function showGroup() {
        commandSelect.replaceChildren();
        for (const command of catalog.filter(item => item.group === groupSelect.value)) {
            commandSelect.append(new Option(command.name.replaceAll('_', ' '), command.name));
        }
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

    form.addEventListener('submit', async event => {
        event.preventDefault();
        const command = selected();
        if (!command) return;
        const args = collectArgs();
        const confirmAction = command.confirmation ? window.confirm(command.confirmation) : false;
        if (command.confirmation && !confirmAction) return;
        const button = form.querySelector('button[type=submit]');
        button.disabled = true;
        result.textContent = 'Waiting for camera…';
        try {
            const response = await fetch(`/api/sdk/commands/${encodeURIComponent(command.name)}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({args, confirm: confirmAction})
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail));
            result.textContent = JSON.stringify(payload.result, null, 2) ?? 'Command sent.';
            if (command.name === 'set_ip_config') {
                document.getElementById('camera-ip-display').textContent = args.cfg.ip;
            }
        } catch (error) {
            result.textContent = `Command failed: ${error.message}`;
        } finally {
            button.disabled = false;
        }
    });

    groupSelect.addEventListener('change', showGroup);
    commandSelect.addEventListener('change', showCommand);
    window.addEventListener('siyi-telemetry', event => {
        const data = event.detail;
        const fresh = data.latency?.attitude_age_ms != null && data.latency.attitude_age_ms < 3000;
        telemetry.textContent = fresh
            ? `Yaw ${data.yaw.toFixed(1)}° · Pitch ${data.pitch.toFixed(1)}° · Roll ${data.roll.toFixed(1)}°`
            : 'Waiting for fresh attitude data.';
        feedback.replaceChildren();
        for (const item of (data.feedback || []).slice(-5).reverse()) {
            const row = document.createElement('li');
            row.textContent = `${new Date(item.time * 1000).toLocaleTimeString()} · ${item.event}`;
            feedback.append(row);
        }
    });

    try {
        const response = await fetch('/api/sdk/commands', {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        catalog = await response.json();
        for (const group of [...new Set(catalog.map(item => item.group))]) {
            groupSelect.append(new Option(group, group));
        }
        showGroup();
    } catch (error) {
        result.textContent = `Command list unavailable: ${error.message}`;
    }
});
