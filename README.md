# Bedrock KPI 4 Pipeline

Automated end-to-end pipeline that:
1. **Every Thursday at 5 AM Central**, a GitHub Action logs into HouseCall Pro with Playwright, triggers the service-agreements CSV export, then downloads and uploads it to Google Drive.
2. **Every Thursday at 7 AM Central**, a Render Cron Job picks up the newest CSV, runs the KPI 4 report generator, updates the Bedrock Restoration Memberships Combined File, and writes the new report back to Google Drive.

The manual weekly click on HouseCall Pro's "download service plans" button goes away.

---

## Repo layout

```
bedrock-kpi4-pipeline/
├── .github/workflows/fetch-hcp-csv.yml   # weekly GitHub Action
├── scripts/
│   ├── fetch_hcp_csv.py                  # Playwright script (runs in Actions)
│   ├── run_kpi4.py                       # Render cron entrypoint
│   └── drive_client.py                   # tiny Google Drive helper
├── kpi4/                                 # KPI 4 skill code (copied from Cowork skill)
│   ├── build_kpi4_report.py
│   ├── combined_file.py
│   └── assets/report_template.html
├── render.yaml                           # Render Cron Job service definition
├── requirements.txt                      # Python deps
└── README.md                             # this file
```

## First-time setup

### 1. Secrets in GitHub

Add these to **Settings → Secrets and variables → Actions** in the new repo:

| Secret name                | Value                                                  |
|----------------------------|--------------------------------------------------------|
| `HCP_EMAIL`                | HouseCall Pro login email (same one you sign in with)  |
| `HCP_PASSWORD`             | HouseCall Pro password                                 |
| `GOOGLE_SERVICE_ACCOUNT`   | JSON blob of a service-account key (see below)         |
| `DRIVE_CSV_FOLDER_ID`      | Google Drive folder ID where CSVs should be dropped    |

### 2. Google service account

Both the GitHub Action and Render need to read/write files in Drive.

1. In **Google Cloud Console → APIs & Services → Credentials → Create → Service account.**
2. Give it any name (e.g. `bedrock-kpi4-bot`).
3. On the **Keys** tab, add a new JSON key. Download the JSON file.
4. Paste the *entire* JSON contents into the `GOOGLE_SERVICE_ACCOUNT` secret in GitHub, and the same value into `GOOGLE_SERVICE_ACCOUNT` in Render.
5. Take the service account's email address (e.g. `bedrock-kpi4-bot@your-project.iam.gserviceaccount.com`) and **share the target Google Drive folder** with it as an Editor. Do the same for the Combined File.

### 3. Render Cron Job

1. In Render, **New → Cron Job**.
2. Connect this GitHub repo.
3. Build command: `pip install -r requirements.txt && playwright install --with-deps chromium` (Playwright not strictly needed here, but harmless if included; safe to trim to just the `pip install`).
4. Cron schedule: `0 12 * * 4` (7 AM Central Thursday, in UTC).
5. Environment variables:
   - `GOOGLE_SERVICE_ACCOUNT` — same JSON as GitHub
   - `DRIVE_CSV_FOLDER_ID` — same folder ID
   - `COMBINED_FILE_ID` — the ID of the Google Sheet (from its URL, between `/d/` and `/edit`)
   - `EXCLUDE_PLANS` (optional) — comma-separated list of plans to drop; defaults to the four Salt Delivery + Free Plus + Non-Renewing already baked in
6. Start command: `python scripts/run_kpi4.py`

### 4. Push the repo

```bash
cd bedrock-kpi4-pipeline
git init
git add .
git commit -m "Initial KPI 4 pipeline"
git branch -M main
git remote add origin git@github.com:<your-org>/bedrock-kpi4-pipeline.git
git push -u origin main
```

That's it. First run happens next Thursday, or you can trigger both manually — GitHub Action via the "Run workflow" button; Render Cron Job via its "Trigger job" button.

## Manual testing before the first scheduled run

**GitHub Action:**
- Go to Actions → `fetch-hcp-csv` → Run workflow. Watch the logs. On success, you'll see a new CSV in the Drive folder.

**Render job:**
- In Render, hit "Trigger job." It logs in the Render dashboard. On success, you'll see:
  - Updated Combined File replacing the previous one (Drive keeps version history)
  - New `kpi4_report_<pull_date>.html` file dropped in the same folder

## When it breaks

Two failure modes are worth knowing:

1. **HouseCall Pro session expired / password changed.** Playwright will log the login step failing. Update `HCP_PASSWORD` in GitHub Secrets.
2. **HCP changed the DOM.** `fetch_hcp_csv.py` uses CSS selectors that could break. Log into HCP, open DevTools, find the new selectors, update the script.

## Cost

- GitHub Actions: free tier easily covers this (a Playwright run takes ~90 seconds, once a week).
- Render Cron Job: $1/month.

Total: about $12/year.
