/* ==========================================================================
   Intelligence Designed To Evolve - landing interactions
   1. Staggered count-up for the four stat metrics
   2. Mobile burger menu
   ========================================================================== */

(function () {
  "use strict";

  /* ------------------------------------------------------------------------
     1) Count-up
     ------------------------------------------------------------------------ */

  var easeOutCubic = function (t) {
    return 1 - Math.pow(1 - t, 3);
  };

  var prefersReducedMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var countersStarted = false;

  function readStat(el) {
    return {
      target: parseFloat(el.getAttribute("data-count")),
      decimals: parseInt(el.getAttribute("data-decimals"), 10) || 0
    };
  }

  function setFinal(el, decimals, target) {
    el.textContent = target.toFixed(decimals);
  }

  function runCounters(list) {
    if (countersStarted) {
      return;
    }
    countersStarted = true;

    list.forEach(function (el, i) {
      var stat = readStat(el);

      if (prefersReducedMotion) {
        setFinal(el, stat.decimals, stat.target);
        return;
      }

      el.textContent = (0).toFixed(stat.decimals);

      window.setTimeout(function () {
        var duration = 1500 + i * 80;
        var startedAt = null;

        function frame(now) {
          if (startedAt === null) {
            startedAt = now;
          }
          var progress = Math.min((now - startedAt) / duration, 1);
          el.textContent = (stat.target * easeOutCubic(progress)).toFixed(stat.decimals);

          if (progress < 1) {
            window.requestAnimationFrame(frame);
          } else {
            setFinal(el, stat.decimals, stat.target);
          }
        }

        window.requestAnimationFrame(frame);
      }, 480 + i * 90);
    });
  }

  var counters = Array.prototype.slice.call(document.querySelectorAll(".count"));
  var stats = document.getElementById("stats");

  if (counters.length) {
    if (typeof window.IntersectionObserver === "function" && stats) {
      var observer = new window.IntersectionObserver(
        function (entries) {
          for (var i = 0; i < entries.length; i++) {
            if (entries[i].isIntersecting) {
              runCounters(counters);
              observer.disconnect();
              return;
            }
          }
        },
        { threshold: 0.25 }
      );

      observer.observe(stats);
    } else {
      runCounters(counters);
    }
  }

  /* ------------------------------------------------------------------------
     2) Mobile burger menu
     ------------------------------------------------------------------------ */

  var burger = document.getElementById("burger");
  var overlay = document.getElementById("menuOverlay");
  var menu = document.getElementById("mobileMenu");

  if (burger && overlay && menu) {
    var setMenuOpen = function (open) {
      document.body.classList.toggle("menu-open", open);
      burger.setAttribute("aria-expanded", open ? "true" : "false");
      burger.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      overlay.hidden = !open;
      menu.hidden = !open;
    };

    burger.addEventListener("click", function () {
      setMenuOpen(burger.getAttribute("aria-expanded") !== "true");
    });

    overlay.addEventListener("click", function () {
      setMenuOpen(false);
    });

    menu.addEventListener("click", function (event) {
      if (event.target && event.target.tagName === "A") {
        setMenuOpen(false);
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" || event.key === "Esc") {
        setMenuOpen(false);
      }
    });

    window.addEventListener("resize", function () {
      if (window.innerWidth > 720) {
        setMenuOpen(false);
      }
    });
  }
})();
