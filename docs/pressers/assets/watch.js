// "Watch" on a quote card: nothing loads from YouTube until it is clicked. The link points at
// the video on youtube.com (it still works without JavaScript); with JavaScript the click puts
// a youtube-nocookie player in its place, starting at the quote's second.
document.addEventListener("click", function (e) {
  var a = e.target.closest && e.target.closest("a.watch");
  if (!a) return;
  var v = a.getAttribute("data-v"), t = parseInt(a.getAttribute("data-t"), 10) || 0;
  if (!/^[A-Za-z0-9_-]{11}$/.test(v || "")) return;
  e.preventDefault();
  var f = document.createElement("iframe");
  f.src = "https://www.youtube-nocookie.com/embed/" + v + "?start=" + t + "&autoplay=1&rel=0";
  f.title = a.getAttribute("data-title") || "Press conference video";
  f.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
  f.allowFullscreen = true;
  f.referrerPolicy = "strict-origin-when-cross-origin";
  var box = document.createElement("div");
  box.className = "player";
  box.appendChild(f);
  var row = a.closest(".foot-row") || a.parentNode;
  row.parentNode.insertBefore(box, row.nextSibling);
  a.remove();
});
