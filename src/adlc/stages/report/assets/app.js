  (function () {
    var root = document.documentElement;
    document.getElementById('theme').addEventListener('click', function () {
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    });
    document.querySelectorAll('.hash').forEach(function (el) {
      el.addEventListener('click', function () {
        navigator.clipboard && navigator.clipboard.writeText(el.title);
        var old = el.textContent; el.textContent = 'copied';
        setTimeout(function () { el.textContent = old; }, 900);
      });
    });
    // Mermaid is progressive enhancement: offline the diagram source stays readable.
    // Its <script> is deferred so a CDN that blackholes the connection (captive
    // portal, corporate proxy) cannot hold the OS connect timeout -- ~21s on
    // Windows, ~130s on Linux -- in front of the handlers above. Deferred scripts
    // run before DOMContentLoaded, so by here mermaid has either loaded or failed.
    function initMermaid() {
      if (window.mermaid) {
        // startOnLoad:false + explicit run(): initialising from inside a
        // DOMContentLoaded handler races mermaid's own auto-start listener.
        mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
        mermaid.run();
      } else {
        document.querySelectorAll('.mermaid').forEach(function (el) {
          var pre = document.createElement('pre'); pre.textContent = el.textContent;
          el.replaceWith(pre);
        });
      }
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initMermaid);
    } else {
      initMermaid();
    }
  })();