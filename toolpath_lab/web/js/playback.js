// 按时间播放一次规划结果。
//
// 后端已经把几何换算成了时间（弧长 / 进给），所以播放只是插值：没有物理，也不会重新规划。
// 相邻采样点就是刀路上相邻的点，因此"采样下标"可以直接当作"已经走到哪一段"。

function decodeRuns(runs, count, Ctor) {
  const values = new Ctor(count);
  if (!runs || runs.length === 0) return values;
  for (let index = 0; index < runs.length; index += 1) {
    const start = runs[index][0];
    const end = index + 1 < runs.length ? runs[index + 1][0] : count;
    values.fill(runs[index][1], start, end);
  }
  return values;
}

export class Playback {
  constructor() {
    this.timeline = null;
    this.time = 0;
    this.playing = false;
    this.duration = 0;
    this.sampleCount = 0;
    this.kindNames = { 0: "cut", 1: "link", 2: "rapid" };
    this.onStateChange = null;
  }

  load(timeline) {
    this.timeline = timeline;
    this.time = 0;
    this.playing = false;
    this.duration = timeline ? timeline.duration_s : 0;
    this.sampleCount = timeline ? timeline.times.length : 0;
    if (timeline) {
      this.kindNames = Object.assign({ 0: "cut", 1: "link", 2: "rapid" }, timeline.kind_codes || {});
      this.kinds = decodeRuns(timeline.kind_runs, this.sampleCount, Uint8Array);
    } else {
      this.kinds = null;
    }
    this._notify();
  }

  state() {
    if (!this.timeline || this.sampleCount === 0) {
      return { time: 0, duration: 0, progress: 0, index: 0, position: [0, 0, 0], playing: false };
    }
    const times = this.timeline.times;
    const clamped = Math.min(Math.max(this.time, 0), this.duration);
    let low = 0;
    let high = this.sampleCount - 1;
    while (low < high) {
      const mid = (low + high + 1) >> 1;
      if (times[mid] <= clamped) low = mid;
      else high = mid - 1;
    }
    const index = low;
    const next = Math.min(index + 1, this.sampleCount - 1);
    const t0 = times[index];
    const t1 = times[next];
    const ratio = t1 > t0 ? (clamped - t0) / (t1 - t0) : 0;
    const positions = this.timeline.positions;
    const a = positions[index];
    const b = positions[next];
    const position = ratio === 0
      ? a
      : [a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio, a[2] + (b[2] - a[2]) * ratio];
    return {
      time: clamped,
      duration: this.duration,
      progress: this.duration > 0 ? clamped / this.duration : 0,
      index,
      position,
      playing: this.playing,
    };
  }

  play() {
    if (!this.timeline || this.duration <= 0) return;
    if (this.time >= this.duration) this.time = 0;
    this.playing = true;
    this._notify();
  }

  pause() {
    this.playing = false;
    this._notify();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  stop() {
    this.playing = false;
    this.time = 0;
    this._notify();
  }

  seekProgress(progress) {
    this.time = Math.min(Math.max(progress, 0), 1) * this.duration;
    this._notify();
  }

  update(dt) {
    if (!this.timeline) return null;
    if (this.playing) {
      this.time += dt;
      if (this.time >= this.duration) {
        this.time = this.duration;
        this.playing = false;
      }
    }
    return this.state();
  }

  _notify() {
    if (this.onStateChange) this.onStateChange(this.state());
  }
}
