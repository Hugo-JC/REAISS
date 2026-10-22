/**
 * REAI Scroll Sequence Engine
 * Canvas renderer for scroll-controlled image sequences.
 * Loads frames in small batches, respects device pixel ratio and reduced motion.
 */
class ScrollSequenceEngine {
  constructor(options = {}) {
    if (!options.canvas || typeof options.getFrameUrl !== 'function') {
      throw new TypeError('ScrollSequenceEngine requires a canvas and getFrameUrl(index).');
    }
    this.canvas = options.canvas;
    this.ctx = this.canvas.getContext('2d', { alpha: false, desynchronized: true });
    if (!this.ctx) throw new Error('Canvas 2D is not available in this browser.');
    this.frameCount = Math.max(1, Number(options.frameCount) || 1);
    this.getFrameUrl = options.getFrameUrl;
    this.onProgress = options.onProgress || (() => {});
    this.onComplete = options.onComplete || (() => {});
    this.smoothingMode = options.smoothingMode || 'auto';
    if (!['auto', 'off'].includes(this.smoothingMode)) {
      throw new RangeError('smoothingMode must be "auto" or "off".');
    }
    this.motionQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)') || null;
    this.prefersReducedMotion = this.motionQuery?.matches || false;
    this.images = new Array(this.frameCount).fill(null);
    this.loadedCount = 0;
    this.currentFrameIndex = 0;
    this.targetFrameIndex = 0;
    this.visualFramePosition = 0;
    this.lastRenderedIndex = -1;
    this.scrollRafId = 0;
    this.resizeRafId = 0;
    this.smoothingRafId = 0;
    this.lastSmoothingTime = null;
    this.discreteWheelUntil = 0;
    this.resizeObserver = null;
    this.destroyed = false;
    this.lastGoodImage = null;
    this.handleScroll = this.handleScroll.bind(this);
    this.handleResize = this.handleResize.bind(this);
    this.handleWheel = this.handleWheel.bind(this);
    this.animateFrame = this.animateFrame.bind(this);
    this.handleMotionPreferenceChange = this.handleMotionPreferenceChange.bind(this);
  }

  async init() {
    this.resizeCanvas();
    window.addEventListener('scroll', this.handleScroll, { passive: true });
    window.addEventListener('resize', this.handleResize, { passive: true });
    window.addEventListener('wheel', this.handleWheel, { passive: true });
    this.motionQuery?.addEventListener?.('change', this.handleMotionPreferenceChange);
    await this.preloadFrames();
    this.handleScroll();
    return this;
  }

  handleResize() {
    if (this.resizeRafId) cancelAnimationFrame(this.resizeRafId);
    this.resizeRafId = requestAnimationFrame(() => {
      this.resizeRafId = 0;
      this.resizeCanvas();
      this.renderFrame(this.currentFrameIndex);
    });
  }

  handleWheel(event) {
    if (this.smoothingMode !== 'auto' || this.prefersReducedMotion) return;
    const deltaY = Number(event.deltaY) || 0;
    if (deltaY === 0) return;
    const delta = Math.abs(deltaY);
    const coarsePixelStep = [100, 120].some((step) => {
      const steps = Math.max(1, Math.round(delta / step));
      return delta >= 60 && Math.abs(delta - steps * step) <= 1.5;
    });
    if (event.deltaMode !== 0 || coarsePixelStep) {
      this.discreteWheelUntil = performance.now() + 220;
    } else {
      this.discreteWheelUntil = 0;
    }
  }

  handleMotionPreferenceChange(event) {
    this.prefersReducedMotion = event.matches;
    if (!this.prefersReducedMotion || !this.smoothingRafId) return;
    cancelAnimationFrame(this.smoothingRafId);
    this.smoothingRafId = 0;
    this.lastSmoothingTime = null;
    this.visualFramePosition = this.targetFrameIndex;
    this.currentFrameIndex = this.targetFrameIndex;
    this.renderFrame(this.currentFrameIndex);
  }

  resizeCanvas() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round((rect.width || window.innerWidth) * dpr));
    const height = Math.max(1, Math.round((rect.height || window.innerHeight) * dpr));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
  }

  async preloadFrames() {
    // A small worker pool avoids opening hundreds of image requests at once.
    let cursor = 0;
    const workers = Array.from({ length: Math.min(8, this.frameCount) }, async () => {
      while (cursor < this.frameCount && !this.destroyed) {
        const index = cursor++;
        const url = this.getFrameUrl(index);
        try {
          const image = await this.loadImage(url);
          this.images[index] = image;
        } catch (error) {
          console.warn(`[REAI] Frame ${index + 1} could not be loaded:`, url, error);
        } finally {
          this.loadedCount++;
          this.onProgress((this.loadedCount / this.frameCount) * 100);
        }
      }
    });
    await Promise.all(workers);
    if (this.destroyed) return;
    const firstValid = this.images.find(Boolean);
    if (!firstValid) throw new Error('No frames could be loaded. Check assets/frames/manifest.json and the generated files.');
    this.lastGoodImage = firstValid;
    this.onComplete();
    this.renderFrame(0);
  }

  loadImage(url) {
    return new Promise((resolve, reject) => {
      if (!url) return reject(new Error('Empty frame URL'));
      const image = new Image();
      image.decoding = 'async';
      image.onload = async () => {
        try { if (image.decode) await image.decode(); } catch (_) { /* onload already confirms usable pixels */ }
        resolve(image);
      };
      image.onerror = () => reject(new Error('Image request failed'));
      image.src = url;
    });
  }

  handleScroll() {
    if (this.destroyed) return;
    if (this.scrollRafId) cancelAnimationFrame(this.scrollRafId);
    this.scrollRafId = requestAnimationFrame(() => {
      this.scrollRafId = 0;
      const maxScroll = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
      const fraction = Math.min(1, Math.max(0, window.scrollY / maxScroll));
      const target = Math.min(this.frameCount - 1, Math.round(fraction * (this.frameCount - 1)));
      this.targetFrameIndex = target;
      const smooth = this.smoothingMode === 'auto'
        && !this.prefersReducedMotion
        && performance.now() <= this.discreteWheelUntil;
      if (!smooth) {
        if (this.smoothingRafId) cancelAnimationFrame(this.smoothingRafId);
        this.smoothingRafId = 0;
        this.lastSmoothingTime = null;
        this.visualFramePosition = target;
        if (target !== this.currentFrameIndex || this.lastRenderedIndex !== target) {
          this.currentFrameIndex = target;
          this.renderFrame(target);
        }
      } else if (!this.smoothingRafId) {
        this.lastSmoothingTime = null;
        this.smoothingRafId = requestAnimationFrame(this.animateFrame);
      }
    });
  }

  animateFrame(timestamp) {
    this.smoothingRafId = 0;
    if (this.destroyed || this.prefersReducedMotion) return;

    const elapsed = this.lastSmoothingTime === null
      ? 16
      : Math.max(1, Math.min(64, timestamp - this.lastSmoothingTime));
    this.lastSmoothingTime = timestamp;
    const easing = 1 - Math.exp(-elapsed / 85);
    const distance = this.targetFrameIndex - this.visualFramePosition;
    if (Math.abs(distance) < 0.05) {
      this.visualFramePosition = this.targetFrameIndex;
    } else {
      this.visualFramePosition += distance * easing;
    }

    const nextFrame = Math.max(
      0,
      Math.min(this.frameCount - 1, Math.round(this.visualFramePosition)),
    );
    if (nextFrame !== this.currentFrameIndex) {
      this.currentFrameIndex = nextFrame;
      this.renderFrame(nextFrame);
    }

    if (this.visualFramePosition !== this.targetFrameIndex) {
      this.smoothingRafId = requestAnimationFrame(this.animateFrame);
    } else {
      this.lastSmoothingTime = null;
    }
  }

  renderFrame(index) {
    if (!this.ctx || this.destroyed) return;
    index = Math.max(0, Math.min(this.frameCount - 1, index));
    let image = this.images[index];
    if (image) this.lastGoodImage = image;
    else image = this.lastGoodImage || this.images.find(Boolean);
    if (!image) return;
    this.lastRenderedIndex = index;

    const cw = this.canvas.width;
    const ch = this.canvas.height;
    const iw = image.naturalWidth || image.width || 1;
    const ih = image.naturalHeight || image.height || 1;
    const scale = Math.min(cw / iw, ch / ih);
    const width = Math.round(iw * scale);
    const height = Math.round(ih * scale);
    const x = Math.round((cw - width) / 2);
    const y = Math.round((ch - height) / 2);
    this.ctx.fillStyle = '#080a08';
    this.ctx.fillRect(0, 0, cw, ch);
    this.ctx.drawImage(image, x, y, width, height);
  }

  destroy() {
    this.destroyed = true;
    window.removeEventListener('scroll', this.handleScroll);
    window.removeEventListener('resize', this.handleResize);
    window.removeEventListener('wheel', this.handleWheel);
    this.motionQuery?.removeEventListener?.('change', this.handleMotionPreferenceChange);
    for (const rafId of [this.scrollRafId, this.resizeRafId, this.smoothingRafId]) {
      if (rafId) cancelAnimationFrame(rafId);
    }
    this.scrollRafId = 0;
    this.resizeRafId = 0;
    this.smoothingRafId = 0;
  }
}

window.ScrollSequenceEngine = ScrollSequenceEngine;
