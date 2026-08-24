// Click/keyboard half of the info-tip component (hover is pure CSS). Delegated
// from the document, so tips rendered after load work without re-binding.

(function () {
  function bodyOf(toggle) {
    return document.getElementById(toggle.getAttribute('aria-controls'));
  }

  function close(toggle) {
    toggle.setAttribute('aria-expanded', 'false');
  }

  function closeAll(except) {
    var open = document.querySelectorAll('.info-tip__toggle[aria-expanded="true"]');
    Array.prototype.forEach.call(open, function (t) {
      if (t !== except) close(t);
    });
  }

  // Hangs off the icon, then slides back inside the viewport. Anchoring alone
  // is not enough: a tip in a right-hand column opens past the right edge, and
  // on a phone a tip wider than the space either side of its icon overflows
  // whichever edge it is pinned to.
  var MARGIN = 8;

  function place(toggle) {
    var tip = toggle.closest('.info-tip');
    var body = bodyOf(toggle);
    if (!tip || !body) return;
    body.style.left = '';
    body.style.right = '';
    var anchor = tip.getBoundingClientRect().left;
    var width = body.getBoundingClientRect().width;
    var rightmost = Math.max(MARGIN, document.documentElement.clientWidth - width - MARGIN);
    body.style.left = Math.min(Math.max(anchor, MARGIN), rightmost) - anchor + 'px';
    body.style.right = 'auto';
  }

  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('.info-tip__toggle');
    if (toggle) {
      var wasOpen = toggle.getAttribute('aria-expanded') === 'true';
      closeAll(toggle);
      toggle.setAttribute('aria-expanded', wasOpen ? 'false' : 'true');
      if (!wasOpen) place(toggle);
      return;
    }
    if (!e.target.closest('.info-tip__body')) closeAll(null);
  });

  // Hover opens the tip in CSS alone, so the overflow check has to run here too.
  document.addEventListener('mouseover', function (e) {
    var tip = e.target.closest && e.target.closest('.info-tip');
    if (tip) place(tip.querySelector('.info-tip__toggle'));
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var open = document.querySelector('.info-tip__toggle[aria-expanded="true"]');
    if (!open) return;
    close(open);
    open.focus();
  });
})();
