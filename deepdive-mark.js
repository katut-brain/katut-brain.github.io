// 深掘り済みのカードを薄くして、ボタンを「続きを話す」にする。
// 一覧（deepdive_state.json）は毎晩の Routine が Vault から作る（deepdive_state.py）。
// 取れなかったら何もしない（印が付かないだけで、ページは今までどおり）。
(function () {
  var s = document.currentScript;
  if (!s) return;
  var url = s.src.replace(/deepdive-mark\.js(\?.*)?$/, 'deepdive_state.json');
  var st = document.createElement('style');
  st.textContent = '.vcard.dd-done{opacity:.5}.vcard.dd-done:hover{opacity:1}';
  document.head.appendChild(st);
  fetch(url, { cache: 'no-cache' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (!j || !Array.isArray(j.done)) return;
      var done = {};
      j.done.forEach(function (rid) { done[String(rid)] = true; });
      document.querySelectorAll('.deepdive[data-rid]').forEach(function (b) {
        if (!done[b.getAttribute('data-rid')]) return;
        var card = b.closest('.vcard');
        if (card) card.classList.add('dd-done');
        b.textContent = '💬 続きを話す';
      });
    })
    .catch(function () {});
})();
