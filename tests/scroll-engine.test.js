const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const engineSource = fs.readFileSync(
  path.join(__dirname, '..', 'js', 'scroll-engine.js'),
  'utf8',
);

function createHarness({ smoothingMode = 'auto', reducedMotion = false } = {}) {
  let now = 0;
  let nextRafId = 1;
  const rafs = new Map();
  const listeners = new Map();
  const motionListeners = new Set();
  const renderedFrames = [];
  const context = {
    fillRect() {},
    drawImage(image) {
      renderedFrames.push(image.frameIndex);
    },
  };
  const canvas = {
    width: 100,
    height: 100,
    getContext: () => context,
    getBoundingClientRect: () => ({ width: 100, height: 100 }),
  };
  const motionQuery = {
    matches: reducedMotion,
    addEventListener: (name, listener) => {
      if (name === 'change') motionListeners.add(listener);
    },
    removeEventListener: (name, listener) => {
      if (name === 'change') motionListeners.delete(listener);
    },
  };
  const window = {
    devicePixelRatio: 1,
    innerWidth: 100,
    innerHeight: 100,
    scrollY: 0,
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(listener);
    },
    removeEventListener(name, listener) {
      listeners.get(name)?.delete(listener);
    },
    matchMedia: () => motionQuery,
  };
  const sandbox = {
    window,
    document: { documentElement: { scrollHeight: 1100 } },
    performance: { now: () => now },
    requestAnimationFrame(callback) {
      const id = nextRafId++;
      rafs.set(id, callback);
      return id;
    },
    cancelAnimationFrame(id) {
      rafs.delete(id);
    },
    console,
    Image: class {
      constructor() {
        this.naturalWidth = 100;
        this.naturalHeight = 100;
      }
      set src(value) {
        this.frameIndex = Number(value);
        this.onload();
      }
      async decode() {}
    },
  };
  vm.runInNewContext(engineSource, sandbox, { filename: 'scroll-engine.js' });
  const engine = new window.ScrollSequenceEngine({
    canvas,
    frameCount: 11,
    getFrameUrl: (index) => String(index),
    smoothingMode,
  });

  function runNextFrame(timestamp) {
    const first = rafs.entries().next().value;
    assert.ok(first, 'expected a pending animation frame');
    rafs.delete(first[0]);
    now = timestamp;
    first[1](timestamp);
  }

  async function initialize() {
    await engine.init();
    runNextFrame(now + 16);
  }

  function scrollTo(position) {
    window.scrollY = position;
    engine.handleScroll();
  }

  return {
    engine,
    initialize,
    listeners,
    motionListeners,
    rafs,
    renderedFrames,
    runNextFrame,
    scrollTo,
    window,
  };
}

test('auto mode eases coarse mouse-wheel steps and settles at the target frame', async () => {
  const harness = createHarness();
  await harness.initialize();

  harness.window.scrollY = 1000;
  harness.engine.handleWheel({ deltaMode: 0, deltaY: 100 });
  harness.engine.handleScroll();
  harness.runNextFrame(32);
  assert.equal(harness.engine.targetFrameIndex, 10);
  assert.equal(harness.engine.currentFrameIndex, 0);

  harness.runNextFrame(48);
  assert.ok(harness.engine.currentFrameIndex > 0);
  assert.ok(harness.engine.currentFrameIndex < 10);
  assert.equal(harness.window.scrollY, 1000, 'the page scroll remains native');

  for (let frame = 64; harness.rafs.size && frame < 2000; frame += 16) {
    harness.runNextFrame(frame);
  }
  assert.equal(harness.engine.currentFrameIndex, 10);
  assert.equal(harness.engine.smoothingRafId, 0);
  harness.engine.destroy();
});

test('auto mode keeps high-resolution trackpad scrolling immediate', async () => {
  const harness = createHarness();
  await harness.initialize();

  harness.engine.handleWheel({ deltaMode: 0, deltaY: 18.5 });
  harness.scrollTo(1000);
  harness.runNextFrame(32);

  assert.equal(harness.engine.currentFrameIndex, 10);
  assert.equal(harness.engine.smoothingRafId, 0);
  harness.engine.destroy();
});

test('line-based wheel input is eased and prefers-reduced-motion snaps immediately', async () => {
  const wheelHarness = createHarness();
  await wheelHarness.initialize();
  wheelHarness.engine.handleWheel({ deltaMode: 1, deltaY: 3 });
  wheelHarness.scrollTo(1000);
  wheelHarness.runNextFrame(32);
  assert.equal(wheelHarness.engine.currentFrameIndex, 0);
  assert.ok(wheelHarness.engine.smoothingRafId);
  wheelHarness.engine.destroy();

  const reducedHarness = createHarness({ reducedMotion: true });
  await reducedHarness.initialize();
  reducedHarness.engine.handleWheel({ deltaMode: 1, deltaY: 3 });
  reducedHarness.scrollTo(1000);
  reducedHarness.runNextFrame(32);
  assert.equal(reducedHarness.engine.currentFrameIndex, 10);
  assert.equal(reducedHarness.engine.smoothingRafId, 0);
  reducedHarness.engine.destroy();
});

test('off mode, scroll boundaries, and destruction behave safely', async () => {
  const harness = createHarness({ smoothingMode: 'off' });
  await harness.initialize();

  harness.engine.handleWheel({ deltaMode: 1, deltaY: 3 });
  harness.scrollTo(1000);
  harness.runNextFrame(32);
  assert.equal(harness.engine.currentFrameIndex, 10);
  assert.equal(harness.engine.smoothingRafId, 0);

  harness.scrollTo(-500);
  harness.runNextFrame(48);
  assert.equal(harness.engine.currentFrameIndex, 0);
  harness.engine.destroy();

  const autoHarness = createHarness();
  await autoHarness.initialize();
  autoHarness.engine.handleWheel({ deltaMode: 1, deltaY: 3 });
  autoHarness.scrollTo(1000);
  autoHarness.runNextFrame(32);
  assert.ok(autoHarness.engine.smoothingRafId);
  autoHarness.engine.destroy();
  assert.equal(autoHarness.rafs.size, 0);
  assert.equal(autoHarness.listeners.get('wheel').size, 0);
  assert.equal(autoHarness.motionListeners.size, 0);
});

test('changing to reduced motion cancels an active interpolation', async () => {
  const harness = createHarness();
  await harness.initialize();

  harness.engine.handleWheel({ deltaMode: 1, deltaY: 3 });
  harness.scrollTo(1000);
  harness.runNextFrame(32);
  assert.ok(harness.engine.smoothingRafId);

  for (const listener of harness.motionListeners) listener({ matches: true });
  assert.equal(harness.engine.currentFrameIndex, 10);
  assert.equal(harness.engine.smoothingRafId, 0);
  harness.engine.destroy();
});
