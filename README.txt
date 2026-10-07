SH HOST - Setup
================
1) pip install -r requirements.txt
2) python app.py            (http://localhost:5000)
   Admin panel: http://localhost:5000/admin

Environment variables (recommended):
   ADMIN_USER   admin username   (default: admin)
   ADMIN_PASS   admin password   (default: admin123  -> CHANGE IT)
   SECRET_KEY   long random string
   BASE_DOMAIN  main domain (default: ffx.qd.je) -> sites open as name.ffx.qd.je
                (also editable later in Admin > Settings; needs wildcard DNS *.shhost.com)
   DATA_DIR     where DB + sites are stored (use a persistent disk on Render)

Render / production start command:
   gunicorn app:app --workers 1 --threads 4 --timeout 120

GitHub import:
   Users can host a public GitHub repo (static site with index.html). Optional env GITHUB_TOKEN
   lets the server also import private repos you own and avoids GitHub rate limits.

Domain setup (ffx.qd.je): add ffx.qd.je AND *.ffx.qd.je as Custom Domains in Render and point DNS (CNAME) to Render.
Sites then open as name.ffx.qd.je. The domain can be changed later in Admin > Settings > Main domain.
