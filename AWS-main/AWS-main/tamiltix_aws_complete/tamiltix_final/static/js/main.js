// TamilTix — main.js

// ── Auto-dismiss flash messages after 4 seconds ──────────────
document.addEventListener('DOMContentLoaded', function() {
  setTimeout(function() {
    document.querySelectorAll('.flash').forEach(function(f) {
      f.style.transition = 'opacity 0.5s, transform 0.5s';
      f.style.opacity = '0';
      f.style.transform = 'translateX(20px)';
      setTimeout(function() { f.remove(); }, 500);
    });
  }, 4000);

  // ── Poster fallback for non-home pages (booking, ticket, etc.) ──
  // These pages don't have the inline fallback script
  document.querySelectorAll('img[data-fallback]').forEach(function(img) {
    var tried = false;
    img.addEventListener('error', function() {
      if (!tried) {
        tried = true;
        var fb = img.getAttribute('data-fallback');
        if (fb && fb !== img.src) { img.src = fb; return; }
      }
      img.style.display = 'none';
      var next = img.nextElementSibling;
      if (next) next.style.display = 'flex';
    });
    if (img.complete && img.naturalWidth === 0) {
      img.dispatchEvent(new Event('error'));
    }
  });
});
