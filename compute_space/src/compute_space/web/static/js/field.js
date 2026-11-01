// Editing a control clears its field's error, so the hint comes back. Delegated
// from the document, so fields rendered after load work without re-binding.

(function () {
  document.addEventListener('input', function (e) {
    var control = e.target;
    var field = control.closest('.field');
    // A custom validity means the page validates this control live, as Setup
    // does for its username, and that check decides when the error goes.
    if (!field || control.validity.customError) return;

    control.classList.remove('invalid');
    var error = field.querySelector(':scope > .field-error');
    if (error) error.hidden = true;
  });
})();
