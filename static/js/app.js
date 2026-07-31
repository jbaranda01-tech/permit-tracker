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
