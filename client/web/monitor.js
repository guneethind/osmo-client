'use strict';
// Monitoring tools over the live view: scopes (histogram, waveform, RGB parade, vectorscope), exposure
// and focus assists (false color, zebras, focus peaking), framing guides, and a display LUT for log
// profiles. They work on the picture this page shows (8-bit, after the camera's live view encoding):
// nothing is sent to the camera and nothing changes in the recording.
//
// While one of them needs the pixels, the frames are read from the MJPEG stream here (fetch) and decoded
// with createImageBitmap: drawing the page's <img> of a multipart stream into a canvas gives only its first
// frame in some browsers (Firefox).
//
// The assist algorithms and their constants follow OpenPocketCine (Apache-2.0,
// https://github.com/erik-sutton95/OpenPocketCine).

const Monitor = (() => {
  // ---- preferences (kept in this browser)
  const PREFS_KEY = 'osmo-client.monitor';
  const LUT_KEY = 'osmo-client.lut';
  const DEFAULTS = {
    histogram: false, waveform: false, parade: false, vectorscope: false,
    falseColor: false, zebras: false, peaking: false, guides: false, lut: false,
    zebraLevel: 99, zebraStyle: 'white', peakLevel: 'medium', peakColor: 'red', grid: 'thirds', frame: 'off', mask: true,
    center: false, lutMode: 'log',
  };
  const prefs = { ...DEFAULTS, ...readJSON(PREFS_KEY) };
  function readJSON(key) {
    try { return JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { return {}; }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* private window */ }
  }

  // ---- constants
  const PEAK_LEVELS = {            // noise gate on the blurred edge energy, threshold on the sharpness ratio
    low: { gate: 0.00522, t: 2.30 }, medium: { gate: 0.00174, t: 2.10 }, high: { gate: 0.00058, t: 1.90 },
  };
  const PEAK_COLORS = { red: [1, 72 / 255, 64 / 255], white: [246 / 255, 241 / 255, 226 / 255],
                        blue: [64 / 255, 142 / 255, 1], green: [74 / 255, 220 / 255, 132 / 255] };
  // False color, by IRE (code / 255 x 100). Outside the bands the picture turns grey.
  const FALSE_BANDS = [
    [0, 2.5, [115, 33, 120], 'Crushed'], [2.5, 10, [29, 2, 221], 'Near black'],
    [38, 42, [123, 207, 87], '18% grey'], [52, 56, [241, 190, 198], 'Skin'],
    [80, 95, [255, 255, 88], 'Bright'], [95, 101, [220, 51, 33], 'Clipping'],
  ];
  const FRAMES = { '2.39': 2.39, '2.35': 2.35, '1.85': 1.85, '16:9': 16 / 9, '4:3': 4 / 3, '1:1': 1, '4:5': 4 / 5, '9:16': 9 / 16 };
  const SAMPLE_W = 256;            // scopes read a downscaled copy of the picture, about 10 times a second
  const SCOPE_MS = 100;

  // ---- elements and state
  let view, img, glCanvas, overlay, scopesEl, legendEl, sampler, sctx;
  let gl = null, glProgram = null, glUniforms = {}, srcTex = null, lutTex = null, falseTex = null;
  let lut = null;                  // { name, size, data: Float32Array }
  let live = false, logProfile = false;
  let rect = null, frameSize = '';
  let source = null, sourceSeq = 0, uploadedSeq = -1;   // latest decoded frame (ImageBitmap)
  let streamOn = false, streamAbort = null, decoding = false;
  let scopeTimer = null;
  const scopeCanvases = {};

  function init(viewer, image) {
    view = viewer; img = image;
    glCanvas = make('canvas', 'mon-layer'); glCanvas.hidden = true;
    overlay = make('canvas', 'mon-layer mon-overlay'); overlay.hidden = true;
    img.after(glCanvas, overlay);
    scopesEl = make('div', 'mon-scopes'); scopesEl.hidden = true;
    legendEl = make('div', 'mon-legend'); legendEl.hidden = true;
    legendEl.innerHTML = FALSE_BANDS.map(([lo, hi, c, label]) =>
      `<span><i style="background:rgb(${c})"></i>${label} <small>${lo}–${hi > 100 ? 100 : hi}</small></span>`).join('');
    overlay.after(scopesEl, legendEl);
    for (const [name, w, h, title] of [['histogram', 256, 120, 'Histogram'], ['waveform', 256, 128, 'Waveform'],
                                        ['parade', 255, 128, 'RGB parade'], ['vectorscope', 150, 150, 'Vectorscope']]) {
      const box = make('figure', 'mon-scope');
      box.dataset.scope = name;
      const canvas = make('canvas');
      canvas.width = w; canvas.height = h;
      const caption = make('figcaption'); caption.textContent = title;
      box.append(canvas, caption);
      scopesEl.append(box);
      scopeCanvases[name] = canvas;
    }
    sampler = document.createElement('canvas');
    sctx = sampler.getContext('2d', { willReadFrequently: true });
    initGL();
    loadStoredLut();
    new ResizeObserver(() => { rect = null; }).observe(view);
    document.addEventListener('fullscreenchange', () => { rect = null; });
    requestAnimationFrame(frame);
    apply();
  }

  function make(tag, cls) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    return el;
  }

  // ---- public: state from the page, toggles, settings
  function update(isLive, isLog) {
    if (isLive !== live || isLog !== logProfile) { live = isLive; logProfile = isLog; apply(); }
  }
  function toggle(tool) {
    if (tool === 'lut' && !lut && !prefs.lut) return 'no-lut';
    prefs[tool] = !prefs[tool];
    savePrefs(); apply();
    return prefs[tool];
  }
  function set(key, value) {
    prefs[key] = value;
    savePrefs(); rect = null; apply();
  }
  const get = key => prefs[key];
  const glReady = () => !!gl;
  const lutName = () => lut ? lut.name : null;
  const lutActive = () => !!(prefs.lut && lut && (prefs.lutMode === 'always' || logProfile));

  function apply() {
    const pixels = live && gl && (prefs.falseColor || prefs.zebras || prefs.peaking || lutActive());
    glCanvas.hidden = !pixels;
    overlay.hidden = !(live && prefs.guides);
    legendEl.hidden = !(live && prefs.falseColor && gl);
    const scopes = ['histogram', 'waveform', 'parade', 'vectorscope'].filter(n => prefs[n]);
    scopesEl.hidden = !live || !scopes.length;
    for (const box of scopesEl.children) box.hidden = !prefs[box.dataset.scope];
    if (scopes.length && live && !scopeTimer) scopeTimer = setInterval(drawScopes, SCOPE_MS);
    if ((!scopes.length || !live) && scopeTimer) { clearInterval(scopeTimer); scopeTimer = null; }
    const frames = !!(pixels || (live && scopes.length));
    if (frames && !streamOn) { streamOn = true; readStream(); }
    if (!frames && streamOn) {
      streamOn = false;
      if (streamAbort) streamAbort.abort();
      if (source) { source.close(); source = null; }
    }
    rect = null;
    document.dispatchEvent(new CustomEvent('monitorchange'));
  }

  // ---- layout: the picture is letterboxed inside the viewer (object-fit: contain)
  // Size of the live view's frames: the decoded frame's when there is one, otherwise the <img>'s.
  const frameW = () => source ? source.width : img.naturalWidth;
  const frameH = () => source ? source.height : img.naturalHeight;

  function layout() {
    const iw = frameW(), ih = frameH();
    if (!iw || !ih) return false;
    const size = `${iw}x${ih}`;
    if (rect && size === frameSize) return true;
    frameSize = size;
    const vw = view.clientWidth, vh = view.clientHeight;
    const scale = Math.min(vw / iw, vh / ih);
    const w = iw * scale, h = ih * scale;
    rect = { x: (vw - w) / 2, y: (vh - h) / 2, w, h };
    const dpr = window.devicePixelRatio || 1;
    for (const c of [glCanvas, overlay]) {
      Object.assign(c.style, { left: rect.x + 'px', top: rect.y + 'px', width: w + 'px', height: h + 'px' });
      c.width = Math.max(1, Math.round(w * dpr)); c.height = Math.max(1, Math.round(h * dpr));
    }
    drawOverlay();
    return true;
  }

  function frame() {
    requestAnimationFrame(frame);
    if (!live || (glCanvas.hidden && overlay.hidden) || !layout()) return;
    if (!glCanvas.hidden && source) {
      if (uploadedSeq !== sourceSeq && !upload()) return;
      drawGL();
    }
  }

  // ---- WebGL: LUT, false color, zebras, focus peaking
  const VERTEX = `#version 300 es
    out vec2 vUv;
    void main() {
      vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
      vUv = p;
      gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
    }`;
  const FRAGMENT = `#version 300 es
    precision highp float;
    precision highp sampler3D;
    in vec2 vUv;
    out vec4 outColor;
    uniform sampler2D uSrc;
    uniform sampler3D uLut;
    uniform sampler2D uFalse;
    uniform bool uLutOn, uFalseOn, uZebraOn, uZebraBW, uPeakOn;
    uniform float uLutSize, uZebra, uPeakGate, uPeakT, uScale;
    uniform vec3 uPeakColor;

    float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
    // Peaking reads exact source pixels: interpolated samples would soften the fine detail it looks for.
    ivec2 srcSize;
    float grey(ivec2 p) {
      vec3 c = texelFetch(uSrc, clamp(p, ivec2(0), srcSize - 1), 0).rgb;
      return (c.r + c.g + c.b) / 3.0;
    }
    float blurred(ivec2 p) {
      float s = 0.0;
      for (int y = -1; y <= 1; y++) for (int x = -1; x <= 1; x++) s += grey(p + ivec2(x, y));
      return s / 9.0;
    }
    float roberts(float a, float b, float c, float d) { return (d - a) * (d - a) + (c - b) * (c - b); }
    // Sharp edges have much more energy at pixel scale than after a slight blur; soft (out of focus)
    // edges have about the same. The gate keeps noise in flat areas out.
    float peak(ivec2 p) {
      float fine = roberts(grey(p), grey(p + ivec2(1, 0)), grey(p + ivec2(0, 1)), grey(p + ivec2(1, 1)));
      float coarse = roberts(blurred(p), blurred(p + ivec2(1, 0)), blurred(p + ivec2(0, 1)), blurred(p + ivec2(1, 1)));
      float ratio = min(fine / max(coarse, 1e-9), 4.0);
      float gate = clamp((coarse - 0.7 * uPeakGate) / (0.3 * uPeakGate), 0.0, 1.0);
      return clamp((ratio - uPeakT) / 0.12, 0.0, 1.0) * gate;
    }

    void main() {
      vec2 uv = vec2(vUv.x, 1.0 - vUv.y);          // the frame's first row is its top
      vec3 src = texture(uSrc, uv).rgb;
      vec3 col = src;
      if (uLutOn) col = texture(uLut, (clamp(src, 0.0, 1.0) * (uLutSize - 1.0) + 0.5) / uLutSize).rgb;
      if (uFalseOn) {
        float y = luma(src);
        vec4 band = texture(uFalse, vec2((y * 255.0 + 0.5) / 256.0, 0.5));
        col = mix(vec3(y), band.rgb, band.a);
      }
      if (uZebraOn) {
        float stripe = step(0.5, fract((gl_FragCoord.x + gl_FragCoord.y) / 14.14));
        float over = clamp((max(max(src.r, src.g), src.b) - uZebra) * 40.0 + 1.0, 0.0, 1.0);
        // white stripes over the picture, or black and white stripes that hide it
        col = uZebraBW ? mix(col, vec3(stripe), over) : mix(col, vec3(1.0), over * stripe);
      }
      if (uPeakOn) {
        srcSize = textureSize(uSrc, 0);
        ivec2 p = ivec2(uv * vec2(srcSize));
        // When the picture is shown smaller than the source, a screen pixel covers more than one source
        // pixel: look at the neighbour too so thin edges don't drop out.
        float stroke = peak(p);
        if (uScale > 1.2) stroke = max(stroke, peak(p + ivec2(1, 0)));
        bool border = any(lessThan(p, ivec2(6))) || any(greaterThan(p, srcSize - 7));
        col = mix(col, uPeakColor, border ? 0.0 : stroke);
      }
      outColor = vec4(col, 1.0);
    }`;

  function initGL() {
    gl = glCanvas.getContext('webgl2', { premultipliedAlpha: false, preserveDrawingBuffer: false });
    if (!gl) return;
    const shader = (type, source) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, source); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    try {
      glProgram = gl.createProgram();
      gl.attachShader(glProgram, shader(gl.VERTEX_SHADER, VERTEX));
      gl.attachShader(glProgram, shader(gl.FRAGMENT_SHADER, FRAGMENT));
      gl.linkProgram(glProgram);
      if (!gl.getProgramParameter(glProgram, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(glProgram));
    } catch (error) {
      console.warn('Monitoring tools: WebGL2 shader failed', error);
      gl = null;
      return;
    }
    gl.useProgram(glProgram);
    for (const name of ['uSrc', 'uLut', 'uFalse', 'uLutOn', 'uFalseOn', 'uZebraOn', 'uZebraBW', 'uPeakOn', 'uLutSize', 'uZebra',
                        'uPeakGate', 'uPeakT', 'uPeakColor', 'uScale']) glUniforms[name] = gl.getUniformLocation(glProgram, name);
    gl.uniform1i(glUniforms.uSrc, 0); gl.uniform1i(glUniforms.uLut, 1); gl.uniform1i(glUniforms.uFalse, 2);
    srcTex = texture2D(gl.LINEAR);
    falseTex = texture2D(gl.NEAREST);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, falseColorTable());
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    glCanvas.addEventListener('webglcontextlost', e => { e.preventDefault(); gl = null; apply(); });
  }

  function texture2D(filter) {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  function falseColorTable() {
    const table = new Uint8Array(256 * 4);
    for (let code = 0; code < 256; code++) {
      const ire = code / 255 * 100;
      const band = FALSE_BANDS.find(([lo, hi]) => ire >= lo && ire < hi);
      if (band) table.set([...band[2], 255], code * 4);
    }
    return table;
  }

  // The latest frame into the source texture (only when a new one arrived).
  function upload() {
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, srcTex);
    try {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    } catch (e) { return false; }
    uploadedSeq = sourceSeq;
    return true;
  }

  function drawGL() {
    const level = PEAK_LEVELS[prefs.peakLevel] || PEAK_LEVELS.medium;
    gl.viewport(0, 0, glCanvas.width, glCanvas.height);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, srcTex);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, falseTex);
    const useLut = lutActive() && lutTex;
    if (useLut) { gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, lutTex); }
    gl.uniform1i(glUniforms.uLutOn, useLut ? 1 : 0);
    gl.uniform1f(glUniforms.uLutSize, lut ? lut.size : 2);
    gl.uniform1i(glUniforms.uFalseOn, prefs.falseColor ? 1 : 0);
    gl.uniform1i(glUniforms.uZebraOn, prefs.zebras ? 1 : 0);
    gl.uniform1i(glUniforms.uZebraBW, prefs.zebraStyle === 'bw' ? 1 : 0);
    // Zebras mark the clipped highlights: the threshold never goes above the camera's clip shelf (248).
    gl.uniform1f(glUniforms.uZebra, Math.min(prefs.zebraLevel / 100, 248 / 255));
    gl.uniform1i(glUniforms.uPeakOn, prefs.peaking ? 1 : 0);
    // Log pictures have less contrast: the noise gate is lower for them.
    gl.uniform1f(glUniforms.uPeakGate, level.gate * (logProfile ? 1 : 1.57 * 1.57));
    gl.uniform1f(glUniforms.uPeakT, level.t);
    gl.uniform1f(glUniforms.uScale, frameW() / glCanvas.width);   // source pixels per screen pixel
    gl.uniform3fv(glUniforms.uPeakColor, PEAK_COLORS[prefs.peakColor] || PEAK_COLORS.red);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // ---- display LUT (.cube)
  function parseCube(text) {
    let size = 0;
    const values = [];
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line[0] === '#') continue;
      const [key, ...rest] = line.split(/\s+/);
      if (key === 'LUT_3D_SIZE') size = parseInt(rest[0], 10);
      else if (key === 'LUT_1D_SIZE') throw new Error('1D LUTs are not supported, only 3D (.cube with LUT_3D_SIZE)');
      else if (key === 'DOMAIN_MIN' || key === 'DOMAIN_MAX') {
        const want = key === 'DOMAIN_MIN' ? 0 : 1;
        if (rest.some(v => parseFloat(v) !== want)) throw new Error('only LUTs with a 0-1 domain are supported');
      } else if (/^[-+.\d]/.test(key)) {
        values.push(parseFloat(key), parseFloat(rest[0]), parseFloat(rest[1]));
      }
    }
    if (!(size >= 2 && size <= 65)) throw new Error('missing or unsupported LUT_3D_SIZE');
    if (values.length !== size ** 3 * 3 || values.some(Number.isNaN)) throw new Error(`expected ${size ** 3} entries`);
    return { size, data: new Float32Array(values) };
  }

  function uploadLut() {
    if (!gl || !lut) return;
    if (!lutTex) lutTex = gl.createTexture();
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_3D, lutTex);
    for (const [p, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
                          [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE],
                          [gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_3D, p, v);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGB16F, lut.size, lut.size, lut.size, 0, gl.RGB, gl.FLOAT, lut.data);
  }

  // Load a .cube file the user picked. Returns '' or the reason it was refused. Small LUTs are remembered
  // in this browser (a 33-point cube is about 1 MB of text).
  async function loadLut(file) {
    let text;
    try { text = await file.text(); } catch (e) { return 'could not read the file'; }
    let parsed;
    try { parsed = parseCube(text); } catch (e) { return e.message; }
    lut = { name: file.name.replace(/\.cube$/i, ''), ...parsed };
    uploadLut();
    let remembered = true;
    try { localStorage.setItem(LUT_KEY, JSON.stringify({ name: lut.name, text })); } catch (e) {
      remembered = false;
      try { localStorage.removeItem(LUT_KEY); } catch (e2) { /* ignore */ }
    }
    prefs.lut = true;
    savePrefs(); apply();
    return remembered ? '' : 'loaded, but too big to be remembered after closing the page';
  }

  function loadStoredLut() {
    const stored = readJSON(LUT_KEY);
    if (!stored.text) return;
    try { lut = { name: stored.name, ...parseCube(stored.text) }; uploadLut(); } catch (e) { lut = null; }
  }

  function clearLut() {
    lut = null; prefs.lut = false;
    try { localStorage.removeItem(LUT_KEY); } catch (e) { /* ignore */ }
    savePrefs(); apply();
  }

  // ---- guides (2D overlay, redrawn when the layout or the settings change)
  function drawOverlay() {
    if (!rect || overlay.hidden) return;
    const ctx = overlay.getContext('2d');
    const W = overlay.width, H = overlay.height, dpr = W / rect.w;
    ctx.clearRect(0, 0, W, H);
    // frame guide: the largest rectangle of that aspect ratio inside the picture, centred
    let fx = 0, fy = 0, fw = W, fh = H;
    const ratio = FRAMES[prefs.frame];
    if (ratio) {
      if (W / H > ratio) { fw = H * ratio; fx = (W - fw) / 2; } else { fh = W / ratio; fy = (H - fh) / 2; }
      if (prefs.mask) {
        ctx.fillStyle = 'rgba(0,0,0,.6)';
        ctx.fillRect(0, 0, W, fy); ctx.fillRect(0, fy + fh, W, H - fy - fh);
        ctx.fillRect(0, fy, fx, fh); ctx.fillRect(fx + fw, fy, W - fx - fw, fh);
      }
      ctx.strokeStyle = 'rgba(94,177,255,.85)'; ctx.lineWidth = dpr;
      ctx.strokeRect(fx + dpr / 2, fy + dpr / 2, fw - dpr, fh - dpr);
    }
    // grid inside the frame
    ctx.strokeStyle = 'rgba(255,255,255,.28)'; ctx.lineWidth = dpr;
    const lines = { thirds: [1 / 3, 2 / 3], phi: [0.382, 0.618], diagonals: [1 / 3, 2 / 3] }[prefs.grid] || [];
    ctx.beginPath();
    for (const f of lines) {
      ctx.moveTo(fx + fw * f, fy); ctx.lineTo(fx + fw * f, fy + fh);
      ctx.moveTo(fx, fy + fh * f); ctx.lineTo(fx + fw, fy + fh * f);
    }
    if (prefs.grid === 'diagonals') {
      ctx.moveTo(fx, fy); ctx.lineTo(fx + fw, fy + fh);
      ctx.moveTo(fx + fw, fy); ctx.lineTo(fx, fy + fh);
    }
    ctx.stroke();
    if (prefs.center) {
      ctx.fillStyle = 'rgba(255,255,255,.65)';
      const cx = fx + fw / 2, cy = fy + fh / 2, len = 20 * dpr, t = 1.4 * dpr;
      ctx.fillRect(cx - len, cy - t / 2, 2 * len, t); ctx.fillRect(cx - t / 2, cy - len, t, 2 * len);
    }
  }

  // ---- scopes
  let sampledSeq = -1;
  function sample() {
    if (!source || sampledSeq === sourceSeq) return null;   // no new frame since the last sample
    sampledSeq = sourceSeq;
    const iw = source.width, ih = source.height;
    const w = SAMPLE_W, h = Math.max(1, Math.round(SAMPLE_W * ih / iw));
    if (sampler.width !== w || sampler.height !== h) { sampler.width = w; sampler.height = h; }
    try {
      sctx.drawImage(source, 0, 0, w, h);
      return { w, h, px: sctx.getImageData(0, 0, w, h).data };
    } catch (e) { return null; }
  }

  function drawScopes() {
    const s = sample();
    if (!s) return;
    if (prefs.histogram) histogram(s);
    if (prefs.waveform) waveform(s, false);
    if (prefs.parade) waveform(s, true);
    if (prefs.vectorscope) vectorscope(s);
  }

  const lumaOf = (r, g, b) => 0.2126 * r + 0.7152 * g + 0.0722 * b;

  function histogram({ px }) {
    const canvas = scopeCanvases.histogram, ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const bins = [new Float32Array(256), new Float32Array(256), new Float32Array(256), new Float32Array(256)];
    for (let i = 0; i < px.length; i += 4) {
      const r = px[i], g = px[i + 1], b = px[i + 2];
      bins[0][r]++; bins[1][g]++; bins[2][b]++; bins[3][Math.round(lumaOf(r, g, b))]++;
    }
    // light smoothing so a sparse histogram still reads as a shape
    const smooth = a => a.map((_, i) => (a[Math.max(0, i - 1)] + 2 * a[i] + a[Math.min(255, i + 1)]) / 4);
    const sm = bins.map(smooth);
    let peak = 1;
    for (const a of sm) for (let i = 2; i < 254; i++) peak = Math.max(peak, a[i]);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = 'rgba(255,69,58,.10)';
    ctx.fillRect(W * 248 / 256, 0, W * 8 / 256, H);               // clip zone
    const path = a => {
      ctx.beginPath(); ctx.moveTo(0, H);
      for (let i = 0; i < 256; i++) ctx.lineTo(i * W / 255, H - Math.min(1, a[i] / peak) * (H - 4));
      ctx.lineTo(W, H); ctx.closePath();
    };
    ctx.globalCompositeOperation = 'lighter';
    [['rgba(255,64,54,.45)', sm[0]], ['rgba(70,240,110,.40)', sm[1]], ['rgba(72,148,255,.50)', sm[2]]].forEach(([c, a]) => {
      path(a); ctx.fillStyle = c; ctx.fill();
    });
    ctx.globalCompositeOperation = 'source-over';
    path(sm[3]); ctx.strokeStyle = 'rgba(232,234,237,.85)'; ctx.lineWidth = 1; ctx.stroke();
    guidesX(ctx, W, H);
    const total = px.length / 4;
    const clipped = bins.slice(0, 3).some(a => (a[254] + a[255]) / total > 0.002);
    const crushed = (bins[3][0] + bins[3][1]) / total > 0.002;
    lamp(ctx, W - 10, 8, clipped, 'rgb(255,69,58)');
    lamp(ctx, 10, 8, crushed, 'rgb(120,90,255)');
  }

  function lamp(ctx, x, y, on, color) {
    if (!on) return;
    ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI); ctx.fillStyle = color; ctx.fill();
  }

  function guidesX(ctx, W, H) {
    ctx.strokeStyle = 'rgba(255,255,255,.12)'; ctx.lineWidth = 1;
    ctx.beginPath();
    for (const f of [0.25, 0.5, 0.75]) { ctx.moveTo(Math.round(W * f) + 0.5, 0); ctx.lineTo(Math.round(W * f) + 0.5, H); }
    ctx.stroke();
  }

  // Waveform: brightness (IRE, 0 at the bottom, 100 at the top) of every column of the picture. The parade
  // shows red, green and blue side by side.
  function waveform({ w, h, px }, parade) {
    const canvas = scopeCanvases[parade ? 'parade' : 'waveform'], ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const counts = new Uint32Array(W * H);
    const lanes = parade ? 3 : 1, laneW = Math.floor(W / lanes);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        if (parade) {
          for (let c = 0; c < 3; c++) {
            const cx = c * laneW + Math.floor(x * laneW / w);
            const row = H - 1 - Math.round(px[i + c] / 255 * (H - 1));
            counts[row * W + cx]++;
          }
        } else {
          const cx = Math.floor(x * W / w);
          const row = H - 1 - Math.round(lumaOf(px[i], px[i + 1], px[i + 2]) / 255 * (H - 1));
          counts[row * W + cx]++;
        }
      }
    }
    let peak = 1;
    for (const n of counts) if (n > peak) peak = n;
    const image = ctx.createImageData(W, H), out = image.data, logPeak = Math.log1p(peak);
    const tints = parade ? [[255, 86, 78], [102, 232, 132], [92, 156, 255]] : [[205, 240, 215]];
    for (let p = 0; p < counts.length; p++) {
      const n = counts[p];
      if (!n) continue;
      const a = 0.25 + 0.75 * Math.log1p(n) / logPeak;
      const tint = tints[parade ? Math.min(2, Math.floor((p % W) / laneW)) : 0];
      out[p * 4] = tint[0]; out[p * 4 + 1] = tint[1]; out[p * 4 + 2] = tint[2]; out[p * 4 + 3] = Math.round(255 * a);
    }
    ctx.clearRect(0, 0, W, H);
    ctx.putImageData(image, 0, 0);
    // graticule: 0 and 100 solid, 5 and 95 dashed, 25/50/75 faint
    const yAt = ire => H - 1 - ire / 100 * (H - 1) + 0.5;
    ctx.lineWidth = 1;
    for (const [ire, style, dash] of [[0, '.30', []], [100, '.30', []], [5, '.22', [3, 3]], [95, '.22', [3, 3]],
                                       [25, '.10', []], [50, '.10', []], [75, '.10', []]]) {
      ctx.strokeStyle = `rgba(255,255,255,${style})`; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(0, yAt(ire)); ctx.lineTo(W, yAt(ire)); ctx.stroke();
    }
    ctx.setLineDash([]);
    if (parade) {
      ctx.strokeStyle = 'rgba(255,255,255,.18)';
      ctx.beginPath();
      for (let c = 1; c < 3; c++) { ctx.moveTo(c * laneW + 0.5, 0); ctx.lineTo(c * laneW + 0.5, H); }
      ctx.stroke();
    }
  }

  // Vectorscope: hue as the angle, saturation as the distance from the centre (BT.709 colour difference).
  function vectorscope({ px }) {
    const canvas = scopeCanvases.vectorscope, ctx = canvas.getContext('2d');
    const S = canvas.width, c0 = S / 2, radius = S / 2 - 6;
    const N = 128, counts = new Uint32Array(N * N), sums = new Float32Array(N * N * 3);
    const cbcr = (r, g, b) => { const y = lumaOf(r, g, b); return [(b - y) / 1.8556, (r - y) / 1.5748]; };
    for (let i = 0; i < px.length; i += 4) {
      const r = px[i] / 255, g = px[i + 1] / 255, b = px[i + 2] / 255;
      const [cb, cr] = cbcr(r, g, b);
      const x = Math.floor((cb + 0.5) * N), y = Math.floor((0.5 - cr) * N);
      if (x < 0 || y < 0 || x >= N || y >= N) continue;
      const k = y * N + x;
      counts[k]++; sums[k * 3] += r; sums[k * 3 + 1] += g; sums[k * 3 + 2] += b;
    }
    let peak = 1;
    for (const n of counts) if (n > peak) peak = n;
    ctx.clearRect(0, 0, S, S);
    // graticule: outer circle (100 %), 75 % colour targets, skin tone line
    ctx.strokeStyle = 'rgba(255,255,255,.18)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(c0, c0, radius, 0, 2 * Math.PI); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(c0 - radius, c0); ctx.lineTo(c0 + radius, c0); ctx.moveTo(c0, c0 - radius); ctx.lineTo(c0, c0 + radius); ctx.stroke();
    const at = (cb, cr) => [c0 + cb * 2 * radius, c0 - cr * 2 * radius];
    for (const [label, rgb] of [['R', [0.75, 0, 0]], ['Mg', [0.75, 0, 0.75]], ['B', [0, 0, 0.75]],
                                ['Cy', [0, 0.75, 0.75]], ['G', [0, 0.75, 0]], ['Yl', [0.75, 0.75, 0]]]) {
      const [x, y] = at(...cbcr(...rgb));
      ctx.strokeStyle = 'rgba(255,255,255,.35)'; ctx.strokeRect(x - 3.5, y - 3.5, 7, 7);
      ctx.fillStyle = 'rgba(255,255,255,.4)'; ctx.font = '8px sans-serif';
      ctx.fillText(label, x + 5, y - 4);
    }
    const skin = 123 * Math.PI / 180;
    ctx.setLineDash([4, 4]); ctx.strokeStyle = 'rgba(255,200,150,.35)';
    ctx.beginPath(); ctx.moveTo(c0, c0); ctx.lineTo(c0 + Math.cos(skin) * radius * 0.92, c0 - Math.sin(skin) * radius * 0.92); ctx.stroke();
    ctx.setLineDash([]);
    const cell = 2 * radius / N, logPeak = Math.log1p(peak);
    for (let k = 0; k < counts.length; k++) {
      const n = counts[k];
      if (!n) continue;
      const a = 0.4 + 0.6 * Math.log1p(n) / logPeak;
      // the dot takes the average colour of its pixels, brightened so dark tones stay visible
      let r = sums[k * 3] / n, g = sums[k * 3 + 1] / n, b = sums[k * 3 + 2] / n;
      const m = Math.max(r, g, b, 0.01);
      r = 0.35 + 0.65 * r / m; g = 0.35 + 0.65 * g / m; b = 0.35 + 0.65 * b / m;
      ctx.fillStyle = `rgba(${r * 255 | 0},${g * 255 | 0},${b * 255 | 0},${a.toFixed(2)})`;
      const x = k % N, y = (k / N) | 0;
      ctx.fillRect(c0 - radius + x * cell, c0 - radius + y * cell, Math.max(1, cell), Math.max(1, cell));
    }
  }

  // The picture as shown (LUT and assists included) at the live view's own size, as a PNG data URL; guides
  // and scopes are left out. null when there is no picture.
  async function still() {
    let frame = source;
    if (!frame) {                          // no tool reading frames: ask the app for the current one
      try {
        const response = await fetch('/frame.jpg?' + Date.now(), { cache: 'no-store' });
        if (!response.ok) return null;
        frame = await createImageBitmap(await response.blob());
      } catch (e) { return null; }
    }
    const iw = frame.width, ih = frame.height;
    if (gl && source && (prefs.falseColor || prefs.zebras || prefs.peaking || lutActive())) {
      if (uploadedSeq !== sourceSeq) upload();
      const { width, height } = glCanvas;
      glCanvas.width = iw; glCanvas.height = ih;
      drawGL();
      const url = glCanvas.toDataURL('image/png');   // read before the buffer is presented and cleared
      glCanvas.width = width; glCanvas.height = height;
      if (!glCanvas.hidden) drawGL();
      return url;
    }
    const canvas = document.createElement('canvas');
    canvas.width = iw; canvas.height = ih;
    canvas.getContext('2d').drawImage(frame, 0, 0);
    if (frame !== source) frame.close();
    return canvas.toDataURL('image/png');
  }

  // ---- frame reader: the MJPEG stream (parts of "Content-Length: N", blank line, N bytes of JPEG)
  const HEADER_END = [13, 10, 13, 10];
  function headerEnd(buf) {
    const limit = Math.min(buf.length, 512) - 4;
    next: for (let i = 0; i <= limit; i++) {
      for (let k = 0; k < 4; k++) if (buf[i + k] !== HEADER_END[k]) continue next;
      return i;
    }
    return -1;
  }

  async function readStream() {
    while (streamOn) {
      const abort = new AbortController();
      streamAbort = abort;
      try {
        const response = await fetch('/stream?tools', { cache: 'no-store', signal: abort.signal });
        const reader = response.body.getReader();
        let buf = new Uint8Array(0);
        while (streamOn) {
          const { value, done } = await reader.read();
          if (done) break;
          const joined = new Uint8Array(buf.length + value.length);
          joined.set(buf); joined.set(value, buf.length);
          buf = joined;
          for (;;) {
            const end = headerEnd(buf);
            if (end < 0) break;
            const length = /content-length:\s*(\d+)/i.exec(new TextDecoder().decode(buf.subarray(0, end)));
            const start = end + 4;
            if (!length) { buf = buf.subarray(start); continue; }
            if (buf.length < start + +length[1]) break;
            decode(buf.slice(start, start + +length[1]));
            buf = buf.subarray(start + +length[1]);
          }
        }
        abort.abort();
      } catch (e) { /* stopped, or the app restarted */ }
      if (streamOn) await new Promise(r => setTimeout(r, 1000));
    }
  }

  function decode(jpeg) {
    if (decoding) return;                  // the previous frame is still decoding: skip this one
    decoding = true;
    createImageBitmap(new Blob([jpeg], { type: 'image/jpeg' })).then(bitmap => {
      if (!streamOn) { bitmap.close(); return; }
      if (source) source.close();
      source = bitmap;
      sourceSeq++;
    }).catch(() => {}).finally(() => { decoding = false; });
  }

  return { init, update, toggle, set, get, glReady, loadLut, clearLut, lutName, lutActive, still };
})();
