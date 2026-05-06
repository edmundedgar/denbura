# Running denbura

Three processes must be running. Start them in separate terminals (or tmux panes).

## 1. GraphHopper (routing engine)

```bash
cd ~/working/denbura
java -jar graphhopper/graphhopper-web-11.0.jar server graphhopper/config.yml
```

Listens on port 2027. First run after changing `config.yml` or any `graphhopper/*.json`
profile file will rebuild the graph cache (~10 min); subsequent starts load the cache
in a few seconds.

## 2. API server (Python / FastAPI)

```bash
cd ~/working/denbura
source ~/venvs/denbura/bin/activate
uvicorn server:app --host 127.0.0.1 --port 2029
```

Listens on port 2029. Proxies route requests from the frontend to GraphHopper.
This is where scoring logic will be added.

## 3. Frontend (static file server)

```bash
cd ~/working/denbura/frontend
python3 -m http.server 2026 --bind 127.0.0.1
```

Listens on port 2026. Serves `index.html`.

## nginx

nginx is already running as a system service and proxies:

- `talk.edochan.com/map/` → port 2026 (frontend)
- `talk.edochan.com/map/api/` → port 2029 (API server)

No action needed unless the config changes (`/etc/nginx/sites-available/talk.edochan.com.conf`),
in which case reload with `sudo nginx -s reload`.
