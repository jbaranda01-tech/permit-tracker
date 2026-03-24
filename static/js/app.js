// ── Theme Toggle ──────────────────────────────────────────────────────

function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
}

// Init theme from localStorage
(function() {
    const saved = localStorage.getItem('theme');
    if (saved) {
        document.documentElement.setAttribute('data-theme', saved);
    } else {
        // Default to light, but check system preference
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.setAttribute('data-theme', prefersDark ? 'dark' : 'light');
    }
})();


// ── Sidebar Toggle ───────────────────────────────────────────────────

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const main = document.querySelector('.main-content');
    sidebar.classList.toggle('collapsed');

    if (sidebar.classList.contains('collapsed')) {
        sidebar.style.width = '64px';
        if (main) main.style.marginLeft = '64px';
        // Hide text elements
        sidebar.querySelectorAll('span, .nav-section-title, .user-info').forEach(el => {
            el.style.display = 'none';
        });
        // Keep logo icon visible
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
                const url = a.entity === 'employee'
                    ? `/employee/${a.id}`
                    : `/equipment/${a.id}`;
                const dateStr = new Date(a.date).toLocaleDateString('es-PR', {
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
