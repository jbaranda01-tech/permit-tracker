// ── Theme Toggle ──────────────────────────────────────────────────────

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
    syncThemeButtons(theme);
    var color = theme === 'dark' ? '#0a0a0b' : '#c81e1e';
    document.querySelectorAll('meta[name="theme-color"]').forEach(function(m) { m.content = color; });
}

function syncThemeButtons(theme) {
    document.querySelectorAll('.theme-btn').forEach(function(btn) {
        btn.classList.toggle('active', btn.getAttribute('data-theme-value') === theme);
    });
}

// Init theme from localStorage
(function() {
    var saved = localStorage.getItem('theme');
    if (!saved) {
        saved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', saved);
    // Sync buttons once DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { syncThemeButtons(saved); });
    } else {
        syncThemeButtons(saved);
    }
})();


// ── Sidebar Toggle ───────────────────────────────────────────────────

function toggleSidebar() {
    if (window.innerWidth <= 768) {
        const sidebar = document.getElementById('sidebar');
        if (sidebar && sidebar.classList.contains('drawer-open')) {
            closeDrawer();
        } else {
            openDrawer();
        }
        return;
    }
    // Desktop: CSS-driven collapse on <html>, persisted; base.html's inline
    // head script re-applies it pre-paint on the next load.
    const collapsed = document.documentElement.classList.toggle('sidebar-collapsed');
    localStorage.setItem('sidebar-collapsed', collapsed ? '1' : '0');
}


// ── Mobile Drawer ────────────────────────────────────────────────────

function openDrawer() {
    var sidebar = document.getElementById('sidebar');
    var backdrop = document.getElementById('drawer-backdrop');
    if (sidebar) sidebar.classList.add('drawer-open');
    if (backdrop) backdrop.classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeDrawer() {
    var sidebar = document.getElementById('sidebar');
    var backdrop = document.getElementById('drawer-backdrop');
    if (sidebar) sidebar.classList.remove('drawer-open');
    if (backdrop) backdrop.classList.remove('active');
    document.body.style.overflow = '';
}

// Close drawer when a nav link is clicked
document.addEventListener('DOMContentLoaded', function() {
    var sidebar = document.getElementById('sidebar');
    if (sidebar) {
        sidebar.querySelectorAll('.nav-link').forEach(function(link) {
            link.addEventListener('click', function() {
                if (window.innerWidth <= 768) closeDrawer();
            });
        });
    }
});


// ── Auto-dismiss flash messages ──────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.flash').forEach(flash => {
        setTimeout(() => {
            flash.style.opacity = '0';
            flash.style.transform = 'translateY(-10px)';
            flash.style.transition = 'all 0.3s ease';
            setTimeout(() => flash.remove(), 300);
        }, 5000);
    });
});


// ── Swipe Gesture for Drawer ─────────────────────────────────────────

(function() {
    var startX = 0, startY = 0, tracking = false;
    var THRESHOLD = 60;

    document.addEventListener('touchstart', function(e) {
        startX = e.touches[0].clientX;
        startY = e.touches[0].clientY;
        var sidebar = document.getElementById('sidebar');
        tracking = startX < 30 || (sidebar && sidebar.classList.contains('drawer-open'));
    }, {passive: true});

    document.addEventListener('touchend', function(e) {
        if (!tracking) return;
        var dx = e.changedTouches[0].clientX - startX;
        var dy = Math.abs(e.changedTouches[0].clientY - startY);
        if (dy > Math.abs(dx)) return;

        if (dx > THRESHOLD && startX < 30) openDrawer();
        else if (dx < -THRESHOLD) closeDrawer();
        tracking = false;
    }, {passive: true});
})();


// ── Sortable data tables (client-side; th[data-sort] + .sort-arrow) ──

function initSortableTables() {
    document.querySelectorAll('table.data-table').forEach(function (table) {
        var ths = table.querySelectorAll('th[data-sort]');
        if (!ths.length) return;
        var state = { key: null, dir: 1 };
        ths.forEach(function (th) {
            th.addEventListener('click', function () {
                state.dir = state.key === th.dataset.sort ? -state.dir : 1;
                state.key = th.dataset.sort;
                ths.forEach(function (h) {
                    h.classList.remove('sort-asc', 'sort-desc');
                    var arr = h.querySelector('.sort-arrow');
                    if (!arr) return;
                    if (h === th) {
                        h.classList.add(state.dir === 1 ? 'sort-asc' : 'sort-desc');
                        arr.textContent = state.dir === 1 ? '↑' : '↓';
                    } else {
                        arr.textContent = '↕';
                    }
                });
                sortTableRows(table, th.cellIndex, th.dataset.sortType || 'text', state.dir);
            });
        });
    });
}

function sortTableRows(table, colIndex, type, dir) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    // Group each data row with its trailing inline-edit row (colspan cell)
    // so the pair travels together and an open edit form stays with its row.
    var groups = [];
    Array.prototype.forEach.call(tbody.rows, function (row) {
        if (row.querySelector('.inline-edit-cell') && groups.length) {
            groups[groups.length - 1].push(row);
        } else {
            groups.push([row]);
        }
    });
    function val(g) {
        var cell = g[0].cells[colIndex];
        var text = cell ? cell.textContent.trim() : '';
        if (type === 'date') {
            var m = text.match(/(\d{2})\/(\d{2})\/(\d{4})/);
            return m ? new Date(+m[3], +m[1] - 1, +m[2]).getTime() : -Infinity;
        }
        return text.toLowerCase();
    }
    groups.sort(function (a, b) {
        var va = val(a), vb = val(b);
        return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    });
    groups.forEach(function (g) {
        g.forEach(function (r) { tbody.appendChild(r); });
    });
}

document.addEventListener('DOMContentLoaded', initSortableTables);

// ── Detail prev/next keyboard shortcuts ────────────────────────────────
// ← / → walk the sibling records rendered by _detail_nav.html. Ignored while
// typing, and while a modifier is held (so browser back/forward still work).
function initDetailNavKeys() {
    var nav = document.querySelector('.detail-nav');
    if (!nav) return;
    document.addEventListener('keydown', function (e) {
        if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        var el = document.activeElement;
        if (el && (el.isContentEditable ||
                   /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName))) return;
        var url = nav.getAttribute(e.key === 'ArrowLeft' ? 'data-prev-url' : 'data-next-url');
        if (!url) return;
        e.preventDefault();
        window.location.href = url;
    });
}

document.addEventListener('DOMContentLoaded', initDetailNavKeys);

// ── Type-to-search filter for long <select>s ───────────────────────────
// Any <select data-filter-input="<input id>" [data-filter-count="<el id>"]>
// is narrowed live by what is typed into that input. Options are REBUILT
// from a snapshot taken at init, never hidden — iOS Safari ignores `hidden`
// on <option>, and the native picker only shows options present in the DOM.
// Matching is accent/case-insensitive over the option text plus its
// `data-search` attribute (unit #, plate, make/model, class…); every typed
// term must match. The selected option always survives a rebuild, and a
// unique hit is auto-selected when nothing is chosen yet.
function initSelectFilters() {
    var selects = document.querySelectorAll('select[data-filter-input]');
    if (!selects.length) return;

    function normalize(s) {
        return (s || '').toLowerCase()
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .replace(/[#\-_.\/]/g, '')
            .replace(/\s+/g, ' ').trim();
    }

    function snapshotOption(opt) {
        var text = (opt.textContent || '').replace(/\s+/g, ' ').trim();
        return {
            value: opt.value,
            text: text,
            disabled: opt.disabled,
            haystack: normalize(text + ' ' + (opt.getAttribute('data-search') || ''))
        };
    }

    function buildOption(item) {
        var opt = document.createElement('option');
        opt.value = item.value;
        opt.textContent = item.text;
        if (item.disabled) opt.disabled = true;
        return opt;
    }

    selects.forEach(function (select) {
        var input = document.getElementById(select.getAttribute('data-filter-input'));
        if (!input) return;
        var countEl = document.getElementById(select.getAttribute('data-filter-count') || '');

        // Snapshot: placeholder (value "") + ungrouped options + optgroups, in DOM order.
        var master = [];
        Array.prototype.forEach.call(select.children, function (node) {
            if (node.tagName === 'OPTGROUP') {
                master.push({ label: node.label,
                              options: Array.prototype.map.call(node.children, snapshotOption) });
            } else if (node.tagName === 'OPTION') {
                master.push({ label: null, options: [snapshotOption(node)] });
            }
        });

        function rebuild() {
            var terms = normalize(input.value).split(' ').filter(Boolean);
            var current = select.value;
            var matches = 0;
            var only = null;

            function keep(item) {
                if (item.value === '') return true;               // placeholder always stays
                if (item.value === current && current !== '') return true;
                if (!terms.length) return true;
                var hay = item.haystack;
                return terms.every(function (t) { return hay.indexOf(t) !== -1; });
            }

            var frag = document.createDocumentFragment();
            master.forEach(function (group) {
                var kept = group.options.filter(keep);
                if (!kept.length) return;
                var parent = frag;
                if (group.label !== null) {
                    parent = document.createElement('optgroup');
                    parent.label = group.label;
                    frag.appendChild(parent);
                }
                kept.forEach(function (item) {
                    parent.appendChild(buildOption(item));
                    if (item.value !== '' && !item.disabled) {
                        matches += 1;
                        only = item;
                    }
                });
            });
            if (!matches) {
                var none = document.createElement('option');
                none.disabled = true;
                none.textContent = 'Sin resultados';
                frag.appendChild(none);
            }

            select.innerHTML = '';
            select.appendChild(frag);

            if (current && select.querySelector('option[value="' + current + '"]')) {
                select.value = current;
            } else if (terms.length && matches === 1 && only) {
                select.value = only.value;      // unique hit → pick it
            } else {
                select.value = '';
            }

            if (countEl) {
                countEl.textContent = terms.length
                    ? (matches === 1 ? '1 unidad' : matches + ' unidades')
                    : '';
            }
        }

        input.addEventListener('input', rebuild);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && input.value) {
                e.preventDefault();
                input.value = '';
                rebuild();
            }
        });
        rebuild();
    });
}

document.addEventListener('DOMContentLoaded', initSelectFilters);
