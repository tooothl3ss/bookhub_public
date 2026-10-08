# Third-party notices

Unless a file states otherwise, original BookHub code and documentation are licensed
under GPL-3.0-only; see LICENSE. Third-party components retain their own licenses and
copyright notices. Bundled source, headers and minified-bundle notices are preserved.

| Component | Location | License / copyright | Source and full terms |
|---|---|---|---|
| KFX Input / kfxlib | `bookhub/engine/vendor/kfxlib/` | GPL v3; John Howell, 2016–2026 | [Pinned upstream](https://github.com/kaikozlov/kindle.koplugin/tree/5ed7485338c331e6502ac33dba1cb026dd7cc2a2/python/kfxlib); [license](bookhub/engine/vendor/LICENSE-GPL-3.txt) |
| KRDS codec / Kindle position utility | `kindle-client/bin/kindle-pos` | MIT; Kai Kozlov, 2026; adapted by BookHub | [Upstream](https://github.com/kaikozlov/kindle.koplugin); [license](kindle-client/LICENSE-MIT-upstream.txt) |
| json.lua | `kindle-client/lib/json.lua` | MIT; rxi, 2020 | [Upstream](https://github.com/rxi/json.lua); [license](licenses/json.lua-MIT.txt) |
| LuaJIT, including Lua notices | `kindle-client/bin/luajit` | MIT; Mike Pall; bundled Lua and dlmalloc notices retained | [Upstream](https://github.com/LuaJIT/LuaJIT/tree/v2.1); [copyright and license](licenses/LuaJIT-COPYRIGHT.txt) |
| Alpine.js 3.17.4 | `myshelf/static/vendor/alpine-3.17.4.min.js` | MIT; Caleb Porzio and contributors | [Versioned license source](https://github.com/alpinejs/alpine/blob/v3.17.4/LICENSE.md); [license](licenses/Alpine-MIT.txt) |
| Vue shared/reactivity 3.5.41 | bundled inside Alpine.js | MIT; Yuxi (Evan) You and Vue contributors | [Versioned license source](https://github.com/vuejs/core/blob/v3.5.41/LICENSE); [license](licenses/Vue-MIT.txt) |
| Tailwind CSS 3.4.17 | `myshelf/static/vendor/tailwind-3.4.17.js` | MIT; Tailwind Labs, Inc. | [Versioned license source](https://github.com/tailwindlabs/tailwindcss/blob/v3.4.17/LICENSE); [license](licenses/Tailwind-MIT.txt) |

Tailwind's browser bundle also includes third-party modules. Its embedded notices for
fill-range, is-number, to-regex-range (Jon Schlinkert) and cssesc (Mathias Bynens) are retained.
Supplementary full terms: [fill-range](licenses/fill-range-MIT.txt),
[is-number](licenses/is-number-MIT.txt), [to-regex-range](licenses/to-regex-range-MIT.txt),
[cssesc](licenses/cssesc-MIT.txt).

The LuaJIT binary reports `2.1.1783773675` and `Copyright (C) 2005-2026 Mike Pall`.
It is an existing ARM build; an exact reproducible build recipe is not available here.
SHA-256: `8174df8c653d26d9daeed5d1c7f452420d8b4ec81419599d9c65cb6ec44a14ee`.
The full upstream COPYRIGHT, including Lua and dlmalloc notices, accompanies it.

Python dependencies are installed from `requirements.txt` and `requirements-dev.txt`;
their individual package licenses remain applicable. Docker's Python base image and
system packages retain their own licenses. These are not relicensed as BookHub code.

No books, covers, private credentials, reading-history database or proprietary Kindle
firmware are included in this snapshot. The application license does not grant rights
to user-supplied books or to material obtained from external catalogues.
