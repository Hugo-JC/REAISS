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
    this.images = new Array(this.frameCount).fill(null);
    this.loadedCount = 0;
    this.currentFrameIndex = 0;
    this.lastRenderedIndex = -1;
    this.rafId = 0;
    this.resizeObserver = null;
    this.destroyed = false;
    this.lastGoodImage = null;
    this.handleScroll = this.handleScroll.bind(this);
    this.handleResize = this.handleResize.bind(this);
  }

  async init() {
    this.resizeCanvas();
    window.addEventListener('scroll', this.handleScroll, { passive: true });
    window.addEventListener('resize', this.handleResize, { passive: true });
    await this.preloadFrames();
    this.handleScroll();
    return this;
  }

  handleResize() {
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = requestAnimationFrame(() => {
      this.resizeCanvas();
      this.renderFrame(this.currentFrameIndex);
    });
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
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = requestAnimationFrame(() => {
      const maxScroll = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
      const fraction = Math.min(1, Math.max(0, window.scrollY / maxScroll));
      const target = Math.min(this.frameCount - 1, Math.round(fraction * (this.frameCount - 1)));
      if (target !== this.currentFrameIndex) {
        this.currentFrameIndex = target;
        this.renderFrame(target);
      }
    });
  }

  renderFrame(index) {
    if (!this.ctx || this.destroyed) return;
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
    if (this.rafId) cancelAnimationFrame(this.rafId);
  }
}

window.ScrollSequenceEngine = ScrollSequenceEngine;
