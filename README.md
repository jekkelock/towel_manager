# Hotel Towel Manager

Internal staff web app for managing a hotel towel deposit workflow.

## Features

- Issue towels by room and department (Beach Bar / Pool Bar)
- Automatic voucher assignment from each department's voucher number pool
- Return flow that validates voucher card numbers before refund
- Unlimited towel exchanges (logged as zero-value transactions)
- Transaction log with last 50 entries in UI
- Admin controls:
  - Configure room number ranges
  - Add and toggle voucher numbers per department
  - See open towel/deposit stats
  - Change admin password from admin screen
  - Filter transaction history by room/date range with pagination
- Split deployment:
  - Client UI on port `5000`
  - Admin panel on port `5001` with simple password gate
- Client flow:
  - Staff chooses Beach Bar or Pool Bar on entry screen
  - "Change Department" button switches to the selector screen
- Towel stock:
  - Issue reduces stock
  - Return adds towels back to stock
  - Client screen includes `Add Clean Towels (+/-)` for manual stock adjustments
- Export reports:
  - Full transaction CSV
  - Snapshot PDF report
- Docker-ready deployment

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate    # Windows PowerShell
pip install -r requirements.txt
python app.py
```

Open [http://localhost:5000](http://localhost:5000).

## Run with Docker

```bash
docker compose up --build -d
```

Then open:

- Client: [http://localhost:5000](http://localhost:5000)
- Admin: [http://localhost:5001](http://localhost:5001)
  - Password default: `1234`

Database is stored at `./instance/towel_manager.db` and persisted by the compose volume mount.

### Change admin password

Edit `docker-compose.yml` and update:

```yaml
ADMIN_PASSWORD=1234
```

Then recreate containers:

```bash
docker compose up --build -d
```

## Suggested First Setup

1. Add voucher ranges for each department from the Admin Voucher Pools section.
2. Set valid room ranges in Admin Room Ranges.
3. Start issuing towels.
