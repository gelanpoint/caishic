/**
 * qr.js —— 纯本地二维码编码器（byte 模式 / 纠错等级 M / 版本 1~10）。
 *
 * 为什么自己写：本项目的硬约束是**前端严禁引用任何 CDN 或外部资源**（`AGENTS.md` §3 硬要求 4），
 * 所以不能引任何一个现成的二维码库。本文件是仓库内本地文件，零依赖。
 *
 * 对外接口（同步）：
 *   window.MTQR.render(canvas, text, opts)  → 在 canvas 上画二维码；返回 {version, size, modules}
 *   window.MTQR.toDataURL(text, opts)       → 返回 PNG dataURL（可直接塞进 <img src>）
 *   window.MTQR.matrix(text)                → 返回 {size, version, m}（m 为 0/1 二维数组，便于测试）
 *   opts = {scale: 每模块像素数（默认 6）, margin: 静区模块数（默认 4）}
 *
 * 文本过长（超出本实现支持范围）时 **throw Error**，不静默截断 —— 宁可报错也不画一个内容不对的码。
 *
 * ⚠️ 验证到什么程度（如实标注，不夸大）：
 *   - 本实现已与一个**独立的第三方编码器**（goQR.me 公开接口）做过**逐模块比对**：
 *     同版本 / 同掩码下 3 个 payload 全部 **0 处差异**（841 个模块逐个比）；
 *   - 同时有"编码→解码"往返自检（本仓库 `/tmp` 的参考实现，不进仓库）；
 *   - **但没有用真实扫码设备（手机摄像头）实扫过** —— 本机没有摄像头与解码器。
 *     故对外只能说"与独立实现逐模块一致"，**不能说"已用真机扫码验证"**。
 */
(function (root) {
  "use strict";

  // ---------------------------------------------------------------- 常量表（等级 M）
  var EC_M = {
    1: [10, [[1, 16]]],
    2: [16, [[1, 28]]],
    3: [26, [[1, 44]]],
    4: [18, [[2, 32]]],
    5: [24, [[2, 43]]],
    6: [16, [[4, 27]]],
    7: [18, [[4, 31]]],
    8: [22, [[2, 38], [2, 39]]],
    9: [22, [[3, 36], [2, 37]]],
    10: [26, [[4, 43], [1, 44]]]
  };
  var ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]
  };
  //: 等级 M 的格式信息 15 位串（BCH(15,5)），下标 = 掩码号
  var FORMAT_M = [0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0];
  var VERSION_INFO = { 7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3 };

  // ---------------------------------------------------------------- GF(256) / Reed-Solomon
  var EXP = new Array(512), LOG = new Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      EXP[i] = x; LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11D;
    }
    for (var j = 255; j < 512; j++) EXP[j] = EXP[j - 255];
  })();

  function gmul(a, b) {
    if (a === 0 || b === 0) return 0;
    return EXP[LOG[a] + LOG[b]];
  }

  function genPoly(n) {
    var g = [1];
    for (var i = 0; i < n; i++) {
      var next = new Array(g.length + 1).fill(0);
      for (var a = 0; a < g.length; a++) {
        next[a] ^= gmul(g[a], 1);
        next[a + 1] ^= gmul(g[a], EXP[i]);
      }
      g = next;
    }
    return g;
  }

  function rsEcc(data, n) {
    var g = genPoly(n);
    var rem = data.concat(new Array(n).fill(0));
    for (var i = 0; i < data.length; i++) {
      var c = rem[i];
      if (c) for (var j = 0; j < g.length; j++) rem[i + j] ^= gmul(g[j], c);
    }
    return rem.slice(data.length);
  }

  // ---------------------------------------------------------------- 编码
  function capacity(v) {
    var gs = EC_M[v][1], t = 0;
    for (var i = 0; i < gs.length; i++) t += gs[i][0] * gs[i][1];
    return t;
  }

  function pickVersion(nbytes) {
    for (var v = 1; v <= 10; v++) {
      var head = 4 + (v < 10 ? 8 : 16);
      if (nbytes * 8 + head <= capacity(v) * 8) return v;
    }
    throw new Error("MTQR: 文本过长（本实现支持到版本 10 / 等级 M，约 213 字节）");
  }

  function bitstream(data, v) {
    var bits = [];
    function put(val, n) { for (var i = n - 1; i >= 0; i--) bits.push((val >> i) & 1); }
    put(0x4, 4);
    put(data.length, v < 10 ? 8 : 16);
    for (var i = 0; i < data.length; i++) put(data[i], 8);
    var cap = capacity(v) * 8;
    var term = Math.min(4, cap - bits.length);
    for (var t = 0; t < term; t++) bits.push(0);
    while (bits.length % 8) bits.push(0);
    var cws = [];
    for (var b = 0; b < bits.length; b += 8) {
      var byte = 0;
      for (var k = 0; k < 8; k++) byte = (byte << 1) | bits[b + k];
      cws.push(byte);
    }
    var pad = [0xEC, 0x11], p = 0;
    while (cws.length < capacity(v)) { cws.push(pad[p % 2]); p++; }
    return cws;
  }

  function interleave(cws, v) {
    var ecPer = EC_M[v][0], groups = EC_M[v][1];
    var blocks = [], pos = 0, i, j;
    for (i = 0; i < groups.length; i++) {
      for (j = 0; j < groups[i][0]; j++) {
        blocks.push(cws.slice(pos, pos + groups[i][1]));
        pos += groups[i][1];
      }
    }
    var eccs = blocks.map(function (b) { return rsEcc(b, ecPer); });
    var out = [], maxLen = 0;
    blocks.forEach(function (b) { maxLen = Math.max(maxLen, b.length); });
    for (i = 0; i < maxLen; i++) {
      for (j = 0; j < blocks.length; j++) if (i < blocks[j].length) out.push(blocks[j][i]);
    }
    for (i = 0; i < ecPer; i++) for (j = 0; j < eccs.length; j++) out.push(eccs[j][i]);
    return out;
  }

  // ---------------------------------------------------------------- 矩阵构建
  function build(v) {
    var n = v * 4 + 17, r, c;
    var m = [], f = [];
    for (r = 0; r < n; r++) {
      m.push(new Array(n).fill(0));
      f.push(new Array(n).fill(false));
    }
    function mark(rr, cc, val) { m[rr][cc] = val; f[rr][cc] = true; }

    function finder(r0, c0) {
      for (var dr = -1; dr <= 7; dr++) {
        for (var dc = -1; dc <= 7; dc++) {
          var rr = r0 + dr, cc = c0 + dc;
          if (rr < 0 || rr >= n || cc < 0 || cc >= n) continue;
          if (dr === -1 || dr === 7 || dc === -1 || dc === 7) mark(rr, cc, 0);
          else {
            var edge = (dr === 0 || dr === 6 || dc === 0 || dc === 6);
            var core = (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4);
            mark(rr, cc, (edge || core) ? 1 : 0);
          }
        }
      }
    }
    finder(0, 0); finder(0, n - 7); finder(n - 7, 0);

    for (var i = 0; i < n; i++) {                       // 定位图形
      if (!f[6][i]) mark(6, i, i % 2 === 0 ? 1 : 0);
      if (!f[i][6]) mark(i, 6, i % 2 === 0 ? 1 : 0);
    }
    var pos = ALIGN[v];                                 // 校正图形
    for (var a = 0; a < pos.length; a++) {
      for (var b = 0; b < pos.length; b++) {
        var ar = pos[a], ac = pos[b];
        if ((ar === 6 && ac === 6) || (ar === 6 && ac === n - 7) || (ar === n - 7 && ac === 6)) continue;
        for (var dr2 = -2; dr2 <= 2; dr2++) {
          for (var dc2 = -2; dc2 <= 2; dc2++) {
            mark(ar + dr2, ac + dc2, Math.max(Math.abs(dr2), Math.abs(dc2)) !== 1 ? 1 : 0);
          }
        }
      }
    }
    for (i = 0; i < 9; i++) {                           // 格式信息区（**不覆盖**定位图形 (8,6)/(6,8)）
      if (!f[8][i]) mark(8, i, 0);
      if (!f[i][8]) mark(i, 8, 0);
    }
    for (i = 0; i < 8; i++) {
      if (!f[8][n - 1 - i]) mark(8, n - 1 - i, 0);
      if (!f[n - 1 - i][8]) mark(n - 1 - i, 8, 0);
    }
    mark(n - 8, 8, 1);                                  // 固定黑模块

    if (v >= 7) {                                       // 版本信息区
      for (i = 0; i < 18; i++) {
        mark(n - 11 + (i % 3), Math.floor(i / 3), 0);
        mark(Math.floor(i / 3), n - 11 + (i % 3), 0);
      }
    }
    return { m: m, f: f, n: n };
  }

  function place(g, bits) {
    var n = g.n, idx = 0, col = n - 1, up = true;
    while (col > 0) {
      if (col === 6) col -= 1;
      for (var k = 0; k < n; k++) {
        var r = up ? (n - 1 - k) : k;
        for (var d = 0; d < 2; d++) {
          var c = col - d;
          if (!g.f[r][c]) { g.m[r][c] = idx < bits.length ? bits[idx] : 0; idx++; }
        }
      }
      up = !up;
      col -= 2;
    }
  }

  var MASKS = [
    function (r, c) { return (r + c) % 2 === 0; },
    function (r) { return r % 2 === 0; },
    function (r, c) { return c % 3 === 0; },
    function (r, c) { return (r + c) % 3 === 0; },
    function (r, c) { return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0; },
    function (r, c) { return (r * c) % 2 + (r * c) % 3 === 0; },
    function (r, c) { return ((r * c) % 2 + (r * c) % 3) % 2 === 0; },
    function (r, c) { return ((r + c) % 2 + (r * c) % 3) % 2 === 0; }
  ];

  function clone(g) {
    var out = { n: g.n, f: g.f, m: [] };
    for (var r = 0; r < g.n; r++) out.m.push(g.m[r].slice());
    return out;
  }

  function applyMask(g, k) {
    var out = clone(g);
    for (var r = 0; r < g.n; r++) {
      for (var c = 0; c < g.n; c++) {
        if (!g.f[r][c] && MASKS[k](r, c)) out.m[r][c] ^= 1;
      }
    }
    return out;
  }

  function writeFormat(g, k) {
    var n = g.n, fmt = FORMAT_M[k], bits = [], i;
    for (i = 0; i < 15; i++) bits.push((fmt >> (14 - i)) & 1);   // bits[0] = 最高位
    for (i = 0; i < 6; i++) g.m[8][i] = bits[i];
    g.m[8][7] = bits[6];
    g.m[8][8] = bits[7];
    g.m[7][8] = bits[8];
    for (i = 9; i < 15; i++) g.m[14 - i][8] = bits[i];
    // 副本 2：位序与副本 1 **相反**（已对独立第三方实现逐模块反推确认）
    for (i = 0; i < 8; i++) g.m[8][n - 1 - i] = bits[14 - i];
    for (i = 0; i < 7; i++) g.m[n - 7 + i][8] = bits[6 - i];
    var v = (n - 17) / 4;
    if (v >= 7 && VERSION_INFO[v]) {
      for (i = 0; i < 18; i++) {
        var bit = (VERSION_INFO[v] >> i) & 1;
        g.m[n - 11 + (i % 3)][Math.floor(i / 3)] = bit;
        g.m[Math.floor(i / 3)][n - 11 + (i % 3)] = bit;
      }
    }
  }

  function penalty(g) {
    var n = g.n, score = 0, r, c, i, j;
    var lines = [];
    for (r = 0; r < n; r++) lines.push(g.m[r]);
    for (c = 0; c < n; c++) { var colv = []; for (r = 0; r < n; r++) colv.push(g.m[r][c]); lines.push(colv); }
    for (i = 0; i < lines.length; i++) {
      var line = lines[i], run = 1, prev = line[0];
      for (j = 1; j < line.length; j++) {
        if (line[j] === prev) run++;
        else { if (run >= 5) score += 3 + (run - 5); run = 1; prev = line[j]; }
      }
      if (run >= 5) score += 3 + (run - 5);
    }
    for (r = 0; r < n - 1; r++) {
      for (c = 0; c < n - 1; c++) {
        if (g.m[r][c] === g.m[r][c + 1] && g.m[r][c] === g.m[r + 1][c] && g.m[r][c] === g.m[r + 1][c + 1]) score += 3;
      }
    }
    return score;
  }

  function encode(text) {
    var data = [];
    for (var i = 0; i < text.length; i++) {                 // UTF-8 编码
      var cp = text.charCodeAt(i);
      if (cp < 0x80) data.push(cp);
      else if (cp < 0x800) { data.push(0xC0 | (cp >> 6), 0x80 | (cp & 0x3F)); }
      else { data.push(0xE0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3F), 0x80 | (cp & 0x3F)); }
    }
    var v = pickVersion(data.length);
    var cws = interleave(bitstream(data, v), v);
    var bits = [];
    for (i = 0; i < cws.length; i++) for (var b = 7; b >= 0; b--) bits.push((cws[i] >> b) & 1);

    var base = build(v);
    place(base, bits);
    var best = null, bestScore = null;
    for (var k = 0; k < 8; k++) {
      var cand = applyMask(base, k);
      writeFormat(cand, k);
      var s = penalty(cand);
      if (bestScore === null || s < bestScore) { best = cand; bestScore = s; }
    }
    return { size: best.n, version: v, m: best.m };
  }

  // ---------------------------------------------------------------- 绘制
  function draw(canvas, text, opts) {
    opts = opts || {};
    var scale = opts.scale || 6, margin = opts.margin === undefined ? 4 : opts.margin;
    var q = encode(text), total = (q.size + margin * 2) * scale;
    canvas.width = total; canvas.height = total;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, total, total);
    ctx.fillStyle = "#000000";
    for (var r = 0; r < q.size; r++) {
      for (var c = 0; c < q.size; c++) {
        if (q.m[r][c]) ctx.fillRect((c + margin) * scale, (r + margin) * scale, scale, scale);
      }
    }
    return { version: q.version, size: q.size, modules: q.size };
  }

  root.MTQR = {
    matrix: encode,
    render: draw,
    toDataURL: function (text, opts) {
      var c = document.createElement("canvas");
      draw(c, text, opts);
      return c.toDataURL("image/png");
    }
  };
})(typeof window !== "undefined" ? window : this);
