# VoxRoom AAF Service

Microservizio Python che genera file AAF (Advanced Authoring Format) compatibili con
Pro Tools, a partire da un manifest JSON + file audio WAV.

Pensato per essere chiamato dall'edge function `export-aaf` di VoxRoom.

## Stack
- FastAPI + Uvicorn
- pyaaf2 (binding Python di libaaf)
- Python 3.11

## Deploy

### Render.com (consigliato, ~7$/mese, zero config)
1. Crea un nuovo "Web Service" e collega questo repo (o carica come zip).
2. Render rileva il `Dockerfile` automaticamente.
3. In **Environment**, aggiungi `AAF_SERVICE_TOKEN` con un valore casuale lungo (es. `openssl rand -hex 32`).
4. Deploy. L'URL pubblico sarà tipo `https://aaf-service-xxx.onrender.com`.
5. Verifica: `curl https://aaf-service-xxx.onrender.com/health` → `{"status":"ok"}`.

### Fly.io (~3$/mese, auto-stop quando non usato)
```bash
fly launch --no-deploy           # accetta il fly.toml incluso
fly secrets set AAF_SERVICE_TOKEN=$(openssl rand -hex 32)
fly deploy
```

### Railway / Docker locale
```bash
docker build -t voxroom-aaf .
docker run -p 8000:8000 -e AAF_SERVICE_TOKEN=devtoken voxroom-aaf
```

## API

### `GET /health`
Liveness check. Restituisce `{"status":"ok"}`.

### `POST /aaf`
Multipart request:
- `manifest`: stringa JSON con la struttura sotto.
- `files`: una o più upload WAV. I nomi devono corrispondere ai `clip.file` nel manifest.

Header obbligatorio (se `AAF_SERVICE_TOKEN` è settato):
```
Authorization: Bearer <AAF_SERVICE_TOKEN>
```

Risposta: `application/octet-stream` con il file `.aaf`.

### Manifest schema
```json
{
  "session_name": "Episode 01",
  "fps": 25.0,
  "sample_rate": 48000,
  "mode": "base",
  "tracks": [
    {
      "name": "MARCO",
      "clips": [
        {
          "file": "marco_take_03.wav",
          "start_seconds": 12.5,
          "duration_seconds": 3.2,
          "name": "L002 take 3"
        }
      ]
    }
  ],
  "markers": [
    { "position_seconds": 12.5, "comment": "più aggressivo" }
  ]
}
```

- `mode = "base"`: solo tracce + clip al timecode.
- `mode = "advanced"`: aggiunge un event slot "Director Notes" coi marker.

## Una volta deployato
Comunica all'AI Lovable:
1. l'URL pubblico (es. `https://aaf-service-xxx.onrender.com`)
2. il valore di `AAF_SERVICE_TOKEN`

L'AI configurerà i secret `AAF_SERVICE_URL` e `AAF_SERVICE_TOKEN` nella tua webapp
e farà partire l'edge function `export-aaf` che lo userà.
