// 문제 영역 하나만 갈아 끼운다. 프레임워크가 필요한 일이 아니다.
(function () {
  "use strict";

  var slot = document.getElementById("card");
  if (!slot) return;

  function swap(html) {
    slot.innerHTML = html;
    bind();
    var typed = slot.querySelector(".typed");
    if (typed) typed.focus();
  }

  async function send(url, options) {
    var opts = options || {};
    opts.headers = Object.assign({ "X-Partial": "1" }, opts.headers || {});
    var res = await fetch(url, opts);

    var redirect = res.headers.get("X-Redirect");
    if (redirect) { location.href = redirect; return; }

    if (res.status === 409) {
      // 문제 토큰이 만료됐다. 새 문제를 받아 오면 된다.
      return send("/study/card");
    }
    if (!res.ok) {
      slot.innerHTML =
        '<section class="panel stack"><p class="alert">문제를 불러오지 못했습니다.</p>' +
        '<button class="primary block" type="button" onclick="location.reload()">다시 시도</button></section>';
      return;
    }
    swap(await res.text());
  }

  function bind() {
    var form = slot.querySelector("#answer-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var data = new FormData(form);
        // 어떤 보기를 눌렀는지는 submitter 로만 알 수 있다.
        var pressed = event.submitter;
        if (pressed && pressed.name) data.set(pressed.name, pressed.value);
        setBusy(form, true);
        send(form.action, { method: "POST", body: data });
      });
    }

    var next = slot.querySelector("#next");
    if (next) {
      next.addEventListener("click", function () {
        setBusy(next.parentNode, true);
        send(next.dataset.url);
      });
      next.focus();
    }
  }

  function setBusy(scope, busy) {
    scope.querySelectorAll("button").forEach(function (b) { b.disabled = busy; });
  }

  // 숫자 키로 보기를 고르고, 채점 화면에서는 Enter/Space 로 넘어간다.
  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    var next = slot.querySelector("#next");
    if (next && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      next.click();
      return;
    }

    var choices = slot.querySelectorAll(".choice");
    if (choices.length && /^[1-9]$/.test(event.key)) {
      var picked = choices[Number(event.key) - 1];
      if (picked) { event.preventDefault(); picked.click(); }
    }
  });

  bind();
})();
