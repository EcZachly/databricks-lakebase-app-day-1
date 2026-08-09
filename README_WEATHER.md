# README_WEATHER

## 1) Data source

This service uses the National Weather Service API (`https://api.weather.gov`) because it is free, public, and provides official U.S. forecast and alert data without requiring API keys.

## 2) Architecture

NWS API → `weather_documents` → chunking → MiniLM embeddings → `weather_embeddings` → pgvector cosine search → Flask `/weather/search`.

- `POST /weather/sync` fetches and normalizes forecast + alert text into `weather_documents`.
- `notebooks/ingest_weather_embeddings.py` chunks weather narratives and writes vectors into `weather_embeddings`.
- `POST /weather/search` embeds user queries and performs semantic retrieval with pgvector cosine distance.

## 3) Database schema

### `weather_documents`

- `id` (PK)
- `location`
- `source_type`
- `headline`
- `narrative_text`
- `issued_at`
- `effective_at`
- `payload` (JSONB)
- `synced_at`

### `weather_embeddings`

- `id` (PK)
- `document_id` (FK → `weather_documents.id`)
- `chunk_index`
- `chunk_text`
- `embedding vector(384)`
- `model_name`
- `created_at`

A cosine-similarity vector index is created with HNSW where supported.

## 4) Chunking

Embedding ingestion uses:

- `CHUNK_SIZE = 800`
- `CHUNK_OVERLAP = 100`

## 5) Embedding model

Both ingestion and search use:

- `sentence-transformers/all-MiniLM-L6-v2`

This model generates 384-dimensional vectors, matching `embedding vector(384)`.

## 6) How to run the pipeline

1. Sync weather documents:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations":["Chicago, IL","Austin, TX"],"limit":50}'
```

2. Generate and store embeddings:

```bash
python notebooks/ingest_weather_embeddings.py
```

3. Run semantic search:

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query":"risk of flooding near rivers","top_k":5}'
```

## 7) Example API requests

### Sync request

```json
{
  "locations": ["Chicago, IL", "Austin, TX"],
  "limit": 50
}
```

### Sync response

```json
{
  "message": "Weather sync completed",
  "documents_synced": 10
}
```

### Search request

```json
{
  "query": "risk of flooding near rivers",
  "top_k": 5
}
```

### Search response

```json
{
  "results": [
    {
      "location": "Chicago, IL",
      "headline": "Flood Warning",
      "chunk_text": "Heavy rainfall...",
      "similarity": 0.87
    }
  ]
}
```

## 8) Known limitations and possible improvements

- City/state resolution currently depends on external geocoding quality.
- Forecast and alert polling is request-driven; no scheduler is included.
- Duplicate prevention is based on stable IDs and unique constraints, but stale records are not pruned.
- Future improvements can include location caching, retry/backoff tuning, and better relevance metadata/ranking.
