// ── Theme Toggle ──────────────────────────────────────────────────────

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
    syncThemeButtons(theme);
    var color = theme === 'dark' ? '#0b0f19' : '#b91c1c';
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
    const sidebar = document.getElementById('sidebar');
    const main = document.querySelector('.main-content');
    sidebar.classList.toggle('collapsed');

    if (sidebar.classList.contains('collapsed')) {
        sidebar.style.width = '64px';
        if (main) main.style.marginLeft = '64px';
        sidebar.querySelectorAll('span, .nav-section-title, .user-info').forEach(el => {
            el.style.display = 'none';
        });
        const logoIcon = sidebar.querySelector('.logo i');
        if (logoIcon) logoIcon.style.display = '';
    } else {
        sidebar.style.width = '';
        if (main) main.style.marginLeft = '';
        sidebar.querySelectorAll('span, .nav-section-title, .user-info').forEach(el => {
            el.style.display = '';
        });
    }
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


// ── Alerts Modal ─────────────────────────────────────────────────────

function showAlerts(type) {
    const modal = document.getElementById('alerts-modal');
    const title = document.getElementById('alerts-title');
    const body = document.getElementById('alerts-body');

    title.textContent = type === 'expired' ? 'Permisos Vencidos' : 'Permisos Por Vencer';
    body.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Cargando...</div>';
    modal.classList.add('active');

    fetch('/api/alerts')
        .then(res => res.json())
        .then(alerts => {
            const filtered = alerts.filter(a => {
                if (type === 'expired') return a.type === 'expired';
                return a.type === 'expiring';
            });

            if (filtered.length === 0) {
                body.innerHTML = '<div class="loading">No hay alertas.</div>';
                return;
            }

            body.innerHTML = filtered.map(a => {
                const url = a.entity === 'company'
                    ? '/?view=company'
                    : a.entity === 'employee'
                    ? `/employee/${a.id}`
                    : `/equipment/${a.id}`;
                const dateStr = new Date(a.date).toLocaleDateString('en-US', {
                    day: '2-digit', month: '2-digit', year: 'numeric'
                });
                return `
                    <a href="${url}" class="alert-item alert-item-${a.type}">
                        <div class="alert-item-info">
                            <div class="alert-item-name">${a.name}
                                <span style="opacity:0.6;font-size:0.75rem;margin-left:4px">${a.company}</span>
                            </div>
                            <div class="alert-item-permit">${a.permit}</div>
                        </div>
                        <div class="alert-item-date">${dateStr}</div>
                    </a>
                `;
            }).join('');
        })
        .catch(err => {
            body.innerHTML = '<div class="loading">Error cargando alertas.</div>';
            console.error(err);
        });
}

function closeAlerts() {
    document.getElementById('alerts-modal').classList.remove('active');
}

// Close modal on Escape
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeAlerts();
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
