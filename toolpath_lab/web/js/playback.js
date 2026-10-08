// Play one planning result back in time.
//
// The backend already turned geometry into time (arc length / feed), so playback is interpolation, not physics.
// Consecutive samples are consecutive points on the toolpath, so the sample index doubles as the segment reached.

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
      // Which move each sample belongs to: the material-removal snapshots are tagged with this index,
      // so it is what links the playback position to the floor shown in the 3D view.
      this.moveIndices = decodeRuns(timeline.move_runs, this.sampleCount, Int32Array);
    } else {
      this.kinds = null;
      this.moveIndices = null;
    }
    this._notify();
  }

  state() {
    if (!this.timeline || this.sampleCount === 0) {
      return {
        time: 0, duration: 0, progress: 0, index: 0, moveIndex: null,
        position: [0, 0, 0], playing: false,
      };
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
      moveIndex: this.moveIndices ? this.moveIndices[index] : null,
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
        // Reaching the end is a state change too: without telling the UI the play button stays on pause.
        this._notify();
      }
    }
    return this.state();
  }

  _notify() {
    if (this.onStateChange) this.onStateChange(this.state());
  }
}
