/** Minimal rolling time-series plot on a <canvas>; colors come from CSS variables. */
class RollingPlot {
    constructor(canvas, series, {windowMs = 30000, minSpan = 10, unit = ''} = {}) {
        this.canvas = canvas;
        this.series = series; // [{key, color: '--css-var'}]
        this.windowMs = windowMs;
        this.minSpan = minSpan;
        this.unit = unit;
        this.points = [];
        this.paused = false;
        this.pending = false;
        new ResizeObserver(() => this.draw()).observe(canvas);
    }

    push(values, t = performance.now()) {
        if (this.paused) return;
        this.points.push({t, ...values});
        while (this.points.length && t - this.points[0].t > this.windowMs) this.points.shift();
        if (!this.pending) {
            this.pending = true;
            requestAnimationFrame(() => { this.pending = false; this.draw(); });
        }
    }

    clear() { this.points = []; this.draw(); }

    draw() {
        const canvas = this.canvas;
        if (!canvas.offsetParent) return; // Hidden tab: draw when shown.
        const dpr = window.devicePixelRatio || 1;
        const width = canvas.clientWidth, height = canvas.clientHeight;
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        const style = getComputedStyle(canvas);
        const dim = style.getPropertyValue('--text-dim').trim();
        const grid = style.getPropertyValue('--glass-border').trim();

        const values = this.points.flatMap(p => this.series.map(s => p[s.key]).filter(v => v != null));
        let lo = values.length ? Math.min(...values) : -this.minSpan / 2;
        let hi = values.length ? Math.max(...values) : this.minSpan / 2;
        if (hi - lo < this.minSpan) {
            const mid = (hi + lo) / 2;
            lo = mid - this.minSpan / 2;
            hi = mid + this.minSpan / 2;
        }
        const pad = {l: 44, r: 6, t: 6, b: 6};
        const plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
        const y = v => pad.t + plotH * (1 - (v - lo) / (hi - lo));
        const now = this.paused && this.points.length ? this.points.at(-1).t : performance.now();
        const x = t => pad.l + plotW * (1 - (now - t) / this.windowMs);

        ctx.font = '10px monospace';
        ctx.fillStyle = dim;
        ctx.strokeStyle = grid;
        ctx.lineWidth = 1;
        for (const v of [lo, (lo + hi) / 2, hi]) {
            ctx.beginPath();
            ctx.moveTo(pad.l, y(v));
            ctx.lineTo(width - pad.r, y(v));
            ctx.stroke();
            ctx.fillText(`${v.toFixed(Math.abs(hi - lo) < 20 ? 1 : 0)}${this.unit}`, 2, y(v) + 3);
        }
        for (const s of this.series) {
            ctx.strokeStyle = style.getPropertyValue(s.color).trim();
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            let started = false;
            for (const p of this.points) {
                const v = p[s.key];
                if (v == null) { started = false; continue; }
                if (started) ctx.lineTo(x(p.t), y(v));
                else ctx.moveTo(x(p.t), y(v));
                started = true;
            }
            ctx.stroke();
        }
    }
}
window.RollingPlot = RollingPlot;
