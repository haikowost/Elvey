/* "Send to KYC" bookmarklet. Built into a javascript: link by dashboard.bookmarklet_source() - the
   __PLACEHOLDERS__ are filled in there (port, the harvester's own JS_PROFILE page reader, photo selector).
   It only READS the LinkedIn profile you are looking at and sends it to your local KYC app. */
(function () {
  var BASE = 'http://127.0.0.1:__PORT__';
  function toast(html, ok) {
    var old = document.getElementById('elvey-kyc-toast');
    if (old) old.remove();
    var el = document.createElement('div');
    el.id = 'elvey-kyc-toast';
    el.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;padding:12px 14px;' +
      'border-radius:10px;font:14px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.25);color:#fff;' +
      'background:' + (ok === false ? '#b4413a' : '#1f5fbf');
    el.innerHTML = html;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, ok === false ? 12000 : 6000);
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  if (!/linkedin\.com\/in\//i.test(location.href)) {
    toast('Send to KYC: open someone\'s LinkedIn profile (linkedin.com/in/...) first.', false);
    return;
  }
  var read = __JS_PROFILE__;
  var d = read() || {};
  var main = document.querySelector('main') || document.body;
  var img = main.querySelector(__PHOTO_SELECTOR__);
  var keep = /^(about|experience)/i;
  var payload = {
    url: location.href.split('?')[0].split('#')[0],
    photo_src: img && /^https:/.test(img.src) ? img.src : '',
    data: {
      name: d.name, headline: d.headline, about: d.about, experience: d.experience, title: d.title,
      topLines: d.topLines, text: (d.text || []).slice(0, 400),
      sections: (d.sections || []).filter(function (s) { return keep.test(s.heading || ''); }),
      connection_degree: d.connection_degree, current_company_top: d.current_company_top, education_top: d.education_top
    }
  };
  function picker(q) {
    var w = window.open(BASE + '/capture' + q, 'elvey_kyc_capture', 'width=780,height=760');
    if (!w) toast('<a style="color:#fff;text-decoration:underline" target="_blank" href="' + esc(BASE + '/capture' + q) +
                  '">Open the KYC picker</a> to match this profile to a contact.');
  }
  toast('Sending ' + esc(d.name || 'this profile') + ' to Elvey KYC…');
  fetch(BASE + '/api/capture', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.status === 'saved') {
        var c = j.contact || {};
        toast('Saved to Elvey KYC: <b>' + esc(c.name) + '</b>' + (c.company ? ' · ' + esc(c.company) : '') +
              (c.role ? '<br>' + esc(c.role) : ''));
      } else if (j.status === 'needs_match') {
        toast('Elvey KYC: ' + esc(j.reason) + ' - pick who this is in the window that opens.');
        picker('?id=' + j.capture_id);
      } else {
        toast('Elvey KYC could not use this page: ' + esc(j.error || j.detail || 'unknown problem'), false);
      }
    })
    .catch(function () {
      // LinkedIn's page security can block the direct call: hand the same data to the local page instead
      picker('#' + encodeURIComponent(JSON.stringify(payload)));
    });
})();
