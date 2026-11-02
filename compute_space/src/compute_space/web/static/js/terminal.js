var config = JSON.parse(document.getElementById('page-config').textContent);

(function() {
  var FONT_SIZE = 14;
  var FONT_WAIT_MS = 3000;

  // xterm measures its character cell once, when it opens. Opening before the web font has loaded would size the
  // grid for the fallback font, so wait for it first (bounded, so a stalled font request can't keep it closed).
  function whenFontReady(fontFamily) {
    if (!document.fonts || !document.fonts.load) return Promise.resolve();
    var timeout = new Promise(function(resolve) { setTimeout(resolve, FONT_WAIT_MS); });
    return Promise.race([document.fonts.load(FONT_SIZE + 'px ' + fontFamily), timeout]).catch(function() {});
  }

  var fontFamily = getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim();

  whenFontReady(fontFamily).then(function() {
    var term = new Terminal({
      cursorBlink: true,
      fontSize: FONT_SIZE,
      fontFamily: fontFamily,
      theme: {
        background: getComputedStyle(document.documentElement).getPropertyValue('--fg-primary').trim(),
        brightBlack: '#8b8b8b',
      },
    });
    var fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('terminal'));
    fitAddon.fit();

    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var ws = new WebSocket(proto + '//' + window.location.host + config.terminalWsUrl);
    ws.binaryType = 'arraybuffer';

    function sendResize() {
      var dims = fitAddon.proposeDimensions();
      if (dims && ws.readyState === WebSocket.OPEN) {
        var json = new TextEncoder().encode(JSON.stringify({type: 'resize', cols: dims.cols, rows: dims.rows}));
        var buf = new Uint8Array(1 + json.length);
        buf[0] = 0x01;
        buf.set(json, 1);
        ws.send(buf.buffer);
      }
    }

    ws.onopen = function() { sendResize(); };

    ws.onmessage = function(ev) {
      if (ev.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(ev.data));
      } else {
        term.write(ev.data);
      }
    };

    ws.onclose = function() { term.write('\r\n\x1b[90m[Connection closed]\x1b[0m\r\n'); };

    term.onData(function(data) {
      if (ws.readyState === WebSocket.OPEN) {
        var encoded = new TextEncoder().encode(data);
        var buf = new Uint8Array(1 + encoded.length);
        buf[0] = 0x00;
        buf.set(encoded, 1);
        ws.send(buf.buffer);
      }
    });

    window.addEventListener('resize', function() { fitAddon.fit(); sendResize(); });
  });
})();
