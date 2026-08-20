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
    if (window.mermaid) {
      mermaid.initialize({ startOnLoad: true, theme: 'dark', securityLevel: 'strict' });
    } else {
      document.querySelectorAll('.mermaid').forEach(function (el) {
        var pre = document.createElement('pre'); pre.textContent = el.textContent;
        el.replaceWith(pre);
      });
    }
  })();