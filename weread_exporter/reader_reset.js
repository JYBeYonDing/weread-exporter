// Rewrite the reader's restored reading position to the start of the chapter.
//
// WeRead stores a per-book reading position (`reader.progress.book.chapterOffset`
// plus a `progress` percentage) and the reader resumes there instead of at the
// chapter's beginning. For an export that is fatal: 《一本书读懂客户心理》's
// chapter 9 had chapterOffset 15725 / progress 99%, so opening it rendered only
// the last sliver — 5,842 of 16,551 characters — while still reporting
// "下一章" (chapter end) and never drawing the rest to the canvas. Retrying hit
// the same saved offset every time, which is why the exporter could not recover
// on its own.
//
// `window.__INITIAL_STATE__` carries that position; the reader reads it while
// painting the first page. Intercepting the assignment lets us zero the offset
// before the reader ever sees it. Verified on the real page: the same chapter
// then renders from "## 第六章 获取承诺…" with the footer showing "下一页"
// (more pages to come) instead of "下一章".
//
// Side effect: the reader autosaves as it turns pages, so the account's saved
// position for an exported chapter ends up wherever the export left it. That is
// already true of exporting at all; this only changes where it starts from.
(function () {
  'use strict';
  var patched = 0;

  function reset(state) {
    try {
      var book = state && state.reader && state.reader.progress &&
                 state.reader.progress.book;
      if (book && book.chapterOffset !== undefined) {
        if (book.chapterOffset !== 0) {
          patched += 1;
          console.log('[reader-reset] chapterOffset ' + book.chapterOffset +
                      ' -> 0 (progress ' + book.progress + ')');
        }
        book.chapterOffset = 0;
        book.progress = 0;
        book.summary = '';
      }
    } catch (err) {
      console.log('[reader-reset] failed: ' + err);
    }
    return state;
  }

  var stored;
  Object.defineProperty(window, '__INITIAL_STATE__', {
    configurable: true,
    get: function () { return stored; },
    set: function (value) { stored = reset(value); }
  });

  // Expose a counter so the exporter can confirm the patch actually fired.
  window.__readerResetPatched = patched;
})();
