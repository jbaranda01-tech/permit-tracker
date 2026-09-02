# PermitTracker — LB Caribe Services

Sistema de gestión y seguimiento de permisos, certificaciones y documentos para empleados y equipos de LB Caribe Services, Inc. y Professional Logistics, Inc.

## Features

- **Dashboard dual**: Vista lado a lado de ambas compañías (LB / PLI)
- **Perfiles de empleados**: Info personal, laboral, licencia de conducir, upload de documentos
- **Perfiles de equipos**: Vehículos y generadores con tablilla, marca/modelo/año, VIN
- **Tracking de permisos**: NTSP, TWIC, Cert. Médico, Antecedentes, Record Choferil, HM-126 / HM-232, Marbete, Seguro, Inspección, y permisos custom
- **Alertas automáticas**: Permisos por vencer (30 días) y vencidos, con contadores en tiempo real
- **Reporte PDF**: Exportación color-coded del estado de todos los permisos
- **Importación Excel**: Carga masiva de datos desde archivo Excel existente
- **Autenticación**: Login con roles (Admin, Manager, Viewer)
- **Tema día/noche**: Toggle entre modo claro y oscuro
- **Upload de archivos**: Copias de licencias, permisos escaneados, documentos

## Tech Stack

- **Backend**: Python / Flask
- **Database**: PostgreSQL
- **ORM**: SQLAlchemy + Flask-Migrate
- **Auth**: Flask-Login con roles
- **PDF**: WeasyPrint
- **Excel**: openpyxl
- **Deploy**: Railway

## Deployment on Railway

### 1. Create a new project on Railway

Go to [railway.app](https://railway.app) and create a new project.

### 2. Add PostgreSQL

Click "New" → "Database" → "PostgreSQL". Railway will automatically set the `DATABASE_URL` variable.

### 3. Deploy from GitHub

- Push this code to a GitHub repository
- In Railway, click "New" → "GitHub Repo" → select your repo
- Railway will auto-detect the Nixpacks config and deploy

### 4. Set environment variables

In Railway project settings → Variables, add:

```
SECRET_KEY=<generate-a-strong-random-key>
```

The `DATABASE_URL` is set automatically by Railway's PostgreSQL add-on.

### 5. First login

The app auto-creates a default admin user on first run:

```
Username: admin
Password: admin123
```

**⚠️ Change this password immediately after first login via Admin → Usuarios.**

### 6. Import your data

1. Log in as admin
2. Go to "Importar Excel" in the sidebar
3. Upload your `Registro_expiracion_de_documentos.xlsx` file
4. The importer handles the 2-digit year bug automatically (1930 → 2030, etc.)

## Local Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SECRET_KEY=dev-secret
export DATABASE_URL=sqlite:///permits.db  # SQLite for local dev

# Run
python app.py
```

App will be at `http://localhost:5000`

## User Roles

| Role    | Permissions                                    |
|---------|------------------------------------------------|
| Admin   | Full access: CRUD employees/equipment, import, manage users, delete |
| Manager | Create/edit employees, equipment, and permits  |
| Viewer  | Read-only access to dashboard and profiles     |

## File Structure

```
permit-tracker/
├── app.py              # Main Flask app with all routes
├── models.py           # SQLAlchemy models
├── config.py           # App configuration
├── requirements.txt    # Python dependencies
├── Procfile            # Railway/Gunicorn process
├── railway.json        # Railway deployment config
├── nixpacks.toml       # System dependencies (WeasyPrint)
├── .env.example        # Environment variable template
├── templates/
│   ├── base.html           # Layout with sidebar + nav
│   ├── login.html          # Login page
│   ├── dashboard.html      # Main dual-panel dashboard
│   ├── employee.html       # Employee detail + permits
│   ├── employee_form.html  # Employee create/edit
│   ├── equipment.html      # Equipment detail + permits
│   ├── equipment_form.html # Equipment create/edit
│   ├── import.html         # Excel import page
│   ├── admin_users.html    # User management
│   └── report_pdf.html     # PDF report template
├── static/
│   ├── css/style.css       # Complete stylesheet (light/dark)
│   └── js/app.js           # Theme toggle, alerts, sidebar
└── uploads/                # Uploaded files directory
```
